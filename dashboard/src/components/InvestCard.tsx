import type { InvestResp } from "../api";
import { EVERY_WORD, money } from "../format";

/** The invest plan at a glance, on the Overview. The Invest page has the rest. */
export function InvestCard({ data, error }: { data: InvestResp | null; error: string | null }) {
  if (!data) {
    if (!error) return null;
    return (
      <section className="panel" aria-labelledby="invcard-title">
        <h2 id="invcard-title">Invest plan</h2>
        <p className="empty">Can't load the invest plan: {error}</p>
      </section>
    );
  }
  const st = data.status;
  if (!data.plan.enabled || !st) {
    return (
      <section className="panel" aria-labelledby="invcard-title">
        <h2 id="invcard-title">Invest plan</h2>
        <p className="panel__sub">Off. <a href="#/invest">Set up a long-term plan</a> to put a set amount into a mix of funds on a schedule.</p>
      </section>
    );
  }
  const nxt = st.next;
  const gain = st.value - st.put_in;
  return (
    <section className="panel" aria-labelledby="invcard-title">
      <div className="panel__head">
        <div>
          <h2 id="invcard-title">Invest plan</h2>
          <p className="panel__sub">
            {st.preview
              ? <><span className="tag tag--info">Preview</span> alerts only</>
              : <><span className={`tag ${st.mode === "live" ? "tag--crit" : "tag--good"}`}>{st.mode === "live" ? "Live" : "Paper"}</span>{" "}
                  {money(st.amount)} {EVERY_WORD[st.every]}</>}
          </p>
        </div>
        <a className="btn btn--sm" href="#/invest">Open</a>
      </div>
      <dl className="stats stats--2">
        {st.preview ? (
          <div>
            <dt>Each buy</dt>
            <dd>{money(st.amount)}</dd>
          </div>
        ) : (
          <div>
            <dt>Worth now</dt>
            <dd>{money(st.value)}</dd>
            <dd className={`stats__sub ${gain < 0 ? "is-down" : ""}`}>{money(st.put_in)} put in</dd>
          </div>
        )}
        <div>
          <dt>Next buy</dt>
          <dd>{nxt ? new Date(`${nxt.date}T12:00:00`).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "—"}</dd>
          {nxt && <dd className="stats__sub">{Object.keys(nxt.split).join(" · ")}</dd>}
        </div>
      </dl>
      {nxt?.cash_short && <p className="notice notice--crit" role="status"><span aria-hidden="true">▲</span> Not enough cash for the full buy.</p>}
    </section>
  );
}
