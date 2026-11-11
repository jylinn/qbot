"""策略模組（公開示範版）。

這裡放的是一個「均線趨勢」示範策略，只用來展示機器人框架怎麼接策略，不是作者實際使用的策略，
也沒有經過回測最佳化，請不要直接拿來交易。

機器人（bot.py）只透過下面這些函式和 VirtualBook 跟策略溝通。要換成自己的策略，
維持相同的函式名稱與回傳欄位即可：

  long_signals(four, daily, flow) -> DataFrame[enter, exit, atr]   以 4h K「收盤時間」為索引
  short_signals(daily_perp)       -> DataFrame[short_enter, short_exit, stop, max_hours]  以日 K 收盤時間為索引
  garch_step / garch_k            -> 每日的波動預測與部位縮放倍數 k（0～1）
  VirtualBook                     -> 「全倍數」虛擬帳戶；實際部位 = 縮放倍數 × 虛擬部位
  alt_frames / alt_step           -> 山寨幣每日判斷（示範版不交易山寨幣）
"""
import math

import numpy as np
import pandas as pd

FEE, SLIP = 0.0005, 0.0005                    # 虛擬帳本假設的手續費與滑價（每邊）
CFG = dict(risk=0.01, cap=3.0, fast=50, slow=200, atr_n=14, stop_atr=2.0)
LEGS = ('BTC_L', 'BTC_S')


