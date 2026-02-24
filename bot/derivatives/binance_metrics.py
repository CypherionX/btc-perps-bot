import ccxt

class BinanceDerivativesMetrics:
    """
    Pulls funding + open interest from Binance USD-M futures public endpoints via ccxt.
    DATA ONLY. (No trading.)
    """

    def __init__(self, timeout_ms: int = 30000):
        # Binance USD-M futures in ccxt:
        # exchange id = binanceusdm
        self.ex = ccxt.binanceusdm({
            "enableRateLimit": True,
            "timeout": timeout_ms,
        })
        self.ex.load_markets()

    def funding_rate_now(self, symbol: str) -> float:
        """
        Returns current funding rate as a decimal (e.g. 0.0001 = 0.01%).
        """
        fr = self.ex.fetch_funding_rate(symbol)
        return float(fr.get("fundingRate"))

    def open_interest_now(self, symbol: str) -> float:
        """
        Returns current open interest if supported by ccxt.
        """
        oi = self.ex.fetch_open_interest(symbol)
        for k in ("openInterestValue", "openInterestAmount", "openInterest"):
            if k in oi and oi[k] is not None:
                return float(oi[k])

        info = oi.get("info", {})
        if "openInterest" in info:
            return float(info["openInterest"])

        raise KeyError(f"Could not parse open interest from response keys={list(oi.keys())}")

    def open_interest_trend(self, symbol: str, timeframe: str = "5m", points: int = 6) -> float:
        """
        Returns a simple trend proxy: last - first from OI history.
        Uses Binance USD-M open interest history endpoint via ccxt raw call.
        """
        method = getattr(self.ex, "fapiPublicGetOpenInterestHist", None)
        if method is None:
            raise NotImplementedError("CCXT method fapiPublicGetOpenInterestHist not available")

        market = self.ex.market(symbol)
        req = {
            "symbol": market["id"],   # e.g. BTCUSDT
            "period": timeframe,      # "5m","15m","30m","1h",...
            "limit": points,
        }
        data = method(req)
        if not data or len(data) < 2:
            raise ValueError("Not enough OI history returned")

        first = float(data[0].get("sumOpenInterest") or data[0].get("openInterest") or 0.0)
        last = float(data[-1].get("sumOpenInterest") or data[-1].get("openInterest") or 0.0)
        return last - first