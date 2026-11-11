"""兩種券商：PaperBroker（紙上交易，不需金鑰）與 LiveBroker（幣安實盤／模擬盤）。介面相同。"""
import csv
import json
import logging
import time
from pathlib import Path

from .binance import BinanceError

log = logging.getLogger('qbot')
DAY_MS = 86_400_000


def _key(sym, side):
    return f'{sym}|{side}'


def _hit(o, hi, lo):
    return (o['side'] == 'SELL' and lo <= o['trigger']) or (o['side'] == 'BUY' and hi >= o['trigger'])


class PaperBroker:
    """用幣安即時公開資料模擬成交，盡量貼近實盤：

    - 手續費：市價單與條件單都是吃單（taker），預設 0.05%（幣安一般用戶 USDⓈ-M；用 BNB 付約 0.045%，可在 .env 改）。
    - 滑價：下單當下抓幣安掛單簿，依數量逐檔吃單算成交均價。抓不到掛單簿時才改用 BTC 5bps／山寨 10bps 估計。
    - 停損：幣安條件單以「最新成交價」觸發。這裡每幾秒檢查一次最新價；觸發時用逐筆成交找出第一筆穿越觸發價的
      成交價，再加上當下掛單簿的衝擊成本。另每分鐘用 1 分鐘 K、每輪用 5 分鐘 K 補抓兩次檢查之間的插針。
    - 資金費：每個持倉幣都用幣安實際結算紀錄（費率＋結算時標記價格），部位以結算時刻為準。
    - 保證金不足會像交易所一樣拒單。
    - 沒有模擬：強平、放貸實際利率（固定年化 PAPER_LEND_APY）、交易所當機或延遲。
    """

    def __init__(self, path, equity=10000.0, lend_apy=0.03, client=None, taker_fee=0.0005, lev=(10, 5)):
        self.path = Path(path); self.lend_apy = lend_apy; self.client = client; self.fee = taker_fee; self.lev = lev
        if self.path.exists():
            self.s = json.loads(self.path.read_text(encoding='utf-8'))
        else:
            self.s = dict(cash=float(equity), pos={}, stops={}, lent=0.0, lend_ts=time.time(), prices={}, fills=0)
        for k, v in dict(start_equity=float(equity), fees=0.0, slippage=0.0, funding=0.0, hist=[], fund_seen={},
                         kseen={}, started=time.time()).items():
            self.s.setdefault(k, v)
        self.filters = {}; self.dirty = False; self._poll = None; self._kcheck = 0.0
        self.fills_csv = self.path.with_name('fills.csv')

    def save(self):
        tmp = self.path.with_suffix('.tmp'); tmp.write_text(json.dumps(self.s, indent=1), encoding='utf-8'); tmp.replace(self.path)
        self.dirty = False

    def _accrue(self):
        now = time.time(); dt = now - self.s['lend_ts']
        self.s['lent'] *= (1 + self.lend_apy) ** (dt / (365 * 86400)); self.s['lend_ts'] = now

    def set_price(self, sym, px):
        self.s['prices'][sym] = float(px)

    def _lev(self, sym):
        return self.lev[0] if sym == 'BTCUSDT' else self.lev[1]

    def account(self):
        self._accrue(); px = self.s['prices']
        unreal = sum(p['qty'] * (px.get(k.split('|')[0], p['entry']) - p['entry']) * (1 if k.endswith('LONG') else -1)
                     for k, p in self.s['pos'].items())
        margin = sum(p['qty'] * px.get(k.split('|')[0], p['entry']) / self._lev(k.split('|')[0]) for k, p in self.s['pos'].items())
        fut = self.s['cash'] + unreal
        return dict(futures=fut, available=fut - margin, lent=self.s['lent'], spot=0.0, total=fut + self.s['lent'])

    def positions(self):
        return {tuple(k.split('|')): p['qty'] for k, p in self.s['pos'].items() if p['qty'] > 0}

    # ---- 成交價
    def _quote(self, sym, side, qty, ref):
        """回傳（吃單均價, 當下中間價, 來源）。"""
        if self.client is not None:
            try:
                d = self.client.depth(sym, 50)
                mid = (float(d['bids'][0][0]) + float(d['asks'][0][0])) / 2
                left, cost, last = qty, 0.0, mid
                for p, q in (d['asks'] if side == 'BUY' else d['bids']):
                    p, q = float(p), float(q); take = min(left, q); cost += take * p; left -= take; last = p
                    if left <= 1e-12:
                        break
                if left > 1e-12:  # 50 檔吃完還不夠：剩下的再差 1%（保守）
                    cost += left * last * (1.01 if side == 'BUY' else 0.99)
                return cost / qty, mid, '掛單簿'
            except Exception as e:
                log.warning('抓 %s 掛單簿失敗（%s），改用固定滑價估計', sym, e)
        slip = 0.0005 if sym == 'BTCUSDT' else 0.001
        return ref * (1 + slip * (1 if side == 'BUY' else -1)), ref, '固定估計'

    def _fill(self, sym, side, pside, qty, ref, why, base=None):
        """base=None：用當下掛單簿成交（市價單）。base=價格：以該價格為觸發成交價，再加上當下掛單簿的衝擊（條件單）。"""
        k = _key(sym, pside); p = self.s['pos'].get(k, dict(qty=0.0, entry=0.0))
        opening = (pside == 'LONG') == (side == 'BUY')
        if not opening:
            qty = min(qty, p['qty'])
        if qty <= 0:
            return None
        sign = 1 if side == 'BUY' else -1
        px, mid, src = self._quote(sym, side, qty, ref if base is None else base)
        fill = px if base is None else base * px / mid
        fee = qty * fill * self.fee
        if opening:
            p['entry'] = (p['qty'] * p['entry'] + qty * fill) / (p['qty'] + qty); p['qty'] += qty
        else:
            self.s['cash'] += qty * (fill - p['entry']) * (1 if pside == 'LONG' else -1); p['qty'] -= qty
        self.s['cash'] -= fee; self.s['fills'] += 1; self.s['fees'] += fee
        self.s['slippage'] += sign * (fill - ref) * qty   # 相對於策略原本的價格：市價單＝參考價，條件單＝觸發價
        if p['qty'] <= 1e-12:
            self.s['pos'].pop(k, None)
        else:
            self.s['pos'][k] = p
        now = int(time.time() * 1000)
        self.s['hist'].append([now, sym, pside, qty if opening else -qty])
        self.s['hist'] = [h for h in self.s['hist'] if h[0] > now - 7 * DAY_MS]
        self.s['fund_seen'].setdefault(sym, now)
        self.s['prices'][sym] = fill; self.dirty = True
        slip_bps = sign * (fill / ref - 1) * 1e4; book_bps = sign * (px / mid - 1) * 1e4
        log.info('紙上成交 %s %s %s %.6f @ %.4f（參考 %.4f，滑價 %.1f bps，手續費 %.4f USDT；%s）',
                 sym, side, pside, qty, fill, ref, slip_bps, fee, why)
        try:
            new = not self.fills_csv.exists()
            with open(self.fills_csv, 'a', newline='', encoding='utf-8-sig') as f:
                w = csv.writer(f)
                if new:
                    w.writerow(['時間UTC', '幣', '買賣', '部位方向', '數量', '參考價', '成交價', '名目USDT', '滑價bps',
                                '掛單簿成本bps', '手續費USDT', '價格來源', '原因'])
                w.writerow([time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()), sym, side, pside, f'{qty:.8g}', f'{ref:.8g}',
                            f'{fill:.8g}', f'{qty * fill:.2f}', f'{slip_bps:.2f}', f'{book_bps:.2f}', f'{fee:.4f}', src, why])
        except OSError as e:
            log.warning('寫入 fills.csv 失敗：%s', e)
        return fill

    def market(self, sym, side, pside, qty, cid, ref_price):
        qty, ref = float(qty), float(ref_price)
        if (pside == 'LONG') == (side == 'BUY'):  # 開倉：保證金不足時交易所會拒單
            need = qty * ref / self._lev(sym) + qty * ref * self.fee
            if need > self.account()['available']:
                raise BinanceError(400, -2019, 'Margin is insufficient.', 'paper')
        return self._fill(sym, side, pside, qty, ref, cid)

    # ---- 條件單
    def stops(self):
        return [dict(cid=c, **{k: v for k, v in o.items() if k != 'placed'}) for c, o in self.s['stops'].items()]

    def place_stop(self, sym, side, pside, trigger, cid, qty=None):
        trigger = float(trigger); qty = None if qty is None else float(qty)
        px = self.s['prices'].get(sym)
        if self.client is not None:
            try:
                px = self.client.price(sym)
            except Exception:
                pass
        if px is not None and ((side == 'SELL' and px <= trigger) or (side == 'BUY' and px >= trigger)):
            raise BinanceError(400, -2021, 'Order would immediately trigger.', 'paper')
        self.s['stops'][cid] = dict(symbol=sym, side=side, pside=pside, trigger=trigger, qty=qty, placed=int(time.time() * 1000))
        self.dirty = True

    def cancel_stop(self, cid):
        if self.s['stops'].pop(cid, None):
            self.dirty = True

    def _first_cross(self, sym, side, trig, t0, t1):
        """[t0, t1] 毫秒之間第一筆穿越觸發價的成交價；沒有就回傳 None。"""
        start = int(t0)
        for _ in range(30):
            rows = self.client.agg_trades(sym, start, int(t1))
            for r in rows:
                p = float(r['p'])
                if (side == 'SELL' and p <= trig) or (side == 'BUY' and p >= trig):
                    return p
            if len(rows) < 1000:
                return None
            start = int(rows[-1]['T']) + 1
        return None

    def _trigger(self, cid, o, t0, t1, fallback, must_find):
        """條件單在 [t0, t1] 被觸發。fallback：查不到逐筆成交時用的觸發成交價（None＝不能確定，先不成交）。"""
        p1 = None
        if self.client is not None:
            try:
                p1 = self._first_cross(o['symbol'], o['side'], o['trigger'], t0, t1)
            except Exception as e:
                log.warning('抓 %s 逐筆成交失敗（%s）', o['symbol'], e)
        if p1 is None:
            if must_find or fallback is None:
                return
            p1 = fallback
        have = self.s['pos'].get(_key(o['symbol'], o['pside']), {}).get('qty', 0.0)
        qty = have if o['qty'] is None else o['qty']
        del self.s['stops'][cid]; self.dirty = True
        if qty > 0:
            self._fill(o['symbol'], o['side'], o['pside'], qty, o['trigger'], '條件單觸發 ' + cid, base=p1)

    def on_bar(self, sym, op, hi, lo, start_ms, span_ms=300_000):
        """用一根已收盤 K 線（開盤時間 start_ms）補抓條件單。只算掛單之後的成交。"""
        end = start_ms + span_ms
        for cid, o in list(self.s['stops'].items()):
            if cid not in self.s['stops'] or o['symbol'] != sym or not _hit(o, hi, lo):
                continue
            placed = o.get('placed', 0)
            if placed >= end:
                continue
            if placed <= start_ms:   # 整根都在掛單之後：查不到逐筆成交就用觸發價（跳空用開盤價）
                fb = min(o['trigger'], op) if o['side'] == 'SELL' else max(o['trigger'], op)
                self._trigger(cid, o, start_ms, end, fb, must_find=False)
            else:                    # 掛單發生在這根 K 中間：必須查到掛單之後真的有穿越才成交
                self._trigger(cid, o, placed, end, None, must_find=True)

    def watch(self):
        """兩輪之間每幾秒呼叫：用最新成交價檢查條件單；每分鐘用 1 分鐘 K 補抓插針。"""
        if not self.s['stops'] or self.client is None:
            self._poll = None; return
        now = int(time.time() * 1000)
        last = self.client.prices()
        t0 = self._poll if self._poll is not None else now - 5000
        for cid, o in list(self.s['stops'].items()):
            px = last.get(o['symbol'])
            if px is None or cid not in self.s['stops']:
                continue
            self.s['prices'][o['symbol']] = px
            if _hit(o, px, px):
                self._trigger(cid, o, max(t0, o.get('placed', 0)) - 1000, now, px, must_find=False)
        self._poll = now
        if time.time() - self._kcheck >= 60:
            self._kcheck = time.time()
            for sym in {o['symbol'] for o in self.s['stops'].values()}:
                try:
                    k = self.client.klines('perp', sym, '1m', limit=5)
                except Exception:
                    continue
                for ts, r in k.iterrows():
                    st = int(ts.timestamp() * 1000)
                    if st + 60_000 > now or st < self.s['kseen'].get(sym, 0):
                        continue
                    self.on_bar(sym, float(r.open), float(r.high), float(r.low), st, 60_000)
                    self.s['kseen'][sym] = st + 60_000

    # ---- 資金費
    def _qty_at(self, sym, pside, ts):
        q = self.s['pos'].get(_key(sym, pside), {}).get('qty', 0.0)
        return max(0.0, q - sum(h[3] for h in self.s['hist'] if h[1] == sym and h[2] == pside and h[0] > ts))

    def funding_sync(self):
        """套用幣安實際資金費結算：持倉或最近 3 天有成交的幣，取得上次之後的結算紀錄。"""
        if self.client is None:
            return
        now = int(time.time() * 1000)
        syms = {k.split('|')[0] for k in self.s['pos']} | {h[1] for h in self.s['hist'] if h[0] > now - 3 * DAY_MS}
        for sym in list(self.s['fund_seen']):
            if sym not in syms:
                del self.s['fund_seen'][sym]
        for sym in syms:
            seen = self.s['fund_seen'].setdefault(sym, now)
            try:
                rows = self.client.funding_history(sym, max(seen + 1, now - 3 * DAY_MS))
            except Exception as e:
                log.warning('抓 %s 資金費紀錄失敗：%s', sym, e); continue
            for ts, rate, mark in rows:
                if ts <= seen or ts > now:
                    continue
                net = self._qty_at(sym, 'LONG', ts) - self._qty_at(sym, 'SHORT', ts)
                if net:
                    mark = mark or self.s['prices'].get(sym, 0.0)
                    amt = net * mark * rate
                    self.s['cash'] -= amt; self.s['funding'] += amt; self.dirty = True
                    log.info('紙上資金費 %s 費率 %.4f%% 標記價 %.4f 淨部位 %.6f → %+.4f USDT', sym, rate * 100, mark, net, -amt)
                seen = ts
            self.s['fund_seen'][sym] = seen

    def funding(self, sym, rate, mark):
        pass  # 紙上資金費改由 funding_sync 依實際結算紀錄計算

    def summary(self):
        a = self.account()
        return dict(start=self.s['start_equity'], total=a['total'], pnl=a['total'] - self.s['start_equity'],
                    fees=self.s['fees'], slippage=self.s['slippage'], funding=-self.s['funding'], fills=self.s['fills'],
                    days=(time.time() - self.s['started']) / 86400)

    def prepare_symbol(self, sym, lev):
        pass

    def lend(self, amount):
        self._accrue(); amount = min(amount, self.account()['available'])
        if amount > 0:
            self.s['cash'] -= amount; self.s['lent'] += amount; self.dirty = True
            log.info('紙上放貸 +%.2f USDT（放貸中 %.2f）', amount, self.s['lent'])

    def redeem(self, amount):
        self._accrue(); amount = min(amount, self.s['lent'])
        self.s['lent'] -= amount; self.s['cash'] += amount; self.dirty = True
        log.info('紙上贖回 %.2f USDT（放貸中 %.2f）', amount, self.s['lent'])


