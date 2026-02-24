from pathlib import Path

# This script writes full working contents into your project files.
# Run it from inside: ...\btc-perps-bot\btc-perps-bot

FILES = {
    "requirements.txt": """ccxt
pandas
pyyaml
python-dotenv
tenacity
""",
    "config.yaml": """mode: paper
symbol: BTC/USDT
timeframe: 1h
poll_seconds: 60

strategy:
  name: sma_cross
  fast: 20
  slow: 50

risk:
  account_equity_usd: 500
  risk_per_trade_pct: 0.005
  max_daily_loss_pct: 0.02
  max_position_pct: 0.25
  cooldown_seconds: 600
  stop_atr_mult: 2.0
  takeprofit_rr: 2.0

execution:
  order_type: market
  slippage_bps: 5
  reduce_only: true
""",
    ".env": """EXCHANGE=binance
API_KEY=
API_SECRET=
API_PASSWORD=
""",
    "main.py": """from runner import main

if __name__ == "__main__":
    main()
""",
    "runner.py": """import time
import ccxt
from dotenv import load_dotenv

from bot.config import BotConfig
from bot.logger import get_logger
from bot.exchange.paper_client import PaperClient
from bot.data.candles import ohlcv_to_df
from bot.strategy.sma_cross import SMACross
from bot.risk.risk_manager import RiskManager
from bot.execution.executor import Executor


def main():
    load_dotenv()
    cfg = BotConfig.load("config.yaml")
    log = get_logger()

    symbol = cfg.get("symbol", default="BTC/USDT")
    timeframe = cfg.get("timeframe", default="1h")
    poll = int(cfg.get("poll_seconds", default=60))

    # Public market data (no keys needed)
    pub = ccxt.binance({"enableRateLimit": True})

    # Paper trading account
    start_equity = float(cfg.get("risk", "account_equity_usd", default=500))
    client = PaperClient(
        equity_usd=start_equity,
        slippage_bps=float(cfg.get("execution", "slippage_bps", default=5))
    )

    strat = SMACross(
        fast=int(cfg.get("strategy", "fast", default=20)),
        slow=int(cfg.get("strategy", "slow", default=50)),
        stop_atr_mult=float(cfg.get("risk", "stop_atr_mult", default=2.0)),
        takeprofit_rr=float(cfg.get("risk", "takeprofit_rr", default=2.0)),
    )

    risk = RiskManager(cfg)
    exe = Executor(client, cfg, log)

    # simple in-process position tracking (paper mode)
    pos_side = None      # "long" | "short" | None
    pos_qty = 0.0
    stop_px = None
    take_px = None

    log.info("BOT STARTED (PAPER MODE)")

    while True:
        try:
            ohlcv = pub.fetch_ohlcv(symbol, timeframe=timeframe, limit=250)
            df = ohlcv_to_df(ohlcv)
            last_px = float(df.iloc[-1]["close"])

            equity_now = float(client.fetch_balance()["USD"]["free"])
            can_trade, reason = risk.can_trade(equity_now)

            sig = strat.generate(df)

            log.info(
                f"px={last_px:.2f} equity={equity_now:.2f} "
                f"pos={pos_side}:{pos_qty:.6f} signal={sig.action} ({sig.reason}) "
                f"trade_ok={can_trade}({reason})"
            )

            # ---- exits ----
            if pos_side and pos_qty > 0:
                stop_hit = stop_px is not None and (
                    (pos_side == "long" and last_px <= stop_px) or
                    (pos_side == "short" and last_px >= stop_px)
                )
                take_hit = take_px is not None and (
                    (pos_side == "long" and last_px >= take_px) or
                    (pos_side == "short" and last_px <= take_px)
                )

                if stop_hit or take_hit:
                    exit_side = "sell" if pos_side == "long" else "buy"
                    exe.market_exit_reduce_only(symbol, exit_side, pos_qty, last_px)

                    pos_side, pos_qty = None, 0.0
                    stop_px, take_px = None, None
                    risk.set_cooldown()

            # ---- entries ----
            if (not pos_side) and can_trade and sig.action in ("buy", "sell") and sig.stop_price and sig.take_price:
                entry = last_px
                stop = float(sig.stop_price)

                qty = risk.position_size(equity_now, entry, stop)
                if qty > 0:
                    entry_side = "buy" if sig.action == "buy" else "sell"
                    exe.market_entry(symbol, entry_side, qty, last_px)

                    pos_side = "long" if sig.action == "buy" else "short"
                    pos_qty = qty
                    stop_px = float(sig.stop_price)
                    take_px = float(sig.take_price)

            time.sleep(poll)

        except Exception as e:
            log.error(f"loop_error: {e}")
            time.sleep(5)
""",

    # ----- modules -----
    "bot/config.py": """from dataclasses import dataclass
import yaml

@dataclass
class BotConfig:
    raw: dict

    @staticmethod
    def load(path: str) -> "BotConfig":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return BotConfig(raw=raw)

    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur
""",
    "bot/logger.py": """import logging

def get_logger(name: str = "tradebot"):
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    h = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    h.setFormatter(fmt)
    log.addHandler(h)
    return log
""",
    "bot/exchange/paper_client.py": """from dataclasses import dataclass

@dataclass
class PaperPosition:
    side: str | None = None   # "long" | "short" | None
    qty: float = 0.0
    entry: float = 0.0

class PaperClient:
    \"""
    Simple in-memory paper trading client.
    - Assumes fills at given 'price' with small slippage.
    - Tracks realized PnL in equity.
    \"""
    def __init__(self, equity_usd: float, slippage_bps: float = 5):
        self.equity = float(equity_usd)
        self.pos = PaperPosition()
        self.slippage_bps = float(slippage_bps)
        self.realized_pnl = 0.0

    def fetch_balance(self):
        return {"USD": {"free": self.equity}, "info": {"realized_pnl": self.realized_pnl}}

    def _fill_price(self, px: float) -> float:
        slip = self.slippage_bps / 10_000.0
        return px * (1 + slip)

    def create_order(self, symbol: str, order_type: str, side: str, amount: float, price: float | None = None, params=None):
        if price is None:
            raise ValueError("PaperClient requires 'price' for fill simulation")
        if amount <= 0:
            raise ValueError("amount must be > 0")

        px = self._fill_price(float(price))
        reduce_only = bool((params or {}).get("reduceOnly", False))

        if side == "buy":
            return self._buy(amount, px, reduce_only)
        if side == "sell":
            return self._sell(amount, px, reduce_only)
        raise ValueError("side must be 'buy' or 'sell'")

    def _buy(self, qty: float, px: float, reduce_only: bool):
        # buy closes short or opens/increases long
        if self.pos.side == "short":
            close_qty = min(qty, self.pos.qty)
            pnl = (self.pos.entry - px) * close_qty
            self.equity += pnl
            self.realized_pnl += pnl
            self.pos.qty -= close_qty
            if self.pos.qty <= 0:
                self.pos = PaperPosition()
            if reduce_only or close_qty == qty:
                return {"status": "reduced_or_closed", "filled": close_qty, "price": px, "pnl": pnl}
            qty -= close_qty

        if reduce_only:
            return {"status": "reduce_only_no_position", "filled": 0, "price": px}

        # open/increase long
        if self.pos.side in (None, "long"):
            new_qty = self.pos.qty + qty
            new_entry = (self.pos.entry * self.pos.qty + px * qty) / new_qty if new_qty else 0.0
            self.pos.side = "long"
            self.pos.qty = new_qty
            self.pos.entry = new_entry
        return {"status": "opened_or_increased", "filled": qty, "price": px}

    def _sell(self, qty: float, px: float, reduce_only: bool):
        # sell closes long or opens/increases short
        if self.pos.side == "long":
            close_qty = min(qty, self.pos.qty)
            pnl = (px - self.pos.entry) * close_qty
            self.equity += pnl
            self.realized_pnl += pnl
            self.pos.qty -= close_qty
            if self.pos.qty <= 0:
                self.pos = PaperPosition()
            if reduce_only or close_qty == qty:
                return {"status": "reduced_or_closed", "filled": close_qty, "price": px, "pnl": pnl}
            qty -= close_qty

        if reduce_only:
            return {"status": "reduce_only_no_position", "filled": 0, "price": px}

        # open/increase short
        if self.pos.side in (None, "short"):
            new_qty = self.pos.qty + qty
            new_entry = (self.pos.entry * self.pos.qty + px * qty) / new_qty if new_qty else 0.0
            self.pos.side = "short"
            self.pos.qty = new_qty
            self.pos.entry = new_entry
        return {"status": "opened_or_increased", "filled": qty, "price": px}
""",
    "bot/data/candles.py": """import pandas as pd

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
""",
    "bot/strategy/base.py": """from dataclasses import dataclass

@dataclass
class Signal:
    action: str              # "buy" | "sell" | "flat"
    reason: str = ""
    stop_price: float | None = None
    take_price: float | None = None

class Strategy:
    def generate(self, df):
        raise NotImplementedError
""",
    "bot/strategy/sma_cross.py": """from .base import Strategy, Signal
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
""",
    "bot/risk/risk_manager.py": """import time

class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.day_start_equity = float(cfg.get("risk", "account_equity_usd", default=0))
        self.cooldown_until = 0

    def can_trade(self, equity_now: float):
        max_daily_loss = float(self.cfg.get("risk", "max_daily_loss_pct", default=0.02))
        if self.day_start_equity > 0:
            dd = (self.day_start_equity - equity_now) / self.day_start_equity
            if dd >= max_daily_loss:
                return False, "daily_loss_limit_hit"

        now = int(time.time())
        if now < self.cooldown_until:
            return False, "cooldown"
        return True, "ok"

    def set_cooldown(self):
        cd = int(self.cfg.get("risk", "cooldown_seconds", default=600))
        self.cooldown_until = int(time.time()) + cd

    def position_size(self, equity_now: float, entry: float, stop: float) -> float:
        risk_pct = float(self.cfg.get("risk", "risk_per_trade_pct", default=0.005))
        risk_usd = equity_now * risk_pct
        per_unit = abs(entry - stop)
        if per_unit <= 0:
            return 0.0

        qty = risk_usd / per_unit

        max_pos_pct = float(self.cfg.get("risk", "max_position_pct", default=0.25))
        max_notional = equity_now * max_pos_pct
        qty_cap = max_notional / entry

        return float(min(qty, qty_cap))
""",
    "bot/execution/executor.py": """class Executor:
    def __init__(self, client, cfg, log):
        self.client = client
        self.cfg = cfg
        self.log = log

    def market_entry(self, symbol: str, side: str, qty: float, px: float):
        if qty <= 0:
            return None
        resp = self.client.create_order(symbol, "market", side, qty, price=px, params={})
        self.log.info(f"ENTRY {side} qty={qty:.6f} px~{px:.2f} resp={resp}")
        return resp

    def market_exit_reduce_only(self, symbol: str, side: str, qty: float, px: float):
        if qty <= 0:
            return None
        params = {"reduceOnly": True}
        resp = self.client.create_order(symbol, "market", side, qty, price=px, params=params)
        self.log.info(f"EXIT {side} reduceOnly qty={qty:.6f} px~{px:.2f} resp={resp}")
        return resp
""",
}

DIRS = [
    "bot",
    "bot/exchange",
    "bot/data",
    "bot/strategy",
    "bot/risk",
    "bot/execution",
]

def main():
    root = Path.cwd()

    # ensure folders exist
    for d in DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)

    # write files
    for rel, content in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        print(f"WROTE: {p}")

    print("\nDone. Next commands:")
    print("  .venv\\Scripts\\activate")
    print("  pip install -r requirements.txt")
    print("  python main.py")

if __name__ == "__main__":
    main()