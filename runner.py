import time
import ccxt
from dotenv import load_dotenv

from bot.config import BotConfig
from bot.logger import get_logger
from bot.exchange.paper_client import PaperClient
from bot.data.candles import ohlcv_to_df
from bot.strategy.sma_cross import SMACross
from bot.risk.risk_manager import RiskManager
from bot.execution.executor import Executor
from bot.filters.htf_structure import compute_htf_bias

from bot.derivatives.binance_metrics import BinanceDerivativesMetrics


def _create_metrics_if_enabled(cfg: BotConfig, log):
    use_filters = bool(cfg.get("filters", "use_derivatives_filters", default=True))
    if not use_filters:
        log.info("Derivatives filters disabled - skipping BinanceDerivativesMetrics init")
        return None

    try:
        m = BinanceDerivativesMetrics()
        log.info("Derivatives filters enabled - BinanceDerivativesMetrics initialized")
        return m
    except Exception as e:
        # Safer option is to return None and just log it (so the bot still runs).
        log.warning(f"Failed to initialize BinanceDerivativesMetrics ({e}) - proceeding WITHOUT filters")
        return None


def _apply_derivatives_filters(cfg: BotConfig, log, metrics, sig):
    """
    Mutates/returns a Signal: blocks trades by converting buy/sell -> flat when filters fail.
    """
    if metrics is None:
        return sig

    if sig.action not in ("buy", "sell"):
        return sig

    try:
        funding = metrics.funding_rate_now("BTC/USDT")
        funding_max_abs = float(cfg.get("filters", "funding_max_abs", default=0.0003))

        # Funding gating
        if sig.action == "buy" and funding > funding_max_abs:
            log.info(f"FILTER BLOCK: funding too high for LONG (funding={funding:.6f} > {funding_max_abs:.6f})")
            sig.action = "flat"
            return sig

        if sig.action == "sell" and funding < -funding_max_abs:
            log.info(f"FILTER BLOCK: funding too low for SHORT (funding={funding:.6f} < {-funding_max_abs:.6f})")
            sig.action = "flat"
            return sig

        # OI trend gating (optional)
        require_oi = bool(cfg.get("filters", "require_oi_rising", default=True))
        if require_oi:
            pts = int(cfg.get("filters", "oi_trend_points", default=6))
            tf = cfg.get("filters", "oi_trend_timeframe", default="5m")
            slope = metrics.open_interest_trend("BTC/USDT", timeframe=tf, points=pts)

            if slope <= 0:
                log.info(f"FILTER BLOCK: OI not rising (slope={slope:.2f})")
                sig.action = "flat"
                return sig

        log.info(f"DERIV METRICS OK: funding={funding:.6f}")

        return sig

    except Exception as e:
        # Fail-safe: if metrics fail, block trading (safer)
        log.info(f"FILTER BLOCK: derivatives metrics unavailable ({e})")
        sig.action = "flat"
        return sig


def main():
    load_dotenv()
    cfg = BotConfig.load("config.yaml")
    log = get_logger()

    # Initialize derivatives metrics based on configuration
    metrics = _create_metrics_if_enabled(cfg, log)

    symbol = cfg.get("symbol", default="BTC/USDT")
    timeframe = cfg.get("timeframe", default="1h")
    poll = int(cfg.get("poll_seconds", default=60))

    # Public market data (no keys needed) - spot candles
    pub = ccxt.binance({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},
    })

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
            # ---- HTF structure ----
            htf_enabled = bool(cfg.get("htf", "enabled", default=True))
            htf_bias = None

            if htf_enabled:
                htf_tf = cfg.get("htf", "timeframe", default="4h")
                ema_period = int(cfg.get("htf", "ema_period", default=200))
                neutral_band = float(cfg.get("htf", "neutral_band_atr", default=0.25))
                neutral_behavior = cfg.get("htf", "neutral_behavior", default="block")

                ohlcv_htf = pub.fetch_ohlcv(symbol, timeframe=htf_tf, limit=max(ema_period + 50, 300))
                df_htf = ohlcv_to_df(ohlcv_htf)

            htf_bias = compute_htf_bias(df_htf, ema_period=ema_period, neutral_band_atr=neutral_band)
            last_px = float(df.iloc[-1]["close"])

            equity_now = float(client.fetch_balance()["USD"]["free"])
            can_trade, reason = risk.can_trade(equity_now)

            sig = strat.generate(df)
            sig = _apply_derivatives_filters(cfg, log, metrics, sig)

            # ---- gate by HTF bias ----
            if htf_enabled and sig.action in ("buy", "sell") and htf_bias is not None:
                if htf_bias.bias == "bull" and sig.action == "sell":
                    log.info(f"HTF BLOCK: HTF=bull blocks SHORT ({htf_bias.reason}) close={htf_bias.close:.2f} ema={htf_bias.ema:.2f}")
                    sig.action = "flat"

            elif htf_bias.bias == "bear" and sig.action == "buy":
                log.info(f"HTF BLOCK: HTF=bear blocks LONG ({htf_bias.reason}) close={htf_bias.close:.2f} ema={htf_bias.ema:.2f}")
                sig.action = "flat"

            elif htf_bias.bias == "neutral":
                neutral_behavior = cfg.get("htf", "neutral_behavior", default="block")
            if neutral_behavior == "block":
                log.info(f"HTF BLOCK: HTF=neutral blocks trade ({htf_bias.reason}) close={htf_bias.close:.2f} ema={htf_bias.ema:.2f}")
            sig.action = "flat"

            if htf_enabled and htf_bias is not None:
                log.info(f"HTF: {cfg.get('htf','timeframe',default='4h')} bias={htf_bias.bias} close={htf_bias.close:.2f} ema={htf_bias.ema:.2f}")

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


if __name__ == "__main__":
    main()