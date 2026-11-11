"""交易機器人主程式。

  python -m livebot.bot check     檢查金鑰權限、持倉模式與餘額（實盤／模擬盤）
  python -m livebot.bot setup     設定雙向持倉、BTC 全倉與槓桿（帳戶沒有持倉時才能切換）
  python -m livebot.bot once      立即跑一輪（測試用）
  python -m livebot.bot run       每 5 分鐘跑一輪，持續運作
  python -m livebot.bot status    顯示目前狀態
  python -m livebot.bot flatten --yes   取消所有條件單、平掉所有部位，並暫停機器人

模式由 .env 的 MODE 決定：paper（紙上交易，預設）／testnet（幣安模擬盤）／live（實盤）。
"""
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
import pandas as pd

from . import strategy as S
from .binance import BinanceError, Client
from .broker import LiveBroker, PaperBroker

HERE = Path(__file__).resolve().parent
BAR = pd.Timedelta(minutes=5)
SPOT_DAILY_START = '2017-08-17'   # 現貨日 K 起算日（波動縮放用）
EMA_DAILY_START = '2021-01-01'    # 傳給策略的日 K 起算日
PERP_DAILY_START = '2022-01-01'   # 永續日 K 起算日
log = logging.getLogger('qbot')


# ---------------------------------------------------------------- 設定
def load_config():
    env = {}
    p = Path(os.environ.get('QBOT_ENV', HERE / '.env'))
    if p.exists():
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); env[k.strip()] = v.strip().strip('"').strip("'")
    known = {'MODE', 'BINANCE_KEY', 'BINANCE_SECRET', 'STATE_DIR', 'PAPER_EQUITY', 'BTC_LEVERAGE', 'ALT_LEVERAGE', 'LEND',
             'LEND_IN_POSITION', 'MIN_FUTURES_FRAC', 'PAPER_LEND_APY', 'ALTS', 'GARCH', 'START_MODE', 'FOLLOW_DAYS',
             'DELAY_SECONDS', 'MAX_GROSS', 'PAPER_TAKER_FEE', 'WATCH_SECONDS'}
    env.update({k: v for k, v in os.environ.items() if k in known})  # 環境變數優先於 .env
    g = lambda k, d: env.get(k, d)
    cfg = dict(mode=g('MODE', 'paper').lower(), key=g('BINANCE_KEY', ''), secret=g('BINANCE_SECRET', ''),
               state_dir=Path(g('STATE_DIR', str(HERE / 'state'))), paper_equity=float(g('PAPER_EQUITY', '10000')),
               btc_leverage=int(g('BTC_LEVERAGE', '10')), alt_leverage=int(g('ALT_LEVERAGE', '5')),
               lending=g('LEND', '1') == '1', lend_in_position=float(g('LEND_IN_POSITION', '0')),
               min_futures_frac=float(g('MIN_FUTURES_FRAC', '0.3')), paper_lend_apy=float(g('PAPER_LEND_APY', '0.03')),
               alts=g('ALTS', '1') == '1', garch=g('GARCH', '1') == '1', start_mode=g('START_MODE', 'flat'),
               follow_days=int(g('FOLLOW_DAYS', '60')), delay=int(g('DELAY_SECONDS', '20')),
               max_gross=float(g('MAX_GROSS', '8')), taker_fee=float(g('PAPER_TAKER_FEE', '0.0005')),
               watch=float(g('WATCH_SECONDS', '3')))
    if cfg['mode'] not in ('paper', 'testnet', 'live'):
        raise SystemExit('MODE 必須是 paper／testnet／live')
    if cfg['mode'] == 'testnet':
        cfg['lending'] = False  # 模擬盤沒有理財與劃轉
    return cfg


def setup_logging(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s', '%Y-%m-%d %H:%M:%S')
    fh = RotatingFileHandler(state_dir / 'bot.log', maxBytes=5_000_000, backupCount=5, encoding='utf-8'); fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout); sh.setFormatter(fmt)
    log.addHandler(fh); log.addHandler(sh)


