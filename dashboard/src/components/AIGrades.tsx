import type { AIGrades as Grades, GradedDecision, GradeTally } from "../api";
import { pct, shortTime } from "../format";

const CALLS: { key: string; label: string; right: string; wrong: string }[] = [
  { key: "entry:buy", label: "Buy", right: "rose", wrong: "fell" },
  { key: "entry:hold", label: "Didn't buy", right: "avoided a drop", wrong: "missed a rise" },
  { key: "exit:sell", label: "Sell", right: "fell after", wrong: "rose after" },
  { key: "exit:hold", label: "Kept holding", right: "rose", wrong: "fell" },
];

function outcomeText(r: GradedDecision): string {
  if (r.verdict === "pending") return "waiting for the next trading day";
  const call = CALLS.find((c) => c.key === `${r.question}:${r.action}`);
  const move = `${pct(r.ret, true, 1)} next day`;
  if (r.verdict === "flat") return `${move}: barely moved`;
  return `${move}: ${r.verdict === "right" ? call?.right : call?.wrong}`;
}

const acc = (t: GradeTally | undefined) => (t?.accuracy == null ? "—" : `${Math.round(t.accuracy)}%`);

export function AIGrades({ grades, error }: { grades: Grades | null; error: string | null }) {
  if (!grades && !error) return null;
  if (grades && grades.graded + grades.pending === 0) return null;
  const g = grades;
  const edge = g && g.ai_buys.avg_ret != null && g.rule_only.avg_ret != null ? g.ai_buys.avg_ret - g.rule_only.avg_ret : null;
  return (
    <section className="panel" aria-labelledby="grades-title">
      <div className="panel__head">
        <div>
          <h2 id="grades-title">AI report card</h2>
          <p className="panel__sub">
            Each AI call checked against what the price did over {g?.horizon ?? "the next trading day"}.
            Moves under {g?.threshold ?? 1}% count as flat.
          </p>
        </div>
      </div>
      {error && <p className="empty">Can't grade AI calls: {error}</p>}
      {g && g.graded === 0 && (
        <p className="empty">
          {g.pending} call{g.pending === 1 ? "" : "s"} waiting to be graded. Each one is graded a trading day after
          it's made.
        </p>
      )}
      {g && g.graded > 0 && (
        <>
          <dl className="stats stats--2">
            <div>
              <dt>Right</dt>
              <dd>{acc(g.overall)}</dd>
              <dd className="stats__sub">{g.overall.right} right, {g.overall.wrong} wrong, {g.overall.flat} flat</dd>
            </div>
            <div>
              <dt>AI buys vs. rule alone</dt>
              <dd className={edge == null ? "" : edge < 0 ? "is-down" : "is-up"}>{edge == null ? "—" : pct(edge, true, 2)}</dd>
              <dd className="stats__sub">
                {g.ai_buys.n === 0
                  ? "no AI buys yet"
                  : `next day: AI buys ${pct(g.ai_buys.avg_ret, true, 2)} vs. ${pct(g.rule_only.avg_ret, true, 2)} buying every time the rule passed`}
              </dd>
            </div>
          </dl>
          <table className="table table--tight">
            <thead><tr><th>Call</th><th className="num">Times</th><th className="num">Right</th><th className="num">Next day</th></tr></thead>
            <tbody>
              {CALLS.filter((c) => g.by_call[c.key]).map((c) => {
                const t = g.by_call[c.key];
                return (
                  <tr key={c.key}>
                    <td>{c.label}</td>
                    <td className="num">{t.n}</td>
                    <td className="num">{acc(t)}</td>
                    <td className="num">{pct(t.avg_ret, true, 1)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {g.confidence.some((b) => b.right + b.wrong > 0) && (
            <p className="note">
              By confidence:{" "}
              {g.confidence.filter((b) => b.n > 0).map((b, i) => (
                <span key={b.label}>{i > 0 && " · "}{b.label} {acc(b)} right ({b.n})</span>
              ))}. Higher confidence should be right more often.
            </p>
          )}
        </>
      )}
      {g && g.graded > 0 && (
        <details className="ai__why">
          <summary>By market conditions</summary>
          <p className="note">
            How the AI's calls did depending on the indicators when it was asked. Percentages appear once a
            condition has {g.min_slice} graded calls; fewer than that is luck either way.
            {g.recalculated > 0 && ` Values for ${g.recalculated} older call${g.recalculated === 1 ? " were" : "s were"} recalculated from price history.`}
          </p>
          {g.by_indicator.map((grp) => (
            <table key={grp.group} className="table table--tight table--conds">
              <thead>
                <tr><th>{grp.group}</th><th className="num">Calls</th><th className="num">Right</th><th className="num">Next day</th></tr>
              </thead>
              <tbody>
                {grp.slices.map((sl) => (
                  <tr key={sl.label} className={sl.enough ? "" : "is-thin"}>
                    <td>{sl.label}</td>
                    <td className="num">{sl.n}</td>
                    <td className="num">{sl.enough ? acc(sl) : <span className="muted">{sl.n === 0 ? "—" : `needs ${g.min_slice}`}</span>}</td>
                    <td className="num">{sl.enough ? pct(sl.avg_ret, true, 1) : <span className="muted">—</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
        </details>
      )}
      {g && g.recent.length > 0 && (
        <details className="ai__why">
          <summary>Recent calls and how they turned out{g.pending > 0 ? ` (${g.pending} waiting)` : ""}</summary>
          <ol className="grades">
            {g.recent.slice(0, 15).map((r) => (
              <li key={r.id} className={`grades__row grades__row--${r.verdict}`}>
                <span className="grades__mark" aria-hidden="true">
                  {r.verdict === "right" ? "✓" : r.verdict === "wrong" ? "✕" : r.verdict === "flat" ? "–" : "…"}
                </span>
                <time className="ledger__time" dateTime={r.ts}>{shortTime(r.ts)}</time>
                <b>{r.symbol}</b>
                <span>{CALLS.find((c) => c.key === `${r.question}:${r.action}`)?.label ?? r.action}</span>
                <span className="grades__out">{outcomeText(r)}</span>
                <span className="sr-only">{r.verdict}</span>
              </li>
            ))}
          </ol>
        </details>
      )}
      {g && g.graded > 0 && g.graded < 30 && (
        <p className="note">Only {g.graded} graded so far. Treat these numbers as noise until there are a few dozen.</p>
      )}
    </section>
  );
}
