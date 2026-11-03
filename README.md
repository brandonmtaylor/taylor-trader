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
- **Live trading needs two switches:** `mode = "live"` in config *and* `TAYLORTRADER_ALLOW_LIVE` set.

## Quick start
```bash
uv sync
cp config.example.toml config.toml        # pick strategies here; no secrets in this file
security add-generic-password -s taylortrader -a ALPACA_PAPER_KEY_ID -w
security add-generic-password -s taylortrader -a ALPACA_PAPER_SECRET_KEY -w
git config core.hooksPath scripts/githooks  # secret scan before every commit

uv run taylortrader doctor                 # config, credentials (found/missing), secret scan
uv run taylortrader strategies             # what you can pick
uv run taylortrader backtest VRT --strategy macd_rsi --timeframe 5Min --days 365
uv run taylortrader backtest VRT --all --timeframe 1Hour --days 1000
uv run taylortrader account                # read-only: positions, stops, unmanaged positions
```

## Running it
```bash
security add-generic-password -s taylortrader -a NTFY_TOPIC -w   # a long random topic name; it's a secret
uv run taylortrader alerts-test            # push + email test
uv run taylortrader run --dry-run --once   # one tick: decides and alerts, places no orders
uv run taylortrader install-service --dry-run   # launchd agents; prints the start/stop commands
uv run taylortrader kill on                # no new positions; exits and stops keep working
uv run taylortrader journal                # recent orders, heartbeat, imported history
```
The service writes a heartbeat every tick. A separate watchdog job (every 10 minutes) pushes a
critical alert if the heartbeat goes stale, and the daily summary after the close is your "still
alive" message. Optional: set `HEALTHCHECK_URL` (healthchecks.io) to be alerted even if the whole
machine is off.

What the service does on its own:
- Recreates a missing stop on any position a strategy owns; if the stop level is already breached, exits.
- Alerts (once a day) about positions no strategy owns, and never touches them.
- Reports when a broker-side stop closes a position.
- After 5 errors in a row it stops with a critical alert, and launchd restarts it.

## Dashboard
A read-only dashboard on this computer only (it binds to 127.0.0.1 and rejects other hosts):
```bash
cd dashboard && npm install && npm run build && cd ..   # once, and after UI changes
uv run taylortrader dashboard                            # http://127.0.0.1:8787
```
It shows whether the service is alive (a heartbeat trace that flatlines when it stops), whether
you're in paper or live mode, every position with its broker-side stop and owning strategy, the
order journal with reasons, account growth vs. SPY, and the guardrail limits. Controls (kill
switch, settings) come later. For UI work: `uv run taylortrader dashboard` plus `npm run dev` in
`dashboard/` (Vite proxies `/api` to it).

## Picking strategies
Add a `[[strategies]]` block per strategy in `config.toml`: name, symbols, timeframe, allocation,
stop-loss, and optional parameter overrides. Run `taylortrader strategies` for the list and defaults.
Built-ins are ported from the original bot: `macd_rsi`, `macd`, `ma_20`, `rsi_14`, `ma_20_rsi`,
`ma_200_rsi`, `ma_20_over_200_rsi`, `chaikin`, `pullback`.

## License
Apache-2.0