def tag(sym, n=12):
    """訂單編號只能用英數字；有些幣名是中文，改用雜湊。"""
    t = re.sub('[^A-Za-z0-9]', '', sym)
    return (t if t == sym else t[:6] + hashlib.md5(sym.encode()).hexdigest()[:6])[:n]


def _decimals(step):
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def fmt(x, step):
    return f'{x:.{_decimals(step)}f}'


# ---------------------------------------------------------------- 機器人
class Bot:
    def __init__(self, cfg):
        self.cfg = cfg; self.dir = cfg['state_dir']; setup_logging(self.dir)
        self.client = Client(cfg['key'], cfg['secret'], testnet=cfg['mode'] == 'testnet')
        if cfg['mode'] == 'paper':
            self.broker = PaperBroker(self.dir / 'paper.json', cfg['paper_equity'], cfg['paper_lend_apy'], client=self.client,
                                      taker_fee=cfg['taker_fee'], lev=(cfg['btc_leverage'], cfg['alt_leverage']))
        else:
            self.broker = LiveBroker(self.client, cfg['lending'])
        self.path = self.dir / 'state.json'
        self.st = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else None
        self.vb = S.VirtualBook.from_dict(self.st['vb']) if self.st else None
        self.data = {}; self.info_day = None; self.filters = {}

    @property
    def halted(self):
        return (self.dir / 'HALT').exists()

    # ---- 狀態
    def save(self):
        self.st['vb'] = self.vb.to_dict()
        tmp = self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(self.st, indent=1, default=str), encoding='utf-8')
        tmp.replace(self.path); self.broker.save()

    # ---- 行情
    def exchange_filters(self, t):
        if self.info_day != t.date():
            info = self.client.exchange_info(); self.filters = {}; self.symbols = []
            for s in info['symbols']:
                f = {x['filterType']: x for x in s['filters']}
                lot = f.get('MARKET_LOT_SIZE') or f['LOT_SIZE']
                self.filters[s['symbol']] = (float(lot['stepSize']), float(f['PRICE_FILTER']['tickSize']),
                                             float(f.get('MIN_NOTIONAL', {}).get('notional', 5)))
                if (s.get('contractType') == 'PERPETUAL' and s.get('quoteAsset') == 'USDT' and s.get('status') == 'TRADING'
                        and s['symbol'] not in S.EXCLUDE):
                    self.symbols.append(s['symbol'])
            self.info_day = t.date(); self.broker.filters = self.filters
        return self.filters

    def refresh(self, t, since):
        """since：最早需要的 5 分鐘 K 開盤時間。"""
        d, c = self.data, self.client
        start = since - pd.Timedelta(hours=4, minutes=5)
        for name, market in (('perp5', 'perp'), ('mark5', 'mark')):
            old = d.get(name)
            frm = start if old is None or old.index[0] > start else old.index[-1]
            new = c.klines(market, 'BTCUSDT', '5m', start=frm, end=t, extra=(name == 'perp5'))
            if new.empty:
                raise RuntimeError(f'{name} 抓不到 K 線')
            df = new if old is None or old.index[0] > start else pd.concat([old[old.index < new.index[0]], new])
            d[name] = df.loc[df.index >= start - pd.Timedelta(days=1)]
        if d.get('four_t') != t.floor('4h'):
            four = c.klines('spot', 'BTCUSDT', '4h', limit=1000)
            d['four'] = four[four.index + pd.Timedelta(hours=4) <= t]; d['four_t'] = t.floor('4h')
        if d.get('daily_t') != t.floor('1D'):
            sd = c.klines('spot', 'BTCUSDT', '1d', start=SPOT_DAILY_START)
            d['spot1d'] = sd[sd.index + pd.Timedelta(days=1) <= t]
            pdl = c.klines('perp', 'BTCUSDT', '1d', start=PERP_DAILY_START)
            d['perp1d'] = pdl[pdl.index + pd.Timedelta(days=1) <= t]
            d['daily_t'] = t.floor('1D')
        fr = c.funding_rates('BTCUSDT', since - pd.Timedelta(hours=9))
        d['funding'] = {}
        for ts, rate in fr:
            d['funding'].setdefault(ts.floor('h'), []).append(rate)
        flow = d['perp5'][d['perp5'].index + BAR <= t]
        d['long'] = S.long_signals(d['four'], d['spot1d'].loc[EMA_DAILY_START:], flow)
        d['short'] = S.short_signals(d['perp1d'])

    # ---- 波動縮放（只縮小，不放大）
    def garch_today(self, day):
        g = self.st['garch']; close = self.data['spot1d'].close
        close = close.set_axis(close.index.tz_localize(None))
        d = pd.Timestamp(day)
        if str(d.date()) in g['hist']:
            hist = [v for k, v in g['hist'].items() if k < str(d.date())]
            return S.garch_k(hist, g['hist'][str(d.date())]), g['hist'][str(d.date())]
        if close.index[-1] < d - pd.Timedelta(days=1):  # 昨天的日 K 還沒抓到：沿用前一個 k，不寫入歷史
            log.warning('現貨日 K 尚未更新到 %s，GARCH 暫用前一天的 k', (d - pd.Timedelta(days=1)).date())
            self.data['daily_t'] = None
            return self.st.get('k', 1.0), float('nan')
        vol, g['params'], m = S.garch_step(close, d, g['params'], tuple(g['month']) if g['month'] else None)
        g['month'] = list(m) if m else None
        hist = [v for k, v in g['hist'].items() if k < str(d.date())]
        k = S.garch_k(hist, vol); g['hist'][str(d.date())] = vol
        return k, vol

    def garch_bootstrap(self, today):
        self.st['garch'] = dict(hist={}, params=None, month=None)
        for d in pd.date_range('2022-02-12', pd.Timestamp(today) - pd.Timedelta(days=1), freq='D'):
            self.garch_today(d)
        log.info('GARCH 歷史已建立（%d 天，最新參數 %s）', len(self.st['garch']['hist']), self.st['garch']['params'])

    def rescale(self, b, mark):
        k, vol = self.garch_today(b.tz_localize(None).normalize()) if self.cfg['garch'] else (1.0, float('nan'))
        total = self.broker.account()['total']
        V = self.vb.equity(mark)
        self.st.update(k=k, garch_vol=vol, scale=k * total / V if V > 0 else 0.0, scale_day=str(b.date()))
        log.info('每日縮放：GARCH 波動 %.3f%%、k=%.3f、總權益 %.2f USDT、實際／虛擬倍數 %.6f', vol, k, total, self.st['scale'])

    # ---- 啟動
    def bootstrap(self, t):
        # 山寨幣從下一個 00:00 UTC 才開始判斷（不在半天中途用昨天的訊號追價）
        self.st = dict(version=1, created=str(t), mode=self.cfg['mode'], alt=dict(pos={}, recent={}), alt_day=str(t.date()),
                       alt_stops={}, btc_stops={}, paper_seen={}, k=1.0, scale=0.0, scale_day=None)
        self.vb = S.VirtualBook(100000.0)
        follow = self.cfg['start_mode'] == 'follow'
        self.st['last_bar'] = str(t - pd.Timedelta(days=self.cfg['follow_days']) if follow else t - BAR)
        self.refresh(t, pd.Timestamp(self.st['last_bar']))
        self.garch_bootstrap(t.tz_localize(None).normalize())
        self.rescale(t, float(self.data['mark5'].open.iloc[-1]))
        log.info('首次啟動（%s 模式，%s）', self.cfg['mode'], '回放最近 %d 天跟上目前部位' % self.cfg['follow_days'] if follow else '從空手開始')

    # ---- 虛擬帳本推進
    def process(self, t):
        last = pd.Timestamp(self.st['last_bar'])
        p5, m5 = self.data['perp5'], self.data['mark5']
        L = self.data['long'][['enter', 'exit', 'atr']]; Sg = self.data['short'][['short_enter', 'short_exit', 'stop', 'max_hours']]
        known = float(m5.open.loc[:last].iloc[-1]) if (m5.index <= last).any() else float(m5.open.iloc[0])
        for b in pd.date_range(last + BAR, t, freq='5min'):
            prev = b - BAR
            n0 = len(self.vb.events)
            if prev in p5.index:
                r = p5.loc[prev]
                self.vb.intrabar(prev, r.open, r.high, r.low, bool(np.isfinite(r.open) and r.volume > 0))
            if b in m5.index and m5.open.loc[b] > 0:
                known = float(m5.open.loc[b])
            if b.hour == 0 and b.minute == 0:
                self.rescale(b, known)
            if b in p5.index:
                r = p5.loc[b]; op = float(r.open); valid = bool(np.isfinite(op) and (r.volume > 0 or b == t))
            else:
                op, valid = float('nan'), False
            fund = self.data['funding'].get(b, [])  # 虛擬帳本用；紙上帳戶的資金費由 broker.funding_sync 依實際結算計算
            l = L.loc[b].to_dict() if b in L.index else None
            s = Sg.loc[b].to_dict() if b in Sg.index else None
            self.vb.open_phase(b, op, valid, known, fund, l, s)
            for e in self.vb.events[n0:]:
                log.info('虛擬帳本：%s %s %.6f @ %.2f（%s）', e['time'], e['leg'], e['delta'], e['raw'], e['reason'])
        self.st['last_bar'] = str(t); self.st['mark'] = known

    # ---- 紙上交易：用已收盤 5 分鐘 K 補抓條件單（平常由 broker.watch 每幾秒即時檢查）
    def paper_bars(self, t, sym, bars):
        seen = pd.Timestamp(self.st['paper_seen'].get(sym, str(t - BAR * 2)))
        for ts, r in bars[(bars.index > seen) & (bars.index + BAR <= t)].iterrows():
            self.broker.set_price(sym, r.close)
            self.broker.on_bar(sym, float(r.open), float(r.high), float(r.low), int(ts.timestamp() * 1000))
            self.st['paper_seen'][sym] = str(ts)

    # ---- BTC 對帳
    def reconcile_btc(self, t, price):
        step, tick, mn = self.filters['BTCUSDT']
        c = self.st['scale']; pos = self.broker.positions(); total = self.broker.account()['total']
        targets = {'LONG': c * self.vb.q['BTC_L'], 'SHORT': c * abs(self.vb.q['BTC_S'])}
        gross = sum(targets.values()) * price
        if total > 0 and gross > self.cfg['max_gross'] * total:  # 安全上限：不應發生，發生就停手
            log.error('目標總曝險 %.2f 倍超過上限 %.1f 倍，本輪不下單', gross / total, self.cfg['max_gross']); return
        for side, tgt in targets.items():
            cur = pos.get(('BTCUSDT', side), 0.0); want = S.floor_to(tgt, step); diff = want - cur
            if abs(diff) < step / 2:
                continue
            opening = diff > 0
            if want > 0 and abs(diff) < max(0.02 * want, step):
                continue
            if opening and (diff * price < mn or self.halted):
                continue
            order_side = ('BUY' if opening else 'SELL') if side == 'LONG' else ('SELL' if opening else 'BUY')
            cid = f"qB{side[0]}{t:%y%m%d%H%M}{'o' if opening else 'c'}"
            try:
                self.broker.market('BTCUSDT', order_side, side, fmt(abs(diff), step), cid, price)
            except BinanceError as e:
                log.error('BTC %s 下單失敗：%s', side, e)

    def manage_btc_stops(self, t):
        step, tick, mn = self.filters['BTCUSDT']; c = self.st['scale']
        pos = self.broker.positions(); want = {}
        if self.vb.q['BTC_L'] and pos.get(('BTCUSDT', 'LONG')):
            want['L'] = ('SELL', 'LONG', S.floor_to(self.vb.long_barrier(), tick), None)
            nxt = self.vb.next_pyramid()
            if nxt and not self.halted:
                q = S.floor_to(c * nxt[1], step)
                if q >= step and q * nxt[0] >= mn:
                    want['P'] = ('BUY', 'LONG', S.ceil_to(nxt[0], tick), q)
        if self.vb.q['BTC_S'] and pos.get(('BTCUSDT', 'SHORT')):
            want['S'] = ('BUY', 'SHORT', S.ceil_to(self.vb.short_barrier(), tick), None)
        live = {o['cid']: o for o in self.broker.stops() if o['symbol'] == 'BTCUSDT'}
        have = {r: v for r, v in self.st['btc_stops'].items() if v['cid'] in live}
        for cid in live:  # 不在紀錄中的 BTC 條件單（例如當機殘留）一律取消
            if cid not in {v['cid'] for v in have.values()}:
                self.broker.cancel_stop(cid)
        new = {}
        for role in set(want) | set(have):
            w, h = want.get(role), have.get(role)
            if w and h and abs(h['trigger'] - w[2]) < tick / 2 and (h['qty'] or 0) == (w[3] or 0):
                new[role] = h; continue
            if h:
                self.broker.cancel_stop(h['cid'])
            if w:
                cid = f'q{role}{t:%y%m%d%H%M}'
                try:
                    self.broker.place_stop('BTCUSDT', w[0], w[1], fmt(w[2], tick), cid, None if w[3] is None else fmt(w[3], step))
                    new[role] = dict(cid=cid, trigger=w[2], qty=w[3])
                except BinanceError as e:
                    log.warning('BTC 條件單 %s 無法掛出（%s），下一輪由虛擬帳本處理', role, e.msg)
        self.st['btc_stops'] = new

    # ---- 山寨幣（每天 00:00 UTC）
    def alt_panel(self, t):
        day = t.normalize()

        def one(sym):
            for i in range(3):
                try:
                    k = self.client.klines('perp', sym, '1d', limit=99)
                    return sym, k[k.index < day]
                except BinanceError as e:
                    if e.code == -1121:  # 交易所已無此幣（下架）
                        return sym, 'gone'
                    time.sleep(2)
                except Exception:
                    time.sleep(2)
            return sym, None
        held = list(self.st['alt']['pos'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            got = dict(pool.map(one, self.symbols + [s for s in held if s not in self.symbols]))
        failed = [s for s in held if got.get(s) is None]
        if failed:  # 持倉幣的行情抓不到時不做判斷，避免誤判為下架而平倉
            raise RuntimeError(f'持倉幣日 K 抓取失敗：{failed}')
        frames = {s: v for s, v in got.items() if isinstance(v, pd.DataFrame) and len(v)}
        return {col: pd.DataFrame({s: v[col] for s, v in frames.items()}).sort_index()
                for col in ('open', 'high', 'low', 'close', 'quote_volume')}

    def alt_daily(self, t):
        day = str(t.date())
        if self.st.get('alt_day') == day or not self.cfg['alts']:
            return
        f = self.alt_panel(t)
        for s in self.st['alt']['pos']:   # 已下架／停止交易的幣：視為缺資料，依回測規則以最後價格平倉
            if s not in self.symbols and s in f['close'].columns:
                f['close'].loc[f['close'].index[-1], s] = np.nan
        btc = self.data['spot1d'].close
        F = S.alt_frames(f, btc)
        k = t.normalize() - pd.Timedelta(days=1)
        if k not in F['close'].index:
            log.warning('山寨幣日 K 尚未更新到 %s，稍後再試', k.date()); return
        exits, entries = S.alt_step(self.st['alt'], k, F)
        log.info('山寨幣每日判斷（%s 收盤）：取得 %d 檔日 K，進場訊號 %d 檔，出場 %d、進場 %d、等待再進場 %d',
                 k.date(), f['close'].shape[1], int(F['en'].loc[k].sum()), len(exits), len(entries), len(self.st['alt']['recent']))
        pos = self.broker.positions(); total = self.broker.account()['total']
        for sym, why, ref in exits:
            cid = self.st['alt_stops'].pop(sym, None)
            if cid:
                self.broker.cancel_stop(cid)
            q = pos.get((sym, 'LONG'), 0.0)
            if q > 0:
                step = self.filters.get(sym, (q, 0, 0))[0]
                try:
                    self.broker.market(sym, 'SELL', 'LONG', fmt(q, step), f'qA{t:%y%m%d}{tag(sym)}c', ref)
                except BinanceError as e:
                    log.error('山寨幣 %s 平倉失敗：%s', sym, e)
            log.info('山寨幣出場 %s（%s）', sym, why)
        for e in entries:
            sym = e['sym']
            if self.halted or sym not in self.filters:
                self.st['alt']['pos'].pop(sym, None); continue
            step, tick, mn = self.filters[sym]
            price = self.client.price(sym); self.broker.set_price(sym, price)
            q = S.floor_to(e['notional'] * total / price, step)
            if q < step or q * price < mn or e['stop'] >= price:
                log.info('山寨幣 %s 部位太小或價格已低於停損，略過', sym); self.st['alt']['pos'].pop(sym, None); continue
            try:
                self.broker.prepare_symbol(sym, self.cfg['alt_leverage'])
                self.broker.market(sym, 'BUY', 'LONG', fmt(q, step), f'qA{t:%y%m%d}{tag(sym)}o', price)
                cid = f'qA{t:%y%m%d}{tag(sym, 14)}'
                self.broker.place_stop(sym, 'SELL', 'LONG', fmt(S.floor_to(e['stop'], tick), tick), cid)
                self.st['alt_stops'][sym] = cid
                log.info('山寨幣進場 %s 名目 %.3f 倍權益，停損 %s', sym, e['notional'], e['stop'])
            except BinanceError as err:
                log.error('山寨幣 %s 進場失敗：%s', sym, err); self.st['alt']['pos'].pop(sym, None)
        self.st['alt_day'] = day

    def alt_intraday(self, t):
        """每輪確認山寨幣停損單還在；紙上模式用 5 分鐘 K 檢查停損。"""
        if not self.st['alt']['pos']:
            return
        pos = self.broker.positions(); live = {o['cid'] for o in self.broker.stops()}
        for sym, p in self.st['alt']['pos'].items():
            if self.cfg['mode'] == 'paper':
                bars = self.client.klines('perp', sym, '5m', limit=60)
                self.paper_bars(t, sym, bars)
                continue
            cid = self.st['alt_stops'].get(sym)
            if pos.get((sym, 'LONG')) and cid not in live:
                step, tick, mn = self.filters[sym]; cid = f'qA{t:%y%m%d%H%M}{tag(sym, 10)}'
                try:
                    self.broker.place_stop(sym, 'SELL', 'LONG', fmt(S.floor_to(p['stop'], tick), tick), cid)
                    self.st['alt_stops'][sym] = cid
                except BinanceError as e:
                    log.warning('山寨幣 %s 停損單補掛失敗：%s', sym, e.msg)

    # ---- 放貸
    def lending(self, phase):
        if not self.cfg['lending']:
            return
        a = self.broker.account(); total = a['total']
        if total <= 0:
            return
        prices = {'BTCUSDT': self.st.get('mark', 0)}
        margin = 0.0
        for (sym, side), q in self.broker.positions().items():
            px = prices.get(sym) or self.client.price(sym)
            margin += q * px / (self.cfg['btc_leverage'] if sym == 'BTCUSDT' else self.cfg['alt_leverage'])
        frac = 1.0 if self.vb.flat() else self.cfg['lend_in_position']
        need = max(self.cfg['min_futures_frac'] * total, (1 - frac) * total, 3 * margin)
        target = max(0.0, total - need); gap = target - a['lent']; thr = max(20.0, 0.02 * total)
        try:
            if phase == 'before' and gap < -thr:
                self.broker.redeem(-gap)
            elif phase == 'after' and gap > thr:
                amount = min(gap, a['available'] - 1)
                if amount > thr:
                    self.broker.lend(amount)
        except Exception as e:
            log.error('放貸調整失敗（不影響交易）：%s', e)

    # ---- 一輪
    def cycle(self, t):
        self.exchange_filters(t)
        if self.st is None:
            self.bootstrap(t)
        else:
            self.refresh(t, pd.Timestamp(self.st['last_bar']))
        p5 = self.data['perp5']
        if self.cfg['mode'] == 'paper':
            self.paper_bars(t, 'BTCUSDT', p5)
        self.process(t)
        if self.cfg['mode'] == 'paper':
            self.broker.funding_sync()
        price = float(p5.open.loc[t]) if t in p5.index else float(p5.close.iloc[-1])
        self.broker.set_price('BTCUSDT', price)
        self.save()
        try:  # 山寨幣出錯不影響 BTC 的對帳與停損
            self.alt_daily(t)
            self.alt_intraday(t)
        except Exception:
            log.error('山寨幣處理失敗，下一輪重試：\n%s', traceback.format_exc())
        self.lending('before')
        self.reconcile_btc(t, price)
        self.manage_btc_stops(t)
        self.lending('after')
        self.save(); self.status(t)

    def status(self, t):
        a = self.broker.account(); pos = self.broker.positions()
        s = dict(time=str(t), mode=self.cfg['mode'], halted=self.halted, equity=a, k=self.st.get('k'),
                 scale=self.st.get('scale'), virtual=dict(long=self.vb.q['BTC_L'], short=self.vb.q['BTC_S'], units=self.vb.units,
                                                         long_stop=self.vb.long_barrier(), short_stop=self.vb.short_barrier()),
                 positions={f'{k[0]} {k[1]}': v for k, v in pos.items()}, alts=self.st['alt']['pos'],
                 btc_stops=self.st['btc_stops'])
        if self.cfg['mode'] == 'paper':
            s['paper'] = self.broker.summary()
        (self.dir / 'status.json').write_text(json.dumps(s, indent=1, ensure_ascii=False, default=str), encoding='utf-8')
        log.info('狀態：總權益 %.2f（合約 %.2f、放貸 %.2f）k=%.3f 實際 BTC 多 %.4f／空 %.4f，山寨幣 %d 檔',
                 a['total'], a['futures'], a['lent'], self.st.get('k') or 0, pos.get(('BTCUSDT', 'LONG'), 0),
                 pos.get(('BTCUSDT', 'SHORT'), 0), len(self.st['alt']['pos']))

    def single_instance(self):
        """同一個狀態資料夾只允許一個機器人在跑（避免重複啟動弄壞狀態）。"""
        f = open(self.dir / 'run.lock', 'a+')
        try:
            if os.name == 'nt':
                import msvcrt
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(f'已經有一個機器人在使用 {self.dir}，不重複啟動。')
        self._lock = f
        (self.dir / 'run.pid').write_text(str(os.getpid()), encoding='ascii')

    def idle(self, until):
        """等到下一輪。紙上模式每隔幾秒用即時價格檢查條件單（實盤由交易所處理）。"""
        paper = self.cfg['mode'] == 'paper'; warned = 0.0
        while True:
            left = (until - pd.Timestamp.now(tz='UTC')).total_seconds()
            if left <= 0:
                return
            time.sleep(min(left, self.cfg['watch']) if paper else left)
            if paper and self.st is not None:
                try:
                    self.broker.watch()
                    if self.broker.dirty:
                        self.broker.save()
                except Exception as e:
                    if time.time() - warned > 600:
                        log.warning('紙上條件單即時檢查失敗（會由 5 分鐘 K 補抓）：%s', e); warned = time.time()

    def run(self):
        self.single_instance()
        log.info('機器人啟動：%s 模式', self.cfg['mode'])
        while True:
            now = pd.Timestamp.now(tz='UTC')
            t = now.ceil('5min') if now != now.floor('5min') else now
            self.idle(t + pd.Timedelta(seconds=self.cfg['delay']))
            try:
                self.cycle(t)
            except Exception:
                log.error('本輪失敗：\n%s', traceback.format_exc())
                time.sleep(30)


# ---------------------------------------------------------------- 指令
def cmd_check(cfg):
    if cfg['mode'] == 'paper':
        print('紙上交易模式不需要金鑰。'); return True
    c = Client(cfg['key'], cfg['secret'], testnet=cfg['mode'] == 'testnet'); c.sync_time(); ok = True
    if cfg['mode'] == 'live':
        r = c.api_restrictions()
        print('金鑰權限：', {k: v for k, v in r.items() if k.startswith(('enable', 'permits', 'ip'))})
        if r.get('enableWithdrawals'):
            print('✗ 這把金鑰開了「提現」權限。請到幣安關掉提現權限後再執行。'); ok = False
        if not r.get('enableFutures'):
            print('✗ 需要開啟「允許合約」權限。'); ok = False
        if cfg['lending'] and not (r.get('permitsUniversalTransfer') and r.get('enableSpotAndMarginTrading')):
            print('✗ 放貸需要「允許萬向劃轉」和「允許現貨及槓桿交易」權限（或在 .env 設 LEND=0 關閉放貸）。'); ok = False
        if not r.get('ipRestrict'):
            print('! 建議把金鑰限制在主機的固定 IP。')
    dual = c.position_mode(); print('雙向持倉：', dual)
    if not dual:
        print('✗ 需要雙向持倉模式，請執行 python -m livebot.bot setup'); ok = False
    a = c.account(); print('合約權益 USDT：', a['totalMarginBalance'], '可用：', a['availableBalance'])
    print('檢查結果：', '通過' if ok else '未通過'); return ok


def cmd_setup(cfg):
    c = Client(cfg['key'], cfg['secret'], testnet=cfg['mode'] == 'testnet'); c.sync_time()
    if not c.position_mode():
        c.set_hedge_mode(); print('已切換為雙向持倉')
    c.set_cross('BTCUSDT'); c.set_leverage('BTCUSDT', cfg['btc_leverage'])
    print(f'BTCUSDT 已設為全倉、{cfg["btc_leverage"]} 倍槓桿（槓桿只影響保證金占用，實際曝險由策略決定）')


def cmd_flatten(cfg):
    bot = Bot(cfg)
    (bot.dir / 'HALT').write_text('flatten', encoding='utf-8')
    bot.exchange_filters(pd.Timestamp.now(tz='UTC'))
    for o in bot.broker.stops():
        bot.broker.cancel_stop(o['cid'])
    for (sym, side), q in bot.broker.positions().items():
        step = bot.filters[sym][0]
        px = bot.client.price(sym)
        bot.broker.set_price(sym, px)
        bot.broker.market(sym, 'SELL' if side == 'LONG' else 'BUY', side, fmt(q, step), f'qX{int(time.time())}{tag(sym, 10)}', px)
    bot.broker.save()
    print('已平倉並暫停。刪除', bot.dir / 'HALT', '後機器人才會再開新倉。')


def main():
    cfg = load_config(); what = sys.argv[1] if len(sys.argv) > 1 else 'status'
    sys.stdout.reconfigure(encoding='utf-8')
    if what == 'check':
        cmd_check(cfg)
    elif what == 'setup':
        cmd_setup(cfg)
    elif what == 'flatten':
        if '--yes' not in sys.argv:
            raise SystemExit('會平掉所有部位，確定請加上 --yes')
        cmd_flatten(cfg)
    elif what in ('once', 'run'):
        if cfg['mode'] != 'paper' and not cmd_check(cfg):
            raise SystemExit('檢查未通過，不啟動。')
        bot = Bot(cfg)
        if what == 'run':
            bot.run()
        else:
            now = pd.Timestamp.now(tz='UTC'); bot.cycle(now.floor('5min'))
    elif what == 'status':
        p = cfg['state_dir'] / 'status.json'
        if not p.exists():
            print('尚未執行過。'); return
        txt = p.read_text(encoding='utf-8'); s = json.loads(txt)
        if s.get('paper'):
            q = s['paper']
            print(f"紙上交易 {q['days']:.1f} 天：起始 {q['start']:.2f} → 現在 {q['total']:.2f} USDT（{q['pnl']:+.2f}，{q['pnl'] / q['start']:+.2%}）")
            print(f"  成交 {q['fills']} 筆，手續費 {q['fees']:.2f}、滑價 {q['slippage']:.2f}、資金費 {q['funding'] + 0.0:+.2f} USDT")
            print(f"  最後更新 {s['time']}，成交明細在 {cfg['state_dir'] / 'fills.csv'}")
            print()
        print(txt)
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
