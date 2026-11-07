import type { ReactNode } from "react";

/** The manual, shipped inside the app so it works offline and always matches this version. */
const SECTIONS: { id: string; title: string; body: ReactNode }[] = [
  {
    id: "what",
    title: "What TaylorTrader does",
    body: (
      <>
        <p>It runs on this computer and places orders in your own Alpaca account. There are two ways to use it,
          and you can use either or both:</p>
        <ul>
          <li><b>Trade with strategies.</b> A strategy watches a few stocks every few minutes while the market is
            open and buys or sells on its rules, always with a stop-loss. See <a href="#/strategies">Strategies</a>.</li>
          <li><b>Invest on a schedule.</b> Put a set amount into a mix of funds every week or month, the way a
            long-term investor would. No stop-losses, no timing. See <a href="#/invest">Invest</a>.</li>
        </ul>
        <p>Everything starts on <b>paper</b> (practice money) in <b>dry run</b> (decides, but places no orders).
          Nothing it does is financial advice.</p>
      </>
    ),
  },
  {
    id: "start",
    title: "Getting started",
    body: (
      <ol>
        <li><b>Get Alpaca paper keys.</b> Sign up free at alpaca.markets (email only for paper), switch to the
          Paper account, and generate API keys on the home page.</li>
        <li><b>Paste them into <a href="#/setup">Setup</a>.</b> They're tested, then stored in the macOS Keychain.
          They're never shown again or written to a project file.</li>
        <li><b>Pick a strategy</b> and run a backtest, or set up an invest plan instead.</li>
        <li><b>Check the safety limits</b> and, if you like, add phone or email alerts.</li>
        <li><b>Start it in dry run.</b> Watch what it would do for a few days, then switch to paper orders.</li>
      </ol>
    ),
  },
  {
    id: "modes",
    title: "Dry run, paper and live",
    body: (
      <dl className="guide__dl">
        <dt>Dry run</dt>
        <dd>Makes every decision and alerts you about it, but orders nothing. Use it to see how a strategy behaves.</dd>
        <dt>Paper</dt>
        <dd>Real orders in Alpaca's practice account, with pretend money. Fills are simulated but realistic.</dd>
        <dt>Live</dt>
        <dd>Real money. It needs separate live keys, an active live account, and typing GO LIVE. Switching
          is only possible on the Mac itself, never from your phone. Give paper a few weeks first.</dd>
      </dl>
    ),
  },
  {
    id: "safety",
    title: "How it keeps you safe",
    body: (
      <ul>
        <li><b>Every buy has a stop-loss</b>, placed at Alpaca, so it works even if this computer is off.</li>
        <li><b>Daily loss limit</b> (3% by default): after the account falls that much in a day, no new buys
          until tomorrow. Sells and stop-losses keep working. This is why you may see "blocked" buys on a bad day.
          Deposits and withdrawals don't count as gains or losses.</li>
        <li><b>Position limits</b>: no single stock can take more than its share of the account, and there's a
          cap on trades per day. Some limits are fixed in the code and can't be loosened from settings.</li>
        <li><b>Pause new buys</b> (the switch at the top) stops all buying at once. Selling still works.</li>
        <li><b>Positions it didn't open</b> are reported once a day and never touched.</li>
        <li><b>A watchdog</b> checks every 10 minutes that the bot is alive and alerts you if it isn't.</li>
      </ul>
    ),
  },
  {
    id: "strategies",
    title: "Strategies and backtests",
    body: (
      <>
        <p>A backtest replays a strategy on past prices. It's useful for seeing how a strategy behaves (how
          often it trades, how deep its losses go) and much less useful for predicting returns.</p>
        <p><b>The honest result so far:</b> on the 30 Dow stocks and 9 sector funds over several years, none of the built-in
          strategies beat simply buying and holding, though some had smaller drops along the way. They're here
          to experiment with, change, and build on. If a strategy looks great, it was probably tuned to the past.</p>
        <p><b>Write your own</b> with the <code>rule</code> strategy: a buy rule and a sell rule, like{" "}
          <code>close &gt; ma_50 and rsi &lt; 40</code> or <code>close &gt; prev_high_20</code>. Lengths count bars, so
          ma_50 is a 50-day average on a daily strategy and 50 hours on an hourly one. If you connect Claude, you
          can describe the idea in your own words and it writes the rules.</p>
        <p>To test an idea properly from Terminal, <code>taylortrader search</code> keeps back the most recent
          data the search never sees, and checks the winner against random entries to rule out luck.</p>
      </>
    ),
  },
  {
    id: "invest",
    title: "Invest plans",
    body: (
      <>
        <p>Choose an amount, a schedule, and a mix (or start from an example). On each scheduled day, after the
          market has been open half an hour, it buys with cash already in your account. New money goes to
          whatever is furthest below its target first, which keeps the mix balanced without selling.</p>
        <p><b>Preview</b> mode tells you what it would buy without buying. "What if you'd started…" shows the
          same plan run on real past prices.</p>
        <p>It can't move money from your bank. Set up deposits in Alpaca; if cash is short, it invests what's
          there and tells you.</p>
      </>
    ),
  },
  {
    id: "phone",
    title: "Alerts and your phone",
    body: (
      <>
        <p>Alerts arrive through the free ntfy app or email: every buy and sell, problems, and a daily summary
          after the close. You also hear if prices stop arriving, or if a strategy holding nothing hasn't traded
          for 14 days. Set them up in <a href="#/settings">Settings</a>.</p>
        <p>To open this dashboard on your phone on the same Wi-Fi, run <code>taylortrader wifi on</code> on the
          Mac. It prints an address and a passcode. Each device signs in once. For access away from home, put
          the Mac and phone on Tailscale.</p>
      </>
    ),
  },
  {
    id: "claude",
    title: "Talk to it from Claude",
    body: (
      <p>TaylorTrader includes an MCP server, so Claude can check on it and adjust it for you: "how did the bot
        do this week?", "pause new buys". It follows the same rules as this dashboard, and it can't go live or see
        your keys. The README has the one-line setup.</p>
    ),
  },
  {
    id: "faq",
    title: "Questions",
    body: (
      <dl className="guide__dl">
        <dt>Why didn't it buy anything?</dt>
        <dd>Usually the strategy's rules weren't met, the daily loss limit was hit, buys are paused, or it's in
          dry run. The Orders and Service log on the Overview say which.</dd>
        <dt>What happens if my Mac sleeps or turns off?</dt>
        <dd>Stop-losses still work, because they live at Alpaca. New buys and sells wait until it's back. A missed
          invest-plan day is bought on the next run.</dd>
        <dt>Where are my settings and data?</dt>
        <dd>Settings in <code>config.toml</code>, trades in a journal file, both in the TaylorTrader folder
          (~/Library/Application Support/TaylorTrader when installed). Keys are in the Keychain.</dd>
        <dt>How do I update or uninstall?</dt>
        <dd><code>uv tool upgrade taylortrader</code> updates it. <code>taylortrader dashboard --stop</code>, then
          stopping the bot in Settings, then <code>uv tool uninstall taylortrader</code> removes it.</dd>
      </dl>
    ),
  },
];

export function GuidePage() {
  return (
    <main className="page page--narrow guide">
      <header className="page__head">
        <div>
          <h1>Guide</h1>
          <p className="page__sub">How TaylorTrader works, what keeps your money safe, and what to expect.</p>
        </div>
      </header>
      <nav className="guide__toc" aria-label="On this page">
        {SECTIONS.map((s) => (
          <a key={s.id} href={`#/guide`} onClick={(e) => {
            e.preventDefault();
            document.getElementById(`g-${s.id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
          }}>{s.title}</a>
        ))}
      </nav>
      <div className="stack">
        {SECTIONS.map((s) => (
          <section key={s.id} id={`g-${s.id}`} className="panel guide__section" aria-labelledby={`g-${s.id}-h`}>
            <h2 id={`g-${s.id}-h`}>{s.title}</h2>
            {s.body}
          </section>
        ))}
      </div>
    </main>
  );
}
