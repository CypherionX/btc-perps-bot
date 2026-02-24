import time
from datetime import datetime

import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import ccxt

# --- Config ---
SYMBOL = "BTC/USDT"
LTF = "1h"         # match your config.yaml timeframe
HTF = "4h"         # match your HTF timeframe
FAST = 20          # match strategy fast
SLOW = 50          # match strategy slow
EMA_HTF = 200      # match HTF ema_period
REFRESH_SEC = 10

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

def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()

def fetch_df(ex, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df

def htf_bias(df_htf: pd.DataFrame):
    if len(df_htf) < EMA_HTF + 20:
        return "neutral", None, None, None
    df = df_htf.copy()
    df["ema"] = ema(df["close"], EMA_HTF)
    last = df.iloc[-1]
    c = float(last["close"])
    e = float(last["ema"])
    # simple regime (neutral band can be added later)
    if c > e:
        return "bull", c, e, df
    if c < e:
        return "bear", c, e, df
    return "neutral", c, e, df

def plot_candles_with_mas(df: pd.DataFrame):
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["ts"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name="Price"
    ))
    fig.add_trace(go.Scatter(x=df["ts"], y=df["fast"], name=f"SMA {FAST}", mode="lines"))
    fig.add_trace(go.Scatter(x=df["ts"], y=df["slow"], name=f"SMA {SLOW}", mode="lines"))
    fig.update_layout(height=520, xaxis_rangeslider_visible=False, margin=dict(l=10,r=10,t=30,b=10))
    return fig

st.title("BTC Bot Dashboard")

# auto-refresh
st.caption(f"Auto-refresh every {REFRESH_SEC}s • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
time.sleep(0.01)

ex = get_exchange()

# --- Fetch data ---
df_ltf = fetch_df(ex, SYMBOL, LTF, limit=300)
df_ltf["fast"] = sma(df_ltf["close"], FAST)
df_ltf["slow"] = sma(df_ltf["close"], SLOW)

df_htf = fetch_df(ex, SYMBOL, HTF, limit=max(EMA_HTF + 50, 300))
bias, htf_close, htf_ema, _ = htf_bias(df_htf)

last = df_ltf.iloc[-1]
prev = df_ltf.iloc[-2]
price = float(last["close"])

bull_cross = prev["fast"] <= prev["slow"] and last["fast"] > last["slow"]
bear_cross = prev["fast"] >= prev["slow"] and last["fast"] < last["slow"]

signal = "flat"
reason = "no_cross"
if bull_cross:
    signal, reason = "buy", "sma_bull_cross"
elif bear_cross:
    signal, reason = "sell", "sma_bear_cross"

# HTF gate preview
htf_gate = "allow"
if bias == "bull" and signal == "sell":
    htf_gate = "blocked_by_htf"
if bias == "bear" and signal == "buy":
    htf_gate = "blocked_by_htf"
if bias == "neutral" and signal in ("buy","sell"):
    htf_gate = "neutral_htf"

# --- Layout ---
c1, c2, c3, c4 = st.columns(4)
c1.metric("BTC Price", f"{price:,.2f}")
c2.metric("LTF Signal", f"{signal.upper()}", help=reason)
c3.metric("HTF Bias", bias.upper(), help=f"HTF close={htf_close:.2f} • EMA{EMA_HTF}={htf_ema:.2f}" if htf_close else "Not enough HTF data")
c4.metric("HTF Gate", htf_gate.upper())

left, right = st.columns([2,1])

with left:
    st.subheader("Price + Moving Averages")
    st.plotly_chart(plot_candles_with_mas(df_ltf.tail(200)), use_container_width=True)

with right:
    st.subheader("Trades (trades.csv)")
    try:
        trades = pd.read_csv("trades.csv")
        st.dataframe(trades.tail(50), use_container_width=True, height=520)
    except Exception:
        st.info("No trades.csv found yet. If you want, we’ll add trade journaling next so this fills automatically.")

# refresh loop trigger
st.markdown(f"<meta http-equiv='refresh' content='{REFRESH_SEC}'>", unsafe_allow_html=True)