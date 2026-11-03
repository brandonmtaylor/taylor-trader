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

## Picking strategies
Add a `[[strategies]]` block per strategy in `config.toml`: name, symbols, timeframe, allocation,
stop-loss, and optional parameter overrides. Run `taylortrader strategies` for the list and defaults.
Built-ins are ported from the original bot: `macd_rsi`, `macd`, `ma_20`, `rsi_14`, `ma_20_rsi`,
`ma_200_rsi`, `ma_20_over_200_rsi`, `chaikin`, `pullback`.

## License
Apache-2.0
