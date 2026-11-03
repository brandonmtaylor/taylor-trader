"""Command line: taylortrader <command>.

  strategies                     list strategies you can pick, with their parameters
  doctor                         check config, credentials (present/missing only) and secret hygiene
  account                        read-only account view: positions, stops, unmanaged positions
  backtest SYMBOL [options]      backtest one strategy, or --all, against buy-and-hold
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taylortrader.config import ConfigError, load_config
from taylortrader.logging_setup import setup_logging
from taylortrader.secrets import SecretsError, load_alpaca_credentials
from taylortrader.strategies import get_strategy, list_strategies

log = logging.getLogger("taylortrader")


def _parse_params(items: list[str]) -> dict:
    params = {}
    for item in items or []:
        key, _, raw = item.partition("=")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


def cmd_strategies(args) -> int:
    for s in list_strategies():
        print(f"\n{s['name']}\n  {s['description']}")
        if s["params"]:
            print("  params: " + ", ".join(f"{k}={v}" for k, v in s["params"].items()))
    return 0


def cmd_doctor(args) -> int:
    from taylortrader import secretscan

    ok = True
    try:
        cfg = load_config(args.config)
        print(f"config: OK ({args.config}), mode={cfg.mode}, feed={cfg.feed}")
        for sc in cfg.strategies:
            state = "on " if sc.enabled else "off"
            print(f"  [{state}] {sc.name} {sc.symbols} {sc.timeframe} alloc={sc.allocation_pct}% stop={sc.stop_loss_pct}%")
        print(f"guardrails: {cfg.limits}")
        mode = cfg.mode
    except ConfigError as e:
        print(f"config: PROBLEM: {e}")
        ok, mode = False, "paper"
    try:
        load_alpaca_credentials(mode)
        print(f"credentials ({mode}): found")
    except SecretsError as e:
        print(f"credentials ({mode}): {e}")
        ok = False
    if secretscan.main([]) != 0:
        ok = False
    return 0 if ok else 1


def cmd_account(args) -> int:
    from guardrails import find_unmanaged_positions
    from taylortrader.broker import AlpacaBroker

    cfg = load_config(args.config)
    broker = AlpacaBroker(load_alpaca_credentials(cfg.mode))
    snap = broker.snapshot()
    clock = broker.clock()
    print(f"[{broker.mode.upper()}] equity ${snap.equity:,.2f}  cash ${snap.cash:,.2f}  "
          f"today {snap.day_pnl_pct:+.2f}%  trades today {snap.trades_today}  market {'open' if clock.is_open else 'closed'}")
    stops = broker.symbols_with_stop()
    for p in snap.positions.values():
        pct = p.market_value / snap.equity * 100 if snap.equity else 0
        print(f"  {p.symbol:8} qty {p.qty:g}  ${p.market_value:,.2f} ({pct:.1f}%)  "
              f"avg ${p.avg_entry_price:,.2f}  stop {'yes' if p.symbol in stops else 'NO'}")
    managed = {s for sc in cfg.enabled_strategies for s in sc.symbols}
    for symbol, issues in find_unmanaged_positions(snap.positions, stops, managed).items():
        print(f"  WARNING {symbol}: " + "; ".join(issues))
    return 0


def cmd_backtest(args) -> int:
    from taylortrader import backtest
    from taylortrader.data import MarketData

    cfg = load_config(args.config)
    creds = load_alpaca_credentials("paper")  # data only; never needs live keys
    md = MarketData(creds, cfg.data_dir / "cache", feed=args.feed or cfg.feed)
    start = datetime.now(UTC) - timedelta(days=args.days)
    df = md.bars(args.symbol.upper(), args.timeframe, start)
    print(f"{args.symbol.upper()} {args.timeframe} {md.feed.upper()}: {len(df):,} bars "
          f"({df.index[0].date()} to {df.index[-1].date()})")

    names = [s["name"] for s in list_strategies()] if args.all else [args.strategy]
    rows = []
    for name in names:
        strat = get_strategy(name, _parse_params(args.param) if not args.all else None)
        r = backtest.run(df, strat, allocation_pct=args.allocation, stop_loss_pct=args.stop,
                         take_profit_pct=args.take_profit, slippage_bps=args.slippage)
        rows.append(r.summary())
        if not args.all and args.trades:
            for t in r.trades:
                print(f"  {t.entry_time:%Y-%m-%d %H:%M} -> {t.exit_time:%Y-%m-%d %H:%M}  "
                      f"{t.return_pct:+6.2f}%  in: {t.entry_reason} | out: {t.exit_reason}")
    import pandas as pd

    table = pd.DataFrame(rows).set_index("strategy").drop(columns=["period", "bars"])
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(table.sort_values("return_%", ascending=False).to_string())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taylortrader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.toml", type=Path)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("strategies").set_defaults(fn=cmd_strategies)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("account").set_defaults(fn=cmd_account)
    bt = sub.add_parser("backtest")
    bt.add_argument("symbol")
    bt.add_argument("--strategy", default="macd_rsi")
    bt.add_argument("--all", action="store_true", help="run every strategy with default params")
    bt.add_argument("--timeframe", default="5Min")
    bt.add_argument("--days", type=int, default=365)
    bt.add_argument("--feed", choices=["iex", "sip"])
    bt.add_argument("--allocation", type=float, default=100.0, help="%% of equity per trade")
    bt.add_argument("--stop", type=float, default=12.0, help="stop-loss %%")
    bt.add_argument("--take-profit", type=float, default=0.0)
    bt.add_argument("--slippage", type=float, default=5.0, help="basis points per fill")
    bt.add_argument("--param", action="append", help="strategy param override, e.g. rsi_buy=30")
    bt.add_argument("--trades", action="store_true", help="list every trade")
    bt.set_defaults(fn=cmd_backtest)

    args = parser.parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return args.fn(args)
    except (ConfigError, SecretsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