# ---------------------------------------------------------------- 訊號
def _atr(f, n):
    prev = f.close.shift(1)
    tr = pd.concat([f.high - f.low, (f.high - prev).abs(), (f.low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def long_signals(four, daily, flow):
    """示範：4h 收盤站上 50 均線、且 50 均線在 200 均線之上就進場；跌破 50 均線出場。
    four：現貨 4h K（索引＝開盤時間）。daily、flow 在示範版沒有用到。"""
    f = four[['open', 'high', 'low', 'close']].reindex(pd.date_range(four.index.min(), four.index.max(), freq='4h'))
    fast = f.close.rolling(CFG['fast']).mean()
    slow = f.close.rolling(CFG['slow']).mean()
    q = pd.DataFrame(index=f.index)
    q['atr'] = _atr(f, CFG['atr_n'])
    q['enter'] = (f.close > fast) & (fast > slow)
    q['exit'] = f.close < fast
    q.index = q.index + pd.Timedelta(hours=4)   # 以收盤時間標記，收盤後才可用
    return q[['enter', 'exit', 'atr']]


def short_signals(daily_perp):
    """示範版不做空：回傳全部 False 的空單訊號。"""
    idx = pd.date_range(daily_perp.index.min(), daily_perp.index.max(), freq='1D') + pd.Timedelta(days=1)
    return pd.DataFrame(dict(short_enter=False, short_exit=False, stop=np.nan, max_hours=0.0), index=idx)


# ---------------------------------------------------------------- 波動縮放（示範：30 日實現波動）
def garch_step(close, d, params, month):
    """回傳 (D 日的波動預測 %, params, month)。示範版用過去 30 天日報酬的標準差，不估計模型。"""
    r = np.log(close).diff().dropna() * 100
    hist = r.loc[:d - pd.Timedelta(days=1)].values
    if len(hist) < 30:
        return np.nan, params, month
    return float(np.std(hist[-30:])), params, month


def garch_k(history, today_vol):
    """k = min(1, 過去波動中位數 ／ 今天的波動)：波動高於平常時縮小部位，不放大。"""
    past = [v for v in history if np.isfinite(v)]
    if not past or not np.isfinite(today_vol) or today_vol <= 0:
        return 1.0
    return float(min(1.0, max(0.0, np.median(past) / today_vol)))


# ---------------------------------------------------------------- 虛擬帳本
class VirtualBook:
    """「全倍數」虛擬帳戶。每根 5 分鐘 K：先 open_phase（資金費、訊號出場、進場，以開盤價成交），
    收盤後 intrabar（停損）。示範版只有一個多單單位、固定 2 ATR 停損、不加碼。"""

    def __init__(self, initial=100000.0):
        self.cash = float(initial); self.q = {'BTC_L': 0.0, 'BTC_S': 0.0}; self.entry = {'BTC_L': 0.0, 'BTC_S': 0.0}
        self.stop = None; self.units = 0; self.events = []

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if k != 'events'}

    @classmethod
    def from_dict(cls, d):
        b = cls(1.0)
        for k, v in d.items():
            setattr(b, k, v)
        b.events = []
        return b

    def equity(self, mark):
        return float(self.cash + sum(q * (mark - self.entry[s]) for s, q in self.q.items() if abs(q) >= 1e-12))

    def flat(self):
        return not any(abs(q) > 1e-10 for q in self.q.values())

    def _trade(self, s, delta, raw, t, reason):
        if abs(delta) < 1e-12:
            return
        fill = raw * (1 + np.sign(delta) * SLIP); charge = abs(delta) * fill * FEE
        old = self.q[s]; new = old + delta
        if old and old * delta < 0:
            self.cash += min(abs(old), abs(delta)) * (fill - self.entry[s]) * np.sign(old)
        self.entry[s] = 0.0 if abs(new) < 1e-10 else (fill if not old else self.entry[s])
        self.q[s] = 0.0 if abs(new) < 1e-10 else new
        self.cash -= charge
        self.units = 1 if self.q['BTC_L'] else 0
        self.events.append(dict(time=str(t), leg=s, delta=float(delta), raw=float(raw), reason=reason))

    def fund(self, rate, mark):
        for s in LEGS:
            if self.q[s]:
                self.cash -= self.q[s] * mark * rate

    def long_barrier(self):
        return self.stop if self.q['BTC_L'] else None

    def short_barrier(self):
        return None

    def next_pyramid(self):
        return None

    def open_phase(self, t, op, valid, mark, fundings, l, s):
        for rate in fundings:
            self.fund(rate, mark)
        if not valid or l is None:
            return
        if self.q['BTC_L'] and bool(l['exit']):
            self._trade('BTC_L', -self.q['BTC_L'], op, t, 'signal_exit')
        elif not self.q['BTC_L'] and bool(l['enter']) and np.isfinite(l['atr']) and l['atr'] > 0:
            dist = CFG['stop_atr'] * float(l['atr'])
            qty = min(self.equity(mark) * CFG['risk'] / dist, CFG['cap'] * self.equity(mark) / float(op))
            if qty > 1e-10:
                self.stop = float(op) - dist
                self._trade('BTC_L', qty, op, t, 'entry')

    def intrabar(self, t, op, hi, lo, valid):
        if valid and self.q['BTC_L'] and lo <= self.stop:
            self._trade('BTC_L', -self.q['BTC_L'], min(self.stop, op), t, 'stop')


# ---------------------------------------------------------------- 山寨幣（示範版不交易）
EXCLUDE = {'BTCUSDT', 'BTCDOMUSDT', 'USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'DAIUSDT'}


def alt_frames(f, btc_close):
    """回傳每日判斷所需的表。示範版的進場訊號 en 全部為 False。"""
    c = f['close']
    return dict(open=f['open'], high=f['high'], low=f['low'], close=c,
                en=pd.DataFrame(False, index=c.index, columns=c.columns))


def alt_step(state, k, F):
    """回傳 (exits, entries)。示範版不交易山寨幣，也會平掉任何殘留部位。"""
    exits = [(s, '示範版不持有山寨幣', p.get('last', 0.0)) for s, p in state['pos'].items()]
    state['pos'].clear(); state['recent'].clear()
    return exits, []


def floor_to(x, step):
    return math.floor(x / step + 1e-9) * step


def ceil_to(x, step):
    return math.ceil(x / step - 1e-9) * step
