"""示範策略的離線測試：確認介面與虛擬帳本的基本行為。"""
import numpy as np
import pandas as pd

from livebot import strategy as S


def _four(n=400, start=100.0, step=0.5):
    idx = pd.date_range('2025-01-01', periods=n, freq='4h', tz='UTC')
    close = start + step * np.arange(n)
    return pd.DataFrame(dict(open=close, high=close + 1, low=close - 1, close=close, volume=1.0), index=idx)


def test_long_signals_interface_and_uptrend_entry():
    q = S.long_signals(_four(), None, None)
    assert list(q.columns) == ['enter', 'exit', 'atr']
    assert q.index[0] == pd.Timestamp('2025-01-01 04:00', tz='UTC')   # 收盤時間
    assert q.enter.iloc[-1] and not q.exit.iloc[-1]


def test_virtual_book_entry_and_stop():
    b = S.VirtualBook(100000.0)
    t = pd.Timestamp('2025-03-01', tz='UTC')
    b.open_phase(t, 100.0, True, 100.0, [], dict(enter=True, exit=False, atr=1.0), None)
    assert b.q['BTC_L'] > 0 and b.long_barrier() == 98.0
    b.intrabar(t, 100.0, 100.5, 97.0, True)
    assert b.flat() and b.events[-1]['reason'] == 'stop'
    restored = S.VirtualBook.from_dict(b.to_dict())
    assert restored.cash == b.cash


def test_garch_k_only_shrinks():
    assert S.garch_k([1.0, 1.0, 1.0], 2.0) == 0.5
    assert S.garch_k([1.0, 1.0, 1.0], 0.5) == 1.0
