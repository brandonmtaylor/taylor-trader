const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const money = (n: number | null | undefined) => (n == null ? "—" : usd.format(n));
export const money0 = (n: number | null | undefined) => (n == null ? "—" : usd0.format(n));

export function pct(n: number | null | undefined, signed = true, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  const s = n.toFixed(digits);
  if (Number(s) === 0) return `${(0).toFixed(digits)}%`; // not "-0.0%"
  return signed && n > 0 ? `+${s}%` : `${s}%`;
}

export function qty(n: number): string {
  return n.toLocaleString("en-US", { maximumFractionDigits: 4 });
}

export function ago(seconds: number): string {
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ${Math.floor((seconds % 3600) / 60)} min ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

const NY = "America/New_York";

export function marketTime(iso: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-US", {
    timeZone: NY,
    weekday: "short",
    hour: "numeric",
    minute: "2-digit",
  }) + " ET";
}

export function shortTime(iso: string): string {
  const d = new Date(iso);
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString("en-US", today
    ? { hour: "numeric", minute: "2-digit" }
    : { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function day(t: number): string {
  return new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}
