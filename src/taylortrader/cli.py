"""Command line: taylortrader [command]. With no command it opens the dashboard.

  strategies                     list strategies you can pick, with their parameters
  doctor                         check config, credentials (present/missing only) and secret hygiene
  account                        read-only account view: positions, stops, unmanaged positions
  backtest SYMBOL [options]      backtest one strategy, or --all, against buy-and-hold
  run [--dry-run] [--once]       the trading service (launchd runs this)
  watchdog                       alert if the service's heartbeat is stale (launchd, every 10 min)
  kill on|off|status             kill switch: blocks new positions; exits and stops keep working
  alerts-test                    send a test alert to every configured channel
  journal [--import-legacy DB]   recent orders; optionally import the original bot's trades
  install-service [--dry-run]    write launchd agents (the dashboard can also start/stop the service)
  dashboard [--port 8787]        setup, settings and status at http://127.0.0.1:8787 (this machine only)
  wifi on|off|status             let your phone open the dashboard on the same Wi-Fi, with a passcode
  mcp                            MCP server over stdio, for Claude Code or the Claude desktop app
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from taylortrader.config import ConfigError, load_config
from taylortrader.logging_setup import setup_logging
from taylortrader.secrets import SecretsError, load_alpaca_credentials
from taylortrader.strategies import get_strategy, list_strategies

log = logging.getLogger("taylortrader")

DASHBOARD_PORT = 8787  # the background agent always uses this one (see launchd.py)


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
    from taylortrader.broker import AlpacaBroker
    from taylortrader.guardrails import find_unmanaged_positions

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

    if args.all:
        names = [s["name"] for s in list_strategies() if s["backtestable"]]
        skipped = [s["name"] for s in list_strategies() if not s["backtestable"]]
        if skipped:
            print(f"Skipping {', '.join(skipped)}: it can't be backtested honestly.")
    else:
        names = [args.strategy]
    try:  # before fetching anything: a mistyped name or parameter shouldn't wait on a download
        strategies = [get_strategy(name, None if args.all else _parse_params(args.param)) for name in names]
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    creds = load_alpaca_credentials("paper")  # data only; never needs live keys
    # Yahoo only keeps ~60 days of 5-minute bars, so backtests use Alpaca. SIP is the closest match:
    # Yahoo's 5-minute bars agree with SIP to the cent.
    md = MarketData(creds, cfg.data_dir / "cache", feed=args.feed or ("sip" if cfg.feed == "yahoo" else cfg.feed))
    start = datetime.now(UTC) - timedelta(days=args.days)
    df = md.bars(args.symbol.upper(), args.timeframe, start)
    if df.empty:
        print(f"error: no {args.timeframe} bars for {args.symbol.upper()} in the last {args.days} days",
              file=sys.stderr)
        return 2
    print(f"{args.symbol.upper()} {args.timeframe} {md.feed.upper()}: {len(df):,} bars "
          f"({df.index[0].date()} to {df.index[-1].date()})")

    rows = []
    for strat in strategies:
        try:
            r = backtest.run(df, strat, allocation_pct=args.allocation, stop_loss_pct=args.stop,
                             take_profit_pct=args.take_profit, slippage_bps=args.slippage,
                             cash_yield_pct=args.cash_yield)
        except ValueError as e:  # too few bars, or a strategy that can't be backtested
            print(f"error: {e}" if not args.all else f"  skipping {strat.name}: {e}", file=sys.stderr)
            continue
        rows.append(r.summary())
        if not args.all and args.trades:
            for t in r.trades:
                print(f"  {t.entry_time:%Y-%m-%d %H:%M} -> {t.exit_time:%Y-%m-%d %H:%M}  "
                      f"{t.return_pct:+6.2f}%  in: {t.entry_reason} | out: {t.exit_reason}")
    if not rows:
        return 2
    import pandas as pd

    table = pd.DataFrame(rows).set_index("strategy").drop(columns=["period", "bars"])
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(table.sort_values("return_%", ascending=False).to_string())
    return 0


def cmd_replay(args) -> int:
    """Ask the AI about past bars (disguised) and grade it like the live report card."""
    from taylortrader import llm as llm_mod
    from taylortrader import replay
    from taylortrader.data import MarketData
    from taylortrader.service import BAR

    cfg = load_config(args.config)
    sc = next((s for s in cfg.strategies if s.name == "ai"), None)
    if sc is None:
        print("error: no AI strategy in the config", file=sys.stderr)
        return 2
    params = {**sc.params, "use_news": False}
    if args.entry_when is not None:
        params["entry_when"] = args.entry_when
    if args.instructions:
        text = Path(args.instructions).read_text() if Path(args.instructions).is_file() else args.instructions
        params["instructions"] = text.strip()
    strat = get_strategy("ai", params)
    symbols = [s.upper() for s in args.symbols] or sc.symbols
    creds = load_alpaca_credentials("paper")  # data only
    md = MarketData(creds, cfg.data_dir / "cache", feed="sip")
    start = datetime.now(UTC) - timedelta(days=args.days + 45)  # + warmup for the 200-bar average
    frames = {s: md.bars(s, sc.timeframe, start) for s in symbols}
    bar = BAR[sc.timeframe]
    qs, qualified = replay.questions(frames, strat, bar=bar, gap=args.gap, max_calls=args.max_calls,
                                     seed=args.seed)
    model = f"{cfg.ai.provider}:{cfg.ai.model}:{cfg.ai.effort}"
    answers = replay.Answers(cfg.data_dir / "replay" / "answers.jsonl")
    todo = sum(q.key not in answers.saved for q in qs)
    rule = params.get("entry_when") or "every bar"
    print(f"{', '.join(symbols)} {sc.timeframe}, last {args.days} days. Rule: {rule}.")
    print(f"{qualified} bars passed the rule ({args.gap}+ bars apart); asking about {len(qs)}, "
          f"{len(qs) - todo} already answered, {todo} to ask with {model}.")
    if qs:
        print(f"Prompt size: about {len(qs[0].prompt) // 4:,} tokens each, plus instructions.")
    if args.plan or not qs:
        return 0

    client = llm_mod.build(cfg.ai.provider, cfg.ai.model, cfg.ai.endpoint, cfg.ai.effort, cfg.ai.timeout_seconds)
    errors = []

    def progress(n, total, a):
        if a.get("error"):
            errors.append(a["error"])
        mark = "!" if a.get("error") else a["action"][0].upper()
        print(f"\r  {n}/{total} asked  last: {mark} {a['symbol']} {a['ts'][:16]}  errors: {len(errors)}   ",
              end="", flush=True)

    try:
        replay.ask_all(qs, client, answers, model, workers=args.workers, on_answer=progress)
    except KeyboardInterrupt:
        print("\nStopped. Answers so far are saved; run again to continue.")
    print()
    if errors:
        print(f"{len(errors)} calls failed (not saved; a rerun retries them). Last: {errors[-1]}")

    rep = replay.grade(qs, answers, frames, float(strat.p.min_confidence), bar)
    out = cfg.data_dir / "replay" / f"report-{datetime.now(UTC):%Y%m%d-%H%M}.json"
    out.write_text(json.dumps(rep, default=str, indent=1))
    _print_replay(rep)
    print(f"\nFull report: {out}")
    return 0


def _print_replay(rep: dict) -> None:
    def p(v, nd=2):
        return "—" if v is None else f"{v:+.{nd}f}%"

    def acc(t):
        return "—" if t.get("accuracy") is None else f"{t['accuracy']:.0f}%"

    o = rep["overall"]
    print(f"\nGraded {rep['graded']} of {rep['answered']} answers over the next trading day "
          f"(moves under {rep['threshold']}% are flat).")
    print(f"  Right: {acc(o)}  ({o['right']} right, {o['wrong']} wrong, {o['flat']} flat)")
    for k, label in (("entry:buy", "Said buy"), ("entry:hold", "Said hold")):
        t = rep["by_call"].get(k)
        if t:
            print(f"  {label:<10} {t['n']:>4}x  right {acc(t):>4}  next day {p(t['avg_ret'])}")
    print(f"  Every bar the rule passed:  {rep['rule_only']['n']:>4}x  next day {p(rep['rule_only']['avg_ret'])}")
    w = rep["would_trade"]
    print(f"  Buys the bot would place (confidence >= {w['min_confidence']:.0%}): {w['n']}x  "
          f"next day {p(w['avg_ret'])}")
    conf = [b for b in rep["confidence"] if b["n"]]
    if conf:
        print("  By confidence: " + " · ".join(f"{b['label']} {acc(b)} ({b['n']})" for b in conf))
    print("  By stock: " + " · ".join(f"{s} {acc(t)} ({t['n']}, {t['buys']} buys)"
                                       for s, t in rep["by_symbol"].items()))
    if rep["graded"] < 100:
        print(f"  Only {rep['graded']} graded: differences of a few points are noise.")


def cmd_search(args) -> int:
    """Try a fixed list of strategies on older data; test the winner once on newer data it never saw."""
    from taylortrader import search
    from taylortrader.data import MarketData

    try:
        stops = tuple(float(x) for x in args.stops.split(","))
    except ValueError:
        print(f"error: --stops needs percentages separated by commas, like 8,12 (got {args.stops!r})", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    if args.universe:
        symbols = list(search.UNIVERSES[args.universe])
    else:
        symbols = [s.upper() for s in args.symbols] or sorted({s for sc in cfg.strategies for s in sc.symbols})
    creds = load_alpaca_credentials("paper")  # data only
    md = MarketData(creds, cfg.data_dir / "cache", feed=args.feed or ("sip" if cfg.feed == "yahoo" else cfg.feed))
    frames = search.load_frames(md, symbols, args.timeframe, args.days)
    if not frames:
        print("error: no symbol had enough history", file=sys.stderr)
        return 2
    grid = search.default_grid(stops)
    print(f"{', '.join(frames)} {args.timeframe}, last {args.days} days. Trying {len(grid)} candidates on the "
          f"older {100 - args.holdout:g}%, then the winner once on the newest {args.holdout:g}%.")

    def progress(k, n, label):
        print(f"\r  {k}/{n} {label[:70]:<70}", end="", flush=True)

    rep = search.run_search(frames, grid, holdout=args.holdout / 100, seeds=args.seeds,
                            log_path=cfg.data_dir / "search" / "holdout-looks.jsonl", on_progress=progress)
    print()
    tag = args.universe or "-".join(frames)[:40]
    out = cfg.data_dir / "search" / f"search-{tag}-{args.timeframe}-{datetime.now(UTC):%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, default=str, indent=1))
    _print_search(rep, args.top)
    print(f"\nFull report (every candidate, every symbol): {out}")
    return 0


def _print_search(rep: dict, top: int) -> None:
    def p(v):
        return "     —" if v is None else f"{v:+6.1f}"

    sp, hp = rep["periods"]["search"], rep["periods"]["holdout"]
    print(f"\nSearch period {sp[0]} to {sp[1]}  (vs. buy-and-hold, average across symbols; points)")
    print(f"  {'':3}{'excess':>7} {'return':>7} {'b&h':>7} {'trades':>6}  candidate")
    for k, r in enumerate(rep["results"][:top]):
        thin = " (too few trades)" if r["trades"] < rep["min_trades"] else ""
        print(f"  {k + 1:>2}.{p(r['excess'])}  {p(r['return'])}  {p(r['buy_hold'])}  {r['trades']:>6}  {r['label']}{thin}")
    if len(rep["results"]) > top:
        print(f"  ... {len(rep['results']) - top} more in the report.")
    print(f"\nTried {rep['tried']}; {rep['eligible']} made at least {rep['min_trades']} trades. The best of "
          f"{rep['tried']} looks better than it is: that's what the holdout is for.")
    w, h, luck = rep["winner"], rep["holdout"], rep["luck"]
    if w and h:
        print(f"\nHoldout {hp[0]} to {hp[1]}: {w['label']}")
        print(f"  return {h['return']:+.1f}% vs. buy-and-hold {h['buy_hold']:+.1f}%  ->  {h['excess']:+.1f} pts, "
              f"{h['trades']} trades")
        print(f"  In the market {h['exposure']:.0f}% of the time. Worst drop {h['max_dd']:.1f}% vs. holding "
              f"{h['buy_hold_max_dd']:.1f}% (average per symbol).")
        if luck and luck["runs"]:
            print(f"  Random entries ({luck['runs']} runs, same frequency, holding time and stop): median "
                  f"{luck['median']:+.1f} pts, top 10% above {luck['p90']:+.1f}. The winner beat {luck['beat_pct']:.0f}%.")
    if rep["previous_looks"]:
        print(f"\n  Note: this holdout has been looked at {rep['previous_looks']} time(s) before. Each look makes "
              "it less trustworthy; wait for new data rather than re-searching.")
    print(f"\n{rep['verdict']}")


def _single_instance(data_dir: Path):
    """Hold a lock for as long as this process runs, so two bots can't trade the same account at
    once (launchd's plus one started by hand). None if another process holds it. The lock goes
    away with the process, crash included; a restart (exec) takes it again."""
    import fcntl

    data_dir.mkdir(parents=True, exist_ok=True)
    f = (data_dir / "service.lock").open("w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    return f


def cmd_run(args) -> int:

    from taylortrader.app import build_service
    from taylortrader.service import RestartRequested

    cfg = load_config(args.config)
    setup_logging(cfg.data_dir / "logs", logging.DEBUG if args.verbose else logging.INFO)
    if not args.once:
        lock = _single_instance(cfg.data_dir)
        if lock is None:
            print("The bot is already running (in the background or another terminal). Stop that one first.",
                  file=sys.stderr)
            return 1
    svc = build_service(cfg, dry_run=args.dry_run, config_path=None if args.once else args.config)
    if args.once:
        wait = svc.tick()
        print(f"tick OK; next tick in {wait:.0f}s")
        return 0
    try:
        svc.run_forever()
    except RestartRequested as r:
        log.warning("restarting: %s", r)
        logging.shutdown()
        # Replace this process with a fresh one: new settings, new broker keys, same arguments.
        os.execv(sys.executable, [sys.executable, "-m", "taylortrader.cli", *sys.argv[1:]])
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

    project = Path.cwd()  # the home folder: main() changes into it
    if args.dry_run:
        from taylortrader.config import save_config

        cfg = load_config(args.config)
        save_config(replace(cfg, dry_run=True), args.config)
        print(f"set dry_run = true in {args.config} (switch it off from the dashboard)")
    for path in write_plists(project):
        print(f"wrote {path}")
    print("\nStart (and on every login):")
    for label in (SERVICE, WATCHDOG):
        print(f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{label}.plist")
    print("Stop:")
    for label in (SERVICE, WATCHDOG):
        print(f"  launchctl bootout gui/$(id -u)/{label}")
    return 0


def cmd_dashboard(args) -> int:
    """Works before anything is set up: the dashboard is where setup happens."""
    if args.port != DASHBOARD_PORT and (args.background or args.restart):
        print(f"error: the background dashboard always uses port {DASHBOARD_PORT}. Drop --port, or run "
              f"`taylortrader dashboard --port {args.port}` to use that port in this terminal.", file=sys.stderr)
        return 2

    import threading
    import webbrowser

    import uvicorn

    from taylortrader.web.api import create_app
    from taylortrader.web.runtime import Runtime

    if args.background or args.restart or args.foreground_again:
        from taylortrader import launchd

        project = Path.cwd()  # the home folder: main() changes into it
        if args.foreground_again:
            launchd.stop_dashboard()
            print("Stopped the background dashboard. Run `taylortrader` to start it in this terminal.")
            return 0
        if args.restart:
            if not launchd.status(launchd.DASHBOARD)["loaded"]:
                print("The dashboard isn't running in the background. Start it with `taylortrader dashboard "
                      "--background`.", file=sys.stderr)
                return 1
            launchd.restart_dashboard()
            print(f"Restarted the dashboard: http://127.0.0.1:{args.port}")
            return 0
        launchd.start_dashboard(project)
        print(f"The dashboard now runs in the background and at every login: http://127.0.0.1:{args.port}\n"
              "Restart it with `taylortrader dashboard --restart`; stop with `taylortrader dashboard --stop`.")
        return 0

    from taylortrader.secrets import DASHBOARD_PASSCODE_VAR, get_secret

    setup_logging(level=logging.INFO)
    rt = Runtime(config_path=args.config)
    url = f"http://127.0.0.1:{args.port}"
    code = get_secret(DASHBOARD_PASSCODE_VAR, rt.dotenv_path)
    passcode = code.get_secret_value() if code else None
    print(f"TaylorTrader dashboard: {url}  (Ctrl-C to stop)")
    if passcode:
        print("Wi-Fi access is on: other devices on your network can open it with the passcode "
              "(`taylortrader wifi off` to stop).")
    if not rt.configured:
        print("No settings yet: the dashboard will walk you through setup.")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Don't wait on open browser polls when stopping: a restart after an update should take a second.
    uvicorn.run(create_app(rt, passcode=passcode), host="0.0.0.0" if passcode else "127.0.0.1",
                port=args.port, log_level="warning", log_config=None,  # our handlers: they redact
                proxy_headers=False,  # a forwarded-for header must never make a request look local
                timeout_graceful_shutdown=2)
    return 0


def cmd_wifi(args) -> int:
    """Wi-Fi access for the dashboard: on (with a passcode), off, or status."""
    from taylortrader import launchd
    from taylortrader.secrets import DASHBOARD_PASSCODE_VAR, delete_secret, get_secret, set_secret
    from taylortrader.web import remote

    current = get_secret(DASHBOARD_PASSCODE_VAR, redact=False)
    background = launchd.status(launchd.DASHBOARD)["loaded"]
    if args.action == "status":
        print(f"Wi-Fi access is {'on' if current else 'off'}.")
        return 0
    if args.action == "off":
        delete_secret(DASHBOARD_PASSCODE_VAR)
        if background:
            launchd.restart_dashboard()
        print("Wi-Fi access is off: the dashboard only opens on this Mac again, and every device is signed out."
              + ("" if background else " Restart the dashboard if it's running."))
        return 0
    if current and not args.new:
        code = current.get_secret_value()
    else:
        code = remote.new_passcode()
        set_secret(DASHBOARD_PASSCODE_VAR, code)
    if background:
        launchd.restart_dashboard()
    name, ip = remote.mac_name(), remote.lan_address()
    print("Wi-Fi access is on. On your phone, on the same Wi-Fi, open:")
    if name:
        print(f"  http://{name}.local:{args.port}")
    if ip:
        print(f"  http://{ip}:{args.port}   (if the name doesn't work; this address can change)")
    print(f"\nPasscode: {code}\n")
    print("Each device signs in once and stays signed in for 30 days. Going live and API keys can only be\n"
          "changed on this Mac. `taylortrader wifi on --new` makes a new passcode and signs every device out;\n"
          "`taylortrader wifi off` closes it again. If macOS asks whether to accept incoming connections, allow it.")
    if not background:
        print("Restart the dashboard (or `taylortrader dashboard --background`) for this to take effect.")
    return 0


def cmd_mcp(args) -> int:
    from taylortrader.mcp_server import main as serve

    serve(args.config)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="taylortrader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="settings file (default: config.toml in the home folder)")
    parser.add_argument("-v", "--verbose", action="store_true")
    from taylortrader import __version__
    parser.add_argument("--version", action="version", version=f"taylortrader {__version__}")
    sub = parser.add_subparsers(dest="cmd")
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
    bt.add_argument("--cash-yield", type=float, default=4.0, help="%% a year earned on uninvested cash")
    bt.add_argument("--param", action="append", help="strategy param override, e.g. rsi_buy=30")
    bt.add_argument("--trades", action="store_true", help="list every trade")
    bt.set_defaults(fn=cmd_backtest)
    se = sub.add_parser("search", help="try a fixed list of strategies; test the winner on data it never saw")
    se.add_argument("symbols", nargs="*", help="default: every symbol in your strategies")
    se.add_argument("--universe", choices=["dow2020", "sectors"], help="a fixed list chosen without hindsight")
    se.add_argument("--timeframe", default="1Hour")
    se.add_argument("--days", type=int, default=730)
    se.add_argument("--holdout", type=float, default=30.0, help="%% of the newest data kept for the final test")
    se.add_argument("--stops", default="8,12", help="stop-loss %%s to try, comma-separated")
    se.add_argument("--seeds", type=int, default=100, help="random-entry runs for the luck test")
    se.add_argument("--feed", choices=["iex", "sip"])
    se.add_argument("--top", type=int, default=12, help="rows to print")
    se.set_defaults(fn=cmd_search)
    rp = sub.add_parser("replay", help="ask the AI about past bars (disguised) and grade it")
    rp.add_argument("symbols", nargs="*", help="default: the AI strategy's symbols")
    rp.add_argument("--days", type=int, default=90)
    rp.add_argument("--max-calls", type=int, default=150)
    rp.add_argument("--entry-when", help="try a different buy rule than the config's (config unchanged); it's "
                    "checked on disguised prices, so avoid absolute levels like close > 50")
    rp.add_argument("--instructions", help="try different instructions (text, or a file); config unchanged")
    rp.add_argument("--gap", type=int, default=7, help="min bars between questions on one stock")
    rp.add_argument("--workers", type=int, default=2)
    rp.add_argument("--seed", type=int, default=7)
    rp.add_argument("--plan", action="store_true", help="show what would be asked; ask nothing")
    rp.set_defaults(fn=cmd_replay)
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
    dash = sub.add_parser("dashboard", help="open the dashboard (the default with no command)")
    dash.add_argument("--port", type=int, default=DASHBOARD_PORT, help="in this terminal only, not with --background")
    dash.add_argument("--no-open", action="store_true", help="don't open a browser tab")
    dash.add_argument("--background", action="store_true", help="run it as a background service (macOS)")
    dash.add_argument("--restart", action="store_true", help="restart the background dashboard (after an update)")
    dash.add_argument("--stop", dest="foreground_again", action="store_true", help="stop the background dashboard")
    dash.set_defaults(fn=cmd_dashboard)
    wifi = sub.add_parser("wifi", help="let other devices on your Wi-Fi open the dashboard, with a passcode")
    wifi.add_argument("action", choices=["on", "off", "status"])
    wifi.add_argument("--new", action="store_true", help="make a new passcode (signs every device out)")
    wifi.add_argument("--port", type=int, default=8787)
    wifi.set_defaults(fn=cmd_wifi)
    sub.add_parser("mcp", help="MCP server over stdio (started by Claude, not by you)").set_defaults(fn=cmd_mcp)

    argv = sys.argv[1:] if argv is None else argv
    if not any(a in sub.choices for a in argv) and not any(a in ("-h", "--help") for a in argv):
        argv = [*argv, "dashboard"]  # `taylortrader` alone opens the dashboard
    args = parser.parse_args(argv)
    if args.config is None:
        # Settings, keys (.env) and data (var/) all live in one home folder, wherever this runs from.
        from taylortrader.home import find_home

        home = find_home()
        home.mkdir(parents=True, exist_ok=True)
        os.chdir(home)
        args.config = Path("config.toml")
    setup_logging(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return args.fn(args)
    except (ConfigError, SecretsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
