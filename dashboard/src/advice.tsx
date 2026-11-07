import type { ReactNode } from "react";

/** Starting points for the safety and setup settings, with the reason in one line, and a gentle
 *  warning past a sensible level. Never advice on what to buy: that's the user's call. */
type Advice = { recommended: number; why: string; warnAbove?: number; warnBelow?: number; warn: string };

export const LIMIT_ADVICE: Record<string, Advice> = {
  max_position_pct: { recommended: 25, why: "one stock going wrong can't sink the account", warnAbove: 25,
    warn: "Over 25% in one stock: a single bad day in it takes a big bite out of the account." },
  max_total_exposure_pct: { recommended: 90, why: "keeps some cash free for the next buy", warnAbove: 95,
    warn: "Fully invested leaves no cash for new buys until something sells." },
  max_daily_loss_pct: { recommended: 3, why: "a bad day stops new buys early", warnAbove: 5,
    warn: "Above 5%, a bad day can cost a lot before the bot stops buying." },
  max_trades_per_day: { recommended: 10, why: "plenty for most strategies", warnAbove: 20,
    warn: "Many buys a day usually means a strategy is reacting to noise, and costs add up." },
  max_stop_loss_pct: { recommended: 15, why: "no single trade can lose too much", warnAbove: 15,
    warn: "Stops wider than 15% let one trade lose a lot before it's sold." },
};

/** For one strategy's own numbers. */
export const STRATEGY_ADVICE: Record<"allocation_pct" | "stop_loss_pct", Advice> = {
  allocation_pct: { recommended: 10, why: "start small, 5–25% per buy", warnAbove: 25,
    warn: "Over 25% per buy puts a lot in each trade. Fine to test on paper; think twice with real money." },
  stop_loss_pct: { recommended: 8, why: "5–12% suits most stocks", warnAbove: 15, warnBelow: 3,
    warn: "" },
};

export function adviceWarning(a: Advice, value: number): string | null {
  if (a.warnAbove !== undefined && value > a.warnAbove) return a.warn || "Wider than most stocks need: one trade can lose a lot.";
  if (a.warnBelow !== undefined && value < a.warnBelow) return "Very tight: normal day-to-day moves may sell it.";
  return null;
}

/** "Recommended: 25% (why)", then a warning line if the value is past a sensible level. */
export function AdviceHint({ advice, value, suffix = "%", extra }: {
  advice: Advice; value: number; suffix?: string; extra?: ReactNode;
}) {
  const warning = adviceWarning(advice, value);
  return (
    <>
      <span>Recommended: {advice.recommended}{suffix} ({advice.why}){extra}</span>
      {warning && <span className="field__warn" role="status">{warning}</span>}
    </>
  );
}
