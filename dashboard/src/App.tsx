import { useState } from "react";
import type { Account, EventRow, History, OrderRow, Performance, Status } from "./api";
import { EquityChart } from "./components/EquityChart";
import { Header } from "./components/Header";
import { Events, Orders, PastResults, Positions, Strategies, Vitals } from "./components/Panels";
import { useNow, usePoll } from "./usePoll";

export function App() {
  const [period, setPeriod] = useState<Performance["period"]>("1M");
  const now = useNow(1000);
  const status = usePoll<Status>("/api/status", 5_000);
  const account = usePoll<Account>("/api/account", 15_000);
  const perf = usePoll<Performance>(`/api/performance?period=${period}`, 300_000);
  const orders = usePoll<OrderRow[]>("/api/orders?limit=40", 15_000);
  const events = usePoll<EventRow[]>("/api/events?limit=40", 15_000);
  const history = usePoll<History>("/api/history", 3_600_000);

  return (
    <div className={`app app--${status.data?.mode ?? "paper"}`}>
      <Header status={status.data} now={now} error={status.error} />
      <main className="layout">
        <Vitals account={account.data} status={status.data} />
        <div className="col col--main">
          <EquityChart perf={perf.data} period={period} onPeriod={setPeriod} error={perf.error} />
          <Positions account={account.data} error={account.error} />
          <Orders orders={orders.data} error={orders.error} />
        </div>
        <aside className="col col--side">
          <Events events={events.data} />
          <Strategies status={status.data} />
          <PastResults history={history.data} />
        </aside>
      </main>
      <footer className="foot">
        Read-only view on this computer. Not financial advice. TaylorTrader is open-source software you run on your
        own account.
      </footer>
    </div>
  );
}
