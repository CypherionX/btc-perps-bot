import time
import json
from pathlib import Path
from datetime import datetime, timezone

import ccxt
from dotenv import load_dotenv

from bot.config import BotConfig
from bot.logger import get_logger
from bot.exchange.paper_client import PaperClient
from bot.data.candles import ohlcv_to_df
from bot.strategy.sma_cross import SMACross
from bot.risk.risk_manager import RiskManager
from bot.execution.executor import Executor

from bot.derivatives.binance_metrics import BinanceDerivativesMetrics
from bot.filters.htf_structure import compute_htf_bias


STATUS_PATH = Path("status.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


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
        log.warning(f"Failed to initialize BinanceDerivativesMetrics ({e}) - proceeding WITHOUT derivatives filters")
        return None


def _apply_derivatives_filters(cfg: BotConfig, log, metrics, sig):
    """
    Returns (sig, deriv_state)
      - sig may be mutated to flat when filters fail
      - deriv_state is written to status.json for dashboard use
    """
    deriv_state = {
        "enabled": metrics is not None,
        "funding": None,
        "funding_max_abs": float(cfg.get("filters", "funding_max_abs", default=0.0003)),
        "oi_slope": None,
        "oi_tf": cfg.get("filters", "oi_trend_timeframe", default="5m"),
        "oi_points": int(cfg.get("filters", "oi_trend_points", default=6)),
        "require_oi_rising": bool(cfg.get("filters", "require_oi_rising", default=True)),
        "blocked": False,
        "block_reason": None,
        "error": None,
    }

    if metrics is None:
        return sig, deriv_state

    if sig.action not in ("buy", "sell"):
        return sig, deriv_state

    try:
        funding = float(metrics.funding_rate_now("BTC/USDT"))
        deriv_state["funding"] = funding
        funding_max_abs = deriv_state["funding_max_abs"]

        # Funding gating
        if sig.action == "buy" and funding > funding_max_abs:
            deriv_state["blocked"] = True
            deriv_state["block_reason"] = f"funding_too_high_for_long ({funding:.6f} > {funding_max_abs:.6f})"
            sig.action = "flat"
            return sig, deriv_state

        if sig.action == "sell" and funding < -funding_max_abs:
            deriv_state["blocked"] = True
            deriv_state["block_reason"] = f"funding_too_low_for_short ({funding:.6f} < {-funding_max_abs:.6f})"
            sig.action = "flat"
            return sig, deriv_state

        # OI trend gating (optional)
        if deriv_state["require_oi_rising"]:
            pts = deriv_state["oi_points"]
            tf = deriv_state["oi_tf"]
            slope = float(metrics.open_interest_trend("BTC/USDT", timeframe=tf, points=pts))
            deriv_state["oi_slope"] = slope

            if slope <= 0:
                deriv_state["blocked"] = True
                deriv_state["block_reason"] = f"oi_not_rising (slope={slope:.2f})"
                sig.action = "flat"
                return sig, deriv_state

        return sig, deriv_state

    except Exception as e:
        # Fail-safe: if derivatives metrics fail, block trading (safer)
        deriv_state["blocked"] = True
        deriv_state["block_reason"] = "derivatives_metrics_unavailable"
        deriv_state["error"] = str(e)
        sig.action = "flat"
        return sig, deriv_state


def _apply_htf_gate(cfg: BotConfig, log, pub, symbol: str, sig):
    """
    Fetches HTF candles, computes bias, and gates the signal.
    Returns (sig, htf_state)
    """
    htf_state = {
        "enabled": bool(cfg.get("htf", "enabled", default=True)),
        "timeframe": cfg.get("htf", "timeframe", default="4h"),
        "ema_period": int(cfg.get("htf", "ema_period", default=200)),
        "neutral_band_atr": float(cfg.get("htf", "neutral_band_atr", default=0.25)),
        "neutral_behavior": cfg.get("htf", "neutral_behavior", default="block"),
        "bias": None,
        "close": None,
        "ema": None,
        "atr": None,
        "reason": None,
        "blocked": False,
        "block_reason": None,
        "error": None,
    }

    if not htf_state["enabled"]:
        return sig, htf_state

    try:
        limit = max(htf_state["ema_period"] + 50, 300)
        ohlcv_htf = pub.fetch_ohlcv(symbol, timeframe=htf_state["timeframe"], limit=limit)
        df_htf = ohlcv_to_df(ohlcv_htf)

        bias = compute_htf_bias(
            df_htf,
            ema_period=htf_state["ema_period"],
            neutral_band_atr=htf_state["neutral_band_atr"],
        )

        htf_state["bias"] = bias.bias
        htf_state["close"] = bias.close
        htf_state["ema"] = bias.ema
        htf_state["atr"] = bias.atr
        htf_state["reason"] = bias.reason

        # Gate only if we have a trade signal
        if sig.action in ("buy", "sell"):
            if bias.bias == "bull" and sig.action == "sell":
                htf_state["blocked"] = True
                htf_state["block_reason"] = "htf_bull_blocks_short"
                sig.action = "flat"
                return sig, htf_state

            if bias.bias == "bear" and sig.action == "buy":
                htf_state["blocked"] = True
                htf_state["block_reason"] = "htf_bear_blocks_long"
                sig.action = "flat"
                return sig, htf_state

            if bias.bias == "neutral" and htf_state["neutral_behavior"] == "block":
                htf_state["blocked"] = True
                htf_state["block_reason"] = "htf_neutral_blocks_trade"
                sig.action = "flat"
                return sig, htf_state

        return sig, htf_state

    except Exception as e:
        htf_state["blocked"] = True
        htf_state["block_reason"] = "htf_unavailable"
        htf_state["error"] = str(e)
        sig.action = "flat"
        return sig, htf_state


def main():
    load_dotenv()
    cfg = BotConfig.load("config.yaml")
    log = get_logger()

    symbol = cfg.get("symbol", default="BTC/USDT")
    timeframe = cfg.get("timeframe", default="1h")
    poll = int(cfg.get("poll_seconds", default=60))

    # Public market data (spot candles)
    pub = ccxt.binance({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},
    })

    # Derivatives metrics client (funding + OI)
    metrics = _create_metrics_if_enabled(cfg, log)

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

    # local position tracking
    pos_side = None
    pos_qty = 0.0
    stop_px = None
    take_px = None

    log.info("BOT STARTED (PAPER MODE)")

    while True:
        loop_started = _utc_now_iso()

        status = {
            "ts_utc": loop_started,
            "symbol": symbol,
            "timeframe": timeframe,
            "price": None,
            "equity": None,
            "position": {
                "side": pos_side,
                "qty": pos_qty,
                "stop": stop_px,
                "take": take_px,
            },
            "signal": {
                "action": None,
                "reason": None,
                "stop_price": None,
                "take_price": None,
            },
            "risk": {
                "can_trade": None,
                "reason": None,
            },
            "filters": {
                "derivatives": None,
                "htf": None,
            },
            "errors": [],
        }

        try:
            ohlcv = pub.fetch_ohlcv(symbol, timeframe=timeframe, limit=250)
            df = ohlcv_to_df(ohlcv)
            last_px = float(df.iloc[-1]["close"])

            equity_now = float(client.fetch_balance()["USD"]["free"])
            can_trade, can_reason = risk.can_trade(equity_now)

            sig = strat.generate(df)

            # Apply derivatives filters first (funding + OI)
            sig, deriv_state = _apply_derivatives_filters(cfg, log, metrics, sig)

            # Apply HTF gate
            sig, htf_state = _apply_htf_gate(cfg, log, pub, symbol, sig)

            # Update status payload
            status["price"] = last_px
            status["equity"] = equity_now
            status["risk"]["can_trade"] = can_trade
            status["risk"]["reason"] = can_reason
            status["signal"] = {
                "action": sig.action,
                "reason": sig.reason,
                "stop_price": sig.stop_price,
                "take_price": sig.take_price,
            }
            status["filters"]["derivatives"] = deriv_state
            status["filters"]["htf"] = htf_state

            log.info(
                f"px={last_px:.2f} equity={equity_now:.2f} "
                f"pos={pos_side}:{pos_qty:.6f} signal={sig.action} ({sig.reason}) "
                f"trade_ok={can_trade}({can_reason})"
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

                    status["position"] = {"side": None, "qty": 0.0, "stop": None, "take": None}

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

                    status["position"] = {"side": pos_side, "qty": pos_qty, "stop": stop_px, "take": take_px}

        except Exception as e:
            log.error(f"loop_error: {e}")
            status["errors"].append(str(e))

        # Always write status.json even if there was an error
        try:
            _atomic_write_json(STATUS_PATH, status)
        except Exception as e:
            log.error(f"status_write_error: {e}")

        time.sleep(poll)


if __name__ == "__main__":
    main()