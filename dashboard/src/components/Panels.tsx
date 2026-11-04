import type { Account, AIDecision, EventRow, History, OrderRow, Performance, PositionRow, Results, Status } from "../api";
import { money, money0, pct, qty, shortTime } from "../format";

const PERIOD_WORDS: Record<Performance["period"], string> = { "1M": "past month", "3M": "past 3 months", "1A": "past year" };

/** Largest fall from a running peak, in percent (negative), or null without enough points. */
function worstDrop(perf: Performance | null): number | null {
  const pts = perf?.equity ?? [];
  if (pts.length < 2) return null;
  let peak = pts[0].v;
  let worst = 0;
  for (const p of pts) {
    peak = Math.max(peak, p.v);
    worst = Math.min(worst, (p.v / peak - 1) * 100);
  }
  return worst;
}

const signedMoney = (n: number) => `${n > 0 ? "+" : n < 0 ? "−" : ""}${money(Math.abs(n))}`;

export function ResultsPanel({ results, perf, error }: { results: Results | null; perf: Performance | null; error: string | null }) {
  const r = results?.bot;
  const drop = worstDrop(perf);
  const other = results?.other;
  return (
    <section className="panel" aria-labelledby="results-title">
      <div className="panel__head">
        <div>
          <h2 id="results-title">Results</h2>
          <p className="panel__sub">Closed trades the bot opened in this account, from Alpaca's fills (stop-losses included).</p>
        </div>
      </div>
      {error && <p className="empty">Can't load results: {error}</p>}
      {r && r.trades === 0 && (
        <p className="empty">No closed trades yet. Results show up here once the bot has bought and sold something.</p>
      )}
      {r && r.trades > 0 && (
        <>
          <dl className="stats">
            <div>
              <dt>Win rate</dt>
              <dd>{pct(r.win_rate, false, 0)}</dd>
              <dd className="stats__sub">{r.wins} of {r.trades} trade{r.trades === 1 ? "" : "s"}</dd>
            </div>
            <div>
              <dt>Average win / loss</dt>
              <dd>
                <span className="is-up">{r.avg_win == null ? "—" : signedMoney(r.avg_win)}</span>
                <span className="stats__slash"> / </span>
                <span className="is-down">{r.avg_loss == null ? "—" : signedMoney(r.avg_loss)}</span>
              </dd>
              <dd className="stats__sub">{pct(r.avg_win_pct, true, 1)} / {pct(r.avg_loss_pct, true, 1)} per trade</dd>
            </div>
            <div>
              <dt>Won per $1 lost</dt>
              <dd>{r.profit_factor == null ? "—" : `$${r.profit_factor.toFixed(2)}`}</dd>
              <dd className="stats__sub">{r.profit_factor == null ? "no losing trades yet" : "above $1 means it's making money"}</dd>
            </div>
            <div>
              <dt>Realized</dt>
              <dd className={r.realized < 0 ? "is-down" : r.realized > 0 ? "is-up" : ""}>{signedMoney(r.realized)}</dd>
              <dd className="stats__sub">today {signedMoney(r.today.realized)} · {r.today.trades} closed</dd>
            </div>
            <div>
              <dt>Worst drop</dt>
              <dd className={drop != null && drop < 0 ? "is-down" : ""}>{drop == null ? "—" : pct(drop, true, 1)}</dd>
              <dd className="stats__sub">account, peak to low, {perf ? PERIOD_WORDS[perf.period] : "—"}</dd>
            </div>
          </dl>
          <p className="streak">
            <span className="streak__label">Last {r.last.length}</span>
            {r.last.map((x, i) => (
              <span key={i} className={`streak__pip streak__pip--${x}`} aria-label={x === "W" ? "win" : "loss"}>{x}</span>
            ))}
            {r.streak && r.streak.n > 1 && (
              <span className="streak__now">{r.streak.n} {r.streak.kind === "W" ? "wins" : "losses"} in a row</span>
            )}
          </p>
          {results.by_strategy.length > 1 && (
            <table className="table">
              <thead><tr><th>Strategy</th><th className="num">Trades</th><th className="num">Won</th><th className="num">Realized</th></tr></thead>
              <tbody>
                {results.by_strategy.map((s) => (
                  <tr key={s.strategy}>
                    <td className="sym">{s.strategy}</td>
                    <td className="num">{s.trades}</td>
                    <td className="num">{pct(s.win_rate, false, 0)}</td>
                    <td className={`num ${s.realized < 0 ? "is-down" : s.realized > 0 ? "is-up" : ""}`}>{signedMoney(s.realized)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      {other && other.trades > 0 && (
        <p className="note">
          Not counted: {other.trades} trade{other.trades === 1 ? "" : "s"} the bot didn't open (bought by hand or by
          the old bot), {signedMoney(other.realized)}.
        </p>
      )}
    </section>
  );
}

export function Vitals({ account, status }: { account: Account | null; status: Status | null }) {
  const maxExposure = status?.limits.max_total_exposure_pct;
  const day = account?.day_pnl_pct ?? null;
  return (
    <section className="vitals" aria-label="Account">
      <div className="vital vital--lead">
        <span className="vital__label">Equity</span>
        <span className="vital__value">{money(account?.equity)}</span>
      </div>
      <div className="vital">
        <span className="vital__label">Today</span>
        <span className={`vital__value ${day == null ? "" : day < 0 ? "is-down" : day > 0 ? "is-up" : ""}`}>
          {day == null ? "—" : <>{day < 0 ? "▼ " : day > 0 ? "▲ " : ""}{pct(day)}</>}
        </span>
      </div>
      <div className="vital">
        <span className="vital__label">Cash</span>
        <span className="vital__value">{money0(account?.cash)}</span>
      </div>
      <div className="vital">
        <span className="vital__label">Invested</span>
        <span className="vital__value">
          {pct(account?.exposure_pct, false, 0)}
          {maxExposure != null && <small> of {maxExposure}% max</small>}
        </span>
      </div>
      <div className="vital">
        <span className="vital__label">Trades today</span>
        <span className="vital__value">
          {account?.trades_today ?? "—"}
          {status && <small> of {status.limits.max_trades_per_day} max</small>}
        </span>
      </div>
    </section>
  );
}

export function Positions({ account, error, onSell, notice }: {
  account: Account | null; error: string | null;
  /** a row to sell it, null to sell everything */
  onSell: (row: PositionRow | null) => void;
  notice: string | null;
}) {
  const rows = account?.positions ?? [];
  const exposed = rows.filter((p) => !p.has_stop).length;
  return (
    <section className="panel" aria-labelledby="pos-title">
      <div className="panel__head">
        <div>
          <h2 id="pos-title">Positions</h2>
          <p className="panel__sub">
            {rows.length === 0
              ? "Nothing held right now."
              : exposed === 0
                ? "Every position has a stop-loss at Alpaca."
                : `${exposed} of ${rows.length} without a stop-loss at Alpaca.`}
          </p>
        </div>
        {rows.length > 0 && (
          <button type="button" className="btn btn--sm btn--danger" onClick={() => onSell(null)}>Sell everything</button>
        )}
      </div>
      {notice && <p className="notice notice--good" role="status">{notice}</p>}
      {error && <p className="empty">Can't load positions: {error}</p>}
      {rows.length > 0 && (
        <div className="scroll">
          <table className="table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Stop-loss</th>
                <th>Managed by</th>
                <th className="num">Value</th>
                <th className="num">Of equity</th>
                <th className="num">Gain</th>
                <th className="num">Shares</th>
                <th className="num">Avg cost</th>
                <th><span className="sr-only">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.symbol} className={p.issues.length ? "row--issue" : ""}>
                  <td className="sym">{p.symbol}</td>
                  <td>
                    {p.has_stop
                      ? <span className="tag tag--good"><span aria-hidden="true">✓</span> At Alpaca</span>
                      : <span className="tag tag--crit"><span aria-hidden="true">✕</span> None</span>}
                  </td>
                  <td>{p.owner ?? <span className="tag tag--warn"><span aria-hidden="true">▲</span> Nobody</span>}</td>
                  <td className="num">{money(p.market_value)}</td>
                  <td className="num">{pct(p.pct_of_equity, false, 1)}</td>
                  <td className={`num ${p.unrealized_pct == null ? "" : p.unrealized_pct < 0 ? "is-down" : "is-up"}`}>
                    {pct(p.unrealized_pct)}
                  </td>
                  <td className="num">{qty(p.qty)}</td>
                  <td className="num">{money(p.avg_entry_price)}</td>
                  <td className="row-action">
                    <button type="button" className="btn btn--sm btn--quiet" onClick={() => onSell(p)}
                      aria-label={`Sell ${p.symbol}`}>Sell</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {rows.some((p) => !p.owner) && (
        <p className="note">
          TaylorTrader doesn't touch positions no strategy owns. <a href="#/strategies">Add the symbol to a strategy</a>,
          or manage it yourself.
        </p>
      )}
    </section>
  );
}

const STATUS_TAG: Record<string, string> = {
  filled: "good", submitted: "info", "dry-run": "info", blocked: "warn", failed: "crit",
  canceled: "warn", expired: "warn", rejected: "crit",
};

export function Orders({ orders, error }: { orders: OrderRow[] | null; error: string | null }) {
  return (
    <section className="panel" aria-labelledby="orders-title">
      <div className="panel__head">
        <div>
          <h2 id="orders-title">Orders</h2>
          <p className="panel__sub">Every order the bot placed or decided against, with its reason.</p>
        </div>
      </div>
      {error && <p className="empty">Can't load orders: {error}</p>}
      {orders && orders.length === 0 && (
        <p className="empty">No orders yet. When a strategy buys or sells, it shows up here with the reason.</p>
      )}
      {orders && orders.length > 0 && (
        <ol className="ledger">
          {orders.map((o) => (
            <li key={o.id} className="ledger__row">
              <time className="ledger__time" dateTime={o.ts}>{shortTime(o.ts)}</time>
              <span className={`side side--${o.side}`}>{o.side}</span>
              <span className="ledger__what">
                <b>{o.symbol}</b> {o.qty != null && <>{qty(o.qty)} sh</>}
                {o.fill_price != null ? <> @ {money(o.fill_price)}</> : o.est_price != null && <> @ ~{money(o.est_price)}</>}
              </span>
              <span className={`tag tag--${STATUS_TAG[o.status] ?? "info"}`}>{o.status}</span>
              <span className="ledger__why">{o.reason}{o.detail ? ` · ${o.detail}` : ""}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function Events({ events }: { events: EventRow[] | null }) {
  const shown = (events ?? []).filter((e) => e.kind !== "summary").slice(0, 12);
  return (
    <section className="panel" aria-labelledby="events-title">
      <div className="panel__head">
        <div>
          <h2 id="events-title">Service log</h2>
          <p className="panel__sub">Starts, stops, errors, and anything the service fixed on its own.</p>
        </div>
      </div>
      {shown.length === 0 && <p className="empty">Nothing logged yet.</p>}
      <ol className="log">
        {shown.map((e) => (
          <li key={e.id} className={`log__row log__row--${e.level}`}>
            <time dateTime={e.ts}>{shortTime(e.ts)}</time>
            <span className="log__kind">{e.kind.replace("_", " ")}</span>
            <span className="log__msg">{e.message}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function Strategies({ status }: { status: Status | null }) {
  const list = status?.strategies ?? [];
  return (
    <section className="panel" aria-labelledby="strat-title">
      <div className="panel__head">
        <div>
          <h2 id="strat-title">Strategies</h2>
          <p className="panel__sub"><a href="#/strategies">Edit strategies</a> · <a href="#/settings">Edit limits and alerts</a></p>
        </div>
      </div>
      {list.length === 0 && <p className="empty">No strategies yet. <a href="#/strategies">Add one</a>.</p>}
      <ul className="strats">
        {list.map((s) => (
          <li key={s.name} className={s.enabled ? "" : "is-off"}>
            <div className="strats__name">
              {s.name} {!s.enabled && <span className="tag tag--info">off</span>}
            </div>
            <div className="strats__meta">
              {s.symbols.join(", ")} · {s.timeframe} bars · {s.allocation_pct}% per position · {s.stop_loss_pct}% stop
              {s.take_profit_pct > 0 && ` · ${s.take_profit_pct}% take-profit`}
            </div>
          </li>
        ))}
      </ul>
      {status && (
        <>
          <h3 className="subhead">Guardrails</h3>
          <dl className="limits">
            <div><dt>One position</dt><dd>≤ {status.limits.max_position_pct}% of equity</dd></div>
            <div><dt>All positions</dt><dd>≤ {status.limits.max_total_exposure_pct}% of equity</dd></div>
            <div><dt>Daily loss</dt><dd>stop buying at −{status.limits.max_daily_loss_pct}%</dd></div>
            <div><dt>Trades</dt><dd>≤ {status.limits.max_trades_per_day} a day</dd></div>
            <div><dt>Stop width</dt><dd>≤ {status.limits.max_stop_loss_pct}%</dd></div>
          </dl>
          <h3 className="subhead">Alerts</h3>
          <ul className="alerts">
            {status.alerts.map((a) => (
              <li key={a} className={a.includes("OFF") ? "is-off" : ""}>{a}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function PastResults({ history }: { history: History | null }) {
  if (!history || history.closed_trades === 0) return null;
  const max = Math.max(...history.by_year.map((y) => Math.abs(y.profit)), 1);
  return (
    <section className="panel" aria-labelledby="hist-title">
      <div className="panel__head">
        <div>
          <h2 id="hist-title">The original bot</h2>
          <p className="panel__sub">
            {history.closed_trades} closed trades, {money0(history.realized)} realized. Imported history.
          </p>
        </div>
      </div>
      <table className="table years">
        <thead>
          <tr><th>Year</th><th className="num">Trades</th><th className="num">Won</th><th>Realized</th></tr>
        </thead>
        <tbody>
          {history.by_year.map((y) => (
            <tr key={y.year}>
              <td>{y.year}</td>
              <td className="num">{y.trades}</td>
              <td className="num">{Math.round((y.wins / y.trades) * 100)}%</td>
              <td>
                <span className="bar">
                  <span
                    className={`bar__fill ${y.profit < 0 ? "bar__fill--neg" : ""}`}
                    style={{ width: `${(Math.abs(y.profit) / max) * 100}%` }}
                  />
                  <span className="bar__val">{money0(y.profit)}</span>
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

const ACTION_TAG: Record<string, string> = { buy: "good", sell: "warn", hold: "info" };

export function AIDecisions({ rows, error }: { rows: AIDecision[] | null; error: string | null }) {
  if (!rows || rows.length === 0) return null;
  const today = rows.filter((r) => new Date(r.ts).toDateString() === new Date().toDateString());
  const calls = today.filter((r) => r.model && r.model !== "skipped").length;
  return (
    <section className="panel" aria-labelledby="ai-title">
      <div className="panel__head">
        <div>
          <h2 id="ai-title">AI decisions</h2>
          <p className="panel__sub">What the model was asked and why it answered as it did. {calls} call{calls === 1 ? "" : "s"} today.</p>
        </div>
      </div>
      {error && <p className="empty">Can't load AI decisions: {error}</p>}
      <ol className="ai">
        {rows.slice(0, 12).map((r) => (
          <li key={r.id} className="ai__row">
            <div className="ai__line">
              <time className="ledger__time" dateTime={r.ts}>{shortTime(r.ts)}</time>
              <b>{r.symbol}</b>
              {r.error
                ? <span className="tag tag--crit"><span aria-hidden="true">✕</span> {r.model === "skipped" ? "skipped" : "error"}</span>
                : <span className={`tag tag--${ACTION_TAG[r.action ?? "hold"]}`}>{r.action}{r.confidence != null && ` · ${Math.round(r.confidence * 100)}%`}</span>}
              {r.acted === 1 && <span className="ai__acted">→ order</span>}
              <span className="ai__q">{r.question === "entry" ? "buy?" : "sell?"}</span>
            </div>
            <p className="ai__summary">{r.error ?? r.summary}</p>
            {(r.pros.length > 0 || r.cons.length > 0) && (
              <details className="ai__why">
                <summary>Pros and cons</summary>
                <div className="ai__cols">
                  <ul aria-label="For">{r.pros.map((x, i) => <li key={i}><span aria-hidden="true">+</span> {x}</li>)}</ul>
                  <ul aria-label="Against">{r.cons.map((x, i) => <li key={i}><span aria-hidden="true">−</span> {x}</li>)}</ul>
                </div>
                {r.model && <p className="field__hint">{r.model}{r.latency_ms ? ` · ${(r.latency_ms / 1000).toFixed(1)}s` : ""}</p>}
              </details>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}
