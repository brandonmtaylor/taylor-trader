import { useEffect, useState } from "react";
import { get, SIGN_IN_EVENT, type Session, type Setup, type Status } from "./api";
import { Header } from "./components/Header";
import { InvestPage } from "./pages/InvestPage";
import { Overview } from "./pages/Overview";
import { SettingsPage } from "./pages/SettingsPage";
import { SignInPage } from "./pages/SignInPage";
import { SetupPage } from "./pages/SetupPage";
import { StrategiesPage } from "./pages/StrategiesPage";
import { useSettings } from "./settings";
import { useNow, usePoll } from "./usePoll";

const ROUTES = ["/", "/strategies", "/invest", "/settings", "/setup"] as const;
type Route = (typeof ROUTES)[number];

function useRoute(): Route {
  const read = (): Route => {
    const r = window.location.hash.replace(/^#/, "") || "/";
    return (ROUTES as readonly string[]).includes(r) ? (r as Route) : "/";
  };
  const [route, setRoute] = useState<Route>(read);
  useEffect(() => {
    const on = () => {
      setRoute(read());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}

/** null while checking; with Wi-Fi access on, other devices sign in before anything else loads. */
function useSession(): [Session | null, (s: Session) => void] {
  const [session, setSession] = useState<Session | null>(null);
  useEffect(() => {
    const check = () => get<Session>("/api/session").then(setSession).catch(() =>
      setSession({ wifi: false, here: true, signed_in: true }));
    check();
    window.addEventListener(SIGN_IN_EVENT, check);
    return () => window.removeEventListener(SIGN_IN_EVENT, check);
  }, []);
  return [session, setSession];
}

export function App() {
  const [session, setSession] = useSession();
  if (session == null) return null;
  if (!session.signed_in) return <SignInPage onSignedIn={setSession} />;
  return <Dashboard />;
}

function Dashboard() {
  const route = useRoute();
  const now = useNow(1000);
  const status = usePoll<Status>("/api/status", 5_000);
  const setupPoll = usePoll<Setup>("/api/setup", 20_000);
  // A change returns fresh setup state at once; use it until the next poll is newer.
  const [fresh, setFresh] = useState<{ at: number; data: Setup } | null>(null);
  const setup = fresh && fresh.at > (setupPoll.loadedAt ?? 0) ? fresh.data : setupPoll.data;
  const store = useSettings();

  // Any change can affect everything else: refresh status, setup and settings together.
  const onSetup = (s: Setup) => {
    setFresh({ at: Date.now(), data: s });
    status.reload();
    store.reload();
  };
  const onStatus = () => {
    status.reload();
    setupPoll.reload();
    store.reload();
  };

  const needsSetup = setup != null && (!setup.configured || !setup.has_strategy ||
    !setup.secrets.ALPACA_PAPER_KEY_ID || !setup.secrets.ALPACA_PAPER_SECRET_KEY);
  const page: Route = route === "/setup" || (needsSetup && route === "/") ? "/setup" : route;

  useEffect(() => {
    document.title = { "/": "TaylorTrader", "/strategies": "Strategies · TaylorTrader", "/invest": "Invest · TaylorTrader",
      "/settings": "Settings · TaylorTrader", "/setup": "Set up · TaylorTrader" }[page];
  }, [page]);

  return (
    <div className={`app app--${status.data?.mode ?? "paper"}`}>
      <Header status={status.data} setup={setup} now={now} error={status.error} route={page}
        onStatus={onStatus} onSetup={onSetup} />
      {page === "/" && <Overview status={status.data} onStatus={() => status.reload()} />}
      {page === "/strategies" && <StrategiesPage store={store} />}
      {page === "/invest" && <InvestPage />}
      {page === "/settings" && <SettingsPage store={store} setup={setup} onSetup={onSetup} />}
      {page === "/setup" && <SetupPage store={store} setup={setup} onSetup={onSetup} />}
      <footer className="foot">
        Runs on this computer only. Not financial advice. TaylorTrader is open-source software you run on your
        own account.
      </footer>
    </div>
  );
}
