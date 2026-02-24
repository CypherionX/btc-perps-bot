from dataclasses import dataclass

@dataclass
class PaperPosition:
    side: str | None = None   # "long" | "short" | None
    qty: float = 0.0
    entry: float = 0.0

class PaperClient:
    """
    Simple in-memory paper trading client.
    - Assumes fills at given 'price' with small slippage.
    - Tracks realized PnL in equity.
    """
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
