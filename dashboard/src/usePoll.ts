import { useEffect, useRef, useState } from "react";
import { get } from "./api";

export interface Polled<T> {
  data: T | null;
  error: string | null;
  loadedAt: number | null;
  /** fetch again now (after a change), instead of waiting for the next poll */
  reload: () => void;
}

/** GET `path` now and every `everyMs`. Keeps the last good data when a refresh fails. */
export function usePoll<T>(path: string, everyMs: number): Polled<T> {
  const [state, setState] = useState<Omit<Polled<T>, "reload">>({ data: null, error: null, loadedAt: null });
  const [nonce, setNonce] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    let timer: number | undefined;
    const tick = async () => {
      try {
        const data = await get<T>(path);
        if (alive.current) setState({ data, error: null, loadedAt: Date.now() });
      } catch (e) {
        if (alive.current) setState((s) => ({ ...s, error: (e as Error).message }));
      }
      if (alive.current) timer = window.setTimeout(tick, everyMs);
    };
    tick();
    return () => {
      alive.current = false;
      window.clearTimeout(timer);
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
