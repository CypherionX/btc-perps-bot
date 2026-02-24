class Executor:
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
