# Contributing

Bug reports, ideas and fixes are welcome. For anything large, open an issue first so we can agree on
the approach before you spend time on it.

## Setup

```sh
uv sync
git config core.hooksPath scripts/githooks   # scans each commit for secrets
uv run pytest
uv run ruff check src tests
```

The dashboard is in `dashboard/` (`npm install`, then `npm run build`).

## Ground rules

- Safety first: every buy has a stop-loss held at the broker, and nothing goes around the guardrails.
- Never commit keys, account numbers or real trading records.
- New behavior comes with tests. Anything that claims a strategy works needs the holdout and luck test.
- Use your own paper account, never live, while developing.

## Your contribution

By submitting a contribution, you confirm it's your own work, and you agree that it's licensed under
this project's [license](LICENSE.md) and that the project's owner may also use, relicense and sell it
as part of TaylorTrader, including in paid versions. You keep the copyright to your work.
