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

For UI work: `uv run taylortrader dashboard --no-open` plus `TAYLORTRADER_DEV=1` and `npm run dev`
in `dashboard/` (Vite proxies `/api` to it).

## License
Apache-2.0
