import time

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
