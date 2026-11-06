import { useState } from "react";
import { send, type Setup, type Status } from "../api";
import { ago, marketTime } from "../format";
import { GoLiveDialog } from "./ModeDialog";

const W = 600;
const H = 44;
const BASE = 28;

/** ECG-style complex centered on x: small P wave, sharp QRS spike, rounded T wave. */
function complex(x: number): string {
  return [
    `L ${x - 34} ${BASE}`,
    `q 6 -6 12 0`, // P
    `L ${x - 8} ${BASE}`,
    `l 3 4 l 5 -30 l 5 36 l 3 -10`, // Q R S
    `L ${x + 14} ${BASE}`,
    `q 9 -10 18 0`, // T
  ].join(" ");
}

function Trace({ ageS, windowS, beatKey }: { ageS: number | null; windowS: number; beatKey: string }) {
  const visible = ageS != null && ageS <= windowS;
  const x = visible ? W - 40 - (W - 80) * (ageS! / windowS) : null;
  const d = x == null ? `M 0 ${BASE} L ${W} ${BASE}` : `M 0 ${BASE} ${complex(x)} L ${W} ${BASE}`;
  const minutes = Math.round(windowS / 60);
  return (
    <svg
      className={`trace ${visible ? "trace--alive" : "trace--flat"}`}
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={visible ? `Last heartbeat ${ago(ageS!)}` : "No recent heartbeat"}
    >
      {/* one tick per minute across the stale window */}
      {Array.from({ length: minutes + 1 }, (_, i) => {
        const tx = 40 + ((W - 80) * i) / minutes;
        return <line key={i} className="trace__tick" x1={tx} x2={tx} y1={BASE + 9} y2={BASE + 13} />;
      })}
      <path className="trace__line" d={d} vectorEffect="non-scaling-stroke" />
      {x != null && <circle key={beatKey} className="trace__blip" cx={x + 8} cy={BASE - 26} r="3" />}
    </svg>
  );
}

type ModeId = "dry-run" | "paper" | "live";
const MODES: { id: ModeId; label: string }[] = [
  { id: "dry-run", label: "Dry run" },
  { id: "paper", label: "Paper" },
  { id: "live", label: "Live" },
];

const NAV = [
  { href: "#/", label: "Overview" },
  { href: "#/strategies", label: "Strategies" },
  { href: "#/invest", label: "Invest" },
  { href: "#/settings", label: "Settings" },
  { href: "#/guide", label: "Guide" },
];

