import { useEffect, useState, type ReactNode } from "react";
import { send, type ConfigDict, type Setup } from "../api";
import { AlpacaKeys } from "../components/Keys";
import { Labeled, NoticeLine, NumberInput, SecretField, Toggle, useAction } from "../components/forms";
import { clone, same, type SettingsStore } from "../settings";

type SectionKey = "guardrails" | "alerts" | "service" | "feed" | "ai" | "mcp";

/** Edit one part of the config with its own Save button. */
function useSection<K extends SectionKey>(store: SettingsStore, key: K) {
  const server = store.resp?.config[key] as ConfigDict[K] | undefined;
  const [draft, setDraft] = useState<ConfigDict[K] | undefined>(server && clone(server));
  useEffect(() => {
    if (server !== undefined && draft === undefined) setDraft(clone(server));
  }, [server, draft]);
  const act = useAction();
  const dirty = draft !== undefined && !same(draft, server);
  const save = (msg = "Saved.") => act.run(async () => {
    if (!store.resp || draft === undefined) return;
    const r = await store.save({ ...store.resp.config, [key]: draft });
    setDraft(clone(r.config[key]));
    return msg;
  });
  const reset = () => { setDraft(server && clone(server)); act.setNotice(null); };
  return { draft, setDraft, dirty, save, reset, act };
}

export function Section({ id, title, sub, children, step, done }: {
  id?: string; title: string; sub?: ReactNode; children: ReactNode; step?: number; done?: boolean;
}) {
  return (
    <section className={`panel section ${step ? "section--step" : ""}`} id={id} aria-labelledby={`${id}-h`}>
      <div className="section__head">
        {step && <span className={`step ${done ? "step--done" : ""}`} aria-hidden="true">{done ? "✓" : step}</span>}
        <div>
          <h2 id={`${id}-h`}>{title}{done && <span className="visually-hidden"> (done)</span>}</h2>
          {sub && <p className="panel__sub">{sub}</p>}
        </div>
      </div>
      {children}
    </section>
  );
}

function SaveRow({ dirty, busy, onSave, onReset, label = "Save" }: {
  dirty: boolean; busy: boolean; onSave: () => void; onReset: () => void; label?: string;
}) {
  return (
    <div className="saverow">
      <button type="button" className="btn btn--primary" disabled={!dirty || busy} onClick={onSave}>
        {busy ? "Saving…" : label}
      </button>
      {dirty && <button type="button" className="btn btn--quiet" onClick={onReset}>Undo changes</button>}
    </div>
  );
}

// --- sections (also used by the setup screen) -------------------------------------------------------

export function LimitsSection({ store, step, done, saveLabel }: { store: SettingsStore; step?: number; done?: boolean; saveLabel?: string }) {
  const s = useSection(store, "guardrails");
  const hard = store.resp?.hard_limits;
  if (!s.draft || !hard) return null;
  const d = s.draft;
  const row = (k: keyof typeof d, label: string, hint: string, suffix?: string) => (
    <Labeled label={label} hint={`${hint} · ceiling ${hard[k]}${suffix ?? ""}`} render={(id) => (
      <NumberInput id={id} value={d[k]} suffix={suffix} onChange={(v) => s.setDraft({ ...d, [k]: v })} />
    )} />
  );
  return (
    <Section id="limits" step={step} done={done} title="Safety limits"
      sub="Checked before every buy, across all strategies. Sells and stop-losses are never blocked. The ceilings are built in and can't be raised from here.">
      <div className="grid-fields">
        {row("max_position_pct", "One position", "Most of your account in a single stock", "%")}
        {row("max_total_exposure_pct", "Everything invested", "Most of your account in stocks at once", "%")}
        {row("max_daily_loss_pct", "Daily loss", "Stop buying for the day after losing this much", "%")}
        {row("max_trades_per_day", "Buys per day", "By your strategies. Sells, stop-losses and invest-plan buys don't count")}
        {row("max_stop_loss_pct", "Widest stop-loss", "No strategy can set a looser stop", "%")}
      </div>
      <SaveRow dirty={s.dirty || (!!step && !done)} busy={s.act.busy} onSave={() => s.save("Limits saved.")} onReset={s.reset} label={saveLabel} />
      <NoticeLine notice={s.act.notice} />
    </Section>
  );
}

