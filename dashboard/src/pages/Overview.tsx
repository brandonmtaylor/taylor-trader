import { useState } from "react";
import type { Account, EventRow, History, OrderRow, Performance, Status } from "../api";
import { EquityChart } from "../components/EquityChart";
import { Events, Orders, PastResults, Positions, Strategies, Vitals } from "../components/Panels";
import { usePoll } from "../usePoll";

export function Overview({ status }: { status: Status | null }) {
  const [period, setPeriod] = useState<Performance["period"]>("1M");
  const account = usePoll<Account>("/api/account", 15_000);
  const perf = usePoll<Performance>(`/api/performance?period=${period}`, 300_000);
  const orders = usePoll<OrderRow[]>("/api/orders?limit=40", 15_000);
  const events = usePoll<EventRow[]>("/api/events?limit=40", 15_000);
  const history = usePoll<History>("/api/history", 3_600_000);

  return (
    <main className="layout">
      <Vitals account={account.data} status={status} />
      <div className="col col--main">
        <EquityChart perf={perf.data} period={period} onPeriod={setPeriod} error={perf.error} />
        <Positions account={account.data} error={account.error} />
        <Orders orders={orders.data} error={orders.error} />
      </div>
      <aside className="col col--side">
        <Events events={events.data} />
        <Strategies status={status} />
        <PastResults history={history.data} />
      </aside>
    </main>
  );
}
