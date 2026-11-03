import { useId, useState, type ReactNode } from "react";
import { send, type SecretName, type Setup } from "../api";

/** Label + control wired together. `render` receives the id to put on the input. */
export function Labeled({ label, hint, render, wide }: {
  label: string; hint?: ReactNode; render: (id: string) => ReactNode; wide?: boolean;
}) {
  const id = useId();
  return (
    <div className={`field ${wide ? "field--wide" : ""}`}>
      <label htmlFor={id} className="field__label">{label}</label>
      {render(id)}
      {hint && <p className="field__hint">{hint}</p>}
    </div>
  );
}

/** Free-typing number box: keeps what you type, reports only valid numbers. The server enforces limits. */
export function NumberInput({ id, value, onChange, suffix }: {
  id?: string; value: number; onChange: (v: number) => void; suffix?: string;
}) {
  const [text, setText] = useState(String(value));
  const [last, setLast] = useState(value);
  if (value !== last) {
    setLast(value);
    setText(String(value));
  }
  return (
    <span className="numin">
      <input
        id={id}
        className="input input--num"
        inputMode="decimal"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number(e.target.value);
          if (e.target.value.trim() !== "" && Number.isFinite(n)) {
            setLast(n);
            onChange(n);
          }
        }}
      />
      {suffix && <span className="numin__suffix">{suffix}</span>}
    </span>
  );
}

export function Toggle({ checked, onChange, label, id }: { checked: boolean; onChange: (v: boolean) => void; label: string; id?: string }) {
  return (
    <label className="toggle">
      <input id={id} type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
      <span className="toggle__track" aria-hidden="true"><span className="toggle__knob" /></span>
      <span>{label}</span>
    </label>
  );
}

export type Notice = { kind: "good" | "crit" | "info"; text: string } | null;

export function NoticeLine({ notice }: { notice: Notice }) {
  if (!notice) return null;
  const glyph = notice.kind === "good" ? "✓" : notice.kind === "crit" ? "✕" : "●";
  return (
    <p className={`notice notice--${notice.kind}`} role={notice.kind === "crit" ? "alert" : "status"}>
      <span aria-hidden="true">{glyph}</span> {notice.text}
    </p>
  );
}

/** Run an async action with a busy flag and a result line. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  async function run(fn: () => Promise<string | void>) {
    setBusy(true);
    setNotice(null);
    try {
      const msg = await fn();
      if (msg) setNotice({ kind: "good", text: msg });
    } catch (e) {
      setNotice({ kind: "crit", text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }
  return { busy, notice, setNotice, run };
}

/** A secret the app stores but never shows: say where it's saved, offer Replace / Remove. */
export function SecretField({ name, label, hint, setup, onSaved, placeholder }: {
  name: SecretName; label: string; hint?: ReactNode; setup: Setup; onSaved: (s: Setup) => void; placeholder?: string;
}) {
  const where = setup.secrets[name];
  const [editing, setEditing] = useState(!where);
  const [value, setValue] = useState("");
  const act = useAction();
  const id = useId();
  const save = () =>
    act.run(async () => {
      const s = await send<Setup>("POST", "/api/secrets", { name, value });
      setValue("");
      setEditing(false);
      onSaved(s);
      return `Saved to the ${s.secrets[name]}.`;
    });
  return (
    <div className="field field--wide">
      <label htmlFor={id} className="field__label">{label}</label>
      {!editing && where ? (
        <div className="secret">
          <span className="tag tag--good"><span aria-hidden="true">✓</span> Saved in the {where}</span>
          {where !== "environment" && (
            <>
              <button type="button" className="btn btn--quiet" onClick={() => setEditing(true)}>Replace</button>
              <button
                type="button"
                className="btn btn--quiet"
                onClick={() => act.run(async () => {
                  const s = await send<Setup>("DELETE", `/api/secrets/${name}`);
                  onSaved(s);
                  setEditing(true);
                  return "Removed.";
                })}
              >Remove</button>
            </>
          )}
        </div>
      ) : (
        <form className="secret" onSubmit={(e) => { e.preventDefault(); if (value.trim()) save(); }}>
          <input
            id={id}
            className="input input--secret"
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder={placeholder}
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
          <button type="submit" className="btn" disabled={act.busy || !value.trim()}>Save</button>
          {where && <button type="button" className="btn btn--quiet" onClick={() => { setEditing(false); setValue(""); }}>Cancel</button>}
        </form>
      )}
      {hint && <p className="field__hint">{hint}</p>}
      <NoticeLine notice={act.notice} />
    </div>
  );
}