export function AlertsSection({ store, setup, onSetup, step, done }: {
  store: SettingsStore; setup: Setup; onSetup: (s: Setup) => void; step?: number; done?: boolean;
}) {
  const s = useSection(store, "alerts");
  const test = useAction();
  const [toText, setToText] = useState<string | null>(null);
  if (!s.draft) return null;
  const d = s.draft;
  return (
    <Section id="alerts" step={step} done={done} title={step ? "Alerts (optional)" : "Alerts"}
      sub="Hear about trades, errors and a daily summary on your phone or by email.">
      <h3 className="subhead">Phone (ntfy)</h3>
      <p className="field__hint">
        Install the free ntfy app, subscribe to a topic name only you know (like <code>tt-7f3k9q2m</code>), and save it here.
        Anyone with the name can read your alerts, so it's stored like a password.
      </p>
      <SecretField name="NTFY_TOPIC" label="Topic name" setup={setup} onSaved={onSetup} placeholder="tt-…" />

      <h3 className="subhead">Email</h3>
      <div className="grid-fields">
        <Labeled label="Send from" hint="A Gmail address with an app password" render={(id) => (
          <input id={id} className="input" type="email" value={d.email_from} autoComplete="off"
            onChange={(e) => s.setDraft({ ...d, email_from: e.target.value.trim() })} />
        )} />
        <Labeled label="Send to" hint="One or more, comma separated" render={(id) => (
          <input id={id} className="input" value={toText ?? d.email_to.join(", ")} autoComplete="off"
            onChange={(e) => {
              setToText(e.target.value);
              s.setDraft({ ...d, email_to: e.target.value.split(/[\s,]+/).filter(Boolean) });
            }} />
        )} />
      </div>
      <SaveRow dirty={s.dirty} busy={s.act.busy} onSave={() => s.save("Email addresses saved.")} onReset={() => { s.reset(); setToText(null); }} />
      <NoticeLine notice={s.act.notice} />
      <SecretField name="EMAIL_PASSWORD" label="Gmail app password"
        hint={<>Make one at myaccount.google.com/apppasswords. It's not your normal Gmail password.</>}
        setup={setup} onSaved={onSetup} />

      <h3 className="subhead">If this computer goes quiet</h3>
      <SecretField name="HEALTHCHECK_URL" label="healthchecks.io ping URL (optional)"
        hint="The service pings it every few minutes. If the pings stop, even because this Mac is off, healthchecks.io emails you."
        setup={setup} onSaved={onSetup} placeholder="https://hc-ping.com/…" />

      <div className="saverow">
        <button type="button" className="btn" disabled={test.busy}
          onClick={() => test.run(async () => {
            const r = await send<{ channels: string[] }>("POST", "/api/alerts/test");
            return `Test sent. ${r.channels.join(" · ")}`;
          })}>
          {test.busy ? "Sending…" : "Send a test alert"}
        </button>
      </div>
      <NoticeLine notice={test.notice} />
    </Section>
  );
}

const FEEDS: { id: ConfigDict["feed"]; title: string; text: string }[] = [
  { id: "yahoo", title: "Yahoo Finance", text: "Every exchange, current, free. Unofficial, so if it fails or falls behind the service uses Alpaca IEX until it's back. Recommended." },
  { id: "iex", title: "Alpaca IEX", text: "Official and real time, but only one exchange (about 2–3% of trading). Prices are close; volume is thin." },
];

function ClaudeSection({ store }: { store: SettingsStore }) {
  const s = useSection(store, "mcp");
  if (!s.draft) return null;
  const d = s.draft;
  return (
    <Section id="claude" title="Claude"
      sub="What Claude may do when you connect it to the bot (optional; see the Guide). It can always read, pause new buys, and change strategies.">
      <div className="toggles">
        <Toggle checked={d.allow_live_selling} onChange={(allow_live_selling) => s.setDraft({ ...d, allow_live_selling })}
          label="Let Claude sell on a live account" />
      </div>
      <p className="field__hint">
        Off: Claude can sell on paper only; on real money you sell here. Claude types its own confirmations, so a
        misleading news story or message it reads could get it to sell. Turn this on only if you want that.
        Changed on the Mac only.
      </p>
      <SaveRow dirty={s.dirty} busy={s.act.busy} onSave={() => s.save("Saved.")} onReset={s.reset} />
      <NoticeLine notice={s.act.notice} />
    </Section>
  );
}

function DataSection({ store }: { store: SettingsStore }) {
  const s = useSection(store, "feed");
  if (!s.draft) return null;
  return (
    <Section id="data" title="Price data" sub="Where the service gets bars to decide on. Backtests always use full-market history.">
      <div className="choices" role="radiogroup" aria-label="Price data source">
        {FEEDS.map((f) => (
          <label key={f.id} className={`choice ${s.draft === f.id ? "choice--on" : ""}`}>
            <input type="radio" name="feed" value={f.id} checked={s.draft === f.id} onChange={() => s.setDraft(f.id)} />
            <span><b>{f.title}</b><span className="choice__text">{f.text}</span></span>
          </label>
        ))}
      </div>
      <SaveRow dirty={s.dirty} busy={s.act.busy} onSave={() => s.save("Saved.")} onReset={s.reset} />
      <NoticeLine notice={s.act.notice} />
    </Section>
  );
}

export function ServiceControls({ setup, onSetup }: { setup: Setup; onSetup: (s: Setup) => void }) {
  const act = useAction();
  const svc = setup.service;
  const go = (action: "start" | "stop" | "restart", msg: string) =>
    act.run(async () => {
      onSetup(await send<Setup>("POST", "/api/service", { action }));
      return msg;
    });
  if (!svc.supported) {
    return (
      <p className="field__hint">
        Starting it from here works on macOS. Elsewhere, run <code>uv run taylortrader run</code> in a terminal
        (or as a systemd service) and keep it running.
      </p>
    );
  }
  return (
    <>
      <p className="svc">
        {svc.running
          ? <span className="tag tag--good"><span aria-hidden="true">●</span> Running{svc.pid ? ` · pid ${svc.pid}` : ""}</span>
          : <span className="tag tag--warn"><span aria-hidden="true">▲</span> {svc.loaded ? "Loaded, not running" : "Not started"}</span>}
        <span className="field__hint">Starts again on its own after a crash or a restart of this Mac.</span>
      </p>
      <div className="saverow">
        {!svc.running && <button type="button" className="btn btn--primary" disabled={act.busy} onClick={() => go("start", "Started.")}>Start the service</button>}
        {svc.running && <button type="button" className="btn" disabled={act.busy} onClick={() => go("restart", "Restarted.")}>Restart</button>}
        {svc.loaded && <button type="button" className="btn btn--quiet btn--danger" disabled={act.busy} onClick={() => go("stop", "Stopped. Your stop-losses at Alpaca still protect open positions.")}>Stop</button>}
      </div>
      <NoticeLine notice={act.notice} />
    </>
  );
}

function ServiceSection({ store, setup, onSetup }: { store: SettingsStore; setup: Setup; onSetup: (s: Setup) => void }) {
  const s = useSection(store, "service");
  if (!s.draft) return null;
  const d = s.draft;
  return (
    <Section id="service" title="Background service" sub="The part that trades. It runs on its own, separate from this page.">
      <ServiceControls setup={setup} onSetup={onSetup} />
      <h3 className="subhead">Behavior</h3>
      <div className="grid-fields">
        <Labeled label="Check every" hint="Seconds between checks while the market is open" render={(id) => (
          <NumberInput id={id} value={d.loop_seconds} suffix="s" onChange={(loop_seconds) => s.setDraft({ ...d, loop_seconds })} />
        )} />
        <Labeled label="Warn if quiet for" hint="The watchdog alerts you after this long without a heartbeat" render={(id) => (
          <NumberInput id={id} value={d.heartbeat_stale_minutes} suffix="min" onChange={(heartbeat_stale_minutes) => s.setDraft({ ...d, heartbeat_stale_minutes })} />
        )} />
        <Labeled label="Tell me if no trades for" hint="For a strategy holding nothing. 0 turns this off" render={(id) => (
          <NumberInput id={id} value={d.no_trades_days} suffix="days" onChange={(no_trades_days) => s.setDraft({ ...d, no_trades_days })} />
        )} />
      </div>
      <div className="toggles">
        <Toggle checked={d.auto_protect} onChange={(auto_protect) => s.setDraft({ ...d, auto_protect })}
          label="Put back a missing stop-loss on positions a strategy owns" />
        <Toggle checked={d.daily_summary} onChange={(daily_summary) => s.setDraft({ ...d, daily_summary })}
          label="Send a summary after each market close" />
      </div>
      <SaveRow dirty={s.dirty} busy={s.act.busy} onSave={() => s.save("Saved.")} onReset={s.reset} />
      <NoticeLine notice={s.act.notice} />
    </Section>
  );
}

type Provider = "anthropic" | "openai" | "claude-code";

const MODEL_HINTS: Record<Provider, string[]> = {
  anthropic: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
  openai: ["gpt-5", "gpt-5-mini", "llama3.1", "qwen2.5"],
  "claude-code": ["sonnet", "opus", "haiku"],
};

const MODEL_HINT_TEXT: Record<Provider, string> = {
  anthropic: "Opus is the most capable; Sonnet and Haiku cost less per call.",
  openai: "The model name your provider or local server uses.",
  "claude-code": "sonnet uses less of your plan's limits than opus; haiku is lightest.",
};

function AISection({ store, setup, onSetup }: { store: SettingsStore; setup: Setup; onSetup: (s: Setup) => void }) {
  const s = useSection(store, "ai");
  const test = useAction();
  if (!s.draft) return null;
  const d = s.draft;
  const setProvider = (provider: Provider) => {
    const keep = MODEL_HINTS[provider].includes(d.model)
      || (provider === "openai" && !d.model.startsWith("claude-") && !MODEL_HINTS["claude-code"].includes(d.model));
    s.setDraft({ ...d, provider, model: keep ? d.model : MODEL_HINTS[provider][0] });
  };
  return (
    <Section id="ai" title="AI model" sub="Used by AI strategies (experimental). The model only proposes buy, sell or hold; your safety limits and stop-losses still apply to everything it does.">
      <div className="choices" role="radiogroup" aria-label="AI provider">
        {([
          ["claude-code", "Claude subscription", "Uses Claude Code on this Mac and the Pro or Max plan it's logged in with. No API key; calls count toward your plan's limits."],
          ["anthropic", "Claude API", "Anthropic's API with a key, billed per call. Best for running unattended or with real money."],
          ["openai", "OpenAI-compatible", "OpenAI, OpenRouter, Groq, Together, or a model on this computer through Ollama or LM Studio (free, no key)."],
        ] as const).map(([id, title, text]) => (
          <label key={id} className={`choice ${d.provider === id ? "choice--on" : ""}`}>
            <input type="radio" name="provider" checked={d.provider === id} onChange={() => setProvider(id)} />
            <span><b>{title}</b><span className="choice__text">{text}</span></span>
          </label>
        ))}
      </div>
      <div className="grid-fields" style={{ marginTop: 16 }}>
        <Labeled label="Model" hint={MODEL_HINT_TEXT[d.provider]}
          render={(id) => (
            <>
              <input id={id} className="input input--mono" list={`${id}-models`} spellCheck={false} value={d.model}
                onChange={(e) => s.setDraft({ ...d, model: e.target.value.trim() })} />
              <datalist id={`${id}-models`}>{MODEL_HINTS[d.provider].map((m) => <option key={m} value={m} />)}</datalist>
            </>
          )} />
        {d.provider === "openai" ? (
          <Labeled label="Endpoint" wide hint={<>Empty for OpenAI. Ollama <code>http://localhost:11434/v1</code> · LM Studio <code>http://localhost:1234/v1</code> · OpenRouter <code>https://openrouter.ai/api/v1</code></>}
            render={(id) => (
              <input id={id} className="input input--mono" spellCheck={false} placeholder="https://api.openai.com/v1"
                value={d.endpoint} onChange={(e) => s.setDraft({ ...d, endpoint: e.target.value.trim() })} />
            )} />
        ) : (
          <Labeled label="Effort" hint="How hard it thinks. Higher is slower and costs more." render={(id) => (
            <select id={id} className="input" value={d.effort} onChange={(e) => s.setDraft({ ...d, effort: e.target.value as typeof d.effort })}>
              {["low", "medium", "high", "xhigh", "max"].map((x) => <option key={x}>{x}</option>)}
            </select>
          )} />
        )}
        <Labeled label="Daily limit" hint="Model calls per day across all AI strategies; after that they hold" render={(id) => (
          <NumberInput id={id} value={d.max_calls_per_day} suffix="calls" onChange={(max_calls_per_day) => s.setDraft({ ...d, max_calls_per_day: Math.round(max_calls_per_day) })} />
        )} />
      </div>
      <SaveRow dirty={s.dirty} busy={s.act.busy} onSave={() => s.save("Saved.")} onReset={s.reset} />
      <NoticeLine notice={s.act.notice} />

      {d.provider === "claude-code" ? (
        <p className="note">
          No key needed. If the test below says Claude Code isn't logged in, run <code>claude</code> in Terminal once and log in.
        </p>
      ) : <SecretField key={d.provider} name={d.provider === "anthropic" ? "ANTHROPIC_API_KEY" : "OPENAI_API_KEY"}
        label={d.provider === "anthropic" ? "Anthropic API key" : "API key (not needed for a local model)"}
        hint={d.provider === "anthropic" ? "From console.anthropic.com → API keys." : "From your provider's dashboard."}
        setup={setup} onSaved={onSetup} placeholder={d.provider === "anthropic" ? "sk-ant-…" : "sk-…"} />}

      <div className="saverow">
        <button type="button" className="btn" disabled={test.busy || s.dirty}
          onClick={() => test.run(async () => {
            const r = await send<{ model: string; latency_ms: number; input_tokens: number | null; output_tokens: number | null }>("POST", "/api/ai/test");
            const tokens = r.input_tokens != null ? ` · ${r.input_tokens + (r.output_tokens ?? 0)} tokens` : "";
            return `${r.model} answered in ${(r.latency_ms / 1000).toFixed(1)}s${tokens}. Decisions come back in the right format.`;
          })}>
          {test.busy ? "Asking the model…" : "Test the model"}
        </button>
        {s.dirty && <span className="field__hint">Save first, then test.</span>}
      </div>
      <NoticeLine notice={test.notice} />
    </Section>
  );
}

export function SettingsPage({ store, setup, onSetup }: { store: SettingsStore; setup: Setup | null; onSetup: (s: Setup) => void }) {
  if (!store.resp || !setup) return <main className="page"><p className="empty">{store.error ?? "Loading…"}</p></main>;
  return (
    <main className="page">
      <header className="page__head">
        <div>
          <h1>Settings</h1>
          <p className="page__sub">
            Saved to <code>{setup.config_path}</code>. Keys and passwords go to the {setup.secret_store} instead and are
            never shown again. The service picks up changes on its next check.
          </p>
        </div>
      </header>
      <div className="stack">
        <Section id="alpaca" title="Alpaca" sub="Paper keys run simulated money. Live keys are only used after you switch to live at the top of the page.">
          <h3 className="subhead">Paper account</h3>
          <AlpacaKeys mode="paper" setup={setup} onSaved={onSetup} />
          <h3 className="subhead">Live account</h3>
          <AlpacaKeys mode="live" setup={setup} onSaved={onSetup} />
        </Section>
        <LimitsSection store={store} />
        <AlertsSection store={store} setup={setup} onSetup={onSetup} />
        <AISection store={store} setup={setup} onSetup={onSetup} />
        <ClaudeSection store={store} />
        <DataSection store={store} />
        <ServiceSection store={store} setup={setup} onSetup={onSetup} />
      </div>
    </main>
  );
}
