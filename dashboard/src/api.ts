// Types mirror src/taylortrader/web/api.py. Read-only: every call is a GET.

export interface Status {
  mode: "paper" | "live";
  running_mode: "ok" | "dry-run" | null;
  heartbeat: { ts: string | null; age_s: number | null; stale: boolean; stale_after_s: number };
  kill_switch: boolean;
  market: { is_open: boolean; next_open: string | null; next_close: string | null };
  strategies: {
    name: string;
    symbols: string[];
    timeframe: string;
    allocation_pct: number;
    stop_loss_pct: number;
    take_profit_pct: number;
    enabled: boolean;
    params: Record<string, unknown>;
  }[];
  limits: Record<string, number>;
  alerts: string[];
  server_time: string;
}

export interface PositionRow {
  symbol: string;
  qty: number;
  market_value: number;
  pct_of_equity: number;
  avg_entry_price: number;
  price: number;
  unrealized_pct: number | null;
  has_stop: boolean;
  owner: string | null;
  issues: string[];
}

export interface Account {
  equity: number;
  cash: number;
  day_start_equity: number;
  day_pnl_pct: number;
  trades_today: number;
  exposure_pct: number;
  positions: PositionRow[];
}

export interface Point {
  t: number;
  v: number;
}

export interface Performance {
  period: "1M" | "3M" | "1A";
  benchmark: string;
  equity: Point[];
  benchmark_series: Point[];
}

export interface OrderRow {
  id: number;
  ts: string;
  mode: string;
  strategy: string;
  symbol: string;
  side: "buy" | "sell";
  qty: number | null;
  est_price: number | null;
  stop_price: number | null;
  reason: string;
  status: string;
  fill_price: number | null;
  detail: string | null;
}

export interface EventRow {
  id: number;
  ts: string;
  level: string;
  kind: string;
  message: string;
}

export interface History {
  closed_trades: number;
  realized: number;
  by_year: { year: string; trades: number; wins: number; profit: number }[];
}

export class ApiError extends Error {}

export async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  return res.json() as Promise<T>;
}
