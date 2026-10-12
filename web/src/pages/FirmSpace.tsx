import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useLive } from "../App";
import { Feed } from "../components/Feed";
import { AGENTS, Badge, Empty } from "../components/ui";
import { usePageVisible, useResource } from "../hooks";
import { ago, when } from "../format";
import { BRAND, Monogram } from "../brand";
import { CioScene, ManagerScene } from "../space/Scene";
import { type AgentState, CIO_TYPES, PHASE, type Space, STATE_META, cioState, managerState } from "../space/state";

const FLASH_MS = 9000;

function useNow(every: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), every);
    return () => window.clearInterval(t);
  }, [every]);
  return now;
}

const etHour = (now: number) => Number(new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", hourCycle: "h23" }).format(now)) % 24;

export default function FirmSpace() {
  const { events, bump, connected } = useLive();
  const visible = usePageVisible();
  // Poll only while the tab is visible (refreshes on return); live events refresh immediately via `bump`.
  const { data: space, error } = useResource<Space>("/api/space", { every: visible ? 5000 : 0, bump });
  const now = useNow(5000);

  const states = useMemo(() => {
    if (!space) return null;
    const out: Record<string, AgentState> = { cio: cioState(space.cio, events, now) };
    for (const a of space.agents) out[a.id] = managerState(a, events, now);
    return out;
  }, [space, events, now]);

  // A room lights up briefly when a real event for that agent arrives on the live stream.
  const fresh = useMemo(() => {
    const out: Record<string, number> = {};
    for (const e of events) {
      if (now - Date.parse(e.ts) > FLASH_MS) break;
      const who = e.manager_id ?? (CIO_TYPES.has(e.type) ? "cio" : null);
      if (who && !out[who]) out[who] = e.id;
    }
    return out;
  }, [events, now]);

  if (error) return <Empty>{error.message}</Empty>;
  if (!space || !states) return <div className="text-slate-500">Opening the office…</div>;
  const byId = Object.fromEntries(space.agents.map((a) => [a.id, a]));
  const floorLog = events.filter((e) => e.manager_id || CIO_TYPES.has(e.type) || e.type.startsWith("ORDER_")).slice(0, 10);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="label">Firm Space</div>
          <h1 className="mt-1 font-display text-3xl text-slate-50">The office, live</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">Every status, task, and symbol you see comes from the firm's records as it happens. Select a person or their desk to open their workspace.</p>
        </div>
        <span className="inline-flex items-center gap-2 text-xs text-slate-400">
          <span className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-emerald-400" : "bg-rose-500"}`} />
          {connected ? "Connected to the live event stream" : "Reconnecting to the event stream…"}
        </span>
      </header>

      <div className="fs-building rounded-[26px] border border-ink-700 bg-[#0c1017] p-2 shadow-[0_30px_80px_-40px_rgba(0,0,0,0.8)] sm:p-3">
        <div className="flex items-center justify-between px-2 pb-2.5 pt-1">
          <span className="flex items-center gap-2">
            <Monogram size={22} />
            <span className="text-[11px] font-semibold tracking-[0.3em] text-slate-300">THE 537 FIRM</span>
          </span>
          <span className="hidden text-[10px] uppercase tracking-[0.22em] text-slate-600 sm:inline">Executive floor · Investment floor</span>
        </div>
        <div className="grid gap-2 md:grid-cols-3">
          <RoomCard to="/cio" name="CIO" title={AGENTS.cio.title} state={states.cio} fresh={fresh.cio} wide cta="Enter the CIO Office">
            <CioScene state={states.cio} pending={space.cio.pending_proposals.length} hourEt={etHour(now)} />
          </RoomCard>
          <Lobby space={space} />
        </div>
        <div className="my-2 h-1.5 rounded-full bg-ink-800/80" aria-hidden="true" />
        <div className="grid gap-2 md:grid-cols-3">
          {["tim", "alex", "max"].map((id) => {
            const a = byId[id];
            if (!a) return null;
            return (
              <RoomCard key={id} to={`/managers/${id}`} name={a.name} title={a.role} mode={a.mode} state={states[id]} fresh={fresh[id]} cta={`Open ${a.name}'s workspace`}>
                <ManagerScene id={id} state={states[id]} nextRun={a.next_run} phase={a.implemented ? undefined : PHASE[id]} />
              </RoomCard>
            );
          })}
        </div>
      </div>

      <section className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <div className="card">
          <header className="mb-4 flex items-center justify-between">
            <h2 className="label">Floor log</h2>
            <Link to="/activity" className="text-xs text-gold-400 hover:underline">All activity</Link>
          </header>
          <Feed events={floorLog} compact />
        </div>
        <Legend />
      </section>
    </div>
  );
}

