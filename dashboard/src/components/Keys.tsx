import { useId, useState } from "react";
import { send, type AccountInfo, type Setup } from "../api";
import { money } from "../format";
import { NoticeLine, useAction } from "./forms";

/** Paste a key pair, test it against Alpaca, and store it only if Alpaca accepts it. */
export function AlpacaKeys({ mode, setup, onSaved }: { mode: "paper" | "live"; setup: Setup; onSaved: (s: Setup) => void }) {
  const prefix = mode === "paper" ? "ALPACA_PAPER" : "ALPACA_LIVE";
  const stored = setup.secrets[`${prefix}_KEY_ID`] && setup.secrets[`${prefix}_SECRET_KEY`];
  const [editing, setEditing] = useState(!stored);
  const [keyId, setKeyId] = useState("");
  const [secret, setSecret] = useState("");
  const [info, setInfo] = useState<AccountInfo | null>(null);
  const act = useAction();
  const idKey = useId();
  const idSecret = useId();

  const testStored = () =>
    act.run(async () => {
      const r = await send<AccountInfo>("POST", "/api/alpaca/test", { mode });
      setInfo(r);
      return `Connected to ${mode} account ${r.account ?? ""}.`;
    });

  const testAndSave = () =>
    act.run(async () => {
      const r = await send<AccountInfo>("POST", "/api/alpaca/test", { mode, key_id: keyId.trim(), secret_key: secret.trim() });
      await send<Setup>("POST", "/api/secrets", { name: `${prefix}_KEY_ID`, value: keyId.trim() });
      const s = await send<Setup>("POST", "/api/secrets", { name: `${prefix}_SECRET_KEY`, value: secret.trim() });
      setKeyId("");
      setSecret("");
      setInfo(r);
      setEditing(false);
      onSaved(s);
      return `Connected to ${mode} account ${r.account ?? ""} and saved the keys to the ${s.secret_store}.`;
    });

  return (
    <div className="keys">
      {!editing && stored ? (
        <div className="secret">
          <span className="tag tag--good"><span aria-hidden="true">✓</span> Saved in the {setup.secrets[`${prefix}_KEY_ID`]}</span>
          <button type="button" className="btn" onClick={testStored} disabled={act.busy}>
            {act.busy ? "Testing…" : "Test connection"}
          </button>
          <button type="button" className="btn btn--quiet" onClick={() => setEditing(true)}>Replace keys</button>
        </div>
      ) : (
        <form className="keys__form" onSubmit={(e) => { e.preventDefault(); testAndSave(); }}>
          <div className="field">
            <label className="field__label" htmlFor={idKey}>API key ID</label>
            <input id={idKey} className="input input--secret" autoComplete="off" spellCheck={false}
              value={keyId} onChange={(e) => setKeyId(e.target.value)} placeholder={mode === "paper" ? "PK…" : "AK…"} />
          </div>
          <div className="field">
            <label className="field__label" htmlFor={idSecret}>Secret key</label>
            <input id={idSecret} className="input input--secret" type="password" autoComplete="off" spellCheck={false}
              value={secret} onChange={(e) => setSecret(e.target.value)} />
          </div>
          <div className="keys__actions">
            <button type="submit" className="btn btn--primary" disabled={act.busy || !keyId.trim() || !secret.trim()}>
              {act.busy ? "Checking with Alpaca…" : "Test and save"}
            </button>
            {stored && <button type="button" className="btn btn--quiet" onClick={() => setEditing(false)}>Cancel</button>}
          </div>
        </form>
      )}
      <NoticeLine notice={act.notice} />
      {info && (
        <p className="keys__account">
          Account {info.account ?? "—"} · {info.status.toLowerCase()} · equity <span className="num">{money(info.equity)}</span>
        </p>
      )}
    </div>
  );
}
