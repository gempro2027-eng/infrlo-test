import os
import io
import base64
import time
import math

import requests
import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from flask import Flask, request, render_template_string

app = Flask(__name__)

BINANCE_BASE = "https://data-api.binance.vision/api/v3/klines"

COINS = [
    ("DOGEUSDT", "DOGE"),
    ("BTCUSDT", "BTC"),
    ("ETHUSDT", "ETH"),
    ("SOLUSDT", "SOL"),
    ("XRPUSDT", "XRP"),
    ("BNBUSDT", "BNB"),
]
VALID_SYMBOLS = {c[0] for c in COINS}

STRATEGIES = [
    ("ema_cross", "EMA20/EMA50 trend (long-only)"),
    ("rsi_bounce", "RSI bounce (buy <30, sell >55)"),
    ("buy_hold", "Buy & Hold (baseline)"),
]
VALID_STRATS = {s[0] for s in STRATEGIES}

TIMEFRAMES = ["1h", "4h", "1d"]
TF_MS = {"1h": 3600_000, "4h": 4 * 3600_000, "1d": 24 * 3600_000}

START_CAPITAL = 1000.0
FEE = 0.001  # 0.1% per trade


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------
def fetch_klines(symbol, interval, days):
    """Fetch OHLCV klines from Binance public data API, looping past the
    1000-candle limit. Returns list of [open_time, open, high, low, close]."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600_000
    tf_ms = TF_MS[interval]
    out = []
    cursor = start_ms
    # hard cap on loops to avoid runaway
    for _ in range(200):
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "limit": 1000,
        }
        data = _get_with_retry(params)
        if not data:
            break
        for k in data:
            out.append([
                int(k[0]),
                float(k[1]),
                float(k[2]),
                float(k[3]),
                float(k[4]),
            ])
        last_open = int(data[-1][0])
        if len(data) < 1000:
            break
        cursor = last_open + tf_ms
        if cursor >= now_ms:
            break
    # dedupe by open_time, keep sorted
    seen = {}
    for row in out:
        seen[row[0]] = row
    rows = [seen[t] for t in sorted(seen)]
    return rows


def _get_with_retry(params, retries=3, timeout=15):
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(BINANCE_BASE, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last_err = "HTTP %s: %s" % (r.status_code, r.text[:200])
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Binance fetch failed: %s" % last_err)


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def ema(values, period):
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan)
    if len(values) == 0:
        return out
    k = 2.0 / (period + 1.0)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()
    for i in range(period, n):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
    return out


# ---------------------------------------------------------------------------
# Signals -> positions (0 = flat, 1 = long). Decided on candle close,
# applied to the next candle to avoid lookahead.
# ---------------------------------------------------------------------------
def signals_ema(closes):
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    pos = np.zeros(len(closes))
    for i in range(len(closes)):
        if np.isnan(e20[i]) or np.isnan(e50[i]):
            pos[i] = 0
        else:
            pos[i] = 1.0 if e20[i] > e50[i] else 0.0
    return pos


def signals_rsi(closes):
    r = rsi(closes, 14)
    pos = np.zeros(len(closes))
    holding = False
    for i in range(len(closes)):
        if np.isnan(r[i]):
            pos[i] = 1.0 if holding else 0.0
            continue
        if not holding and r[i] < 30:
            holding = True
        elif holding and r[i] > 55:
            holding = False
        pos[i] = 1.0 if holding else 0.0
    return pos


def signals_buyhold(closes):
    return np.ones(len(closes))


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
def run_backtest(rows, strategy):
    times = [r[0] for r in rows]
    closes = np.array([r[4] for r in rows], dtype=float)
    n = len(closes)
    if n < 2:
        raise RuntimeError("Not enough data to backtest.")

    if strategy == "ema_cross":
        raw_pos = signals_ema(closes)
    elif strategy == "rsi_bounce":
        raw_pos = signals_rsi(closes)
    else:
        raw_pos = signals_buyhold(closes)

    # apply decision on next candle (shift by 1) to avoid lookahead
    pos = np.zeros(n)
    pos[1:] = raw_pos[:-1]

    equity = np.full(n, START_CAPITAL)
    cash = START_CAPITAL
    units = 0.0
    in_market = False
    trades = []  # (type, time, price, pnl)
    entry_price = None
    entry_times = []
    exit_times = []
    wins = 0
    losses = 0

    for i in range(n):
        price = closes[i]
        want = pos[i] > 0.5
        if want and not in_market:
            # buy with all cash
            units = (cash * (1 - FEE)) / price
            cash = 0.0
            in_market = True
            entry_price = price
            entry_times.append((times[i], price))
        elif not want and in_market:
            # sell everything
            cash = units * price * (1 - FEE)
            units = 0.0
            in_market = False
            exit_times.append((times[i], price))
            if entry_price is not None:
                if price > entry_price:
                    wins += 1
                else:
                    losses += 1
                entry_price = None
        equity[i] = cash + units * price

    # mark-to-market final
    final_equity = cash + units * closes[-1]

    total_return = (final_equity / START_CAPITAL - 1.0) * 100.0
    bh_return = (closes[-1] / closes[0] - 1.0) * 100.0

    # count round-trip trades
    num_trades = len(exit_times) + (1 if in_market else 0)
    closed = wins + losses
    win_rate = (wins / closed * 100.0) if closed > 0 else 0.0

    # max drawdown
    peak = -math.inf
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (v / peak - 1.0) * 100.0 if peak > 0 else 0.0
        if dd < max_dd:
            max_dd = dd

    metrics = {
        "total_return": total_return,
        "bh_return": bh_return,
        "num_trades": num_trades,
        "win_rate": win_rate,
        "max_dd": max_dd,
        "final_equity": final_equity,
        "candles": n,
        "start": times[0],
        "end": times[-1],
    }
    return metrics, times, closes, equity, entry_times, exit_times


# ---------------------------------------------------------------------------
# Charting
# ---------------------------------------------------------------------------
def make_chart(times, closes, equity, entry_times, exit_times, symbol, strategy, interval):
    import datetime as dt
    xs = [dt.datetime.utcfromtimestamp(t / 1000) for t in times]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    ax1.plot(xs, closes, color="#1f77b4", linewidth=1.0, label="Price")
    if entry_times:
        ex = [dt.datetime.utcfromtimestamp(t / 1000) for t, _ in entry_times]
        ey = [p for _, p in entry_times]
        ax1.scatter(ex, ey, marker="^", color="green", s=40, label="Buy", zorder=5)
    if exit_times:
        xx = [dt.datetime.utcfromtimestamp(t / 1000) for t, _ in exit_times]
        xy = [p for _, p in exit_times]
        ax1.scatter(xx, xy, marker="v", color="red", s=40, label="Sell", zorder=5)
    ax1.set_title("%s price — %s (%s)" % (symbol, strategy, interval))
    ax1.set_ylabel("Price (USDT)")
    ax1.legend(loc="best", fontsize=8)
    ax1.grid(alpha=0.3)

    ax2.plot(xs, equity, color="#2ca02c", linewidth=1.2, label="Equity")
    ax2.axhline(START_CAPITAL, color="gray", linestyle="--", linewidth=0.8)
    ax2.set_title("Equity curve (start $%.0f)" % START_CAPITAL)
    ax2.set_ylabel("Equity (USDT)")
    ax2.legend(loc="best", fontsize=8)
    ax2.grid(alpha=0.3)

    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90)
    plt.close(fig)
    buf.seek(0)
    return base64.b64encode(buf.read()).decode("ascii")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
FORM_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Crypto Backtest</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto; padding: 0 1rem; color:#1a1a1a; }
  h1 { font-size: 1.5rem; }
  form { background:#f6f8fa; border:1px solid #d0d7de; border-radius:10px; padding:1.2rem; }
  label { display:block; margin:.6rem 0 .2rem; font-weight:600; font-size:.9rem; }
  select, input { width:100%; padding:.5rem; border:1px solid #d0d7de; border-radius:6px; font-size:1rem; }
  button { margin-top:1rem; background:#1f6feb; color:#fff; border:0; padding:.6rem 1.2rem; border-radius:6px; font-size:1rem; cursor:pointer; }
  table { border-collapse:collapse; margin:1rem 0; width:100%; }
  td,th { border:1px solid #d0d7de; padding:.4rem .6rem; text-align:left; font-size:.92rem; }
  img { max-width:100%; border:1px solid #d0d7de; border-radius:8px; margin-top:1rem; }
  .pos { color:#1a7f37; font-weight:600; } .neg { color:#cf222e; font-weight:600; }
  .err { background:#ffebe9; border:1px solid #cf222e; padding:.8rem; border-radius:8px; }
  .muted { color:#656d76; font-size:.82rem; }
</style>
</head>
<body>
<h1>📈 Crypto Backtest Service</h1>
<p class="muted">Pick a coin, strategy and period. Data: Binance public API. Start capital $1000, fee 0.1%/trade, long-only.</p>
<form method="get" action="/backtest">
  <label>Coin</label>
  <select name="coin">
    {% for sym, name in coins %}<option value="{{sym}}" {% if sym==sel_coin %}selected{% endif %}>{{name}} ({{sym}})</option>{% endfor %}
  </select>
  <label>Strategy</label>
  <select name="strategy">
    {% for s, desc in strategies %}<option value="{{s}}" {% if s==sel_strat %}selected{% endif %}>{{desc}}</option>{% endfor %}
  </select>
  <label>Timeframe</label>
  <select name="timeframe">
    {% for tf in timeframes %}<option value="{{tf}}" {% if tf==sel_tf %}selected{% endif %}>{{tf}}</option>{% endfor %}
  </select>
  <label>Lookback (days)</label>
  <input type="number" name="days" value="{{sel_days}}" min="7" max="1460">
  <button type="submit">Run backtest</button>
</form>

{% if error %}<div class="err"><b>Error:</b> {{error}}</div>{% endif %}

{% if metrics %}
<h2>Results — {{sel_coin}} / {{sel_strat}} / {{sel_tf}}</h2>
<table>
  <tr><th>Total return</th><td class="{{'pos' if metrics.total_return>=0 else 'neg'}}">{{'%.2f'|format(metrics.total_return)}}%</td></tr>
  <tr><th>Buy &amp; hold</th><td class="{{'pos' if metrics.bh_return>=0 else 'neg'}}">{{'%.2f'|format(metrics.bh_return)}}%</td></tr>
  <tr><th>Final equity</th><td>${{'%.2f'|format(metrics.final_equity)}}</td></tr>
  <tr><th>Trades</th><td>{{metrics.num_trades}}</td></tr>
  <tr><th>Win rate</th><td>{{'%.1f'|format(metrics.win_rate)}}%</td></tr>
  <tr><th>Max drawdown</th><td class="neg">{{'%.2f'|format(metrics.max_dd)}}%</td></tr>
  <tr><th>Candles</th><td>{{metrics.candles}}</td></tr>
</table>
{% if chart %}<img src="data:image/png;base64,{{chart}}" alt="backtest chart">{% endif %}
{% endif %}

<p class="muted" style="margin-top:2rem">Not financial advice. Past performance ≠ future results.</p>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(
        FORM_HTML,
        coins=COINS, strategies=STRATEGIES, timeframes=TIMEFRAMES,
        sel_coin="DOGEUSDT", sel_strat="ema_cross", sel_tf="1d", sel_days=365,
        metrics=None, chart=None, error=None,
    )


@app.route("/backtest")
def backtest():
    coin = request.args.get("coin", "DOGEUSDT").upper()
    strategy = request.args.get("strategy", "ema_cross")
    timeframe = request.args.get("timeframe", "1d")
    try:
        days = int(request.args.get("days", 365))
    except ValueError:
        days = 365
    days = max(7, min(days, 1460))

    error = None
    metrics = None
    chart = None

    if coin not in VALID_SYMBOLS:
        error = "Invalid coin."
    elif strategy not in VALID_STRATS:
        error = "Invalid strategy."
    elif timeframe not in TF_MS:
        error = "Invalid timeframe."
    else:
        try:
            rows = fetch_klines(coin, timeframe, days)
            if len(rows) < 60:
                error = "Not enough data returned (%d candles)." % len(rows)
            else:
                m, times, closes, equity, entries, exits = run_backtest(rows, strategy)
                metrics = m
                chart = make_chart(times, closes, equity, entries, exits, coin, strategy, timeframe)
        except Exception as e:
            error = str(e)

    return render_template_string(
        FORM_HTML,
        coins=COINS, strategies=STRATEGIES, timeframes=TIMEFRAMES,
        sel_coin=coin, sel_strat=strategy, sel_tf=timeframe, sel_days=days,
        metrics=metrics, chart=chart, error=error,
    )


@app.route("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
