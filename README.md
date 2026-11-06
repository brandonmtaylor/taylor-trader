# TaylorTrader

An open-source autonomous trading bot for [Alpaca](https://alpaca.markets), built safety-first.

> **Not financial advice.** TaylorTrader is software you run on your own account, with your own keys.
> It trades on **paper** unless you explicitly turn live trading on. Most trading strategies lose to
> buy-and-hold; the backtester always shows you that comparison.

**Status:** early. Milestone 1 (a safe bot on paper) is in progress. See [docs/PLAN.md](docs/PLAN.md).

## What makes it safer
- **Stops live at the broker.** Every entry is an OTO/bracket order with a GTC stop-loss held by Alpaca,
  so the stop still fires if TaylorTrader crashes.
- **Guardrails** (`src/guardrails`, no broker dependency) check every order: position size, total
  exposure, daily loss, trades per day, stop width, kill switch. Config can tighten the built-in hard
  limits but never loosen them. Exits are never blocked.
- **Secrets stay secret.** Keys come from the environment, a git-ignored `.env`, or the macOS Keychain;
  they're masked in logs and tracebacks, config files that contain secret-looking keys are rejected,
  and a pre-commit hook scans for credentials.
- **Live trading needs two things:** live keys that open an active account, and typing `GO LIVE`
  in the dashboard (which sets `TAYLORTRADER_ALLOW_LIVE`; switching back to paper removes it).

## Quick start
```bash
uv sync
cd dashboard && npm install && npm run build && cd ..   # once, and after UI changes
uv run taylortrader                                     # opens the dashboard and walks you through setup
```
Setup takes about ten minutes: connect your Alpaca **paper** keys (tested, then saved to the macOS
Keychain, or a mode-600 `.env` elsewhere), pick a strategy and backtest it, check the safety
limits, optionally add phone/email alerts, and start the background service in **dry run**.
Then switch Dry run / Paper / Live from the bar at the top of the page.

The dashboard listens on 127.0.0.1 only. Every change it makes needs a custom header and a
same-origin request, so another website open in your browser can't change your settings.
Keys go in and are never shown again: the page only says where each one is stored.

## Command line
Everything the dashboard does also works from a terminal:
```bash
git config core.hooksPath scripts/githooks  # secret scan before every commit
uv run taylortrader doctor                 # settings, credentials (found/missing), secret scan
uv run taylortrader strategies             # what you can pick
uv run taylortrader backtest VRT --strategy macd_rsi --timeframe 5Min --days 365
uv run taylortrader backtest VRT --all --timeframe 1Hour --days 1000
uv run taylortrader account                # read-only: positions, stops, unmanaged positions
uv run taylortrader alerts-test            # push + email test
uv run taylortrader run --dry-run --once   # one check: decides and alerts, places no orders
uv run taylortrader install-service        # launchd agents (the dashboard can start/stop them)
uv run taylortrader kill on                # pause new buys; sells and stops keep working
uv run taylortrader journal                # recent orders, heartbeat, imported history
```
Settings live in `config.toml` (no secrets; `config.example.toml` explains each one). The dashboard
writes it after validating, keeping the previous version as `config.toml.bak`; editing by hand
is fine too. The service notices a saved change on its next check and restarts itself with it. A
file with a mistake in it is reported and ignored, so the service keeps running on the old settings.

## What the service does on its own
- Writes a heartbeat every check. A watchdog job (every 10 minutes) alerts if it goes stale, and
  the daily summary after the close is your "still alive" message. Optional `HEALTHCHECK_URL`
  (healthchecks.io) alerts you even if the whole machine is off.
- Recreates a missing stop on any position a strategy owns; if the stop level is already breached, exits.
- Alerts (once a day) about positions no strategy owns, and never touches them.
- Reports when a broker-side stop closes a position.
- Keeps checking other symbols when one fails. After 5 failed checks in a row it stops with a
  critical alert, and launchd restarts it.

## Price data
`feed = "yahoo"` (default): every exchange, current, free, unofficial; the service switches to
Alpaca IEX on its own if Yahoo fails or falls behind. `"iex"`: Alpaca, real time, one exchange.
`"sip"`: Alpaca, every exchange, 15 minutes delayed on the free plan. Backtests always use SIP.

## Strategies
Built-ins are ported from the original bot: `macd_rsi`, `macd`, `ma_20`, `rsi_14`, `ma_20_rsi`,
`ma_200_rsi`, `ma_20_over_200_rsi`, `chaikin`, `pullback`. Run as many as you like; each symbol
belongs to one strategy so it's always clear who manages a position.

## AI strategy (experimental)
`ai` asks a language model whether to buy, sell or hold. It works with:

- **Claude subscription**: if [Claude Code](https://claude.com/claude-code) is installed and
  logged in to a Pro or Max plan, each question is a one-shot `claude -p` (no tools, no MCP,
  none of your own settings, run in an empty folder). No API key; calls count toward your plan.
- **Claude API**: the Anthropic SDK with structured outputs, so answers always match the
  decision format. Billed per call; the sturdier choice for running unattended or with real money.
- **Any OpenAI-compatible API**: OpenAI, OpenRouter, Groq, or a free local model through Ollama
  or LM Studio.

Set it up under Settings → AI model, then add an `ai` strategy with plain-English instructions.

- Each question includes your instructions, recent bars, indicators, and (optionally) the last
  3 days of Alpaca news, which the model is told to treat as information, never instructions.
- An optional rule (`rsi < 40 and close > ma_200`) is checked for free before each call. Rules
  are parsed, never evaluated as code.
- It only proposes. Position size, the stop-loss, and every guardrail apply as usual. An error,
  an off-menu answer, or confidence under your threshold means hold.
- A daily call limit caps cost; every decision, with pros, cons and confidence, is shown on the
  overview.
- It can't be backtested honestly (the model has read what happened next), so the backtester
  refuses it. Try it in dry run, then on paper.
- The **AI report card** on the overview grades every call against what the price did over the
  next trading day (free: no extra model calls): how often it was right, whether its buys beat
  buying every time your rule passed, and whether higher confidence really is right more often.
- To get a read in a day instead of weeks, `uv run taylortrader replay` asks the model about past
  bars and grades the answers the same way. Each stock is disguised first (no ticker, prices
  scaled, dates moved years ahead, no news) so the model can't recall what happened next. It uses
  your AI settings and your plan or API credit: `--plan` shows how many calls it would make
  first. Answers are saved, so stopping and rerunning picks up where it left off.

For UI work: `uv run taylortrader dashboard --no-open` plus `TAYLORTRADER_DEV=1` and `npm run dev`
in `dashboard/` (Vite proxies `/api` to it).

## Long-term investing

Not everyone wants a trading bot. An **invest plan** puts a set amount into a fixed mix of funds
on a schedule (say $200 a month into US stocks, international stocks and bonds) and holds it.

- Each contribution goes to whatever is furthest under its target weight first, so new money keeps
  the mix balanced without selling. Fractional shares, so any amount splits exactly.
- No stop-losses and no daily loss limit for plan buys (holding through dips is the point). The
  kill switch, plan-only symbols, no margin and a size cap still apply.
- A month is never bought twice, even across restarts or crashes: each buy is journaled first and
  carries an order id Alpaca won't accept twice.
- It invests cash already in the account (the bot can't move money from your bank). Short on cash,
  it invests what's there and tells you.
- Start with `preview = true`: it runs on schedule and alerts what it would buy, placing nothing.
- Trading strategies size from what's left after the plan, so a strategy's "50%" never spends
  plan money.

Set it up on the dashboard's **Invest** page: start from a ready-made mix, edit the targets, pick the
amount and schedule, and see each holding's share against its target and the next buy's split.
A **"what if you'd started 5 or 10 years ago"** table runs your amount and schedule on real past
prices for your mix and each ready-made one (put in, worth now, the worst drop along the way, and the
same money all at once). Turning on real buying asks you to type the amount and schedule. (Or edit `[invest]` in
`config.toml`; see `config.example.toml`.) From Claude, `get_invest_plan` reports on it and
`set_invest_plan_state` can turn it off or back to preview, never on. Not financial advice; the
ready-made mixes are examples. The design and what's next (dashboard page, rebalancing by
selling) are in [docs/proposals/investing.md](docs/proposals/investing.md).

## Talk to it from Claude (MCP)
TaylorTrader includes an MCP server, so Claude Code or the Claude desktop app can check on the bot
and adjust it: "how did the AI strategy do this week?", "move PLTR to macd_rsi", "backtest rsi_14
on NVDA hourly", "pause new buys".

```sh
claude mcp add taylortrader --scope user -- uv --directory /path/to/taylor-trader run taylortrader mcp
```

It can also read the AI report card ("how often is the AI right?") and how well each trade was
timed ("did we sell VRT too early?").

It runs on your Mac and uses the same rules as the dashboard: every change is validated, hard
limits still apply, and changes are logged as coming from Claude. Selling needs the exact
confirmation (the symbol, or `SELL ALL`). It can't switch to live trading or read or change keys;
those stay in the dashboard.

## License
Apache-2.0
