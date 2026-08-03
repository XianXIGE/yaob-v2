# -*- coding: utf-8 -*-
"""币安合约 FAPI 客户端 —— 带代理 + 自实现签名, 无第三方依赖"""
import hmac, hashlib, json, time, urllib.request, urllib.parse, os

BASE = "https://fapi.binance.com"
# 本机 Mihomo/clash 代理 (可通过环境覆盖)
PROXY = os.getenv("FAPI_PROXY", "http://127.0.0.1:7890")


class BinanceError(Exception):
    pass


class FapiClient:
    def __init__(self, api_key="", api_secret="", proxy=PROXY):
        self.api_key = api_key
        self.api_secret = api_secret
        self.proxy = proxy
        self._dry_run = not (api_key and api_secret)
        self._opener = self._build_opener()

    def _build_opener(self):
        if self.proxy:
            ph = urllib.request.ProxyHandler({
                "http": self.proxy, "https": self.proxy})
            return urllib.request.build_opener(ph)
        return urllib.request.build_opener()

    def _get(self, path, params=None, signed=False, method="GET"):
        url = BASE + path
        qs = urllib.parse.urlencode(params or {})
        if signed:
            if self._dry_run:
                raise BinanceError("未配置有效API Key, 处于模拟模式(不实际下单)")
            qs += "&timestamp=%d" % int(time.time() * 1000)
            sig = hmac.new(self.api_secret.encode(), qs.encode(),
                           hashlib.sha256).hexdigest()
            qs += "&signature=" + sig
        if qs:
            url += "?" + qs
        headers = {"User-Agent": "yaob/2.0"}
        if signed:
            headers["X-MBX-APIKEY"] = self.api_key
        req = urllib.request.Request(url, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=10) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            raise BinanceError(f"FAPI {e.code}: {body[:200]}")
        except Exception as e:
            raise BinanceError(f"FAPI网络错误: {e}")

    # ---------- 公开行情 ----------
    def ping(self):
        return self._get("/fapi/v1/ping")

    def exchange_info(self):
        return self._get("/fapi/v1/exchangeInfo")

    def all_tickers(self):
        return self._get("/fapi/v1/ticker/24hr")

    def klines(self, symbol, interval="1m", limit=100):
        return self._get("/fapi/v1/klines",
                         {"symbol": symbol, "interval": interval, "limit": limit})

    # ---------- 私有(需签名) ----------
    def account(self):
        return self._get("/fapi/v2/account", {"recvWindow": 5000}, signed=True)

    def set_leverage(self, symbol, lev):
        return self._get("/fapi/v1/leverage",
                         {"symbol": symbol, "leverage": lev}, signed=True, method="POST")

    def new_order(self, symbol, side, qty, order_type="MARKET"):
        return self._get("/fapi/v1/order", {
            "symbol": symbol, "side": side, "type": order_type,
            "quantity": f"{qty:.3f}", "reduceOnly": "false",
        }, signed=True, method="POST")

    def set_api_keys(self, key, secret):
        self.api_key = key
        self.api_secret = secret
        self._dry_run = not (key and secret)