function RoomCard({ to, name, title, mode, state, fresh, wide = false, cta, children }: { to: string; name: string; title: string; mode?: string; state: AgentState; fresh?: number; wide?: boolean; cta: string; children: ReactNode }) {
  const m = STATE_META[state.key];
  const color = AGENTS[name.toLowerCase()]?.color ?? "#94a3b8";
  return (
    <Link
      key={fresh ?? "steady"}
      to={to}
      aria-label={`${name}, ${m.label}: ${state.headline}. ${cta}.`}
      className={`group relative flex flex-col overflow-hidden rounded-2xl border border-ink-700/80 bg-ink-900 outline-none transition hover:border-ink-600 focus-visible:ring-2 focus-visible:ring-gold-500 ${wide ? "md:col-span-2" : ""} ${fresh ? "fs-flash" : ""}`}
      style={{ ["--glow" as string]: `${color}88` }}
    >
      <div className={`relative aspect-[8/5] md:aspect-auto md:h-64 overflow-hidden`}>
        <div className="absolute inset-0 transition duration-500 motion-safe:group-hover:scale-[1.015]">{children}</div>
        <div className="pointer-events-none absolute left-3 top-3 flex items-center gap-2 rounded-full bg-ink-950/75 py-1 pl-1.5 pr-3 backdrop-blur">
          <span className="h-2 w-2 rounded-full" style={{ background: color }} />
          <span className="text-xs font-semibold text-slate-100">{name}</span>
          {mode && <Badge tone={mode}>{mode}</Badge>}
        </div>
        <div className="pointer-events-none absolute right-3 top-3 inline-flex items-center gap-1.5 rounded-full bg-ink-950/75 px-2.5 py-1 text-[11px] font-semibold backdrop-blur" style={{ color: m.color }}>
          <span className={`h-1.5 w-1.5 rounded-full ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.color }} />
          {m.label}
        </div>
      </div>
      <div className="flex flex-1 flex-col gap-1.5 border-t border-ink-800 p-4">
        <div className="text-[11px] uppercase tracking-[0.14em] text-slate-500">{title}</div>
        <p className="font-medium text-slate-100">{state.headline}</p>
        {state.detail && <p className="line-clamp-2 text-sm text-slate-400" title={state.detail}>{state.detail}</p>}
        {state.symbols.length > 0 && (
          <div className="flex flex-wrap gap-1.5 pt-0.5">
            {state.symbols.slice(0, 6).map((s, i) => (
              <span key={`${s.symbol}-${i}`} className="rounded-md border border-ink-700 bg-ink-850 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-200">
                {s.side && <span className={s.side === "sell" ? "text-rose-300" : "text-emerald-300"}>{s.side.toUpperCase()} </span>}
                {s.symbol}
              </span>
            ))}
            {state.book === "SIM" && <span className="rounded-md bg-sky-500/10 px-1.5 py-0.5 text-[11px] font-medium text-sky-300">simulation</span>}
          </div>
        )}
        <div className="mt-auto flex items-center justify-between gap-3 pt-2 text-xs text-slate-500">
          <span title={state.at ? `${when(state.at)} · source: ${state.source}` : `Source: ${state.source}`}>{state.at ? `${ago(state.at)} · ` : ""}{state.source}</span>
          <span className="shrink-0 text-gold-400 opacity-80 transition group-hover:opacity-100">{cta} →</span>
        </div>
      </div>
    </Link>
  );
}

function Clock() {
  const now = useNow(1000);
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "numeric", second: "numeric", hourCycle: "h23" }).formatToParts(now);
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  const h = get("hour") % 12, mnt = get("minute"), s = get("second");
  const hand = (deg: number, len: number, w: number, c: string) => <path d={`M0 0 L0 ${-len}`} stroke={c} strokeWidth={w} strokeLinecap="round" transform={`rotate(${deg})`} />;
  return (
    <svg viewBox="-30 -30 60 60" className="h-16 w-16" role="img" aria-label="New York time">
      <circle r="28" fill="#0d1017" stroke="#b8975a" strokeWidth="2" />
      {Array.from({ length: 12 }, (_, i) => <path key={i} d="M0 -24 V-21" stroke="#64748b" strokeWidth={i % 3 ? 1 : 2} transform={`rotate(${i * 30})`} />)}
      {hand(h * 30 + mnt / 2, 13, 2.6, "#e5e7eb")}
      {hand(mnt * 6, 19, 1.8, "#e5e7eb")}
      {hand(s * 6, 21, 0.8, "#cdb27a")}
      <circle r="2" fill="#cdb27a" />
    </svg>
  );
}

function Lobby({ space }: { space: Space }) {
  const c = space.controls;
  const flags = [c.kill_switch && "Kill switch engaged", c.reconcile_hold && "Buying paused (reconcile)", c.drawdown_brake && "Drawdown brake engaged"].filter(Boolean) as string[];
  const rows: [string, string, boolean | null][] = [
    ["Market", space.market.regular_hours_open ? "Open" : space.market.trading_day ? "Closed now" : "Closed today", space.market.regular_hours_open ?? false],
    ["Broker", space.broker_connected ? "Connected" : "Offline", space.broker_connected],
    ["Live orders", c.live_writes_enabled ? "Enabled" : "Off (owner switch)", null],
    ["Safeguards", flags.length ? flags.join(" · ") : "All clear", !flags.length],
  ];
  return (
    <section className="flex flex-col overflow-hidden rounded-2xl border border-ink-700/80 bg-gradient-to-b from-[#16130f] to-ink-900" aria-label="Lobby">
      <div className="relative flex flex-1 flex-col items-center justify-center gap-3 px-5 pb-4 pt-6 md:h-64 md:flex-none">
        <div className="text-[9px] font-medium uppercase tracking-[0.34em] text-slate-500">{BRAND.descriptor}</div>
        <div className="font-display text-2xl tracking-[0.16em] text-gold-300">THE 537 FIRM</div>
        <div className="h-px w-20 bg-gradient-to-r from-transparent via-gold-500/60 to-transparent" />
        <div className="text-xs tracking-wide text-slate-400">{BRAND.tagline}</div>
        <div className="mt-1 flex items-center gap-3">
          <Clock />
          <div className="text-left text-[11px] leading-tight text-slate-500">
            New York
            <br />
            <span className="text-slate-300">{new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "long" }).format(Date.now())}</span>
          </div>
        </div>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 border-t border-ink-800 p-4 text-xs">
        {rows.map(([k, v, ok]) => (
          <div key={k} className="min-w-0">
            <dt className="text-slate-500">{k}</dt>
            <dd className="mt-0.5 flex items-center gap-1.5 text-slate-200">
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${ok === null ? "bg-slate-500" : ok ? "bg-emerald-400" : "bg-amber-400"}`} />
              <span className="truncate" title={v}>{v}</span>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function Legend() {
  const keys = ["analyzing", "reviewing", "preparing", "executing", "completed", "error", "idle", "inactive"] as const;
  const meaning: Record<string, string> = {
    analyzing: "A manager's run is in progress on the scheduler.",
    reviewing: "The CIO is answering you right now, step by step.",
    preparing: "A decision was recorded and orders are going to the desk.",
    executing: "Orders are open at the broker.",
    completed: "A decision, fill, or proposal was just recorded.",
    error: "Something failed and needs attention.",
    idle: "Nothing in progress; shows the last decision and the next one.",
    inactive: "Not operational yet.",
  };
  return (
    <div className="card">
      <h2 className="label mb-4">Reading the floor</h2>
      <ul className="space-y-2 text-sm">
        {keys.map((k) => (
          <li key={k} className="flex gap-2.5">
            <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: STATE_META[k].color }} />
            <span>
              <span className="text-slate-200">{STATE_META[k].label}</span> <span className="text-slate-500">— {meaning[k]}</span>
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-4 border-t border-ink-800 pt-3 text-xs text-slate-500">
        Breathing, blinking, plants, steam, clouds, and lamp glow are decorative. Statuses, tasks, symbols, the in-tray on the CIO's desk (one sheet per pending proposal), and the window's time of day come from live records.
      </p>
    </div>
  );
}
