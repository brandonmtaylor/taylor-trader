import { useEffect, useState } from "react";
import type { StrategyCfg } from "../api";
import { NoticeLine, useAction } from "../components/forms";
import { StrategyEditor } from "../components/StrategyEditor";
import { clone, same, type SettingsStore } from "../settings";

export const NEW_STRATEGY: StrategyCfg = {
  name: "macd_rsi", symbols: [], timeframe: "5Min", allocation_pct: 10, stop_loss_pct: 8,
  take_profit_pct: 0, enabled: true, params: {},
};

export function StrategiesPage({ store }: { store: SettingsStore }) {
  const resp = store.resp;
  const [draft, setDraft] = useState<StrategyCfg[] | null>(null);
  const act = useAction();
  useEffect(() => {
    if (resp && draft === null) setDraft(clone(resp.config.strategies));
  }, [resp, draft]);
  if (!resp || !draft) return <main className="page"><p className="empty">{store.error ?? "Loading…"}</p></main>;

  const dirty = !same(draft, resp.config.strategies);
  const save = () => act.run(async () => {
    const r = await store.save({ ...resp.config, strategies: draft });
    setDraft(clone(r.config.strategies));
    return "Saved. The service switches to these on its next check.";
  });

  return (
    <main className="page">
      <header className="page__head">
        <div>
          <h1>Strategies</h1>
          <p className="page__sub">
            Each strategy watches its own symbols and decides when to buy and sell. Every buy goes in with a
            stop-loss held at Alpaca, and the safety limits in Settings apply to all of them together.
          </p>
        </div>
        <button type="button" className="btn" onClick={() => setDraft([...draft, clone(NEW_STRATEGY)])}>Add strategy</button>
      </header>

      {draft.length === 0 && (
        <p className="empty panel">No strategies yet. Add one, pick its symbols, and run a backtest to see how it would have done.</p>
      )}
      <div className="stack">
        {draft.map((s, i) => (
          <StrategyEditor
            key={i}
            index={i}
            value={s}
            catalog={resp.catalog}
            limits={resp.config.guardrails}
            timeframes={resp.timeframes}
            onChange={(v) => setDraft(draft.map((d, j) => (j === i ? v : d)))}
            onRemove={() => setDraft(draft.filter((_, j) => j !== i))}
          />
        ))}
      </div>

      {(dirty || act.notice) && (
        <div className="savebar" role="region" aria-label="Save changes">
          <div className="savebar__inner">
            {dirty ? <span>Unsaved changes</span> : <NoticeLine notice={act.notice} />}
            {dirty && act.notice?.kind === "crit" && <NoticeLine notice={act.notice} />}
            {dirty && (
              <div className="savebar__actions">
                <button type="button" className="btn btn--quiet" onClick={() => { setDraft(clone(resp.config.strategies)); act.setNotice(null); }}>
                  Discard
                </button>
                <button type="button" className="btn btn--primary" onClick={save} disabled={act.busy}>
                  {act.busy ? "Saving…" : "Save strategies"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
