import { useEffect, useMemo, useState } from "react";
import type { ChartPeriod, TradeChartData } from "../api";
import { money, pct, qty } from "../format";
import { useChartWidth } from "../useChartWidth";
import { niceTicks } from "./EquityChart";

const PERIODS: { id: ChartPeriod; label: string }[] = [
  { id: "1W", label: "1 week" },
  { id: "1M", label: "1 month" },
  { id: "3M", label: "3 months" },
  { id: "1A", label: "1 year" },
];
const M = { top: 14, right: 70, bottom: 26, left: 52 };
const H = 280;
const STRIP = 84;   // height of the RSI / MACD strips under the price
const GAP = 24;

type LineKey = "ma_20" | "ma_200" | "rsi" | "macd";
const LINE_OPTS: { id: LineKey; label: string; hint: string }[] = [
  { id: "ma_20", label: "20-bar avg", hint: "Average of the last 20 bars: the short trend" },
  { id: "ma_200", label: "200-bar avg", hint: "Average of the last 200 bars: the long trend" },
  { id: "rsi", label: "RSI", hint: "Momentum from 0 to 100. Under 30 is oversold, over 70 overbought" },
  { id: "macd", label: "MACD", hint: "Momentum: the line crossing above its signal means it's turning up" },
];
const LINES_KEY = "tt.chart.lines";

