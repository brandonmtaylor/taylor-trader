import { useEffect, useId, useMemo, useRef, useState } from "react";
import { send, type BacktestResult, type CatalogItem, type Limits, type Point, type StrategyCfg } from "../api";
import { day, pct } from "../format";
import { useChartWidth } from "../useChartWidth";
import { AIFields } from "./AIFields";
import { Labeled, NoticeLine, NumberInput, Toggle, useAction } from "./forms";

const TF_LABEL: Record<string, string> = {
  "1Min": "1 minute", "5Min": "5 minutes", "15Min": "15 minutes", "1Hour": "1 hour", "1Day": "1 day",
};

const RULE_NAMES = "close, open, high, low, volume, macd, macd_signal, macd_diff, chaikin, and at any length up to 250 bars: ma_50, ema_20, high_20, low_20, change_5 (percent), avg_volume_20, rsi_14, atr_14";

/** "rsi_buy" -> "RSI buy", "legacy_crossover" -> "Legacy crossover" */
function paramLabel(k: string): string {
  const words = k.split("_").map((w) => (["rsi", "macd", "ma"].includes(w) ? w.toUpperCase() : w));
  const s = words.join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function parseSymbols(text: string): string[] {
  return text.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
}

export function StrategyEditor({ value, onChange, onRemove, catalog, limits, timeframes, index, aiDailyLimit }: {
  value: StrategyCfg;
  onChange: (v: StrategyCfg) => void;
  onRemove?: () => void;
  catalog: CatalogItem[];
  limits: Limits;
  timeframes: string[];
  index: number;
  aiDailyLimit?: number;
}) {
  const isAI = value.name === "ai";
  const entry = catalog.find((c) => c.name === value.name);
  const [symText, setSymText] = useState(value.symbols.join(", "));
  const lastSyms = useRef(value.symbols.join(","));
  useEffect(() => {
    // keep the text box in sync when the value changes from outside (e.g. reset after save)
    if (value.symbols.join(",") !== lastSyms.current) {
      lastSyms.current = value.symbols.join(",");
      setSymText(value.symbols.join(", "));
    }
  }, [value.symbols]);
  const set = (patch: Partial<StrategyCfg>) => onChange({ ...value, ...patch });

  return (
    <article className={`strat ${value.enabled ? "" : "strat--off"}`} aria-label={`Strategy ${index + 1}: ${value.name}`}>
      <div className="strat__head">
        <Labeled label="Strategy" render={(id) => (
          <select id={id} className="input" value={value.name}
            onChange={(e) => {
              const name = e.target.value;
              // AI calls cost money and take seconds: start it on hourly bars instead of minutes.
              const slow = name === "ai" && ["1Min", "5Min", "15Min"].includes(value.timeframe);
              set({ name, params: {}, ...(slow ? { timeframe: "1Hour" } : {}) });
            }}>
            {catalog.map((c) => <option key={c.name} value={c.name}>{c.name === "ai" ? "ai (experimental)" : c.name}</option>)}
          </select>
        )} />
        <div className="strat__toggles">
          <Toggle checked={value.enabled} onChange={(enabled) => set({ enabled })} label={value.enabled ? "On" : "Off"} />
          {onRemove && <button type="button" className="btn btn--quiet btn--danger" onClick={onRemove}>Remove</button>}
        </div>
      </div>
      {entry && <p className="strat__desc">{entry.description}</p>}

      <div className="grid-fields">
        <Labeled label="Symbols" wide hint="Comma or space separated. Each symbol can belong to one strategy." render={(id) => (
          <input id={id} className="input input--mono" value={symText} spellCheck={false} autoCapitalize="characters"
            onChange={(e) => {
              setSymText(e.target.value);
              const syms = parseSymbols(e.target.value);
              lastSyms.current = syms.join(",");
              set({ symbols: syms });
            }} />
        )} />
        <Labeled label="Bar size" hint="How often it decides" render={(id) => (
          <select id={id} className="input" value={value.timeframe} onChange={(e) => set({ timeframe: e.target.value })}>
            {timeframes.map((t) => <option key={t} value={t}>{TF_LABEL[t] ?? t}</option>)}
          </select>
        )} />
        <Labeled label="Position size" hint={`% of your account per buy · max ${limits.max_position_pct}%`} render={(id) => (
          <NumberInput id={id} value={value.allocation_pct} suffix="%" onChange={(allocation_pct) => set({ allocation_pct })} />
        )} />
        <Labeled label="Stop-loss" hint={`Sell if it falls this far · max ${limits.max_stop_loss_pct}%`} render={(id) => (
          <NumberInput id={id} value={value.stop_loss_pct} suffix="%" onChange={(stop_loss_pct) => set({ stop_loss_pct })} />
        )} />
        <Labeled label="Take-profit" hint="Sell once up this much · 0 = off" render={(id) => (
          <NumberInput id={id} value={value.take_profit_pct} suffix="%" onChange={(take_profit_pct) => set({ take_profit_pct })} />
        )} />
      </div>

      {isAI && <AIFields value={value} onChange={onChange} dailyLimit={aiDailyLimit} defaults={entry?.params ?? {}} />}

      {!isAI && entry && Object.keys(entry.params).length > 0 && (
        <details className="params" open={value.name === "rule"}>
          <summary>{value.name === "rule" ? "Rules" : "Fine-tune"} <span className="params__count">{Object.keys(value.params).length} changed</span></summary>
          <div className="grid-fields">
            {Object.entries(entry.params).map(([k, def]) => {
              const cur = value.params[k] ?? def;
              const update = (v: number | boolean | string) => {
                const params = { ...value.params };
                if (v === def) delete params[k];
                else params[k] = v;
                set({ params });
              };
              return typeof def === "boolean" ? (
                <div className="field" key={k}>
                  <span className="field__label">{paramLabel(k)}</span>
                  <Toggle checked={Boolean(cur)} onChange={update} label={cur ? "Yes" : "No"} />
                  <p className="field__hint">Default {def ? "yes" : "no"}</p>
                </div>
              ) : (
                <Labeled key={k} label={paramLabel(k)} wide={typeof def === "string"}
                  hint={typeof def === "string" && value.name === "rule"
                    ? (k === "buy_when"
                      ? <>Default <code>{String(def)}</code>. Use {RULE_NAMES}, numbers, + − × ÷, comparisons, and/or/not; <code>prev_</code> means the bar before.</>
                      : <>Default <code>{String(def)}</code>. Same names as above; empty means only the stop-loss sells.</>)
                    : `Default ${String(def)}`}
                  render={(id) =>
                    typeof def === "number"
                      ? <NumberInput id={id} value={Number(cur)} onChange={update} />
                      : <input id={id} className={`input ${value.name === "rule" ? "input--mono" : ""}`} value={String(cur)}
                          spellCheck={false} onChange={(e) => update(e.target.value)} />
                  } />
              );
            })}
          </div>
        </details>
      )}

      {entry?.backtestable !== false && <BacktestPreview strategy={value} />}
    </article>
  );
}

const SPANS = [{ days: 92, label: "3 months" }, { days: 365, label: "1 year" }, { days: 1095, label: "3 years" }];

function BacktestPreview({ strategy }: { strategy: StrategyCfg }) {
  const [symbol, setSymbol] = useState(strategy.symbols[0] ?? "");
  const [days, setDays] = useState(365);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const act = useAction();
  const sym = strategy.symbols.includes(symbol) ? symbol : strategy.symbols[0] ?? "";
  const tooShort = strategy.timeframe === "1Min";

  const run = () => act.run(async () => {
    setResult(null);
    const r = await send<BacktestResult>("POST", "/api/backtest", {
      strategy: strategy.name, symbol: sym, timeframe: strategy.timeframe, params: strategy.params, days,
      allocation_pct: 100, stop_loss_pct: strategy.stop_loss_pct, take_profit_pct: strategy.take_profit_pct,
    });
    setResult(r);
  });

  return (
    <section className="bt" aria-label="Backtest">
      <div className="bt__bar">
        <span className="bt__title">Backtest</span>
        {strategy.symbols.length > 1 && (
          <select className="input input--sm" value={sym} onChange={(e) => setSymbol(e.target.value)} aria-label="Symbol to backtest">
            {strategy.symbols.map((s) => <option key={s}>{s}</option>)}
          </select>
        )}
        <div className="segmented" role="group" aria-label="Backtest period">
          {SPANS.map((s) => (
            <button key={s.days} type="button" aria-pressed={days === s.days} onClick={() => setDays(s.days)}>{s.label}</button>
          ))}
        </div>
        <button type="button" className="btn" onClick={run} disabled={act.busy || !sym}>
          {act.busy ? "Running…" : `Run on ${sym || "—"}`}
        </button>
      </div>
      <p className="field__hint">
        Every trade uses 100% of a $10,000 test account so it compares fairly with buying and holding.
        Uses full-market history{tooShort ? ", up to 60 days for 1-minute bars" : ""}. Past results don't predict future ones.
      </p>
      {act.busy && <p className="empty">Fetching history and replaying every bar… the first run for a symbol can take a few seconds.</p>}
      <NoticeLine notice={act.notice} />
      {result && <BacktestResultView r={result} />}
    </section>
  );
}

function BacktestResultView({ r }: { r: BacktestResult }) {
  const s = r.summary;
  const ret = Number(s["return_%"]);
  const bh = Number(s["buy_hold_%"]);
  const beat = ret > bh;
  return (
    <div className="bt__result">
      <p className="bt__verdict">
        <b className={beat ? "is-up" : "is-down"}>{beat ? "▲ Beat" : "▼ Trailed"}</b> buying and holding {r.symbol} by{" "}
        <b className="num">{Math.abs(ret - bh).toFixed(1)} pts</b> <span className="muted">({s.period})</span>
      </p>
      <dl className="bt__stats">
        <div><dt>Strategy</dt><dd className="num">{pct(ret)}</dd></div>
        <div><dt>Buy &amp; hold</dt><dd className="num">{pct(bh)}</dd></div>
        <div><dt>Worst drop</dt><dd className="num">{pct(Number(s["max_dd_%"]), false, 1)}</dd></div>
        <div><dt>Trades</dt><dd className="num">{s.trades}</dd></div>
        <div><dt>Won</dt><dd className="num">{pct(Number(s["win_rate_%"]), false, 0)}</dd></div>
        <div><dt>Stopped out</dt><dd className="num">{s.stopped_out}</dd></div>
      </dl>
      <CompareChart a={r.equity} b={r.buy_hold} aLabel="Strategy" bLabel="Buy & hold" />
    </div>
  );
}

const M = { top: 10, right: 92, bottom: 22, left: 44 };
const H = 190;

/** Two lines indexed to 100, with legend, end labels and a crosshair tooltip. */
export function CompareChart({ a, b, aLabel, bLabel }: { a: Point[]; b: Point[]; aLabel: string; bLabel: string }) {
  const { wrap, w } = useChartWidth(560, 260);
  const [hover, setHover] = useState<number | null>(null);
  const titleId = useId();
  const n = Math.min(a.length, b.length);
  const { y0, y1 } = useMemo(() => {
    const vals = [...a.slice(0, n), ...b.slice(0, n)].map((p) => p.v);
    const lo = Math.min(...vals, 100), hi = Math.max(...vals, 100);
    const pad = (hi - lo) * 0.08 || 1;
    return { y0: lo - pad, y1: hi + pad };
  }, [a, b, n]);
  if (n < 2) return null;
  const iw = w - M.left - M.right, ih = H - M.top - M.bottom;
  const x = (i: number) => M.left + (iw * i) / (n - 1);
  const y = (v: number) => M.top + ih * (1 - (v - y0) / (y1 - y0));
  const line = (pts: Point[]) => pts.slice(0, n).map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const ticks = [y0 + (y1 - y0) * 0.1, 100, y0 + (y1 - y0) * 0.9].map((t) => Math.round(t));
  const la = a[n - 1].v, lb = b[n - 1].v;
  // keep end labels from overlapping
  let ya = y(la), yb = y(lb);
  if (Math.abs(ya - yb) < 14) {
    const mid = (ya + yb) / 2, aAbove = ya <= yb;
    ya = mid + (aAbove ? -7 : 7);
    yb = mid + (aAbove ? 7 : -7);
  }
  const pick = (clientX: number, el: SVGSVGElement) => {
    const box = el.getBoundingClientRect();
    const px = ((clientX - box.left) / box.width) * w;
    setHover(Math.min(n - 1, Math.max(0, Math.round(((px - M.left) / iw) * (n - 1)))));
  };
  const h = hover;
  return (
    <div className="cmp">
      <ul className="legend">
        <li><span className="swatch swatch--1" />{aLabel} <b>{pct(la - 100)}</b></li>
        <li><span className="swatch swatch--2" />{bLabel} <b>{pct(lb - 100)}</b></li>
      </ul>
      <div className="chart" ref={wrap}>
        <svg width={w} height={H} viewBox={`0 0 ${w} ${H}`} tabIndex={0} role="img" aria-labelledby={titleId}
          onPointerMove={(e) => pick(e.clientX, e.currentTarget)} onPointerLeave={() => setHover(null)}
          onKeyDown={(e) => {
            if (e.key === "ArrowLeft") setHover((i) => Math.max(0, (i ?? n) - 1));
            if (e.key === "ArrowRight") setHover((i) => Math.min(n - 1, (i ?? -1) + 1));
            if (e.key === "Escape") setHover(null);
          }}
          onBlur={() => setHover(null)}>
          <title id={titleId}>{`Growth of $100: ${aLabel} ${pct(la - 100)}, ${bLabel} ${pct(lb - 100)}. Arrow keys read values.`}</title>
          {[...new Set(ticks)].map((t) => (
            <g key={t}>
              <line className={t === 100 ? "grid grid--base" : "grid"} x1={M.left} x2={w - M.right} y1={y(t)} y2={y(t)} />
              <text className="axis" x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end">${t}</text>
            </g>
          ))}
          <text className="axis" x={x(0)} y={H - 4}>{day(a[0].t)}</text>
          <text className="axis" x={x(n - 1)} y={H - 4} textAnchor="end">{day(a[n - 1].t)}</text>
          <path className="line line--2" d={line(b)} />
          <path className="line line--1" d={line(a)} />
          <text className="endlabel" x={x(n - 1) + 8} y={ya} dy="0.32em">{aLabel}</text>
          <text className="endlabel endlabel--muted" x={x(n - 1) + 8} y={yb} dy="0.32em">{bLabel}</text>
          {h != null && (
            <g className="crosshair">
              <line x1={x(h)} x2={x(h)} y1={M.top} y2={M.top + ih} />
              <circle className="dot dot--2" cx={x(h)} cy={y(b[h].v)} r="4.5" />
              <circle className="dot dot--1" cx={x(h)} cy={y(a[h].v)} r="4.5" />
            </g>
          )}
        </svg>
        {h != null && (
          <div className="tooltip" style={{ left: Math.min(x(h) + 12, w - 180), top: M.top }} aria-live="polite">
            <div className="tooltip__date">{day(a[h].t)}</div>
            <div className="tooltip__row"><span className="swatch swatch--1" /><b>{pct(a[h].v - 100)}</b><span className="tooltip__label">{aLabel}</span></div>
            <div className="tooltip__row"><span className="swatch swatch--2" /><b>{pct(b[h].v - 100)}</b><span className="tooltip__label">{bLabel}</span></div>
          </div>
        )}
      </div>
    </div>
  );
}
