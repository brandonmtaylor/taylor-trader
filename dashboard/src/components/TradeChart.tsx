import { useEffect, useMemo, useRef, useState } from "react";
import type { ChartPeriod, TradeChartData } from "../api";
import { money, pct, qty } from "../format";
import { niceTicks } from "./EquityChart";

const PERIODS: { id: ChartPeriod; label: string }[] = [
  { id: "1W", label: "1 week" },
  { id: "1M", label: "1 month" },
  { id: "3M", label: "3 months" },
  { id: "1A", label: "1 year" },
];
const M = { top: 14, right: 70, bottom: 26, left: 52 };
const H = 280;
const NY = "America/New_York";

function when(t: number, daily: boolean): string {
  const d = new Date(t * 1000);
  return daily
    ? d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" })
    : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: NY });
}

const dateOf = (iso: string) => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: NY });

function Tri({ x, y, up, cls }: { x: number; y: number; up: boolean; cls: string }) {
  // Points at the fill price from below (buy) or above (sell), so the tip marks the exact level.
  const s = 7;
  const tip = up ? y + 3 : y - 3;
  const base = up ? tip + s * 1.5 : tip - s * 1.5;
  return <path className={cls} d={`M${x},${tip} L${x - s},${base} L${x + s},${base} Z`} />;
}

