import { useEffect, useState } from "react";
import { get } from "./api";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loadedAt: number | null;
  /** fetch again now (after a change), instead of waiting for the next poll */
  reload: () => void;
}

/** GET `path` now and every `everyMs`. Keeps the last good data when a refresh fails. Stops while the
 *  tab is hidden; on coming back, anything due by then refreshes at once (a slow poll like the what-if
 *  isn't refetched on every tab switch). */
export function usePoll<T>(path: string, everyMs: number): Polled<T> {
  const [state, setState] = useState<Omit<Polled<T>, "reload">>({ data: null, error: null, loadedAt: null });
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    // Per effect, not shared: a request still in flight for an old path must never write state or
    // re-arm the timer once the path changes, or the old path keeps polling next to the new one.
    let cancelled = false;
    let timer: number | undefined;
    let inFlight = false;
    let lastDone = 0;
    const controller = new AbortController();

    const arm = (delay: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(tick, delay);
    };
    const tick = async () => {
      inFlight = true;
      try {
        const data = await get<T>(path, controller.signal);
        if (!cancelled) setState({ data, error: null, loadedAt: Date.now() });
      } catch (e) {
        if (!cancelled) setState((s) => ({ ...s, error: (e as Error).message }));
      }
      inFlight = false;
      lastDone = Date.now();
      if (!cancelled && !document.hidden) arm(everyMs);
    };
    const onVisibility = () => {
      if (document.hidden) window.clearTimeout(timer);
      else if (!inFlight) arm(Math.max(0, lastDone + everyMs - Date.now()));
    };

    tick();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [path, everyMs, nonce]);

  return { ...state, reload: () => setNonce((n) => n + 1) };
}

/** Re-render every `ms` so relative times ("12s ago") stay current. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}
