import { useEffect, useId, useState } from "react";
import { send, type Contribution, type Holding, type InvestEvery, type InvestPlan, type InvestResp, type InvestStatus } from "../api";
import { Labeled, NoticeLine, NumberInput, Toggle, useAction, useDialog } from "../components/forms";
import { WhatIf } from "../components/WhatIf";
import { EVERY_WORD, money, pct, STATUS_TAG } from "../format";
import { clone, same } from "../settings";
import { usePoll } from "../usePoll";

const EVERY: { id: InvestEvery; label: string }[] = [
  { id: "week", label: "Weekly" },
  { id: "2weeks", label: "Every 2 weeks" },
  { id: "month", label: "Monthly" },
  { id: "once", label: "All at once" },
];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"];

const dateText = (iso: string) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });

export function InvestPage() {
  const poll = usePoll<InvestResp>("/api/invest", 30_000);
  const resp = poll.data;
  const [draft, setDraft] = useState<InvestPlan | null>(null);
  const [confirm, setConfirm] = useState<{ phrase: string } | null>(null);
  const act = useAction();

  useEffect(() => {
    if (resp && draft === null) setDraft(clone(resp.plan));
  }, [resp, draft]);
  if (!resp || !draft) {
    return <main className="page"><p className="empty">{poll.error ?? "Loading…"}</p></main>;
  }

  const dirty = !same(draft, resp.plan);
  const save = (typed?: string) => act.run(async () => {
    try {
      const r = await send<InvestResp>("PUT", "/api/invest", { plan: draft, confirm: typed });
      setDraft(clone(r.plan));
      setConfirm(null);
      poll.reload();
      return r.plan.enabled
        ? r.plan.preview ? "Saved. Preview only: it'll alert what it would buy." : "Saved. The plan buys on schedule."
        : "Saved. The plan is off.";
    } catch (e) {
      const m = /confirm with "([^"]+)"/.exec((e as Error).message);
      if (m && !typed) {
        setConfirm({ phrase: m[1] });
        return;
      }
      throw e;
    }
  });

  return (
    <main className="page">
      <header className="page__head">
        <div>
          <h1>Invest</h1>
          <p className="page__sub">
            Put a set amount into a fixed mix of funds on a schedule and hold it. No signals and no stop-losses.
            Each buy goes to whatever is furthest under its target first, so new money keeps the mix balanced
            without selling.
          </p>
        </div>
      </header>

      {resp.error && <p className="notice notice--crit">Can't reach Alpaca: {resp.error}</p>}
      <div className="stack">
        {resp.status && resp.plan.enabled && <PlanStatus status={resp.status} cash={resp.cash} />}
        <WhatIf />
        <PlanEditor draft={draft} resp={resp} onChange={setDraft} />
        {resp.history.length > 0 && <History rows={resp.history} />}
      </div>

      <p className="note">
        The ready-made mixes are common examples, not recommendations, and this isn't financial advice. Funds can
        lose value. The bot invests cash already in your Alpaca account; it can't move money from your bank.
      </p>

      {(dirty || act.notice) && (
        <div className="savebar" role="region" aria-label="Save changes">
          <div className="savebar__inner">
            {dirty ? <span>Unsaved changes</span> : <NoticeLine notice={act.notice} />}
            {dirty && act.notice?.kind === "crit" && <NoticeLine notice={act.notice} />}
            {dirty && (
              <div className="savebar__actions">
                <button type="button" className="btn btn--quiet"
                  onClick={() => { setDraft(clone(resp.plan)); act.setNotice(null); }}>Discard</button>
                <button type="button" className="btn btn--primary" onClick={() => save()} disabled={act.busy}>
                  {act.busy ? "Saving…" : "Save plan"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirm !== null} phrase={confirm?.phrase ?? ""} plan={draft} live={resp.account_mode === "live"}
        busy={act.busy} notice={act.notice} onCancel={() => setConfirm(null)} onConfirm={(t) => save(t)} />
    </main>
  );
}

// --- status ------------------------------------------------------------------------------------------

function PlanStatus({ status, cash }: { status: InvestStatus; cash: number | null }) {
  const nxt = status.next;
  const gain = status.value - status.put_in;
  const symbols = Object.keys(status.holdings);
  const held = status.value > 0;
  return (
    <section className="panel" aria-labelledby="inv-status">
      <div className="panel__head">
        <div>
          <h2 id="inv-status">Your plan</h2>
          <p className="panel__sub">
            {status.preview
              ? <><span className="tag tag--info">Preview</span> Runs on schedule and alerts what it would buy. Nothing is ordered.</>
              : <><span className={`tag ${status.mode === "live" ? "tag--crit" : "tag--good"}`}>
                  {status.mode === "live" ? "Buying with real money" : "Buying on paper"}</span>{" "}
                  {money(status.amount)} {EVERY_WORD[status.every]}.</>}
          </p>
        </div>
      </div>
      <dl className="stats">
        {!status.preview && (
          <div>
            <dt>Put in</dt>
            <dd>{money(status.put_in)}</dd>
          </div>
        )}
        <div>
          <dt>Worth now</dt>
          <dd>{money(status.value)}</dd>
          {!status.preview && status.put_in > 0 && (
            <dd className={`stats__sub ${gain < 0 ? "is-down" : "is-up"}`}>
              {gain < 0 ? "−" : "+"}{money(Math.abs(gain))} ({pct(gain / status.put_in * 100, true, 1)})
            </dd>
          )}
        </div>
        <div>
          <dt>Next buy</dt>
          <dd>{nxt ? dateText(nxt.date) : "None scheduled"}</dd>
          {nxt && (
            <dd className="stats__sub">
              {Object.entries(nxt.split).map(([s, n]) => `${s} ${money(n)}`).join(" · ") || "—"}
            </dd>
          )}
        </div>
      </dl>
      {nxt?.cash_short && (
        <p className="notice notice--crit" role="status">
          <span aria-hidden="true">▲</span> {money(cash)} cash for a {money(nxt.amount)} buy. It'll invest what's
          there; deposit more at Alpaca to buy the full amount.
        </p>
      )}

      <h3 className="subhead">Mix: now vs. target</h3>
      {!held && <p className="muted">Nothing bought yet. The first buy follows the targets exactly.</p>}
      <ul className="mix" aria-label="Each holding's share of the plan now, against its target">
        {symbols.map((s) => {
          const h = status.holdings[s];
          const off = Math.abs(h.drift) >= 0.5;
          return (
            <li key={s} className="mix__row">
              <span className="sym">{s}</span>
              <span className="mix__track" aria-hidden="true">
                <span className="mix__fill" style={{ width: `${Math.min(100, held ? h.now : 0)}%` }} />
                <span className="mix__target" style={{ left: `${Math.min(100, h.target)}%` }} />
              </span>
              <span className="mix__nums num">
                {held ? `${h.now.toFixed(1)}%` : "—"} <span className="muted">of {h.target.toFixed(0)}%</span>
              </span>
              <span className={`mix__drift num ${off ? "" : "muted"}`}>
                {held ? (off ? `${h.drift > 0 ? "+" : "−"}${Math.abs(h.drift).toFixed(1)} pts` : "on target") : ""}
              </span>
              <span className="sr-only">
                {s}: {held ? `${h.now.toFixed(1)} percent now` : "not bought yet"}, target {h.target.toFixed(0)} percent.
              </span>
            </li>
          );
        })}
      </ul>
      {held && (
        <p className="field__hint">
          <span className="mix__key mix__key--fill" aria-hidden="true" /> now{" "}
          <span className="mix__key mix__key--target" aria-hidden="true" /> target. Anything under its target gets
          the next buy.
        </p>
      )}
    </section>
  );
}

// --- editor ------------------------------------------------------------------------------------------

function PlanEditor({ draft, resp, onChange }: { draft: InvestPlan; resp: InvestResp; onChange: (p: InvestPlan) => void }) {
  const set = (patch: Partial<InvestPlan>) => onChange({ ...draft, ...patch });
  const total = draft.holdings.reduce((a, h) => a + (Number.isFinite(h.weight) ? h.weight : 0), 0);
  const matches = resp.examples.find((e) => same(e.holdings, draft.holdings))?.id ?? null;
  const setHolding = (i: number, h: Holding) => set({ holdings: draft.holdings.map((x, j) => (j === i ? h : x)) });
  const dayId = useId();

  return (
    <section className="panel" aria-labelledby="inv-edit">
      <div className="panel__head">
        <div>
          <h2 id="inv-edit">{resp.plan.enabled ? "Change the plan" : "Set up a plan"}</h2>
          <p className="panel__sub">Pick a mix, an amount and a schedule. Turn on preview first to watch it run.</p>
        </div>
      </div>

      <h3 className="subhead">Start from a mix</h3>
      <div className="choices choices--grid" role="radiogroup" aria-label="Ready-made mixes">
        {resp.examples.map((ex) => (
          <label key={ex.id} className={`choice ${matches === ex.id ? "choice--on" : ""}`}>
            <input type="radio" name="example" checked={matches === ex.id}
              onChange={() => set({ holdings: clone(ex.holdings) })} />
            <span>
              <b>{ex.name}</b>
              <span className="choice__text">{ex.about}</span>
              <span className="choice__text num">{ex.holdings.map((h) => `${h.symbol} ${h.weight}%`).join(" · ")}</span>
            </span>
          </label>
        ))}
      </div>

      <h3 className="subhead">Holdings</h3>
      <table className="table table--tight holdings">
        <thead>
          <tr><th>Fund</th><th className="num">Target</th><th><span className="sr-only">Remove</span></th></tr>
        </thead>
        <tbody>
          {draft.holdings.map((h, i) => (
            <tr key={i}>
              <td>
                <input className="input input--mono input--sm" aria-label={`Fund ${i + 1} symbol`} value={h.symbol}
                  maxLength={10} spellCheck={false} autoCapitalize="characters"
                  onChange={(e) => setHolding(i, { ...h, symbol: e.target.value.toUpperCase().trim() })} />
              </td>
              <td className="num">
                <NumberInput value={h.weight} suffix="%" onChange={(w) => setHolding(i, { ...h, weight: w })} />
              </td>
              <td className="row-action">
                <button type="button" className="btn btn--quiet btn--sm"
                  onClick={() => set({ holdings: draft.holdings.filter((_, j) => j !== i) })}
                  aria-label={`Remove ${h.symbol || "this fund"}`}>Remove</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="saverow">
        <button type="button" className="btn btn--sm" disabled={draft.holdings.length >= 20}
          onClick={() => set({ holdings: [...draft.holdings, { symbol: "", weight: Math.max(0, 100 - total) }] })}>
          Add a fund
        </button>
        <span className={`holdings__total ${Math.abs(total - 100) > 0.01 ? "is-down" : ""}`} role="status">
          {Math.abs(total - 100) > 0.01 ? `Targets add up to ${total.toFixed(2).replace(/\.?0+$/, "")}%, not 100%` : "Adds up to 100%"}
        </span>
      </div>

      <h3 className="subhead">How much, how often</h3>
      <div className="field">
        <span className="field__label" id={`${dayId}-every`}>How often</span>
        <div className="segmented segmented--wrap" role="group" aria-labelledby={`${dayId}-every`}>
          {EVERY.map((e) => (
            <button key={e.id} type="button" aria-pressed={draft.every === e.id}
              onClick={() => set({ every: e.id, day: e.id === "month" ? Math.min(Math.max(draft.day, 1), 28) : Math.min(draft.day, 4) })}>
              {e.label}
            </button>
          ))}
        </div>
      </div>
      <div className="grid-fields invest-fields">
        <Labeled label="Each buy" render={(id) => (
          <NumberInput id={id} value={draft.amount} suffix="$" onChange={(v) => set({ amount: v })} />
        )} hint={`From cash in the account. Up to ${money(resp.hard_max)}.`} />
        {draft.every !== "once" && (
          <Labeled label={draft.every === "month" ? "Day of the month" : "Day of the week"} render={(id) => (
            <select id={id} className="input" value={draft.day} onChange={(e) => set({ day: Number(e.target.value) })}>
              {draft.every === "month"
                ? Array.from({ length: 28 }, (_, i) => <option key={i + 1} value={i + 1}>{i + 1}</option>)
                : WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
            </select>
          )} hint="Bought after 10:00 a.m. New York time. A weekend or holiday buys on the next open day." />
        )}
        <Labeled label="Start (optional)" render={(id) => (
          <input id={id} type="date" className="input" value={draft.start} onChange={(e) => set({ start: e.target.value })} />
        )} />
        {draft.every !== "once" && (
          <Labeled label="Stop after (optional)" render={(id) => (
            <input id={id} type="date" className="input" value={draft.until} onChange={(e) => set({ until: e.target.value })} />
          )} />
        )}
      </div>
      {draft.every !== "once" && draft.amount >= 5000 && (
        <p className="note">
          Have a lump sum? Vanguard found investing it all at once beat spreading it out over months about two
          times in three. Spreading it out is for not regretting a buy right before a drop. Both are fine choices.
        </p>
      )}

      <h3 className="subhead">On or off</h3>
      <div className="toggles">
        <Toggle checked={draft.enabled} onChange={(v) => set({ enabled: v })} label="Plan on" />
        <Toggle checked={draft.preview} onChange={(v) => set({ preview: v })} label="Preview only: alert, don't buy" />
        <Toggle checked={draft.reserve} onChange={(v) => set({ reserve: v })}
          label="Keep the next buy's cash away from trading strategies" />
      </div>
      {draft.enabled && !draft.preview && (
        <p className="field__hint">
          Saving asks you to type the amount and schedule, since the plan will start placing orders
          {resp.account_mode === "live" ? " with real money" : " on your paper account"}.
        </p>
      )}
    </section>
  );
}

// --- history -----------------------------------------------------------------------------------------

function History({ rows }: { rows: Contribution[] }) {
  return (
    <section className="panel" aria-labelledby="inv-hist">
      <div className="panel__head">
        <h2 id="inv-hist">Buys</h2>
      </div>
      <div className="scroll">
        <table className="table table--tight">
          <thead><tr><th>When</th><th>Mode</th><th className="num">Total</th><th>Split</th></tr></thead>
          <tbody>
            {rows.map((c) => (
              <tr key={`${c.period}-${c.mode}`}>
                <td>{new Date(c.ts).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" })}</td>
                <td>{c.mode === "preview" ? <span className="muted">Preview</span> : c.mode === "live" ? "Live" : "Paper"}</td>
                <td className="num">{money(c.total)}</td>
                <td className="holdings__split">
                  {c.buys.map((b) => (
                    <span key={b.symbol} className={`tag tag--${STATUS_TAG[b.status] ?? "info"}`} title={b.detail || b.status}>
                      {b.symbol} {money(b.notional)}{b.status !== "filled" && b.status !== "preview" ? ` · ${b.status}` : ""}
                    </span>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// --- confirm -------------------------------------------------------------------------------------------

function ConfirmDialog({ open, phrase, plan, live, busy, notice, onCancel, onConfirm }: {
  open: boolean; phrase: string; plan: InvestPlan; live: boolean; busy: boolean;
  notice: ReturnType<typeof useAction>["notice"]; onCancel: () => void; onConfirm: (typed: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const ref = useDialog(open, () => setTyped(""));
  const titleId = useId();
  const inputId = useId();
  const when = plan.every === "once" ? "once, on the next open day"
    : plan.every === "month" ? `on day ${plan.day} of every month`
      : `every ${plan.every === "2weeks" ? "other " : ""}${WEEKDAYS[plan.day]}`;
  return (
    <dialog ref={ref} className={`dialog ${live ? "dialog--live" : ""}`} aria-labelledby={titleId} onClose={onCancel}>
      {live && <div className="dialog__band" aria-hidden="true">Real money</div>}
      <div className="dialog__body">
        <h2 id={titleId}>Start buying</h2>
        <p>
          The plan will buy <b>{money(plan.amount)}</b> {when}
          {live ? <>, in your <b>live account with real money</b></> : ", on your paper account"}:
        </p>
        <ul className="dialog__list">
          {plan.holdings.map((h) => <li key={h.symbol}>{h.symbol}: {h.weight}% of the plan</li>)}
          <li>No stop-losses: these are held through ups and downs. The kill switch still stops new buys.</li>
        </ul>
        <label className="field__label" htmlFor={inputId}>Type <b className="num">{phrase}</b> to confirm</label>
        <input id={inputId} className="input input--mono" value={typed} autoComplete="off" spellCheck={false}
          onChange={(e) => setTyped(e.target.value)} />
        <NoticeLine notice={notice} />
        <div className="dialog__actions">
          <button type="button" className="btn btn--quiet" onClick={onCancel}>Not yet</button>
          <button type="button" className={`btn ${live ? "btn--live" : "btn--primary"}`}
            disabled={typed.trim() !== phrase || busy} onClick={() => onConfirm(typed.trim())}>
            {busy ? "Saving…" : "Start buying"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
