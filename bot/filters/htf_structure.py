from dataclasses import dataclass
import pandas as pd

from bot.data.candles import atr


@dataclass
class HTFBias:
    bias: str          # "bull" | "bear" | "neutral"
    ema: float
    atr: float
    close: float
    reason: str


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def compute_htf_bias(df_htf: pd.DataFrame, ema_period: int = 200, neutral_band_atr: float = 0.25) -> HTFBias:
    """
    Bias rules:
      - bull: close > EMA + neutral_band_atr * ATR
      - bear: close < EMA - neutral_band_atr * ATR
      - neutral: otherwise
    """
    if df_htf is None or len(df_htf) < ema_period + 20:
        last_close = float(df_htf.iloc[-1]["close"]) if df_htf is not None and len(df_htf) else 0.0
        return HTFBias("neutral", 0.0, 0.0, last_close, "not_enough_htf_data")

    d = df_htf.copy()
    d["ema"] = ema(d["close"], ema_period)
    d["atr"] = atr(d, 14)

    last = d.iloc[-1]
    c = float(last["close"])
    e = float(last["ema"])
    a = float(last["atr"]) if last["atr"] == last["atr"] else 0.0  # handle NaN

    upper = e + neutral_band_atr * a
    lower = e - neutral_band_atr * a

    if c > upper:
        return HTFBias("bull", e, a, c, "close_above_ema")
    if c < lower:
        return HTFBias("bear", e, a, c, "close_below_ema")
    return HTFBias("neutral", e, a, c, "near_ema_neutral")