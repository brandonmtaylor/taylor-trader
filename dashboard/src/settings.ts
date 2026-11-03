import { useCallback, useEffect, useState } from "react";
import { get, send, type ConfigDict, type SettingsResp } from "./api";

/** Settings from the server plus a local draft. save() writes the whole config; the server validates it. */
export function useSettings() {
  const [resp, setResp] = useState<SettingsResp | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setResp(await get<SettingsResp>("/api/settings"));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = useCallback(async (config: ConfigDict) => {
    const r = await send<SettingsResp>("PUT", "/api/settings", { config });
    setResp(r);
    return r;
  }, []);

  return { resp, error, reload: load, save };
}

export type SettingsStore = ReturnType<typeof useSettings>;

export const clone = <T,>(v: T): T => JSON.parse(JSON.stringify(v)) as T;
export const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
