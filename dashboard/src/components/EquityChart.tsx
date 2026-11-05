import { useEffect, useMemo, useRef, useState } from "react";
import type { Performance } from "../api";
import { day, money, pct } from "../format";

type Period = Performance["period"];
const PERIODS: { id: Period; label: string }[] = [
  { id: "1M", label: "1 month" },
  { id: "3M", label: "3 months" },
  { id: "1A", label: "1 year" },
];
const M = { top: 12, right: 64, bottom: 26, left: 44 };
const H = 260;

export function niceTicks(lo: number, hi: number, count = 4): number[] {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

export function EquityChart({
  perf, period, onPeriod, error,
}: { perf: Performance | null; period: Period; onPeriod: (p: Period) => void; error: string | null }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, e.contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);

  const rows = useMemo(() => {
    if (!perf || perf.equity.length < 2) return [];
    const bench = new Map(perf.benchmark_series.map((p) => [p.t, p.v]));
    const e0 = perf.equity[0].v;
    const b0 = perf.benchmark_series[0]?.v;
    return perf.equity.map((p) => {
      const b = bench.get(p.t);
      return { t: p.t, equity: p.v, acct: (p.v / e0) * 100, bench: b && b0 ? (b / b0) * 100 : null };
    });
  }, [perf]);

  const innerW = w - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const vals = rows.flatMap((r) => (r.bench == null ? [r.acct] : [r.acct, r.bench]));
  const lo = Math.min(...vals, 100);
  const hi = Math.max(...vals, 100);
  const pad = (hi - lo) * 0.08 || 1;
  const y0 = lo - pad;
  const y1 = hi + pad;
  const x = (i: number) => M.left + (rows.length > 1 ? (innerW * i) / (rows.length - 1) : 0);
  const y = (v: number) => M.top + innerH * (1 - (v - y0) / (y1 - y0));
  // Gaps in a series (missing benchmark days) lift the pen instead of drawing across them.
  const path = (key: "acct" | "bench") => {
    let d = "";
    let pen = false;
    rows.forEach((r, i) => {
      const v = r[key];
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  };

  const last = rows[rows.length - 1];
  const hasBench = rows.some((r) => r.bench != null);
  const h = hover != null ? rows[hover] : null;

  const pick = (clientX: number, el: SVGSVGElement) => {
    const box = el.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * w;
    const i = Math.round(((px - M.left) / innerW) * (rows.length - 1));
    setHover(Math.min(rows.length - 1, Math.max(0, i)));
  };

  return (
    <section className="panel panel--chart" aria-labelledby="perf-title">
      <div className="panel__head">
        <div>
          <h2 id="perf-title">Growth of $100</h2>
          <p className="panel__sub">
            Your account vs. {perf?.benchmark ?? "SPY"} (buy-and-hold), daily close
          </p>
        </div>
        <div className="segmented" role="group" aria-label="Time range">
          {PERIODS.map((p) => (
            <button key={p.id} aria-pressed={period === p.id} onClick={() => onPeriod(p.id)}>
              {p.label}
            </button>
          ))}
        </div>
      </div>

      {rows.length > 0 && (
        <ul className="legend">
          <li><span className="swatch swatch--1" />Your account <b>{pct(last.acct - 100)}</b></li>
          {hasBench && last.bench != null && (
            <li><span className="swatch swatch--2" />{perf?.benchmark} <b>{pct(last.bench - 100)}</b></li>
          )}
        </ul>
      )}

      <div className="chart" ref={wrap}>
        {error && <p className="empty">Can't load performance: {error}</p>}
        {!error && rows.length === 0 && (
          <p className="empty">No account history for this period yet. It fills in one point per trading day.</p>
        )}
        {rows.length > 0 && (
          <>
            <svg
              width={w}
              height={H}
              viewBox={`0 0 ${w} ${H}`}
              tabIndex={0}
              role="img"
              aria-label={`Growth of $100 over ${period}: account ${pct(last.acct - 100)}${
                last.bench != null ? `, ${perf?.benchmark} ${pct(last.bench - 100)}` : ""
              }. Use left and right arrows to read values.`}
              onPointerMove={(e) => pick(e.clientX, e.currentTarget)}
              onPointerLeave={() => setHover(null)}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? rows.length) - 1));
                if (e.key === "ArrowRight") setHover((i) => Math.min(rows.length - 1, (i ?? -1) + 1));
                if (e.key === "Escape") setHover(null);
              }}
              onBlur={() => setHover(null)}
            >
              {niceTicks(y0, y1).map((t) => (
                <g key={t}>
                  <line className={t === 100 ? "grid grid--base" : "grid"} x1={M.left} x2={w - M.right} y1={y(t)} y2={y(t)} />
                  <text className="axis" x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end">${t}</text>
                </g>
              ))}
              {[0, Math.floor((rows.length - 1) / 2), rows.length - 1].map((i) => (
                <text key={i} className="axis" x={x(i)} y={H - 6}
                  textAnchor={i === 0 ? "start" : i === rows.length - 1 ? "end" : "middle"}>
                  {day(rows[i].t)}
                </text>
              ))}
              {hasBench && <path className="line line--2" d={path("bench")} />}
              <path className="line line--1" d={path("acct")} />
              {/* direct labels at the line ends */}
              <text className="endlabel" x={x(rows.length - 1) + 8} y={y(last.acct)} dy="0.32em">You</text>
              {last.bench != null && (
                <text className="endlabel endlabel--muted" x={x(rows.length - 1) + 8} y={y(last.bench)} dy="0.32em">
                  {perf?.benchmark}
                </text>
              )}
              {h && (
                <g className="crosshair">
                  <line x1={x(hover!)} x2={x(hover!)} y1={M.top} y2={M.top + innerH} />
                  {h.bench != null && <circle className="dot dot--2" cx={x(hover!)} cy={y(h.bench)} r="4.5" />}
                  <circle className="dot dot--1" cx={x(hover!)} cy={y(h.acct)} r="4.5" />
                </g>
              )}
            </svg>
            {h && (
              <div
                className="tooltip"
                style={{ left: Math.min(x(hover!) + 12, w - 190), top: M.top }}
                aria-live="polite"
              >
                <div className="tooltip__date">{day(h.t)}</div>
                <div className="tooltip__row">
                  <span className="swatch swatch--1" />
                  <b>{pct(h.acct - 100)}</b>
                  <span className="tooltip__label">You · {money(h.equity)}</span>
                </div>
                {h.bench != null && (
                  <div className="tooltip__row">
                    <span className="swatch swatch--2" />
                    <b>{pct(h.bench - 100)}</b>
                    <span className="tooltip__label">{perf?.benchmark}</span>
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </div>

      {rows.length > 0 && (
        <details className="tableview">
          <summary>Show as table</summary>
          <table>
            <thead>
              <tr><th>Date</th><th>Equity</th><th>You</th>{hasBench && <th>{perf?.benchmark}</th>}</tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.t}>
                  <td>{day(r.t)}</td>
                  <td className="num">{money(r.equity)}</td>
                  <td className="num">{pct(r.acct - 100)}</td>
                  {hasBench && <td className="num">{r.bench == null ? "—" : pct(r.bench - 100)}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  );
}
