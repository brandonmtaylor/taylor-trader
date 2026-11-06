# Proposal: long-term investing mode

Status: phases 1 and 2 built (2026-11-05). Phase 3 (selling to rebalance) not started.

## The idea

Some people don't want a trading bot. They want "put $200 a month into a simple, diversified
portfolio and keep it balanced", done reliably, without logging in every month. TaylorTrader
already has the hard parts: an always-on service, a broker adapter, guardrails, alerts, a journal
and a dashboard. An **invest plan** reuses all of it, with no strategies, signals or stop-losses.

## What the user sets up

```toml
[invest]
enabled = true
amount = 200            # dollars per contribution
every = "month"         # week | 2weeks | month | once (lump sum)
day = 1                 # day of the month, or weekday for weekly
until = "2028-11-01"    # optional: stop after this date (or after a total)
rebalance = "new-money" # new-money (default) | bands
band_pts = 5            # bands only: sell back when a holding drifts this far from its weight
holdings = [
  { symbol = "VTI",  weight = 60 },
  { symbol = "VXUS", weight = 20 },
  { symbol = "BND",  weight = 20 },
]
```

In the dashboard this is an **Invest** page: start from an example portfolio, edit the weights,
pick the amount and schedule, and see the next buy before it happens.

## How it behaves

**Buying.** On each scheduled day, about 30 minutes after the open, the bot invests `amount` from
cash already in the account. It sends the money to whatever is furthest **under** its target weight
first, so new money does the rebalancing (M1 Finance and Betterment work this way, and it avoids
selling). Orders are fractional, dollar-amount market orders (`notional`, time in force `day`).
Each contribution is recorded with its period ("2026-11"), so a restart, a crash or a second
service can never buy the same month twice. A missed day (the Mac was asleep) is bought on the next
run, and the alert says so.

**Not enough cash.** The bot can't move money from a bank; Alpaca's Trading API has no transfers.
If cash is short, it invests what's there and alerts: "Invested $120 of $200; deposit more at
Alpaca." The user sets up deposits on Alpaca's side.

**Dividends.** Alpaca pays them in cash and has no automatic reinvestment, so the bot treats
dividend cash in the plan as new money and invests it with the next contribution. Paper accounts
don't pay dividends, so this only shows up live.

**Rebalancing by selling** is opt-in (`rebalance = "bands"`). Checked quarterly, it sells only
what's more than `band_pts` off target, back to the edge of the band rather than all the way, and
never sells shares held under a year at a gain unless the user overrides. Before any sell it shows
the planned trades and their estimated tax effect, and waits for approval in the dashboard (or
through Claude via MCP).

**Lump sum vs. spreading it out.** When the user has a large amount to invest at once, the page
says what Vanguard found: investing it all at once beat spreading it over months about two-thirds
of the time. Spreading it out (`every` + `until`) is there for people who would regret buying
right before a drop. The user chooses.

## Safety: an investing profile in the guardrails

The trading guardrails stay exactly as they are. Plan orders go through a separate check:

| Rule | Trading | Investing |
|---|---|---|
| Stop-loss on every buy | required | none (a stop sells the dips a long-term investor wants to hold through) |
| Daily loss limit blocks buys | yes | no: buying on down days is the point |
| Kill switch | blocks | blocks |
| What it can buy | strategy symbols | only the plan's symbols, checked as tradable and fractionable |
| Size cap | position % of account | each order at most the contribution (or the approved rebalance), with a hard cap in code |
| Sells | strategy exits | only approved rebalances, or the user selling |

Other changes:
- The unmanaged-position check knows plan holdings are managed and need no stop, so there's no
  "unmanaged" alert and no automatic stop gets added.
- A symbol belongs to exactly one owner, the plan or one strategy, and settings refuse overlaps.
  Alpaca holds one position per symbol, so two owners couldn't be told apart.
- Strategies size from the **trading slice**: account value minus the plan's holdings and its
  uninvested contributions. A strategy's "50%" never spends money meant for the plan.

## What the dashboard shows

- Progress toward the target, current vs. target weights as one bar per holding, and drift.
- Put in vs. now worth (contributions vs. growth), and dividends received.
- The next scheduled buy and how it will be split; the history of contributions.
- The daily email and MCP get a line each: "Invest plan: $2,400 in, worth $2,551; next $200 on Dec 1."

## Phases

1. **Core: buys and safety.** Settings and validation; the plan math as pure, tested functions
   (split a contribution, measure drift); notional orders in the broker adapter; the investing
   check in guardrails; the scheduler with once-per-period buys; plan-aware unmanaged and stop
   checks; trading-slice sizing; journal tables; alerts; the daily email line. Usable from config
   alone. About two sessions.
2. **Dashboard and MCP.** The Invest page (examples, weights editor, schedule, preview, progress)
   and MCP tools (status, preview, pause). About one session.
3. **Selling to rebalance.** Bands, a lot ledger (Alpaca appears to sell oldest shares first,
   FIFO, with no lot choice through the API, so the bot keeps its own record), short-term-gain
   and wash-sale warnings, approval flow. Plus a "what if" simulator on past prices: this plan
   since 2015, monthly vs. all at once. About one to two sessions.

Phases 1–2 cover what most people want. Phase 3 can wait until someone needs selling.

## Decisions for the owner

1. **Same paper account or a separate one during the paper trial?** Same account is the real
   design (slices), but it changes how much the trading strategies get during their trial, and a
   small paper account can't fund both. Options: raise the paper balance (reset it in Alpaca) and
   run both, or keep the plan in preview-only mode until the trading trial ends.
2. **Which example portfolios to include.** Suggested: a three-fund (US/international/bonds) at
   80/20 and 60/40 stocks/bonds, a single all-world fund (VT), and a conservative mostly-bonds mix.
   They're labeled as examples, not recommendations, with no questionnaire that picks one for you.
3. **Selling in phase 1?** Suggested no: new money only, which needs no tax handling.

## Already checked

- Alpaca's Trading API has no built-in rebalancing or recurring buys for individual accounts
  (only the Broker API for partner apps), so the bot does it.
- Fractional trading is enabled on the paper account. VTI, VXUS, BND, VOO, VT, SCHD, AGG, BNDX,
  SGOV, SCHB, VXF, IAU and VTIP are all tradable and fractionable (checked live, 2026-11-05).
  The bot still checks each symbol at startup and before each order.
- No commissions for self-directed stock/ETF trades; small regulatory fees apply to sells.
- Vanguard: no single best rebalancing schedule; annual checks with ~5-point bands are a common
  default, and the allocation itself matters far more than the rebalancing rule.

## Open-source and legal

Software the user runs on their own account, with labeled example portfolios and no
"answer these questions and we'll pick for you", is the conservative shape. SEC guidance on
robo-advisers targets registered advisers managing client accounts. Whether free self-run software
is clearly outside that isn't settled in anything found, so this goes on the list for the lawyer
before taking money (it's already in the plan).

Sources: Alpaca fractional trading and paper trading docs; Vanguard, "Cost averaging: invest now or
temporarily hold your cash?" (2023) and rebalancing research (2010, 2022, 2024); IRS Publication 550
(wash sales, holding periods); SEC IM Guidance Update 2017-02; M1 Finance and Betterment rebalancing
help pages.
