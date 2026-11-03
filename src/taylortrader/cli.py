"""Command line: taylortrader <command>.

  strategies                     list strategies you can pick, with their parameters
  doctor                         check config, credentials (present/missing only) and secret hygiene
  account                        read-only account view: positions, stops, unmanaged positions
  backtest SYMBOL [options]      backtest one strategy, or --all, against buy-and-hold
  run [--dry-run] [--once]       the trading service (launchd runs this)
  watchdog                       alert if the service's heartbeat is stale (launchd, every 10 min)
  kill on|off|status             kill switch: blocks new positions; exits and stops keep working
  alerts-test                    send a test alert to every configured channel
  journal [--import-legacy DB]   recent orders; optionally import the original bot's trades
  install-service [--dry-run]    write launchd agents (prints the commands to start them)
  dashboard [--port 8787]        read-only dashboard at http://127.0.0.1:8787 (this machine only)
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
    if ok:
        from taylortrader.app import build_alerts

        for note in build_alerts(cfg)[1]:
            print(f"alerts {note}")
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


def cmd_run(args) -> int:
    from taylortrader.app import build_service

    cfg = load_config(args.config)
    setup_logging(cfg.data_dir / "logs", logging.DEBUG if args.verbose else logging.INFO)
    svc = build_service(cfg, dry_run=args.dry_run)
    if args.once:
        wait = svc.tick()
        print(f"tick OK; next tick in {wait:.0f}s")
        return 0
    svc.run_forever()
    return 0


def cmd_watchdog(args) -> int:
    from taylortrader.app import build_alerts, open_journal
    from taylortrader.service import watchdog

    cfg = load_config(args.config)
    alerts, _ = build_alerts(cfg)
    healthy = watchdog(open_journal(cfg), alerts, cfg.service.heartbeat_stale_minutes,
                       cfg.data_dir / "watchdog_alerted")
    print("healthy" if healthy else "STALE")
    return 0 if healthy else 1


def cmd_kill(args) -> int:
    from taylortrader.service import kill_file

    cfg = load_config(args.config)
    path = kill_file(cfg)
    if args.state == "on":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("kill switch on\n")
    elif args.state == "off" and path.exists():
        path.unlink()
    print(f"kill switch: {'ON (no new positions)' if path.exists() else 'off'}")
    return 0


def cmd_alerts_test(args) -> int:
    from taylortrader.alerts import Level
    from taylortrader.app import build_alerts

    cfg = load_config(args.config)
    alerts, notes = build_alerts(cfg)
    for n in notes:
        print(n)
    if not alerts.push and not alerts.email:
        print("no alert channels configured")
        return 1
    alerts.send("TaylorTrader test alert", "If you can read this, alerts work.", Level.WARNING, email_copy=True)
    print("sent")
    return 0


def cmd_journal(args) -> int:
    from taylortrader.app import open_journal

    cfg = load_config(args.config)
    j = open_journal(cfg)
    if args.import_legacy:
        n = j.import_legacy(Path(args.import_legacy).expanduser())
        print(f"imported {n} legacy trades from {args.import_legacy}")
    beat = j.last_beat()
    print(f"last heartbeat: {beat[0].isoformat() if beat else 'never'} ({beat[1] if beat else '-'})")
    for o in j.recent_orders(args.limit):
        print(f"{o['ts']} [{o['mode']}] {o['side']:4} {o['symbol']:6} {o['qty'] or '':>6} "
              f"{o['status']:9} {o['reason']}")
    legacy = j.db.execute("SELECT COUNT(*), COUNT(sell_date), ROUND(SUM(profit), 2) FROM legacy_trades").fetchone()
    if legacy[0]:
        print(f"legacy trades: {legacy[0]} ({legacy[1]} closed), realized ${legacy[2]:,}")
    return 0


def cmd_install_service(args) -> int:
    from taylortrader.launchd import SERVICE, WATCHDOG, write_plists

    project = Path(__file__).resolve().parents[2]
    for path in write_plists(project, args.dry_run):
        print(f"wrote {path}")
    print("\nStart (and on every login):")
    for label in (SERVICE, WATCHDOG):
        print(f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist")
    print("Stop:")
    for label in (SERVICE, WATCHDOG):
        print(f"  launchctl bootout gui/$(id -u)/{label}")
    return 0


def cmd_dashboard(args) -> int:
    import uvicorn

    from taylortrader.app import build_alerts, open_journal
    from taylortrader.broker import AlpacaBroker
    from taylortrader.data import MarketData
    from taylortrader.web.api import create_app

    cfg = load_config(args.config)
    setup_logging(level=logging.INFO)
    creds = load_alpaca_credentials(cfg.mode)
    app = create_app(cfg, AlpacaBroker(creds), MarketData(creds, cfg.data_dir / "cache", feed=cfg.feed),
                     open_journal(cfg), build_alerts(cfg)[1])
    print(f"TaylorTrader dashboard: http://127.0.0.1:{args.port}  (Ctrl-C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
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
    run = sub.add_parser("run")
    run.add_argument("--dry-run", action="store_true", help="decide and alert, but place no orders")
    run.add_argument("--once", action="store_true", help="one tick, then exit")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("watchdog").set_defaults(fn=cmd_watchdog)
    kill = sub.add_parser("kill")
    kill.add_argument("state", choices=["on", "off", "status"])
    kill.set_defaults(fn=cmd_kill)
    sub.add_parser("alerts-test").set_defaults(fn=cmd_alerts_test)
    jr = sub.add_parser("journal")
    jr.add_argument("--import-legacy", metavar="DB")
    jr.add_argument("--limit", type=int, default=20)
    jr.set_defaults(fn=cmd_journal)
    inst = sub.add_parser("install-service")
    inst.add_argument("--dry-run", action="store_true", help="service decides and alerts but never orders")
    inst.set_defaults(fn=cmd_install_service)
    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int, default=8787)
    dash.set_defaults(fn=cmd_dashboard)

    args = parser.parse_args(argv)
    setup_logging(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return args.fn(args)
    except (ConfigError, SecretsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
