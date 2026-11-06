import { useState } from "react";
import { ApiError, send, type Session } from "../api";

/** Shown on another device (phone, laptop) when Wi-Fi access is on and it isn't signed in yet. */
export function SignInPage({ onSignedIn }: { onSignedIn: (s: Session) => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await send<Session>("POST", "/api/login", { passcode: code }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Can't reach the Mac running TaylorTrader.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="signin">
      <form className="panel signin__card" onSubmit={submit}>
        <h1>TaylorTrader</h1>
        <p className="panel__sub">
          Enter the passcode from your Mac. Run <code>taylortrader wifi on</code> there to see it.
        </p>
        <label className="field">
          <span className="field__label">Passcode</span>
          <input className="input input--mono" value={code} onChange={(e) => setCode(e.target.value)}
            autoComplete="current-password" autoCapitalize="none" autoCorrect="off" spellCheck={false}
            placeholder="xxxx-xxxx-xxxx" autoFocus required />
        </label>
        {error && <p className="signin__error" role="alert">{error}</p>}
        <button className="btn btn--primary" type="submit" disabled={busy || !code.trim()}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="field__hint">
          This device stays signed in for 30 days. Going live and API keys can only be changed on the Mac.
        </p>
      </form>
    </main>
  );
}
