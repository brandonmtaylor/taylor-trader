# TaylorTrader: Plan

A source-available, autonomous stock-trading bot for Alpaca, built to be safe first.

## Why

I wrote the original bot myself starting in February 2022, and it traded my own Alpaca account from
October 2023. I built it before AI coding tools and before Alpaca's modern SDK, and it first ran on a
Windows machine. It worked, but it was hacky. It died silently in July 2026 and nobody noticed for
four months (81 failed runs from July 7 to November 2).

TaylorTrader is the rebuild. It keeps the features that made the original fun, makes trading safer,
uses AI to help build and operate it, and its source is available so others can learn from it or run it.

**Not financial advice.** TaylorTrader is software you run on your own account with your own keys.
It trades on paper unless you explicitly turn live trading on.

## Decisions

| Area | Decision |
|---|---|
| Language | Python 3.13 engine; TypeScript only for the dashboard UI |
| Broker | Alpaca via `alpaca-py` (pinned), behind a broker adapter |
| Market data | Live: Yahoo Finance (all exchanges, current; unofficial) with automatic fallback to Alpaca IEX. Backtests: Alpaca SIP history. Cached locally |
| Timeframe | Compare 5-minute vs hourly/daily in backtests before choosing |
| Autonomy | Fully autonomous, bounded by the guardrails engine |
| Runs on | Mac mini, launchd service (no cron + sleep) |
| Dashboard | Local FastAPI + Vite/React/TS, served on localhost only |
| Alerts | ntfy for live alerts, email for the daily summary |
| Secrets | Env vars / macOS Keychain; nothing secret in the repo |
| License | FSL-1.1-ALv2 (free to use, change and learn from; no competing commercial copies; each version becomes Apache-2.0 after two years). Chosen 2026-11-06 over Apache (anyone could sell a clone) and AGPL (protects hosting, which regulation makes impractical anyway). Decide any change before outside contributions arrive |
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
- Go live with what has evidence. First the invest plan (see "What the tests found" below). A trading
  strategy goes live when it passes `taylortrader search` on a list chosen without hindsight (beats
  buy-and-hold on the holdout and beats random entries), or deliberately as a small, labeled
  experiment. Strategies that haven't passed stay on paper; they aren't removed
- Position sizing: paper moved to 25% of the account with an 8% stop on 2026-11-06, about 2% at risk
  per trade under the 3% daily loss limit. Before that, 50% with a 12% stop lost ~6% per stop, double
  the limit, so every stop blocked buying for the day (first seen 2026-11-05: VRT stopped out, two
  rebuys blocked)
- ✅ Daily loss limit ignores deposits and withdrawals (Alpaca account activities since the last close)

### What the tests found (2026-11-05/06)
The honest tools (replay, search with a holdout, the luck test) were pointed at the strategies
before any real money. Recorded here so the reasoning survives; numbers are in `var/search/`.
- **AI strategy** (Claude Sonnet via Claude Code, price data only, disguised): about 500 replayed
  decisions over 180 days. As configured it never bought. With looser wording it bought 18 times, all
  under the confidence needed to trade, and those picks did worse than the cheap rule alone.
  Prompt tuning stopped there (with ~100 test moments, rewording finds luck). Not tested: with news.
- **Strategy search**: 28 candidates (the built-ins, five rule strategies, 8% and 12% stops), each
  searched on the older 70% and the winner tested once on the newest 30%:
  - The bot's own five stocks (hourly 2 years, daily 6 years): nothing beat holding. These had huge
    runs (PLTR, NVDA, VRT), and picking them with hindsight makes holding hard to beat.
  - Fixed lists, no hindsight (the Dow 30 of 2020, the 9 sector funds), daily, 7 years: nothing beat
    holding on the holdout. MACD with a 12% stop came closest: on the Dow its timing beat 98% of
    random entries (rerun 2026-11-06 after fixing the luck test so random runs match the winner's time
    in the market; a second look at that holdout, so weigh it accordingly; in the sector funds it beat
    only 67%, no better than chance), it was in the market ~39% of the time, and its worst drop was a third smaller
    (-20% vs. -31%). With 4% a year on idle cash it trailed holding by ~10 points on the holdout, and
    beat it in the sector funds' search period (+92% vs. +74%) but not their holdout.
- **So**: for growth, a held mix is the evidence-backed default, which is what the invest plan does.
  Trading strategies aren't disproven: these were a few lists and one stretch of years, mostly a rising
  market. MACD looks like a risk reducer (smaller drops, less growth). Strategies stay first-class:
  easy to change, a no-code `rule` strategy for writing new ones, and the search to test them,
  because the point is that anyone can try ideas and the tools will tell them honestly.
- The holdouts above have each been looked at (logged in `var/search/holdout-looks.jsonl`). Retesting
  new ideas should use new lists or new months, not the same holdout again.
- Private work (e.g. an ML filter on top of a rule) can plug in as its own package that registers a
  strategy; the bar it has to clear is the same: the holdout and the luck test.

### Long-term investing (built 2026-11-05: phases 1-2)
For people who want a diversified portfolio built up over time, not trading: the same safety and
dashboard, with no strategies, signals or stop-losses involved.
- An **invest plan**: target weights (e.g. a few broad ETFs), and how to get there: invest $X every
  week/month until a total or end date (dollar-cost averaging), or all at once
- Fractional shares (Alpaca notional orders), so small amounts still match the weights
- **Rebalancing**: buy what's under its weight with new money first; sell only when something drifts
  past a band (e.g. 5 points), checked monthly or quarterly. Fewer sells, fewer taxable events
- Example portfolios to start from (labeled examples, not advice); the user picks and edits weights
- Guardrails need an investing profile: no stop-loss on plan holdings (a stop fights buy-and-hold),
  scheduled buys not blocked by the daily loss limit (buying dips is the point), kill switch and
  max-order-size still apply, and the unmanaged-position alert knows plan holdings are managed
- Can share an account with trading strategies: the plan gets its own slice, and strategy allocation
  is a share of the trading slice only
- Dashboard: progress toward the target, current vs. target weights, contributions vs. growth, and
  next scheduled buy. The bot can't pull money from a bank; it invests cash already in the account
- Broader audience for the open-source release; the blog can show both modes

### M4: MCP server and open-source release
- ✅ Local MCP server (`taylortrader mcp`, stdio): status, account, results, orders, log, AI decisions,
  strategies, backtests; change strategies and limits; pause; dry run/paper; sell with exact
  confirmation. Never: go live, keys, or loosen limits on live. Same validation as the dashboard,
  journaled as from Claude
- Public repo, README, demo, docs; blog series links to it
- Before going public: check the product name (domain, GitHub, a basic trademark search)

## Later

If the free version gets real users, a packaged Mac app and a phone app may follow, for people who'd
rather not use Terminal. The engine, guardrails and dashboard stay free and source-available.
