"""幣安 REST 介面（只用標準函式庫）。公開行情不需要金鑰；下單、放貸、劃轉才需要。

端點依官方文件與 binance-connector-python（2026-11 版）：
- 條件單（停損）自 2025-12-09 起改用 POST /fapi/v1/algoOrder（algoType=CONDITIONAL），
  舊的 POST /fapi/v1/order 送 STOP_MARKET 會回 -4120。
- 活期理財：/sapi/v1/simple-earn/flexible/{list,position,subscribe,redeem}，需要「允許現貨及槓桿交易」權限。
- 萬向劃轉：POST /sapi/v1/asset/transfer（MAIN_UMFUTURE／UMFUTURE_MAIN），需要「允許萬向劃轉」權限。
"""
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import pandas as pd

SPOT = 'https://api.binance.com'
FAPI = 'https://fapi.binance.com'
FAPI_TESTNET = 'https://demo-fapi.binance.com'


class BinanceError(Exception):
    def __init__(self, status, code, msg, path):
        super().__init__(f'{path} HTTP {status} code={code} {msg}')
        self.status, self.code, self.msg = status, code, msg


class Client:
    def __init__(self, key='', secret='', testnet=False, timeout=20):
        self.key, self.secret, self.timeout = key, secret.encode(), timeout
        self.fapi = FAPI_TESTNET if testnet else FAPI
        self.offset = 0  # 本機時間與幣安伺服器時間差（毫秒）

    # ---- 基本請求
    def _request(self, method, base, path, params=None, signed=False, retries=3):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(retries):
            p = dict(params)
            headers = {'User-Agent': 'qbot/1.0'}
            if signed:
                if not self.key:
                    raise RuntimeError('需要 API 金鑰（.env 的 BINANCE_KEY／BINANCE_SECRET）')
                p['timestamp'] = int(time.time() * 1000) + self.offset
                p['recvWindow'] = 10000
                q = urllib.parse.urlencode(p)
                q += '&signature=' + hmac.new(self.secret, q.encode(), hashlib.sha256).hexdigest()
                headers['X-MBX-APIKEY'] = self.key
            else:
                q = urllib.parse.urlencode(p)
            url = base + path
            data = None
            if method == 'GET':
                url += ('?' + q) if q else ''
            elif method == 'DELETE':
                url += '?' + q
            else:
                data = q.encode(); headers['Content-Type'] = 'application/x-www-form-urlencoded'
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b'null')
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors='replace')
                try:
                    j = json.loads(body); code, msg = j.get('code'), j.get('msg')
                except Exception:
                    code, msg = None, body[:200]
                if code == -1021 and attempt < retries - 1:  # 時間差過大：重新同步再試
                    self.sync_time(); continue
                if e.code in (418, 429) or (e.code >= 500 and attempt < retries - 1):
                    time.sleep(5 * (attempt + 1)); continue
                raise BinanceError(e.code, code, msg, path)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == retries - 1:
                    raise
                # 下單類（POST）不盲目重送：交給呼叫端以 clientOrderId 查詢是否已成交
                if method == 'POST' and signed:
                    raise
                time.sleep(3 * (attempt + 1))

    def sync_time(self):
        t = self._request('GET', self.fapi, '/fapi/v1/time')['serverTime']
        self.offset = int(t - time.time() * 1000)
        return self.offset

    # ---- 公開行情
    @staticmethod
    def _frame(rows, extra=False):
        cols = ['open', 'high', 'low', 'close', 'volume']
        df = pd.DataFrame([[float(x) for x in r[1:6]] + [float(r[7]), float(r[9])] for r in rows],
                          columns=cols + ['quote_volume', 'taker_buy'],
                          index=pd.to_datetime([r[0] for r in rows], unit='ms', utc=True))
        df.index.name = 'time'
        return df if extra else df[cols + ['quote_volume']]

    def klines(self, market, symbol, interval, start=None, end=None, limit=1000, extra=False):
        """market: 'spot'／'perp'／'mark'。自動分頁到 end（不含尚未收盤的 K 線由呼叫端處理）。"""
        base, path, cap = {'spot': (SPOT, '/api/v3/klines', 1000), 'perp': (self.fapi, '/fapi/v1/klines', 1500),
                           'mark': (self.fapi, '/fapi/v1/markPriceKlines', 1500)}[market]
        out = []
        start_ms = None if start is None else int(pd.Timestamp(start).timestamp() * 1000)
        end_ms = None if end is None else int(pd.Timestamp(end).timestamp() * 1000)
        while True:
            rows = self._request('GET', base, path, dict(symbol=symbol, interval=interval, startTime=start_ms,
                                                          endTime=end_ms, limit=min(limit, cap)))
            out += rows
            if start_ms is None or len(rows) < min(limit, cap):
                break
            start_ms = rows[-1][0] + 1
            if end_ms is not None and start_ms > end_ms:
                break
        if not out:
            return pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume', 'quote_volume'])
        if market == 'mark':
            for r in out:
                r += [0] * (12 - len(r))
                r[5] = r[5] or 0; r[7] = r[7] or 0; r[9] = r[9] or 0
        df = self._frame(out, extra)
        return df[~df.index.duplicated(keep='last')].sort_index()

    def funding_rates(self, symbol, start):
        rows = self._request('GET', self.fapi, '/fapi/v1/fundingRate',
                             dict(symbol=symbol, startTime=int(pd.Timestamp(start).timestamp() * 1000), limit=1000))
        return [(pd.Timestamp(r['fundingTime'], unit='ms', tz='UTC'), float(r['fundingRate'])) for r in rows]

    def exchange_info(self):
        return self._request('GET', self.fapi, '/fapi/v1/exchangeInfo')

    def price(self, symbol):
        return float(self._request('GET', self.fapi, '/fapi/v1/ticker/price', dict(symbol=symbol))['price'])

    # ---- 紙上交易模擬成交用（公開資料）
    def prices(self):
        """全部合約的最新成交價（權重 2）。"""
        return {r['symbol']: float(r['price']) for r in self._request('GET', self.fapi, '/fapi/v1/ticker/price')}

    def depth(self, symbol, limit=50):
        """掛單簿（limit ≤ 50 時權重 2）。"""
        return self._request('GET', self.fapi, '/fapi/v1/depth', dict(symbol=symbol, limit=limit))

    def agg_trades(self, symbol, start_ms, end_ms):
        """逐筆成交（權重 20；起訖不可超過 1 小時）。"""
        return self._request('GET', self.fapi, '/fapi/v1/aggTrades',
                             dict(symbol=symbol, startTime=int(start_ms), endTime=int(end_ms), limit=1000))

    def funding_history(self, symbol, start_ms):
        """資金費結算紀錄：[(結算時間毫秒, 費率, 結算時標記價格)]。"""
        rows = self._request('GET', self.fapi, '/fapi/v1/fundingRate', dict(symbol=symbol, startTime=int(start_ms), limit=1000))
        return [(int(r['fundingTime']), float(r['fundingRate']), float(r.get('markPrice') or 0)) for r in rows]

    # ---- 合約帳戶（需金鑰）
    def account(self):
        return self._request('GET', self.fapi, '/fapi/v3/account', signed=True)

    def positions(self):
        return self._request('GET', self.fapi, '/fapi/v3/positionRisk', signed=True)

    def position_mode(self):
        return self._request('GET', self.fapi, '/fapi/v1/positionSide/dual', signed=True)['dualSidePosition']

    def set_hedge_mode(self):
        return self._request('POST', self.fapi, '/fapi/v1/positionSide/dual', dict(dualSidePosition='true'), signed=True)

    def set_leverage(self, symbol, lev):
        return self._request('POST', self.fapi, '/fapi/v1/leverage', dict(symbol=symbol, leverage=int(lev)), signed=True)

    def set_cross(self, symbol):
        try:
            return self._request('POST', self.fapi, '/fapi/v1/marginType', dict(symbol=symbol, marginType='CROSSED'), signed=True)
        except BinanceError as e:
            if e.code == -4046:  # 已經是全倉
                return None
            raise

    def market_order(self, symbol, side, position_side, qty, client_id):
        return self._request('POST', self.fapi, '/fapi/v1/order', dict(
            symbol=symbol, side=side, positionSide=position_side, type='MARKET', quantity=qty,
            newClientOrderId=client_id, newOrderRespType='RESULT'), signed=True)

    def query_order(self, symbol, client_id):
        return self._request('GET', self.fapi, '/fapi/v1/order', dict(symbol=symbol, origClientOrderId=client_id), signed=True)

    def stop_order(self, symbol, side, position_side, trigger, client_id, qty=None):
        """條件市價單。qty=None → closePosition（觸發時平掉該方向全部部位）。觸發價用最新成交價。"""
        p = dict(algoType='CONDITIONAL', symbol=symbol, side=side, positionSide=position_side, type='STOP_MARKET',
                 triggerPrice=trigger, workingType='CONTRACT_PRICE', clientAlgoId=client_id)
        if qty is None:
            p['closePosition'] = 'true'
        else:
            p['quantity'] = qty
        return self._request('POST', self.fapi, '/fapi/v1/algoOrder', p, signed=True)

    def open_stops(self, symbol=None):
        r = self._request('GET', self.fapi, '/fapi/v1/openAlgoOrders', dict(symbol=symbol), signed=True)
        return r.get('orders', r) if isinstance(r, dict) else r

    def cancel_stop(self, client_id):
        return self._request('DELETE', self.fapi, '/fapi/v1/algoOrder', dict(clientAlgoId=client_id), signed=True)

    # ---- 現貨錢包、劃轉、活期理財（需金鑰）
    def api_restrictions(self):
        """金鑰權限（enableWithdrawals、permitsUniversalTransfer、enableFutures、ipRestrict…）。"""
        return self._request('GET', SPOT, '/sapi/v1/account/apiRestrictions', signed=True)

    def spot_free(self, asset='USDT'):
        bal = self._request('GET', SPOT, '/api/v3/account', dict(omitZeroBalances='true'), signed=True)['balances']
        return next((float(b['free']) for b in bal if b['asset'] == asset), 0.0)

    def transfer(self, kind, amount, asset='USDT'):
        return self._request('POST', SPOT, '/sapi/v1/asset/transfer', dict(type=kind, asset=asset, amount=f'{amount:.2f}'), signed=True)

    def earn_product(self, asset='USDT'):
        rows = self._request('GET', SPOT, '/sapi/v1/simple-earn/flexible/list', dict(asset=asset, size=10), signed=True)['rows']
        rows = [r for r in rows if r.get('asset') == asset and r.get('canPurchase', True)]
        return rows[0] if rows else None

    def earn_balance(self, asset='USDT'):
        rows = self._request('GET', SPOT, '/sapi/v1/simple-earn/flexible/position', dict(asset=asset, size=10), signed=True)['rows']
        return sum(float(r['totalAmount']) for r in rows if r.get('asset') == asset)

    def earn_subscribe(self, product_id, amount):
        return self._request('POST', SPOT, '/sapi/v1/simple-earn/flexible/subscribe',
                             dict(productId=product_id, amount=f'{amount:.2f}', sourceAccount='SPOT'), signed=True)

    def earn_redeem(self, product_id, amount=None):
        p = dict(productId=product_id, destAccount='SPOT')
        if amount is None:
            p['redeemAll'] = 'true'
        else:
            p['amount'] = f'{amount:.2f}'
        return self._request('POST', SPOT, '/sapi/v1/simple-earn/flexible/redeem', p, signed=True)
