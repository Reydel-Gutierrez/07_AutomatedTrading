import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api, type FirmEvent, type Overview } from "./api";
import { useEventStream, useResource } from "./hooks";
import Activity from "./pages/Activity";
import CapitalPage from "./pages/Capital";
import HQ from "./pages/HQ";
import Login from "./pages/Login";
import ManagerPage from "./pages/Manager";
import SystemPage from "./pages/System";

type Live = { events: FirmEvent[]; bump: number; connected: boolean; overview: Overview | null; reloadOverview: () => void };
const LiveContext = createContext<Live>({ events: [], bump: 0, connected: false, overview: null, reloadOverview: () => {} });
export const useLive = () => useContext(LiveContext);

export default function App() {
  const [session, setSession] = useState<{ user: string | null; has_admin: boolean } | null>(null);
  const check = useCallback(() => api.get<{ user: string | null; has_admin: boolean }>("/api/session").then(setSession).catch(() => setSession({ user: null, has_admin: true })), []);
  useEffect(() => {
    check();
  }, [check]);
  if (!session) return <div className="grid min-h-screen place-items-center text-slate-500">Opening HQ…</div>;
  if (!session.user) return <Login hasAdmin={session.has_admin} onLogin={check} />;
  return <Shell user={session.user} onLogout={() => api.post("/api/logout").finally(check)} />;
}

function Shell({ user, onLogout }: { user: string; onLogout: () => void }) {
  const [events, setEvents] = useState<FirmEvent[]>([]);
  const [bump, setBump] = useState(0);
  const connected = useEventStream((e) => {
    setEvents((prev) => [e, ...prev].slice(0, 200));
    setBump((b) => b + 1);
  });
  const { data: overview, reload } = useResource<Overview>("/api/firm", { every: 60000, bump });
  useEffect(() => {
    api.get<{ events: FirmEvent[] }>("/api/events?limit=60").then((r) => setEvents(r.events)).catch(() => {});
  }, []);
  const c = overview?.controls;
  return (
    <LiveContext.Provider value={{ events, bump, connected, overview, reloadOverview: reload }}>
      <div className="min-h-screen">
        <header className="sticky top-0 z-40 border-b border-ink-800 bg-ink-950/90 backdrop-blur">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-3">
            <NavLink to="/" className="font-display text-lg text-slate-50">
              Agentic <span className="text-gold-400">Capital</span>
            </NavLink>
            <nav className="flex gap-1 text-sm">
              {[["/", "Headquarters"], ["/capital", "Capital"], ["/activity", "Activity"], ["/system", "System"]].map(([to, label]) => (
                <NavLink key={to} to={to} end className={({ isActive }) => `rounded-lg px-3 py-1.5 ${isActive ? "bg-ink-800 text-slate-50" : "text-slate-400 hover:text-slate-200"}`}>
                  {label}
                </NavLink>
              ))}
            </nav>
            <div className="ml-auto flex flex-wrap items-center gap-2 text-xs">
              <Light ok={!!overview?.broker.connected} label={overview?.broker.connected ? "Broker connected" : "Broker offline"} />
              <Light ok={connected} label={connected ? "Live" : "Reconnecting"} />
              {c && <Light ok={c.live_writes_enabled} label={c.live_writes_enabled ? "Live orders on" : "Live orders off"} neutral={!c.live_writes_enabled} />}
              {c?.kill_switch.engaged && <span className="rounded-full bg-rose-600 px-2 py-0.5 font-semibold text-white">KILL SWITCH</span>}
              {c?.reconcile_hold.active && <span className="rounded-full bg-amber-500 px-2 py-0.5 font-semibold text-ink-950">BUYS PAUSED</span>}
              {c?.drawdown_brake.engaged && <span className="rounded-full bg-amber-500 px-2 py-0.5 font-semibold text-ink-950">DRAWDOWN BRAKE</span>}
              <button onClick={onLogout} className="ml-2 text-slate-500 hover:text-slate-300">{user} · sign out</button>
            </div>
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">
          <Routes>
            <Route path="/" element={<HQ />} />
            <Route path="/managers/:id" element={<ManagerPage />} />
            <Route path="/capital" element={<CapitalPage />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="/system" element={<SystemPage />} />
          </Routes>
        </main>
      </div>
    </LiveContext.Provider>
  );
}

function Light({ ok, label, neutral = false }: { ok: boolean; label: string; neutral?: boolean }) {
  const color = neutral ? "bg-slate-500" : ok ? "bg-emerald-400" : "bg-rose-500";
  return (
    <span className="inline-flex items-center gap-1.5 rounded-full border border-ink-700 px-2 py-0.5 text-slate-300">
      <span className={`h-1.5 w-1.5 rounded-full ${color}`} />
      {label}
    </span>
  );
}
