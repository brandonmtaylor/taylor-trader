import { useId, useState } from "react";
import { send, type PositionRow, type SellAllResult } from "../api";
import { money } from "../format";
import { NoticeLine, useAction, useDialog } from "./forms";

const PHRASE = "SELL ALL";

/** Confirm before any hand-placed sell. `target` null = sell everything (needs the typed phrase). */
export function SellDialog({ open, target, positions, live, marketOpen, onClose, onDone }: {
  open: boolean;
  target: PositionRow | null;
  positions: PositionRow[];
  live: boolean;
  marketOpen: boolean | null;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const act = useAction();
  const titleId = useId();
  const phraseId = useId();
  const ref = useDialog(open, () => { setTyped(""); act.setNotice(null); });

  const all = target === null;
  const total = all ? positions.reduce((s, p) => s + p.market_value, 0) : target.market_value;
  const ready = all ? typed.trim() === PHRASE : true;
  const account = live ? "live account (real money)" : "paper account";

  const go = () => act.run(async () => {
    if (all) {
      const r = await send<SellAllResult>("POST", "/api/positions/sell-all", { confirm: typed });
      const sold = r.sold.map((s) => s.symbol).join(", ") || "nothing";
      if (r.failed.length) {
        throw new Error(`Selling ${sold}. Couldn't sell ${r.failed.map((f) => `${f.symbol} (${f.error})`).join("; ")}. New buys are paused.`);
      }
      onDone(`Selling ${sold}. New buys are paused until you resume.`);
    } else {
      await send("POST", "/api/positions/sell", { symbol: target.symbol });
      onDone(`Selling ${target.symbol}.`);
    }
    onClose();
  });

  return (
    <dialog ref={ref} className={`dialog dialog--sell ${live ? "dialog--live" : ""}`} aria-labelledby={titleId} onClose={onClose}>
      <div className="dialog__band" aria-hidden="true">{live ? "Real money" : "Paper account"}</div>
      <div className="dialog__body">
        <h2 id={titleId}>{all ? "Sell everything" : `Sell all of ${target.symbol}`}</h2>
        {all ? (
          <p>
            Sells all {positions.length} position{positions.length === 1 ? "" : "s"} in your {account} at market price,
            about <b>{money(total)}</b>, cancels every open order, and pauses new buys.
          </p>
        ) : (
          <p>
            Sells {target.qty} share{target.qty === 1 ? "" : "s"} of <b>{target.symbol}</b> in your {account} at
            market price, about <b>{money(total)}</b>.{target.has_stop && " Its stop-loss is cancelled first."}
          </p>
        )}
        <ul className="dialog__list">
          {marketOpen === false && <li>The market is closed. The sell waits and fills at the next open, at whatever the price is then.</li>}
          {all
            ? <li>This includes positions you bought yourself, not only the bot's. To sell just one, use its Sell button instead.</li>
            : target.owner && <li>{target.owner} can buy {target.symbol} again on its next signal. Pause new buys at the top if you don't want that.</li>}
          <li>This sends a real order even when the bot is in dry run. Dry run only stops the bot from trading.</li>
        </ul>
        {all && (
          <>
            <label className="field__label" htmlFor={phraseId}>Type <b className="num">{PHRASE}</b> to confirm</label>
            <input id={phraseId} className="input input--mono" value={typed} autoComplete="off" spellCheck={false}
              onChange={(e) => setTyped(e.target.value)} />
          </>
        )}
        <NoticeLine notice={act.notice} />
        <div className="dialog__actions">
          <button type="button" className="btn btn--quiet" onClick={onClose}>Keep {all ? "them" : "it"}</button>
          <button type="button" className="btn btn--sell" disabled={!ready || act.busy} onClick={go}>
            {act.busy ? "Selling…" : all ? "Sell everything" : `Sell ${target.symbol}`}
          </button>
        </div>
      </div>
    </dialog>
  );
}