function loadLines(): Set<LineKey> {
  try {
    const v = JSON.parse(localStorage.getItem(LINES_KEY) ?? "[]");
    return new Set(Array.isArray(v) ? v.filter((k) => LINE_OPTS.some((o) => o.id === k)) : []);
  } catch {
    return new Set();
  }
}
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
  const { wrap, w } = useChartWidth();
  const [hover, setHover] = useState<number | null>(null);
  const [lines, setLines] = useState<Set<LineKey>>(loadLines);
  const toggle = (k: LineKey) => {
    const next = new Set(lines);
    if (next.has(k)) next.delete(k);
    else next.add(k);
    setLines(next);
    try {
      localStorage.setItem(LINES_KEY, JSON.stringify([...next]));
    } catch {
      /* private window: the choice just isn't remembered */
    }
  };

  useEffect(() => setHover(null), [symbol, period]);

  const pts = data?.points ?? [];
  const marks = data?.marks ?? [];
  const ai = data?.ai ?? [];
  const pos = data?.position ?? null;
  const daily = data?.timeframe === "1Day";
  const shown = data?.symbol ?? symbol;

  const scale = useMemo(() => {
    const vals = [...pts.map((p) => p.c), ...marks.map((m) => m.price)];
    for (const k of ["ma_20", "ma_200"] as const) {
      if (lines.has(k)) pts.forEach((p) => p[k] != null && vals.push(p[k]!));
    }
    if (pos?.stop) vals.push(pos.stop);
    if (pos?.target) vals.push(pos.target);
    if (pos?.entry) vals.push(pos.entry);
    const lo = Math.min(...vals);
    const hi = Math.max(...vals);
    const pad = (hi - lo) * 0.08 || hi * 0.01 || 1;
    const macdMax = Math.max(1e-9, ...pts.flatMap((p) => [p.macd, p.macd_signal, p.macd_diff].map((v) => Math.abs(v ?? 0))));
    return { y0: lo - pad, y1: hi + pad, macdMax };
  }, [pts, marks, pos, lines]);

  const innerW = w - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const x = (i: number) => M.left + (pts.length > 1 ? (innerW * i) / (pts.length - 1) : innerW / 2);
  const y = (v: number) => M.top + innerH * (1 - (v - scale.y0) / (scale.y1 - scale.y0));
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join(" ");
  // Lifts the pen where a value is missing (the 200-bar average needs 200 bars before it starts).
  const pathOf = (get: (p: (typeof pts)[number]) => number | undefined, yf: (v: number) => number) => {
    let d = "";
    let pen = false;
    pts.forEach((p, i) => {
      const v = get(p);
      if (v == null) {
        pen = false;
        return;
      }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${yf(v).toFixed(1)} `;
      pen = true;
    });
    return d.trim();
  };
  const strips = (["rsi", "macd"] as const).filter((k) => lines.has(k));
  const stripTop = (k: "rsi" | "macd") => M.top + innerH + GAP + strips.indexOf(k) * (STRIP + GAP);
  const totalH = H + strips.length * (STRIP + GAP);
  const bottom = strips.length ? stripTop(strips[strips.length - 1]) + STRIP : M.top + innerH;
  const yRsi = (v: number) => stripTop("rsi") + STRIP * (1 - v / 100);
  const yMacd = (v: number) => stripTop("macd") + STRIP / 2 - (STRIP / 2 - 4) * (v / scale.macdMax);
  const barW = Math.max(1, (innerW / Math.max(1, pts.length)) * 0.6);

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
          {pos?.target && <li><span className="swatch swatch--target" />Take-profit</li>}
        </ul>
      )}
      {pts.length > 0 && (
        <div className="chips" role="group" aria-label="Indicator lines">
          <span className="chips__label">Show</span>
          {LINE_OPTS.map((o) => (
            <button key={o.id} className="chip" aria-pressed={lines.has(o.id)} title={o.hint} onClick={() => toggle(o.id)}>
              {o.label}
            </button>
          ))}
        </div>
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
              height={totalH}
              viewBox={`0 0 ${w} ${totalH}`}
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
                <text key={k} className="axis" x={x(i)} y={totalH - 6}
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
              {pos?.target && (
                <g>
                  <line className="level level--target" x1={M.left} x2={w - M.right} y1={y(pos.target)} y2={y(pos.target)} />
                  <text className="levellabel levellabel--target" x={M.left + 6} y={y(pos.target)} dy="1.15em">Take-profit {money(pos.target)}</text>
                </g>
              )}
              {pos?.entry && (
                <g>
                  <line className="level" x1={M.left} x2={w - M.right} y1={y(pos.entry)} y2={y(pos.entry)} />
                  <text className="levellabel" x={M.left + 6} y={y(pos.entry)} dy={pos.entry > (pts[0]?.c ?? 0) ? "1.15em" : "-0.45em"}>Bought {money(pos.entry)}</text>
                </g>
              )}

              {lines.has("ma_200") && <path className="ind ind--ma200" d={pathOf((p) => p.ma_200, y)} />}
              {lines.has("ma_20") && <path className="ind ind--ma20" d={pathOf((p) => p.ma_20, y)} />}
              <path className="line line--1" d={line} />
              {(["ma_20", "ma_200"] as const).filter((k) => lines.has(k)).map((k) => {
                const lastV = [...pts].reverse().find((p) => p[k] != null)?.[k];
                return lastV == null ? null : (
                  <text key={k} className="indlabel" x={M.left + innerW - 4} y={y(lastV)} dy={k === "ma_20" ? "-0.5em" : "1.1em"} textAnchor="end">
                    {k === "ma_20" ? "20-bar avg" : "200-bar avg"}
                  </text>
                );
              })}

              {lines.has("rsi") && (
                <g aria-hidden="true">
                  <rect className="strip" x={M.left} y={stripTop("rsi")} width={innerW} height={STRIP} />
                  <rect className="strip__band" x={M.left} y={yRsi(70)} width={innerW} height={yRsi(30) - yRsi(70)} />
                  {[30, 70].map((v) => (
                    <g key={v}>
                      <line className="grid grid--base" x1={M.left} x2={w - M.right} y1={yRsi(v)} y2={yRsi(v)} />
                      <text className="axis" x={M.left - 8} y={yRsi(v)} dy="0.32em" textAnchor="end">{v}</text>
                    </g>
                  ))}
                  <text className="striplabel" x={M.left} y={stripTop("rsi") - 7}>RSI</text>
                  <path className="ind ind--rsi" d={pathOf((p) => p.rsi, yRsi)} />
                </g>
              )}
              {lines.has("macd") && (
                <g aria-hidden="true">
                  <rect className="strip" x={M.left} y={stripTop("macd")} width={innerW} height={STRIP} />
                  <line className="grid grid--base" x1={M.left} x2={w - M.right} y1={yMacd(0)} y2={yMacd(0)} />
                  <text className="axis" x={M.left - 8} y={yMacd(0)} dy="0.32em" textAnchor="end">0</text>
                  {pts.map((p, i) => p.macd_diff == null ? null : (
                    <rect key={i} className={p.macd_diff >= 0 ? "hist hist--up" : "hist hist--down"}
                      x={x(i) - barW / 2} width={barW}
                      y={Math.min(yMacd(0), yMacd(p.macd_diff))} height={Math.abs(yMacd(p.macd_diff) - yMacd(0))} />
                  ))}
                  <path className="ind ind--signal" d={pathOf((p) => p.macd_signal, yMacd)} />
                  <path className="ind ind--macd" d={pathOf((p) => p.macd, yMacd)} />
                  <text className="striplabel" x={M.left} y={stripTop("macd") - 7}>
                    MACD <tspan className="striplabel__key">— line</tspan> <tspan className="striplabel__key striplabel__key--sig">- - signal</tspan>
                  </text>
                </g>
              )}
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
                  <line x1={x(hover!)} x2={x(hover!)} y1={M.top} y2={bottom} />
                  <circle className="dot dot--1" cx={x(hover!)} cy={y(hp.c)} r="4.5" />
                </g>
              )}
            </svg>
            {hp && (
              <div className="tooltip tooltip--wide" style={{ left: Math.max(0, Math.min(x(hover!) + 12, w - 250)), top: M.top }} aria-live="polite">
                <div className="tooltip__date">{when(hp.t, daily)}{daily ? "" : " ET"}</div>
                <div className="tooltip__row"><span className="swatch swatch--1" /><b>{money(hp.c)}</b><span className="tooltip__label">{shown}</span></div>
                {lines.size > 0 && (
                  <div className="tooltip__inds">
                    {lines.has("ma_20") && hp.ma_20 != null && <span>20-bar avg {money(hp.ma_20)}</span>}
                    {lines.has("ma_200") && hp.ma_200 != null && <span>200-bar avg {money(hp.ma_200)}</span>}
                    {lines.has("rsi") && hp.rsi != null && <span>RSI {hp.rsi.toFixed(0)}</span>}
                    {lines.has("macd") && hp.macd_diff != null && (
                      <span>MACD {hp.macd_diff >= 0 ? "above" : "below"} signal</span>
                    )}
                  </div>
                )}
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
          {pos.stop != null && pos.entry != null && <>. Stop at {money(pos.stop)} ({pct((pos.stop / pos.entry - 1) * 100, true, 1)})</>}
          {pos.target != null && pos.entry != null && <>, take-profit at {money(pos.target)} ({pct((pos.target / pos.entry - 1) * 100, true, 1)})</>}.
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
                    <td className="num">{t.captured == null ? "—" : t.pct < 0
                      ? <span className="muted" title="Sold at a loss, so none of the best gain was kept">none</span>
                      : `${Math.round(t.captured)}%`}</td>
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
