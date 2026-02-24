import json
from pathlib import Path
from datetime import datetime

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import ccxt

# --- Config ---
STATUS_PATH = Path("status.json")
REFRESH_SEC = 5

# Optional: only used for the chart (status.json provides "price" but not candles)
SYMBOL = "BTC/USDT"
LTF = "1h"
FAST = 20
SLOW = 50


st.set_page_config(page_title="BTC Bot Dashboard", layout="wide")


@st.cache_resource
def get_exchange():
    return ccxt.binance({
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {"defaultType": "spot"},
    })


def sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def fetch_df(ex, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def plot_candles_with_mas(df: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["ts"],
        open=df["open"],
        high=df["high"],
        low=df["low"],
        close=df["close"],
        name="Price"
    ))
    fig.add_trace(go.Scatter(x=df["ts"], y=df["fast"], name=f"SMA {FAST}", mode="lines"))
    fig.add_trace(go.Scatter(x=df["ts"], y=df["slow"], name=f"SMA {SLOW}", mode="lines"))
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10, r=10, t=30, b=10))
    return fig


def read_status():
    if not STATUS_PATH.exists():
        return None, f"{STATUS_PATH} not found. Start the bot (python main.py) to generate it."
    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8")), None
    except Exception as e:
        return None, f"Could not read status.json: {e}"


def fmt_float(x, nd=6):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return "—"


def fmt_price(x):
    if x is None:
        return "—"
    try:
        return f"{float(x):,.2f}"
    except Exception:
        return "—"


def badge(text: str):
    st.markdown(
        f"<span style='padding:4px 10px;border-radius:999px;border:1px solid #ddd;font-size:12px;'>{text}</span>",
        unsafe_allow_html=True,
    )


st.title("BTC Bot Dashboard")

# Auto-refresh (simple)
st.caption(f"Auto-refresh every {REFRESH_SEC}s • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
st.markdown(f"<meta http-equiv='refresh' content='{REFRESH_SEC}'>", unsafe_allow_html=True)

status, err = read_status()
if err:
    st.error(err)
    st.stop()

# Pull key fields with safe defaults
symbol = status.get("symbol", SYMBOL)
timeframe = status.get("timeframe", LTF)
ts = status.get("ts_utc")
price = status.get("price")
equity = status.get("equity")

pos = status.get("position", {}) or {}
pos_side = pos.get("side")
pos_qty = pos.get("qty")
pos_stop = pos.get("stop")
pos_take = pos.get("take")

sig = status.get("signal", {}) or {}
sig_action = sig.get("action")
sig_reason = sig.get("reason")
sig_stop = sig.get("stop_price")
sig_take = sig.get("take_price")

risk = status.get("risk", {}) or {}
can_trade = risk.get("can_trade")
risk_reason = risk.get("reason")

filters = status.get("filters", {}) or {}
deriv = (filters.get("derivatives") or {})
htf = (filters.get("htf") or {})

errors = status.get("errors", []) or []

# --- Top metrics ---
c1, c2, c3, c4, c5 = st.columns(5)

c1.metric("Symbol", symbol)
c2.metric("Price", fmt_price(price))
c3.metric("Equity (paper)", fmt_price(equity))
c4.metric("Signal", (sig_action or "—").upper(), help=str(sig_reason))
c5.metric("Trade OK", str(can_trade), help=str(risk_reason))

# --- Status row (position + gating) ---
st.subheader("Bot State")

row1, row2, row3, row4 = st.columns(4)

with row1:
    st.write("**Position**")
    badge(f"side: {pos_side or 'none'}")
    st.write(f"qty: {pos_qty if pos_qty is not None else '—'}")
    st.write(f"stop: {fmt_price(pos_stop)}")
    st.write(f"take: {fmt_price(pos_take)}")

with row2:
    st.write("**Signal Details**")
    badge(f"action: {(sig_action or '—')}")
    st.write(f"reason: {sig_reason or '—'}")
    st.write(f"stop_price: {fmt_price(sig_stop)}")
    st.write(f"take_price: {fmt_price(sig_take)}")

with row3:
    st.write("**HTF Filter**")
    badge(f"enabled: {htf.get('enabled', False)}")
    badge(f"tf: {htf.get('timeframe', '—')}")
    badge(f"bias: {str(htf.get('bias', '—')).upper()}")
    st.write(f"close: {fmt_price(htf.get('close'))}")
    st.write(f"EMA{htf.get('ema_period', '—')}: {fmt_price(htf.get('ema'))}")
    if htf.get("blocked"):
        st.error(f"HTF BLOCK: {htf.get('block_reason')}")
    else:
        st.success("HTF: OK")
    if htf.get("error"):
        st.warning(f"HTF error: {htf.get('error')}")

with row4:
    st.write("**Derivatives Filter**")
    badge(f"enabled: {deriv.get('enabled', False)}")
    st.write(f"funding: {fmt_float(deriv.get('funding'), 6)}")
    st.write(f"funding_max_abs: {fmt_float(deriv.get('funding_max_abs'), 6)}")
    st.write(f"oi_slope: {fmt_float(deriv.get('oi_slope'), 2)}")
    st.write(f"oi_tf: {deriv.get('oi_tf', '—')} • pts: {deriv.get('oi_points', '—')}")
    if deriv.get("blocked"):
        st.error(f"DERIV BLOCK: {deriv.get('block_reason')}")
        if deriv.get("error"):
            st.warning(f"Deriv error: {deriv.get('error')}")
    else:
        st.success("Derivatives: OK")

# --- Errors ---
if errors:
    st.subheader("Bot Errors")
    for e in errors[-5:]:
        st.error(e)

st.divider()

# --- Chart + Trades ---
left, right = st.columns([2, 1])

with left:
    st.subheader("Price + Moving Averages (Chart feed)")

    try:
        ex = get_exchange()
        df = fetch_df(ex, symbol, timeframe, limit=300)
        df["fast"] = sma(df["close"], FAST)
        df["slow"] = sma(df["close"], SLOW)
        st.plotly_chart(plot_candles_with_mas(df.tail(200)), use_container_width=True)
    except Exception as e:
        st.warning(f"Chart unavailable (exchange fetch failed): {e}")

with right:
    st.subheader("Trades (trades.csv)")
    try:
        trades = pd.read_csv("trades.csv")
        st.dataframe(trades.tail(50), use_container_width=True, height=520)
    except Exception:
        st.info("No trades.csv found yet. If you want, we’ll add trade journaling so this fills automatically.")

# Footer
st.caption(f"status.json updated: {ts or '—'}")