export function Header({ status, setup, now, error, route, onStatus, onSetup }: {
  status: Status | null; setup: Setup | null; now: number; error: string | null; route: string;
  onStatus: (s?: Status) => void; onSetup: (s: Setup) => void;
}) {
  const [liveOpen, setLiveOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const mode = status?.mode ?? "paper";
  const current: ModeId = status ? (status.dry_run ? "dry-run" : status.mode) : "paper";
  const hb = status?.heartbeat;
  // Age from the heartbeat timestamp, corrected for any clock difference with the server.
  const skew = status ? Date.now() - new Date(status.server_time).getTime() : 0;
  const ageS = hb?.ts ? (now - skew - new Date(hb.ts).getTime()) / 1000 : null;
  const stale = ageS == null || (hb ? ageS > hb.stale_after_s : true);
  // The service reports what it's running as; after a switch it catches up on its next check.
  const reported = status?.running_mode ? (status.running_mode === "dry-run" ? "dry-run" : "trading") : null;
  const catchingUp = !stale && reported != null && (reported === "dry-run") !== Boolean(status?.dry_run);

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const switchTo = (m: ModeId) => {
    if (m === current) return;
    if (m === "live") return setLiveOpen(true);
    act(async () => onStatus(await send<Status>("POST", "/api/mode", { mode: m })));
  };
  const setPaused = (paused: boolean) => act(async () => onStatus(await send<Status>("POST", "/api/pause", { paused })));
  const startService = () => act(async () => {
    onSetup(await send<Setup>("POST", "/api/service", { action: "start" }));
    onStatus();
  });

  return (
    <header className="masthead">
      <div className={`modeband modeband--${mode}`}>
        <span className="modeband__label">
          {mode === "live" ? "Live trading · real money" : "Paper trading · simulated money"}
        </span>
        {status?.dry_run && <span className="modeband__dry">Dry run · decisions only, no orders</span>}
        {status?.configured && (
          <div className="modeswitch" role="group" aria-label="Trading mode">
            {MODES.map((m) => (
              <button key={m.id} type="button" aria-pressed={current === m.id} disabled={busy}
                className={m.id === "live" ? "modeswitch__live" : ""} onClick={() => switchTo(m.id)}>
                {m.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="masthead__row">
        <div className="brand">
          <span className="brand__name">
            <img className="brand__mark" src="/favicon.svg" alt="" width="30" height="30" />
            TaylorTrader
          </span>
          <nav className="nav" aria-label="Pages">
            {status?.configured
              ? NAV.map((n) => (
                  <a key={n.href} href={n.href} aria-current={route === n.href.slice(1) ? "page" : undefined}>{n.label}</a>
                ))
              : <a href="#/setup" aria-current="page">Setup</a>}
          </nav>
        </div>

        <div className="pulse">
          <Trace ageS={ageS} windowS={hb?.stale_after_s ?? 900} beatKey={hb?.ts ?? "none"} />
          <div className="pulse__scale" aria-hidden="true">
            <span>{Math.round((hb?.stale_after_s ?? 900) / 60)} min ago</span>
            <span>now</span>
          </div>
        </div>

        <dl className="readouts">
          <div className={`readout ${stale ? "readout--crit" : "readout--good"}`}>
            <dt>Service</dt>
            <dd>
              <span className="glyph" aria-hidden="true">{stale ? "✕" : "●"}</span>
              {ageS == null
                ? "No heartbeat yet"
                : stale
                  ? `Not running · last beat ${ago(ageS)}`
                  : `Running · beat ${ago(ageS)}`}
            </dd>
          </div>
          <div className="readout">
            <dt>Market</dt>
            <dd>
              {status?.market
                ? status.market.is_open
                  ? `Open · closes ${marketTime(status.market.next_close)}`
                  : `Closed · opens ${marketTime(status.market.next_open)}`
                : "—"}
            </dd>
          </div>
          {status?.configured && (
            <div className={`readout ${status.kill_switch ? "readout--warn" : ""}`}>
              <dt>New buys</dt>
              <dd className="readout__action">
                {status.kill_switch
                  ? <><span className="glyph" aria-hidden="true">▲</span>Paused</>
                  : "Allowed"}
                <button type="button" className="btn btn--sm" disabled={busy} onClick={() => setPaused(!status.kill_switch)}
                  title={status.kill_switch ? "Let strategies open positions again" : "Stop opening positions. Sells and stop-losses keep working."}>
                  {status.kill_switch ? "Resume" : "Pause"}
                </button>
              </dd>
            </div>
          )}
        </dl>
      </div>

      {catchingUp && (
        <p className="masthead__note" role="status">
          Switched to {status!.mode_label}. The service is still running as {reported === "dry-run" ? "dry run" : "a trading bot"} and
          picks up the change on its next check.
        </p>
      )}
      {status?.configured && stale && (
        <p className="masthead__alert" role="alert">
          {ageS == null ? "The service hasn't started yet, so nothing is being watched. " :
            "The service isn't running, so nothing is being traded or watched. Your stop-losses at Alpaca still work. "}
          {setup?.service.supported
            ? <button type="button" className="btn btn--sm" disabled={busy || !status.connected} onClick={startService}>Start the service</button>
            : <>Start it with <code>uv run taylortrader run</code>.</>}
        </p>
      )}
      {status && status.configured && !status.connected && (
        <p className="masthead__alert" role="alert">
          Not connected to Alpaca: {status.broker_error} <a href="#/settings">Open settings</a>
        </p>
      )}
      {(error || actionError) && (
        <p className="masthead__alert" role="alert">{actionError ?? `Can't load status: ${error}`}</p>
      )}

      {setup && (
        <GoLiveDialog open={liveOpen} onClose={() => setLiveOpen(false)} setup={setup} onSetup={onSetup}
          onDone={(s) => onStatus(s)} />
      )}
    </header>
  );
}
