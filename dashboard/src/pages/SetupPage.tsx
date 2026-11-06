import { useEffect, useState } from "react";
import type { Setup, StrategyCfg } from "../api";
import { NoticeLine, useAction } from "../components/forms";
import { AlpacaKeys } from "../components/Keys";
import { StrategyEditor } from "../components/StrategyEditor";
import { clone, type SettingsStore } from "../settings";
import { AlertsSection, LimitsSection, Section, ServiceControls } from "./SettingsPage";
import { NEW_STRATEGY } from "./StrategiesPage";

/** First run: the same pieces as Settings, in the order you need them. */
export function SetupPage({ store, setup, onSetup }: { store: SettingsStore; setup: Setup | null; onSetup: (s: Setup) => void }) {
  const resp = store.resp;
  const [strategy, setStrategy] = useState<StrategyCfg>(() => ({ ...clone(NEW_STRATEGY), allocation_pct: 20, stop_loss_pct: 12 }));
  const act = useAction();
  useEffect(() => {
    if (resp?.config.strategies.length) setStrategy(clone(resp.config.strategies[0]));
  }, [resp]);
  if (!resp || !setup) return <main className="page"><p className="empty">{store.error ?? "Loading…"}</p></main>;

  const keysDone = Boolean(setup.secrets.ALPACA_PAPER_KEY_ID && setup.secrets.ALPACA_PAPER_SECRET_KEY);
  const strategyDone = setup.has_strategy || setup.has_plan;
  const limitsDone = setup.configured && resp.configured;
  const alertsDone = Boolean(setup.secrets.NTFY_TOPIC || (setup.secrets.EMAIL_PASSWORD && resp.config.alerts.email_to.length));
  const ready = keysDone && strategyDone;

  const saveStrategy = () => act.run(async () => {
    const others = resp.config.strategies.slice(1);
    await store.save({ ...resp.config, strategies: [strategy, ...others] });
    return "Strategy saved.";
  });

  return (
    <main className="page page--narrow">
      <header className="page__head">
        <div>
          <h1>Set up TaylorTrader</h1>
          <p className="page__sub">
            Five steps, about ten minutes. Everything starts on <b>paper</b> (simulated money) in <b>dry run</b>:
            the bot makes its decisions and tells you about them, but places no orders until you say so. New to
            this? The <a href="#/guide">Guide</a> explains how it all works.
          </p>
        </div>
      </header>

      <div className="stack">
        <Section id="s-keys" step={1} done={keysDone} title="Connect your Alpaca paper account"
          sub={<>In Alpaca, switch to your Paper account, open <b>API Keys</b>, and generate a pair. They're saved to the {setup.secret_store}, never to a file in this project.</>}>
          <details className="howto">
            <summary>No Alpaca account yet?</summary>
            <ol>
              <li>Sign up at <a href="https://app.alpaca.markets/signup" target="_blank" rel="noreferrer">alpaca.markets</a>.
                A paper (practice) account is free and needs only an email: no money, no ID check.</li>
              <li>After signing in, make sure the account switcher at the top left says <b>Paper</b>.</li>
              <li>On the home page, find <b>API Keys</b> and choose <b>Generate New Keys</b>.</li>
              <li>Copy both into the boxes below. Alpaca shows the secret only once; if you lose it, generate a new pair.</li>
            </ol>
          </details>
          <AlpacaKeys mode="paper" setup={setup} onSaved={onSetup} />
        </Section>

        <Section id="s-strategy" step={2} done={strategyDone} title="Pick a strategy"
          sub={<>Choose how it decides, which stocks it watches, and how much each buy uses. Run a backtest to see how it
            would have done. Honest note: in our tests no strategy beat simply buying and holding. If you'd rather
            invest a set amount on a schedule, <a href="#/invest">set up an invest plan</a> instead and skip this step.</>}>
          <StrategyEditor value={strategy} onChange={setStrategy} catalog={resp.catalog}
            limits={resp.config.guardrails} timeframes={resp.timeframes} aiDailyLimit={resp.config.ai.max_calls_per_day} index={0} />
          <div className="saverow">
            <button type="button" className="btn btn--primary" onClick={saveStrategy}
              disabled={act.busy || strategy.symbols.length === 0}>
              {act.busy ? "Saving…" : strategyDone ? "Save changes" : "Use this strategy"}
            </button>
            {strategy.symbols.length === 0 && <span className="field__hint">Add at least one symbol, like VRT or AAPL.</span>}
          </div>
          <NoticeLine notice={act.notice} />
          {resp.config.strategies.length > 1 && (
            <p className="note">You have {resp.config.strategies.length} strategies. Manage them all on the <a href="#/strategies">Strategies</a> page.</p>
          )}
        </Section>

        <LimitsSection store={store} step={3} done={limitsDone} saveLabel={limitsDone ? "Save" : "Use these limits"} />

        <AlertsSection store={store} setup={setup} onSetup={onSetup} step={4} done={alertsDone} />

        <Section id="s-start" step={5} done={ready && setup.service.running} title="Start it in dry run"
          sub="The background service checks your strategies every few minutes while the market is open. In dry run it logs and alerts what it would do, and orders nothing. Switch to paper orders from the bar at the top once you're comfortable.">
          {ready ? (
            <>
              <ServiceControls setup={setup} onSetup={onSetup} />
              {setup.service.running && <p className="note"><a className="btn" href="#/">Go to the overview</a></p>}
            </>
          ) : (
            <p className="empty">Finish steps 1 and 2 first.</p>
          )}
        </Section>
      </div>
    </main>
  );
}
