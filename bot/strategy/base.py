from dataclasses import dataclass

@dataclass
class Signal:
    action: str              # "buy" | "sell" | "flat"
    reason: str = ""
    stop_price: float | None = None
    take_price: float | None = None

class Strategy:
    def generate(self, df):
        raise NotImplementedError
