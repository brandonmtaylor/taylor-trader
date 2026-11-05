// Types mirror src/taylortrader/web/api.py. Reads are GETs; every change goes through send().

export interface StrategyCfg {
  name: string;
  symbols: string[];
  timeframe: string;
  allocation_pct: number;
  stop_loss_pct: number;
  take_profit_pct: number;
  enabled: boolean;
  params: Record<string, number | boolean | string>;
}

export interface Limits {
  max_position_pct: number;
  max_total_exposure_pct: number;
  max_daily_loss_pct: number;
  max_trades_per_day: number;
  max_stop_loss_pct: number;
}

export interface Status {
  configured: boolean;
  connected: boolean;
  broker_error: string | null;
  mode: "paper" | "live";
  dry_run: boolean;
  mode_label: string;
  running_mode: "ok" | "dry-run" | null;
  heartbeat: { ts: string | null; age_s: number | null; stale: boolean; stale_after_s: number };
  kill_switch: boolean;
  market: { is_open: boolean; next_open: string | null; next_close: string | null } | null;
  strategies: StrategyCfg[];
  limits: Limits;
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
  /** a sell is already waiting to fill (e.g. sent while the market was closed) */
  selling: boolean;
  stop_price: number | null;
  /** distance from the current price, negative */
  stop_pct: number | null;
  /** dollars lost from here if the stop fires (negative) */
  stop_loss_usd: number | null;
  target_price: number | null;
  target_pct: number | null;
  issues: string[];
}

export interface Account {
  stops_at_risk_usd: number | null;
  equity: number;
  cash: number;
  day_start_equity: number;
  day_pnl_pct: number;
  trades_today: number;
  exposure_pct: number;
  positions: PositionRow[];
}

export interface ResultSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  avg_win_pct: number | null;
  avg_loss_pct: number | null;
  profit_factor: number | null;
  realized: number;
  today: { trades: number; wins: number; realized: number };
  last: ("W" | "L")[];
  streak: { kind: "W" | "L"; n: number } | null;
}

export interface Results {
  bot: ResultSummary;
  by_strategy: { strategy: string; trades: number; wins: number; win_rate: number | null; realized: number }[];
  other: { trades: number; realized: number };
}

export interface SellAllResult {
  sold: { symbol: string; qty: number }[];
  failed: { symbol: string; error: string }[];
  paused: boolean;
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

export interface ServiceState {
  supported: boolean;
  installed: boolean;
  loaded: boolean;
  running: boolean;
  pid: number | null;
}

export type SecretName =
  | "ALPACA_PAPER_KEY_ID" | "ALPACA_PAPER_SECRET_KEY" | "ALPACA_LIVE_KEY_ID" | "ALPACA_LIVE_SECRET_KEY"
  | "NTFY_TOPIC" | "EMAIL_PASSWORD" | "HEALTHCHECK_URL" | "ANTHROPIC_API_KEY" | "OPENAI_API_KEY";

export interface Setup {
  configured: boolean;
  config_path: string;
  config_error: string | null;
  connected: boolean;
  broker_error: string | null;
  mode: "paper" | "live";
  dry_run: boolean;
  has_strategy: boolean;
  secret_store: "Keychain" | ".env";
  /** where each secret is stored, or null. Never the value. */
  secrets: Record<SecretName, string | null>;
  service: ServiceState;
}

export interface ConfigDict {
  mode: "paper" | "live";
  dry_run: boolean;
  data_dir: string;
  feed: "yahoo" | "iex" | "sip";
  strategies: StrategyCfg[];
  guardrails: Limits;
  alerts: { ntfy_server: string; email_from: string; email_to: string[]; smtp_host: string; smtp_port: number };
  service: { loop_seconds: number; heartbeat_stale_minutes: number; daily_summary: boolean; auto_protect: boolean };
  ai: AISettings;
}

export interface AISettings {
  provider: "anthropic" | "openai" | "claude-code";
  model: string;
  endpoint: string;
  effort: "low" | "medium" | "high" | "xhigh" | "max";
  max_calls_per_day: number;
  timeout_seconds: number;
}

export interface AIDecision {
  id: number;
  ts: string;
  strategy: string;
  symbol: string;
  question: "entry" | "exit";
  action: "buy" | "sell" | "hold" | null;
  confidence: number | null;
  summary: string | null;
  pros: string[];
  cons: string[];
  acted: number;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  error: string | null;
}

export interface CatalogItem {
  name: string;
  description: string;
  backtestable: boolean;
  params: Record<string, number | boolean | string>;
}

export interface SettingsResp {
  config: ConfigDict;
  configured: boolean;
  catalog: CatalogItem[];
  hard_limits: Limits;
  timeframes: string[];
  feeds: string[];
}

export interface AccountInfo {
  ok: boolean;
  account: string | null;
  status: string;
  equity: number;
  paper: boolean;
}

export interface BacktestResult {
  summary: Record<string, number | string>;
  symbol: string;
  timeframe: string;
  equity: Point[];
  buy_hold: Point[];
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

/** Every change goes through here. The header proves the request came from this page:
 *  another website can't add it without a CORS preflight the server never approves. */
export async function send<T>(method: "POST" | "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json", "X-TaylorTrader": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: { msg: string }) => d.msg).join("; ") : detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(detail);
  }
  return res.json() as Promise<T>;
}

export interface GradeTally {
  n: number;
  right: number;
  wrong: number;
  flat: number;
  accuracy: number | null;
  avg_ret: number | null;
}

export interface GradedDecision {
  id: number;
  ts: string;
  strategy: string;
  symbol: string;
  question: "entry" | "exit";
  action: "buy" | "sell" | "hold";
  confidence: number | null;
  acted: number;
  summary: string | null;
  price: number | null;
  ret: number | null;
  up: number | null;
  down: number | null;
  verdict: "right" | "wrong" | "flat" | "pending";
}

export interface AIGrades {
  horizon: string;
  threshold: number;
  graded: number;
  pending: number;
  overall: GradeTally;
  by_call: Record<string, GradeTally>;
  rule_only: { n: number; avg_ret: number | null };
  ai_buys: { n: number; avg_ret: number | null };
  confidence: (GradeTally & { label: string })[];
  by_indicator: { group: string; slices: (GradeTally & { label: string; enough: boolean })[] }[];
  min_slice: number;
  recalculated: number;
  recent: GradedDecision[];
}

export type ChartPeriod = "1W" | "1M" | "3M" | "1A";

export interface ChartTrade {
  symbol: string;
  strategy: string | null;
  qty: number;
  entry: number;
  exit: number;
  opened: string;
  closed: string;
  pnl: number;
  pct: number;
  win: boolean;
  peak: number | null;
  peak_pct: number | null;
  captured: number | null;
  after_high: number | null;
  after_pct: number | null;
  after_complete: boolean;
}

export interface TradeChartData {
  symbols: string[];
  symbol: string | null;
  period?: ChartPeriod;
  timeframe?: string;
  points?: {
    t: number; c: number;
    ma_20?: number; ma_200?: number; rsi?: number; macd?: number; macd_signal?: number; macd_diff?: number;
  }[];
  marks?: { i: number; t: string; side: "buy" | "sell"; qty: number; price: number; strategy: string | null }[];
  trades?: ChartTrade[];
  position?: {
    qty: number; entry: number | null; opened: string | null; last: number | null; stop: number | null;
    target: number | null;
    peak: number | null; peak_pct: number | null; pct: number | null;
  } | null;
  ai?: { i: number; t: string; action: string; question: string; confidence: number | null; acted: boolean; summary: string | null }[];
}
