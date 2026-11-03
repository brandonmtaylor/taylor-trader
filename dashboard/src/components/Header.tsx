import type { Status } from "../api";
import { ago, marketTime } from "../format";

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

export function Header({ status, now, error }: { status: Status | null; now: number; error: string | null }) {
  const mode = status?.mode ?? "paper";
  const dry = status?.running_mode === "dry-run";
  const hb = status?.heartbeat;
  // Age from the heartbeat timestamp, corrected for any clock difference with the server.
  const skew = status ? Date.now() - new Date(status.server_time).getTime() : 0;
  const ageS = hb?.ts ? (now - skew - new Date(hb.ts).getTime()) / 1000 : null;
  const stale = ageS == null || (hb ? ageS > hb.stale_after_s : true);

  return (
    <header className="masthead">
      <div className={`modeband modeband--${mode}`}>
        <span className="modeband__label">
          {mode === "live" ? "Live trading · real money" : "Paper trading · simulated money"}
        </span>
        {dry && <span className="modeband__dry">Dry run · decisions only, no orders</span>}
      </div>

      <div className="masthead__row">
        <div className="brand">
          <span className="brand__name">TaylorTrader</span>
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
              {status
                ? status.market.is_open
                  ? `Open · closes ${marketTime(status.market.next_close)}`
                  : `Closed · opens ${marketTime(status.market.next_open)}`
                : "—"}
            </dd>
          </div>
          <div className={`readout ${status?.kill_switch ? "readout--warn" : ""}`}>
            <dt>Kill switch</dt>
            <dd>
              {status?.kill_switch ? (
                <>
                  <span className="glyph" aria-hidden="true">▲</span>On · no new positions
                </>
              ) : (
                "Off"
              )}
            </dd>
          </div>
        </dl>
      </div>

      {status && stale && (
        <p className="masthead__alert" role="alert">
          {ageS == null
            ? "The service hasn't started yet. Start it with: uv run taylortrader run --dry-run"
            : "The service isn't running, so nothing is being traded or watched. Your stops at Alpaca still work. " +
              "Restart it with: launchctl kickstart -k gui/$(id -u)/com.taylortrader.service"}
        </p>
      )}
      {error && (
        <p className="masthead__alert" role="alert">
          Can't load status: {error}
        </p>
      )}
    </header>
  );
}
