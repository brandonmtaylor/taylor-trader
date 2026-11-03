# TaylorTrader: Plan

An open-source, autonomous stock-trading bot for Alpaca, built to be safe first.

## Why

I wrote the original bot myself starting in February 2022, and it traded my own Alpaca account from
October 2023. I built it before AI coding tools and before Alpaca's modern SDK, and it first ran on a
Windows machine. It worked, but it was hacky. It died silently in July 2026 and nobody noticed for
four months (81 failed runs from July 7 to November 2).

TaylorTrader is the rebuild. It keeps the features that made the original fun, makes trading safer,
uses AI to help build and operate it, and is open source so others can learn from it or run it.

**Not financial advice.** TaylorTrader is software you run on your own account with your own keys.
It trades on paper unless you explicitly turn live trading on.

## Decisions

| Area | Decision |
|---|---|
| Language | Python 3.13 engine; TypeScript only for the dashboard UI |
| Broker | Alpaca via `alpaca-py` (pinned), behind a broker adapter |
| Market data | Alpaca free plan: full SIP history, IEX real-time; cached locally |
| Timeframe | Compare 5-minute vs hourly/daily in backtests before choosing |
| Autonomy | Fully autonomous, bounded by the guardrails engine |
| Runs on | Mac mini, launchd service (no cron + sleep) |
| Dashboard | Local FastAPI + Vite/React/TS, served on localhost only |
| Alerts | ntfy for live alerts, email for the daily summary |
| Secrets | Env vars / macOS Keychain; nothing secret in the repo |
| License | Apache-2.0 |
| Audience | Built for me, open-source-ready from day one |

## Features carried over from the original bot

| Original | Keep as |
|---|---|
| 12 strategies (MA, MACD, RSI, Chaikin, pullback, MACD_RSI with LightGBM filter, LSTM) | A strategy library behind one interface. Port MACD_RSI first, then the rest |
| Leader scan ("Beta Override"): backtest 27 stocks x 7 strategies over the last 5 days and switch to the winner after selling | **Rotation**: a scanner that ranks candidates. Proven with walk-forward tests first, since picking last week's winner is easy to overfit |
| Daily leaderboard email ranked against the S&P 500 | Daily report in the dashboard plus an email summary |
| Several accounts side by side (live + paper "beta" experiment) | Paper vs live as first-class modes; experiments run on paper in parallel |
| Trade journal in SQLite (hold time, % change, strategy, order IDs) | Journal with the reason for every trade; import the old history |
| Walk-forward backtests, ML filter | Backtest harness that always compares against buy-and-hold |
| Crypto (BTC, ETH) | Later; stocks first |

## What the original got wrong, and the fix

| Failure | Fix |
|---|---|
| Stop-loss lived in the bot, so a dead bot never sold | Every entry is a bracket order; the stop lives at Alpaca |
| Died silently for 4 months; bare `except:` everywhere | Heartbeat, crash alerts, no swallowed errors |
| Alpaca removed `daytrade_count` (PDT rule retired) and it crashed | Typed `alpaca-py` models; fail loudly on schema changes |
| Account ended up ~97% in one stock | Guardrails: max position size, daily loss cap, max trades/day, kill switch |
| A position stayed open with nothing managing it | Unmanaged-position alert |
| Keys and phone number in a tracked ini file | Env vars / Keychain, `.env.example` only |
| yfinance (unofficial, 60-day cap on 5-minute data) | Alpaca market data, cached |
| Cron + sleep-until-open, 2 GB of cron mail | Long-running launchd service with structured logs |

## Architecture

```
            ┌── Dashboard (localhost)
Engine ── API ──┼── MCP server (Claude)
            └── Alerts (ntfy, email)

Engine = strategies + scanner + guardrails + broker adapter + journal + data cache
```

`guardrails` is its own package with a clean interface, so it can stand alone later.

## Milestones

### M1: Safe bot on paper
- Broker adapter (paper by default), bracket orders on every entry
- Guardrails engine with hard minimums that config can tighten but not loosen
- launchd service, trade-update stream, heartbeat, ntfy and email alerts
- Data layer on Alpaca data with a local cache
- Port MACD_RSI behind the strategy interface; confirm it matches the old bot's decisions
- Small backtest harness: 5-minute vs hourly, always against buy-and-hold
- Journal (import old trades); config and secrets done properly
- Read-only dashboard: status, positions with their stops, journal, equity vs buy-and-hold

**Done when** it has run 2 to 4 weeks on paper with no unexplained gaps, every position has a broker-side
stop, alerts reach my phone, and paper results roughly match the backtest for the same period.

### M2: Dashboard controls and rotation
- Settings, paper/live switch (typed confirmation), kill switch, backtest runner
- Port the remaining strategies; rotation scanner tested walk-forward

### M3: Live, small
- Small amount of money, tight limits; raise them only as results earn it

### M4: MCP server and open-source release
- Tools for status, positions, journal, backtests, pause/kill switch
- Public repo, README, demo, docs; blog series links to it
