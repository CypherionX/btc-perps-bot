import pandas as pd

def ohlcv_to_df(ohlcv):
    # ccxt OHLCV: [timestamp(ms), open, high, low, close, volume]
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def sma(series, n: int):
    return series.rolling(n).mean()

def atr(df: pd.DataFrame, n: int = 14):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = (high - low).to_frame("hl")
    tr["hc"] = (high - close.shift()).abs()
    tr["lc"] = (low - close.shift()).abs()
    return tr.max(axis=1).rolling(n).mean()
