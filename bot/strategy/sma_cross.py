from .base import Strategy, Signal
from bot.data.candles import sma, atr

class SMACross(Strategy):
    def __init__(self, fast=20, slow=50, stop_atr_mult=2.0, takeprofit_rr=2.0):
        self.fast = fast
        self.slow = slow
        self.stop_atr_mult = stop_atr_mult
        self.takeprofit_rr = takeprofit_rr

    def generate(self, df):
        if len(df) < max(self.fast, self.slow) + 5:
            return Signal("flat", "not_enough_data")

        df = df.copy()
        df["fast"] = sma(df["close"], self.fast)
        df["slow"] = sma(df["close"], self.slow)
        df["atr"] = atr(df, 14)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        px = float(last["close"])
        a = float(last["atr"]) if last["atr"] == last["atr"] else None  # not NaN

        bull = prev["fast"] <= prev["slow"] and last["fast"] > last["slow"]
        bear = prev["fast"] >= prev["slow"] and last["fast"] < last["slow"]

        if bull and a:
            stop = px - self.stop_atr_mult * a
            take = px + self.takeprofit_rr * (px - stop)
            return Signal("buy", "sma_bull_cross", stop, take)

        if bear and a:
            stop = px + self.stop_atr_mult * a
            take = px - self.takeprofit_rr * (stop - px)
            return Signal("sell", "sma_bear_cross", stop, take)

        return Signal("flat", "no_cross")
