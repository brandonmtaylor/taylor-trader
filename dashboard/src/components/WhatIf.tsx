import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { InvestEvery, Point, WhatIfResp } from "../api";
import { money0, pct } from "../format";
import { usePoll } from "../usePoll";
import { niceTicks } from "./EquityChart";

const EVERY_WORD: Record<InvestEvery, string> = { week: "every week", "2weeks": "every 2 weeks", month: "every month", once: "once" };
const M = { top: 14, right: 72, bottom: 24, left: 56 };
const H = 230;
const monthYear = (t: number) => new Date(t * 1000).toLocaleDateString(undefined, { month: "short", year: "numeric" });

/** Your plan's amount and schedule, run on the past: your mix and each ready-made one. */
export function WhatIf() {
  const [years, setYears] = useState(10);
  const poll = usePoll<WhatIfResp>(`/api/invest/whatif?years=${years}`, 3_600_000);
  const d = poll.data?.years === years ? poll.data : null;
  const [pick, setPick] = useState<string | null>(null);
  const rows = d?.mixes.filter((m) => m.result) ?? [];
  const shown = rows.find((m) => m.id === pick) ?? rows[0];
  const since = d ? new Date(`${d.start}T12:00:00`).getFullYear() : null;

  return (
    <section className="panel" aria-labelledby="whatif-title">
      <div className="panel__head">
        <div>
          <h2 id="whatif-title">What if you'd started {since ?? `${years} years ago`}?</h2>
          <p className="panel__sub">
            {d ? `${money0(d.amount)} ${EVERY_WORD[d.every]}` : "Your plan"}, run on real past prices for each mix.
            Dividends reinvested, no taxes. The past, not a forecast.
          </p>
        </div>
        <div className="segmented" role="group" aria-label="How far back">
          {[5, 10].map((y) => (
            <button key={y} type="button" aria-pressed={years === y} onClick={() => setYears(y)}>{y} years</button>
          ))}
        </div>
      </div>
      {poll.error && !d && <p className="empty">Can't load past prices: {poll.error}</p>}
      {!d && !poll.error && <p className="empty">Running the numbers…</p>}
      {d && (
        <>
          <div className="scroll">
            <table className="table table--tight whatif">
              <thead>
                <tr>
                  <th>Mix</th><th className="num">Put in</th><th className="num">Worth now</th>
                  <th className="num">Worst drop</th><th className="num">All at once</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m) => {
                  const r = m.result!;
                  const on = shown?.id === m.id;
                  return (
                    <tr key={m.id} className={on ? "is-on" : ""}>
                      <td>
                        <button type="button" className="whatif__pick" aria-pressed={on} onClick={() => setPick(m.id)}>
                          {m.name}
                        </button>
                        {m.short && <span className="whatif__since">since {new Date(`${r.from}T12:00:00`).toLocaleDateString(undefined, { month: "short", year: "numeric" })}</span>}
                      </td>
                      <td className="num">{money0(r.put_in)}</td>
                      <td className="num">
                        {money0(r.value)} <span className={r.gain_pct != null && r.gain_pct < 0 ? "is-down" : "is-up"}>{pct(r.gain_pct, true, 0)}</span>
                      </td>
                      <td className="num">{pct(r.worst_drop_pct, true, 0)}</td>
                      <td className="num">{money0(r.lump.value)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="field__hint">
            <b>Worst drop</b> is the biggest fall someone in the plan sat through, from a high to a low.{" "}
            <b>All at once</b> is the same total put in on the first day instead of spread out. It usually ends
            higher and swings harder at the start. A mix marked "since" couldn't go back further: one of its
            funds didn't exist yet.
          </p>
          {shown?.result && (
            <WorthChart value={shown.result.series.value} putIn={shown.result.series.put_in} name={shown.name} />
          )}
        </>
      )}
    </section>
  );
}

function WorthChart({ value, putIn, name }: { value: Point[]; putIn: Point[]; name: string }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const [hover, setHover] = useState<number | null>(null);
  const titleId = useId();
  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, e.contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);
  const n = Math.min(value.length, putIn.length);
  const maxV = useMemo(() => Math.max(...value.slice(0, n).map((p) => p.v), 1), [value, n]);
  const ticks = useMemo(() => niceTicks(0, maxV, 4).filter((t) => t >= 0), [maxV]);
  if (n < 2) return null;
  const top = Math.max(ticks[ticks.length - 1], maxV * 1.06);  // headroom so the line and its label fit
  const iw = w - M.left - M.right, ih = H - M.top - M.bottom;
  const x = (i: number) => M.left + (iw * i) / (n - 1);
  const y = (v: number) => M.top + ih * (1 - v / top);
  const path = (pts: Point[], step = false) => pts.slice(0, n).map((p, i) =>
    i === 0 ? `M${x(0).toFixed(1)},${y(p.v).toFixed(1)}`
      : step ? `H${x(i).toFixed(1)}V${y(p.v).toFixed(1)}` : `L${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const lv = value[n - 1].v, lp = putIn[n - 1].v;
  let yv = y(lv), yp = y(lp);
  if (Math.abs(yv - yp) < 14) {
    const mid = (yv + yp) / 2, valueAbove = yv <= yp;
    yv = mid + (valueAbove ? -7 : 7);
    yp = mid + (valueAbove ? 7 : -7);
  }
  const pickAt = (clientX: number, el: SVGSVGElement) => {
    const box = el.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * w;
    setHover(Math.min(n - 1, Math.max(0, Math.round(((px - M.left) / iw) * (n - 1)))));
  };
  const h = hover;
  const short = (v: number) => (v >= 1000 ? `$${Math.round(v / 1000)}k` : `$${Math.round(v)}`);
  return (
    <div className="whatif__chart">
      <ul className="legend">
        <li><span className="swatch swatch--1" />Worth <b>{money0(lv)}</b></li>
        <li><span className="swatch swatch--putin" />Put in <b>{money0(lp)}</b></li>
      </ul>
      <div className="chart" ref={wrap}>
        <svg width={w} height={H} viewBox={`0 0 ${w} ${H}`} tabIndex={0} role="img" aria-labelledby={titleId}
          onPointerMove={(e) => pickAt(e.clientX, e.currentTarget)} onPointerLeave={() => setHover(null)}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? n) - 1));
            if (e.key === "ArrowRight") setHover((i) => Math.min(n - 1, (i ?? -1) + 1));
            if (e.key === "Escape") setHover(null);
          }}
          onBlur={() => setHover(null)}>
          <title id={titleId}>{`${name}: put in ${money0(lp)}, worth ${money0(lv)} at the end. Arrow keys read each month.`}</title>
          {ticks.map((t) => (
            <g key={t}>
              <line className={t === 0 ? "grid grid--base" : "grid"} x1={M.left} x2={w - M.right} y1={y(t)} y2={y(t)} />
              <text className="axis" x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end">{short(t)}</text>
            </g>
          ))}
          <text className="axis" x={x(0)} y={H - 4}>{monthYear(value[0].t)}</text>
          <text className="axis" x={x(n - 1)} y={H - 4} textAnchor="end">{monthYear(value[n - 1].t)}</text>
          <path className="line line--putin" d={path(putIn, true)} />
          <path className="line line--1" d={path(value)} />
          <text className="endlabel" x={x(n - 1) + 8} y={yv} dy="0.32em">Worth</text>
          <text className="endlabel endlabel--muted" x={x(n - 1) + 8} y={yp} dy="0.32em">Put in</text>
          {h != null && (
            <g className="crosshair">
              <line x1={x(h)} x2={x(h)} y1={M.top} y2={M.top + ih} />
              <circle className="dot dot--putin" cx={x(h)} cy={y(putIn[h].v)} r="4" />
              <circle className="dot dot--1" cx={x(h)} cy={y(value[h].v)} r="4.5" />
            </g>
          )}
        </svg>
        {h != null && (
          <div className="tooltip" style={{ left: Math.min(x(h) + 12, w - 170), top: M.top }} aria-live="polite">
            <div className="tooltip__date">{monthYear(value[h].t)}</div>
            <div className="tooltip__row"><span className="swatch swatch--1" /><b>{money0(value[h].v)}</b><span className="tooltip__label">worth</span></div>
            <div className="tooltip__row"><span className="swatch swatch--putin" /><b>{money0(putIn[h].v)}</b><span className="tooltip__label">put in</span></div>
          </div>
        )}
      </div>
    </div>
  );
}
