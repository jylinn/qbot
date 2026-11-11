"""紙上券商的離線測試：用假的行情物件，不連網路。執行：python -m pytest tests"""
import time

import pandas as pd
import pytest

from livebot.binance import BinanceError
from livebot.broker import PaperBroker


class FakeClient:
    def __init__(self):
        self.last = {'BTCUSDT': 100.0}
        self.book = {'bids': [['99.9', '1'], ['99.8', '2']], 'asks': [['100.1', '1'], ['100.2', '2']]}
        self.trades, self.fund = [], []

    def depth(self, s, limit): return self.book
    def price(self, s): return self.last[s]
    def prices(self): return dict(self.last)
    def agg_trades(self, s, a, b): return [t for t in self.trades if a <= t['T'] <= b]
    def funding_history(self, s, start): return [r for r in self.fund if r[0] >= start]
    def klines(self, *a, **k): return pd.DataFrame()


@pytest.fixture
def setup(tmp_path):
    c = FakeClient()
    return c, PaperBroker(tmp_path / 'paper.json', 400.0, 0.0, client=c, taker_fee=0.0005, lev=(10, 5))


def test_market_order_walks_book(setup):
    c, b = setup
    fill = b.market('BTCUSDT', 'BUY', 'LONG', '2', 'o1', 100.0)
    assert fill == pytest.approx(100.15)                      # 1@100.1 + 1@100.2
    assert b.s['fees'] == pytest.approx(2 * 100.15 * 0.0005)
    assert b.s['slippage'] == pytest.approx(0.30)


def test_insufficient_margin_rejected(setup):
    _, b = setup
    with pytest.raises(BinanceError) as e:
        b.market('BTCUSDT', 'BUY', 'LONG', '50', 'o2', 100.0)  # 5000 名目 / 10 倍 = 500 > 400
    assert e.value.code == -2019


def test_stop_fills_at_first_crossing_trade(setup, monkeypatch):
    c, b = setup
    b.market('BTCUSDT', 'BUY', 'LONG', '2', 'o1', 100.0)
    b.place_stop('BTCUSDT', 'SELL', 'LONG', '99', 'sL')
    now = int(time.time() * 1000)
    c.trades = [dict(p='99.2', T=now + 100), dict(p='98.9', T=now + 200), dict(p='98.5', T=now + 300)]
    c.last['BTCUSDT'] = 98.5
    b._poll = now - 1000
    monkeypatch.setattr(time, 'time', lambda: (now + 400) / 1000)
    b.watch()
    assert 'BTCUSDT|LONG' not in b.s['pos']
    assert b.s['prices']['BTCUSDT'] == pytest.approx(98.9 * 99.85 / 100)   # 穿越價 × 賣 2 顆的吃單衝擊


def test_wick_before_stop_placement_ignored(setup):
    c, b = setup
    b.market('BTCUSDT', 'BUY', 'LONG', '1', 'o1', 100.0)
    b.place_stop('BTCUSDT', 'SELL', 'LONG', '99', 'sL')
    placed = b.s['stops']['sL']['placed']
    c.trades = [dict(p='98.8', T=placed - 10_000)]
    b.on_bar('BTCUSDT', 100.0, 100.2, 98.8, placed - 30_000, 60_000)
    assert 'sL' in b.s['stops']
    c.trades = [dict(p='98.7', T=placed + 5_000)]
    b.on_bar('BTCUSDT', 100.0, 100.2, 98.7, placed - 30_000, 60_000)
    assert 'sL' not in b.s['stops']


def test_gap_fills_at_open(setup):
    c, b = setup
    b.market('BTCUSDT', 'BUY', 'LONG', '1', 'o1', 100.0)
    b.place_stop('BTCUSDT', 'SELL', 'LONG', '99', 'sL')
    placed = b.s['stops']['sL']['placed']
    b.on_bar('BTCUSDT', 97.0, 97.5, 96.0, placed + 1, 300_000)
    assert b.s['prices']['BTCUSDT'] == pytest.approx(97.0 * 99.9 / 100)


def test_funding_only_after_open(setup, monkeypatch):
    c, b = setup
    b.market('BTCUSDT', 'BUY', 'LONG', '1', 'o1', 100.0)
    t_open = b.s['hist'][-1][0]
    b.s['fund_seen']['BTCUSDT'] = t_open - 1000
    c.fund = [(t_open - 500, 0.01, 100.0), (t_open + 1000, 0.0001, 100.0)]
    monkeypatch.setattr(time, 'time', lambda: (t_open + 2000) / 1000)
    cash0 = b.s['cash']; b.funding_sync()
    assert cash0 - b.s['cash'] == pytest.approx(0.01)       # 只收開倉後那一次


def test_state_round_trip(setup, tmp_path):
    c, b = setup
    b.market('BTCUSDT', 'BUY', 'LONG', '1', 'o1', 100.0); b.save()
    b2 = PaperBroker(tmp_path / 'paper.json', 400.0, 0.0, client=c)
    assert b2.s['fills'] == 1 and (tmp_path / 'fills.csv').exists()