export function TradeChart({
  data, error, symbol, period, onSymbol, onPeriod,
}: {
  data: TradeChartData | null; error: string | null; symbol: string; period: ChartPeriod;
  onSymbol: (s: string) => void; onPeriod: (p: ChartPeriod) => void;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    if (!wrap.current) return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, e.contentRect.width)));
    ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);
  useEffect(() => setHover(null), [symbol, period]);

  const pts = data?.points ?? [];
  const marks = data?.marks ?? [];
  const ai = data?.ai ?? [];
  const pos = data?.position ?? null;
  const daily = data?.timeframe === "1Day";
  const shown = data?.symbol ?? symbol;

  const scale = useMemo(() => {
    const vals = [...pts.map((p) => p.c), ...marks.map((m) => m.price)];
    if (pos?.stop) vals.push(pos.stop);
    if (pos?.entry) vals.push(pos.entry);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = (hi - lo) * 0.08 || hi * 0.01 || 1;
    return { y0: lo - pad, y1: hi + pad };
  }, [pts, marks, pos]);

  const innerW = w - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const x = (i: number) => M.left + (pts.length > 1 ? (innerW * i) / (pts.length - 1) : innerW / 2);
  const y = (v: number) => M.top + innerH * (1 - (v - scale.y0) / (scale.y1 - scale.y0));
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join(" ");

  const pick = (clientX: number, el: SVGSVGElement) => {
    const box = el.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * w;
    const i = Math.round(((px - M.left) / innerW) * (pts.length - 1));
    setHover(Math.min(pts.length - 1, Math.max(0, i)));
  };
  const near = (i: number) => (m: { i: number }) => Math.abs(m.i - i) <= Math.max(0, Math.round(pts.length / 150));
  const hp = hover != null ? pts[hover] : null;
  const hMarks = hover != null ? marks.filter(near(hover)) : [];
  const hAI = hover != null ? ai.filter(near(hover)) : [];

  const trades = data?.trades ?? [];
  const symbols = data?.symbols ?? [];

  return (
    <section className="panel panel--chart" aria-labelledby="tc-title">
      <div className="panel__head">
        <div>
          <h2 id="tc-title">Trades on the chart</h2>
          <p className="panel__sub">The price with every buy and sell, and how close each sell came to the top.</p>
        </div>
        <div className="tc__controls">
          {symbols.length > 0 && (
            <label className="tc__symbol">
              <span className="sr-only">Stock</span>
              <select className="input" value={shown ?? ""} onChange={(e) => onSymbol(e.target.value)}>
                {symbols.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </label>
          )}
          <div className="segmented" role="group" aria-label="Time range">
            {PERIODS.map((p) => (
              <button key={p.id} aria-pressed={period === p.id} onClick={() => onPeriod(p.id)}>{p.label}</button>
            ))}
          </div>
        </div>
      </div>

      {pts.length > 0 && (
        <ul className="legend legend--wrap">
          <li><span className="swatch swatch--1" />{shown}</li>
          <li><svg width="12" height="12" aria-hidden="true"><path className="mk mk--buy" d="M6,1 L11,10 L1,10 Z" /></svg>Buy</li>
          <li><svg width="12" height="12" aria-hidden="true"><path className="mk mk--sell" d="M6,11 L11,2 L1,2 Z" /></svg>Sell</li>
          {ai.length > 0 && <li><svg width="12" height="12" aria-hidden="true"><circle className="mk mk--ai" cx="6" cy="6" r="3.5" /></svg>AI call</li>}
          {pos?.stop && <li><span className="swatch swatch--stop" />Stop-loss</li>}
        </ul>
      )}

      <div className="chart" ref={wrap}>
        {error && <p className="empty">Can't load the chart: {error}</p>}
        {!error && data && symbols.length === 0 && (
          <p className="empty">Nothing to chart yet. Stocks show up here once a strategy watches them or the bot trades them.</p>
        )}
        {!error && data && symbols.length > 0 && pts.length === 0 && (
          <p className="empty">No price history for {shown} in this period.</p>
        )}
        {pts.length > 0 && (
          <>
            <svg
              width={w}
              height={H}
              viewBox={`0 0 ${w} ${H}`}
              tabIndex={0}
              role="img"
              aria-label={`${shown} price over ${PERIODS.find((p) => p.id === period)?.label}, with ${marks.length} buy and sell marker${marks.length === 1 ? "" : "s"}. Use left and right arrows to read values.`}
              onPointerMove={(e) => pick(e.clientX, e.currentTarget)}
              onPointerLeave={() => setHover(null)}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? pts.length) - 1));
                if (e.key === "ArrowRight") setHover((i) => Math.min(pts.length - 1, (i ?? -1) + 1));
                if (e.key === "Escape") setHover(null);
              }}
              onBlur={() => setHover(null)}
            >
              {niceTicks(scale.y0, scale.y1).map((t) => (
                <g key={t}>
                  <line className="grid" x1={M.left} x2={w - M.right} y1={y(t)} y2={y(t)} />
                  <text className="axis" x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end">${t.toLocaleString("en-US")}</text>
                </g>
              ))}
              {[0, Math.floor((pts.length - 1) / 2), pts.length - 1].map((i, k) => (
                <text key={k} className="axis" x={x(i)} y={H - 6}
                  textAnchor={k === 0 ? "start" : k === 2 ? "end" : "middle"}>
                  {period === "1A"
                    ? new Date(pts[i].t * 1000).toLocaleDateString("en-US", { month: "short", year: "numeric", timeZone: "UTC" })
                    : when(pts[i].t, true).replace(/, \d{4}$/, "")}
                </text>
              ))}

              {pos?.stop && (
                <g>
                  <line className="level level--stop" x1={M.left} x2={w - M.right} y1={y(pos.stop)} y2={y(pos.stop)} />
                  <text className="levellabel levellabel--stop" x={M.left + 6} y={y(pos.stop)} dy="-0.45em">Stop {money(pos.stop)}</text>
                </g>
              )}
              {pos?.entry && (
                <g>
                  <line className="level" x1={M.left} x2={w - M.right} y1={y(pos.entry)} y2={y(pos.entry)} />
                  <text className="levellabel" x={M.left + 6} y={y(pos.entry)} dy={pos.entry > (pts[0]?.c ?? 0) ? "1.15em" : "-0.45em"}>Bought {money(pos.entry)}</text>
                </g>
              )}

              <path className="line line--1" d={line} />
              {ai.map((a, k) => (
                <circle key={`a${k}`} className={`mk mk--ai${a.acted ? " mk--acted" : ""}`} cx={x(a.i)} cy={y(pts[a.i].c)} r="3.5" />
              ))}
              {marks.map((m, k) => (
                <Tri key={`m${k}`} x={x(m.i)} y={y(m.price)} up={m.side === "buy"} cls={`mk mk--${m.side}`} />
              ))}
              <text className="endlabel" x={x(pts.length - 1) + 8} y={y(pts[pts.length - 1].c)} dy="0.32em">
                {money(pts[pts.length - 1].c)}
              </text>

              {hp && (
                <g className="crosshair">
                  <line x1={x(hover!)} x2={x(hover!)} y1={M.top} y2={M.top + innerH} />
                  <circle className="dot dot--1" cx={x(hover!)} cy={y(hp.c)} r="4.5" />
                </g>
              )}
            </svg>
            {hp && (
              <div className="tooltip tooltip--wide" style={{ left: Math.max(0, Math.min(x(hover!) + 12, w - 250)), top: M.top }} aria-live="polite">
                <div className="tooltip__date">{when(hp.t, daily)}{daily ? "" : " ET"}</div>
                <div className="tooltip__row"><span className="swatch swatch--1" /><b>{money(hp.c)}</b><span className="tooltip__label">{shown}</span></div>
                {hMarks.map((m, k) => (
                  <div key={k} className="tooltip__row">
                    <span className={`tooltip__mk tooltip__mk--${m.side}`} aria-hidden="true">{m.side === "buy" ? "▲" : "▼"}</span>
                    <b>{m.side === "buy" ? "Bought" : "Sold"} {qty(m.qty)} @ {money(m.price)}</b>
                    {m.strategy && <span className="tooltip__label">{m.strategy}</span>}
                  </div>
                ))}
                {hAI.slice(0, 2).map((a, k) => (
                  <div key={`a${k}`} className="tooltip__ai">
                    <b>AI {a.action}{a.confidence != null && ` · ${Math.round(a.confidence * 100)}%`}</b>
                    {a.summary && <span> {a.summary}</span>}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {pos && (
        <p className="tc__open">
          <b>Holding {qty(pos.qty)} {shown}</b>
          {pos.entry != null && <> from {money(pos.entry)}{pos.opened && ` (${dateOf(pos.opened)})`}</>}
          {pos.pct != null && <>, now <span className={pos.pct < 0 ? "is-down" : "is-up"}>{pct(pos.pct, true, 1)}</span></>}
          {pos.peak_pct != null && <>. Best since buying: {pct(pos.peak_pct, true, 1)}</>}
          {pos.stop != null && pos.entry != null && <>. Stop at {money(pos.stop)} ({pct((pos.stop / pos.entry - 1) * 100, true, 1)})</>}.
        </p>
      )}

      {trades.length > 0 && (
        <>
          <h3 className="subhead">How each sell was timed</h3>
          <div className="scroll">
            <table className="table">
              <thead>
                <tr>
                  <th>Trade</th>
                  <th className="num">Result</th>
                  <th className="num" title="Highest price while it was held, vs. the buy">Best while held</th>
                  <th className="num" title="Share of that best gain the sell kept">Kept</th>
                  <th className="num" title="Highest price in the week after selling, vs. the sell">Week after sell</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((t, k) => (
                  <tr key={k}>
                    <td>
                      {dateOf(t.opened)} → {dateOf(t.closed)}
                      <span className="tc__sub"> {money(t.entry)} → {money(t.exit)}{t.strategy ? ` · ${t.strategy}` : " · not the bot"}</span>
                    </td>
                    <td className={`num ${t.pct < 0 ? "is-down" : "is-up"}`}>{pct(t.pct, true, 1)}</td>
                    <td className="num">{t.peak_pct == null ? <span className="muted" title="Bought before this period; pick a longer range">—</span> : pct(t.peak_pct, true, 1)}</td>
                    <td className="num">{t.captured == null ? "—" : `${Math.max(-999, Math.round(t.captured))}%`}</td>
                    <td className="num">
                      {t.after_pct == null ? "—" : <>up to {pct(t.after_pct, true, 1)}{!t.after_complete && <span className="muted"> so far</span>}</>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="note">
            <b>Kept</b> is how much of the best possible gain the sell locked in (100% means it sold at the top).
            <b> Week after sell</b> shows how much higher it went after selling, so you can see if sells come too early.
          </p>
        </>
      )}
    </section>
  );
}
