import { useId, useState } from "react";
import { send, type Setup, type Status } from "../api";
import { AlpacaKeys } from "./Keys";
import { NoticeLine, useAction, useDialog } from "./forms";

const PHRASE = "GO LIVE";

/** The only way to real money: live keys that open an active account, plus a typed phrase. */
export function GoLiveDialog({ open, onClose, setup, onSetup, onDone }: {
  open: boolean; onClose: () => void; setup: Setup; onSetup: (s: Setup) => void; onDone: (s: Status) => void;
}) {
  const [typed, setTyped] = useState("");
  const ref = useDialog(open, () => setTyped(""));
  const act = useAction();
  const titleId = useId();
  const phraseId = useId();
  const hasKeys = Boolean(setup.secrets.ALPACA_LIVE_KEY_ID && setup.secrets.ALPACA_LIVE_SECRET_KEY);
  const go = () => act.run(async () => {
    const s = await send<Status>("POST", "/api/mode", { mode: "live", confirm: typed });
    onDone(s);
    onClose();
  });

  return (
    <dialog ref={ref} className="dialog dialog--live" aria-labelledby={titleId} onClose={onClose}>
      <div className="dialog__band" aria-hidden="true">Real money</div>
      <div className="dialog__body">
        <h2 id={titleId}>Switch to live trading</h2>
        <p>
          From the service's next check, buys and sells go to your <b>live Alpaca account with real money</b>.
          Your strategies, safety limits and stop-losses stay exactly as they are.
        </p>
        <ul className="dialog__list">
          <li>Start small: a low position size and a tight daily-loss limit.</li>
          <li>You can switch back to paper here at any time. Open positions stay open and keep their stop-losses.</li>
          <li>This is software, not advice. It can and will lose money on some trades.</li>
        </ul>

        <h3 className="subhead">1 · Live API keys</h3>
        <AlpacaKeys mode="live" setup={setup} onSaved={onSetup} />

        <h3 className="subhead">2 · Confirm</h3>
        <label className="field__label" htmlFor={phraseId}>Type <b className="num">{PHRASE}</b> to confirm</label>
        <input id={phraseId} className="input input--mono" value={typed} autoComplete="off" spellCheck={false}
          onChange={(e) => setTyped(e.target.value)} disabled={!hasKeys} />
        <NoticeLine notice={act.notice} />

        <div className="dialog__actions">
          <button type="button" className="btn btn--quiet" onClick={onClose}>Stay on paper</button>
          <button type="button" className="btn btn--live" disabled={!hasKeys || typed.trim() !== PHRASE || act.busy} onClick={go}>
            {act.busy ? "Checking your live account…" : "Switch to live trading"}
          </button>
        </div>
      </div>
    </dialog>
  );
}
