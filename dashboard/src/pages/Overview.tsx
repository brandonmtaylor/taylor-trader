import { useState } from "react";
import type { Account, AIDecision, AIGrades as Grades, ChartPeriod, EventRow, History, InvestResp, OrderRow, Performance, PositionRow, Results, Status, TradeChartData } from "../api";
import { AIGrades } from "../components/AIGrades";
import { EquityChart } from "../components/EquityChart";
import { InvestCard } from "../components/InvestCard";
import { AIDecisions, Events, Orders, PastResults, Positions, ResultsPanel, Strategies, Vitals } from "../components/Panels";
import { SellDialog } from "../components/Sell";
import { TradeChart } from "../components/TradeChart";
import { usePoll } from "../usePoll";

export function Overview({ status, onStatus }: { status: Status | null; onStatus: () => void }) {
  const [period, setPeriod] = useState<Performance["period"]>("1M");
  const account = usePoll<Account>("/api/account", 15_000);
  const perf = usePoll<Performance>(`/api/performance?period=${period}`, 300_000);
  const results = usePoll<Results>("/api/results", 60_000);
  const orders = usePoll<OrderRow[]>("/api/orders?limit=40", 15_000);
  const events = usePoll<EventRow[]>("/api/events?limit=40", 15_000);
  const history = usePoll<History>("/api/history", 3_600_000);
  const ai = usePoll<AIDecision[]>("/api/ai/decisions?limit=30", 30_000);
  const grades = usePoll<Grades>("/api/ai/grades", 600_000);
  const invest = usePoll<InvestResp>("/api/invest", 60_000);
  const [chartSymbol, setChartSymbol] = useState("");
  const [chartPeriod, setChartPeriod] = useState<ChartPeriod>("1M");
  const chart = usePoll<TradeChartData>(`/api/chart?symbol=${encodeURIComponent(chartSymbol)}&period=${chartPeriod}`, 60_000);
  // undefined: closed. null: sell everything. a row: sell that one.
  const [selling, setSelling] = useState<PositionRow | null | undefined>(undefined);
  const [sold, setSold] = useState<string | null>(null);

  const done = (message: string) => {
    setSold(message);
    account.reload();
    orders.reload();
    events.reload();
    results.reload();
    chart.reload();
    onStatus();
  };

  return (
    <main className="layout">
      <Vitals account={account.data} status={status} />
      <div className="col col--main">
        <EquityChart perf={perf.data} period={period} onPeriod={setPeriod} error={perf.error} />
        <ResultsPanel results={results.data} perf={perf.data} error={results.error} />
        <TradeChart data={chart.data} error={chart.error} symbol={chartSymbol} period={chartPeriod}
          onSymbol={setChartSymbol} onPeriod={setChartPeriod} />
        <Positions account={account.data} error={account.error} onSell={(row) => { setSold(null); setSelling(row); }} notice={sold}
          marketOpen={status?.market?.is_open ?? null} />
        <Orders orders={orders.data} error={orders.error} />
      </div>
      <aside className="col col--side">
        <InvestCard data={invest.data} />
        <AIGrades grades={grades.data} error={grades.error} />
        <AIDecisions rows={ai.data} error={ai.error} />
        <Events events={events.data} />
        <Strategies status={status} />
        <PastResults history={history.data} />
      </aside>
      <SellDialog
        open={selling !== undefined}
        target={selling ?? null}
        positions={(account.data?.positions ?? []).filter((p) => !p.selling)}
        live={status?.mode === "live"}
        marketOpen={status?.market?.is_open ?? null}
        onClose={() => setSelling(undefined)}
        onDone={done}
      />
    </main>
  );
}
