import type { StrategyCfg } from "../api";
import { Labeled, NumberInput, Toggle } from "./forms";

// Hourly is 6, not 7: the bar stamped 9:00 starts before the open and is dropped (see grading.horizon_bars).
const BARS_PER_DAY: Record<string, number> = { "1Min": 390, "5Min": 78, "15Min": 26, "1Hour": 6, "1Day": 1 };
const NAMES = "close, open, high, low, volume, macd, macd_signal, macd_diff, chaikin, and at any length up to 250 bars: ma_50, ema_20, high_20, low_20, change_5 (percent), avg_volume_20, rsi_14, atr_14";

/** The AI strategy's settings: plain-English instructions plus optional rules that gate each model call. */
export function AIFields({ value, onChange, dailyLimit, defaults }: {
  value: StrategyCfg; onChange: (v: StrategyCfg) => void; dailyLimit?: number;
  defaults: Record<string, string | number | boolean>;
}) {
  const p = value.params;
  // Show the strategy's real defaults (e.g. the starter instructions) until they're changed.
  const get = <T,>(k: string, d: T): T => (p[k] ?? defaults[k] ?? d) as T;
  const set = (k: string, v: string | number | boolean) => onChange({ ...value, params: { ...p, [k]: v } });
  const perDay = (BARS_PER_DAY[value.timeframe] ?? 6) * Math.max(1, value.symbols.length);
  const gated = Boolean(String(get("entry_when", "")).trim());

  return (
    <div className="aifields">
      <Labeled label="Instructions" wide
        hint="Plain English: what to look for, what to avoid, when to sell. The model also sees prices, indicators and recent news."
        render={(id) => (
          <textarea id={id} className="input textarea" rows={6} maxLength={4000}
            value={String(get("instructions", ""))} onChange={(e) => set("instructions", e.target.value)} />
        )} />
      <div className="grid-fields">
        <Labeled label="Only consider buying when" hint={<>Optional rule, checked for free before asking. Names: {NAMES}; add <code>prev_</code> for the bar before.</>}
          render={(id) => (
            <input id={id} className="input input--mono" spellCheck={false} placeholder="rsi < 35 and close > ma_200"
              value={String(get("entry_when", ""))} onChange={(e) => set("entry_when", e.target.value)} />
          )} />
        <Labeled label="Only consider selling when" hint="Optional. Empty means it's asked on every new bar while holding."
          render={(id) => (
            <input id={id} className="input input--mono" spellCheck={false} placeholder="rsi > 65 or close < ma_20"
              value={String(get("exit_when", ""))} onChange={(e) => set("exit_when", e.target.value)} />
          )} />
        <Labeled label="Act only when at least" hint="How sure the model must say it is" render={(id) => (
          <NumberInput id={id} value={Math.round(Number(get("min_confidence", 0.65)) * 100)} suffix="% sure"
            onChange={(v) => set("min_confidence", Math.max(0, Math.min(100, v)) / 100)} />
        )} />
        <Labeled label="Bars to show it" hint="Recent price history in each question (5–120)" render={(id) => (
          <NumberInput id={id} value={Number(get("bars_shown", 30))} onChange={(v) => set("bars_shown", Math.round(v))} />
        )} />
      </div>
      <div className="toggles">
        <Toggle checked={Boolean(get("use_news", true))} onChange={(v) => set("use_news", v)}
          label="Include the last 3 days of news headlines (from Alpaca, free)" />
      </div>
      <p className="aifields__cost">
        <span aria-hidden="true">●</span> Up to about <b className="num">{perDay}</b> model calls a day at this bar size
        {gated ? " (fewer, since the buy rule filters first)" : ""}
        {dailyLimit ? <>; the daily limit across AI strategies is <b className="num">{dailyLimit}</b>, then they hold until tomorrow.</> : "."}
        {" "}Longer bars mean fewer, cheaper calls.
      </p>
      <p className="note">
        AI strategies can't be backtested: a model has already read what happened next, so replaying history makes it look
        brilliant. Run it in dry run first and read its decisions on the overview, then try paper.
      </p>
    </div>
  );
}