class LiveBroker:
    def __init__(self, client, lending=True):
        self.c = client; self.lending = lending; self._pid = None; self.filters = {}; self._earn = None

    def save(self):
        pass

    def set_price(self, sym, px):
        pass

    def account(self):
        a = self.c.account()
        fut = float(a['totalMarginBalance']); avail = float(a['availableBalance'])
        lent = spot = 0.0
        if self.lending:
            if self._earn is None or time.time() - self._earn[0] > 60:  # 理財查詢權重高，快取 60 秒
                try:
                    self._earn = (time.time(), self.c.earn_balance(), self.c.spot_free())
                except BinanceError as e:
                    log.warning('查詢放貸／現貨餘額失敗：%s', e)
                    self._earn = (time.time(), 0.0, 0.0)
            lent, spot = self._earn[1], self._earn[2]
        return dict(futures=fut, available=avail, lent=lent, spot=spot, total=fut + lent + spot)

    def positions(self):
        out = {}
        for p in self.c.positions():
            amt = float(p['positionAmt'])
            if amt:
                out[(p['symbol'], p['positionSide'])] = abs(amt)
        return out

    def market(self, sym, side, pside, qty, cid, ref_price):
        try:
            r = self.c.market_order(sym, side, pside, qty, cid)
        except BinanceError:
            raise
        except Exception as e:  # 網路中斷：先查是否已成交，避免重複下單
            log.warning('下單連線異常 %s，查詢 %s 是否已成交', e, cid)
            time.sleep(2)
            r = self.c.query_order(sym, cid)
        px = float(r.get('avgPrice') or 0) or ref_price
        log.info('成交 %s %s %s %s @ %.4f（%s）狀態 %s', sym, side, pside, qty, px, cid, r.get('status'))
        return px

    def stops(self):
        out = []
        for o in self.c.open_stops() or []:
            cp = str(o.get('closePosition', 'false')).lower() == 'true'
            out.append(dict(cid=o.get('clientAlgoId'), symbol=o['symbol'], side=o['side'], pside=o.get('positionSide'),
                            trigger=float(o.get('triggerPrice') or 0), qty=None if cp else float(o.get('quantity') or 0)))
        return out

    def place_stop(self, sym, side, pside, trigger, cid, qty=None):
        self.c.stop_order(sym, side, pside, trigger, cid, qty)
        log.info('掛條件單 %s %s %s 觸發 %s 數量 %s（%s）', sym, side, pside, trigger, qty or '全部', cid)

    def cancel_stop(self, cid):
        try:
            self.c.cancel_stop(cid)
        except BinanceError as e:
            log.info('取消條件單 %s：%s', cid, e.msg)

    def on_bar(self, sym, op, hi, lo):
        pass

    def funding(self, sym, rate, mark):
        pass

    def prepare_symbol(self, sym, lev):
        self.c.set_cross(sym)
        self.c.set_leverage(sym, lev)

    def _product(self):
        if self._pid is None:
            p = self.c.earn_product('USDT')
            if p is None:
                raise RuntimeError('找不到 USDT 活期理財產品')
            self._pid = p['productId']
        return self._pid

    def lend(self, amount):
        if not self.lending:
            return
        amount = int(amount * 100) / 100; self._earn = None
        self.c.transfer('UMFUTURE_MAIN', amount)
        time.sleep(1)
        free = int(self.c.spot_free() * 100) / 100
        self.c.earn_subscribe(self._product(), free)
        log.info('放貸：合約 → 現貨 → 活期理財 %.2f USDT', free)

    def redeem(self, amount):
        if not self.lending:
            return
        self._earn = None
        before = self.c.spot_free()
        lent = self.c.earn_balance()
        if amount >= lent * 0.999:
            self.c.earn_redeem(self._product())
        else:
            self.c.earn_redeem(self._product(), int(amount * 100 + 99) / 100)
        free = before
        for _ in range(30):  # 活期贖回通常即時到帳；最多等 60 秒
            time.sleep(2)
            free = self.c.spot_free()
            if free >= before + min(amount, lent) * 0.99:
                break
        move = int(free * 100) / 100
        if move > 0:
            self.c.transfer('MAIN_UMFUTURE', move)
        log.info('贖回放貸 %.2f USDT，轉回合約 %.2f', amount, move)
