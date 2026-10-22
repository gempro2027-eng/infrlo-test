# Crypto Backtest Service

A tiny, self-contained Flask web app that backtests simple crypto trading
strategies on Binance public data. Pick a coin, strategy and lookback period
in a web form; it fetches OHLCV candles, runs the backtest and shows a metrics
table plus an equity-curve / price chart.

No API keys needed — it uses the public `data-api.binance.vision` endpoint.

## Features

- **Coins:** DOGE, BTC, ETH, SOL, XRP, BNB (USDT pairs)
- **Strategies:**
  - `ema_cross` — EMA20 > EMA50 trend, long-only
  - `rsi_bounce` — buy when RSI(14) < 30, sell when RSI > 55
  - `buy_hold` — baseline buy & hold
- **Timeframes:** 1h, 4h, 1d
- **Lookback:** last N days (default 365, max 1460)
- **Assumptions:** start capital $1000, 0.1% fee per trade, long-only,
  full position in/out on signals, signals applied on the next candle (no lookahead).
- **Metrics:** total return %, buy & hold %, number of trades, win rate,
  max drawdown %, final equity.
- **Chart:** price with buy/sell markers + equity curve (matplotlib PNG,
  embedded as base64 in the page).

## Run locally

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py              # serves on http://localhost:8000
# or production-style:
# gunicorn app:app --bind 0.0.0.0:8000
```

Open <http://localhost:8000>, fill the form, run a backtest.
The app reads `PORT` from the environment (default `8000`).

Health check: `GET /health` → `{"status":"ok"}`.

## Deploy (infrlo.com / Heroku / Railway-style PaaS)

This repo is PaaS-ready: `Procfile`, `requirements.txt`, `runtime.txt` and a
`Dockerfile` fallback are included, and the app binds `0.0.0.0:$PORT`.

1. Push this project to the GitHub repo
   [`gempro2027-eng/infrlo-test`](https://github.com/gempro2027-eng/infrlo-test).
2. In infrlo, create a new app and **connect that GitHub repo** (branch `main`).
3. infrlo detects Python (via `requirements.txt` / `Procfile`) and builds
   automatically. If it uses Docker instead, the included `Dockerfile` works too.
4. On deploy the platform injects `$PORT`; `gunicorn` binds to it via the `Procfile`.
5. Open the assigned HTTPS URL — the form is at `/`.

No secrets or Binance API keys are required or stored.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask app: form, data fetch, indicators, backtest, chart |
| `requirements.txt` | Python dependencies |
| `Procfile` | PaaS start command (gunicorn) |
| `Dockerfile` | Container fallback |
| `runtime.txt` | Python version hint |
| `.gitignore` | venv / cache / png ignores |

## Caveats

- Simplified backtest: no slippage, fills at candle close, full-position sizing.
- `data-api.binance.vision` may be geo-restricted or rate-limited from some hosts.
- Not financial advice. Past performance ≠ future results.
