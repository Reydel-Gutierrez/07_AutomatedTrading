import { type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useLive } from "../App";
import { type FirmEvent, type ManagerDetail, type Usage } from "../api";
import { Donut } from "../components/charts";
import { AGENTS, Avatar, Badge, Empty } from "../components/ui";
import { usePageVisible, useResource } from "../hooks";
import { ago, money, pct, signedPct, tone, when } from "../format";
import { Foreground, Partition, RoomDecor, Shell, StateGradients, VB, Workstation, headPoint, type AgentId } from "../space/Building";
import { type AgentState, CIO_TYPES, type Space, STATE_META, cioState, etTime, managerState } from "../space/state";

const IDS: AgentId[] = ["cio", "tim", "alex", "max"];
const NAMES: Record<AgentId, string> = { cio: "CIO", tim: "Tim", alex: "Alex", max: "Max" };
const ALLOC_COLORS: Record<string, string> = { tim: AGENTS.tim.color, alex: AGENTS.alex.color, max: AGENTS.max.color, unallocated: "#475569" };

function useNow(every: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), every);
    return () => window.clearInterval(t);
  }, [every]);
  return now;
}

/** Height of the app header, which wraps on narrower screens. */
function useHeaderHeight() {
  const [h, setH] = useState(57);
  useEffect(() => {
    const el = document.querySelector("header");
    if (!el) return;
    const ro = new ResizeObserver(() => setH(el.getBoundingClientRect().height));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return h;
}

function useWide() {
  const [wide, setWide] = useState(() => window.matchMedia("(min-width: 1024px)").matches);
  useEffect(() => {
    const mq = window.matchMedia("(min-width: 1024px)");
    const on = () => setWide(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return wide;
}

export default function FirmSpace() {
  const { events, bump } = useLive();
  const visible = usePageVisible();
  // Poll only while the tab is visible (refreshes on return); live events refresh immediately via `bump`.
  const { data: space, error } = useResource<Space>("/api/space", { every: visible ? 5000 : 0, bump });
  const now = useNow(5000);
  const [selected, setSelected] = useState<AgentId | null>(null);
  const header = useHeaderHeight();

  const states = useMemo(() => {
    if (!space) return null;
    const out = { cio: cioState(space.cio, events, now) } as Record<AgentId, AgentState>;
    for (const a of space.agents) out[a.id as AgentId] = managerState(a, events, now);
    return out;
  }, [space, events, now]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setSelected(null);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!space || !states) return <div className="p-6 text-slate-500">Opening the building…</div>;
  const pick = (id: AgentId) => () => setSelected((cur) => (cur === id ? null : id));

  return (
    <div className="grid bg-[#0a0e19] lg:grid-cols-[minmax(0,1fr)_360px]" style={{ ["--fs-h" as string]: `calc(100dvh - ${header}px)` }}>
      <div className="relative flex items-center justify-center overflow-hidden bg-gradient-to-b from-[#070b17] via-[#141a2e] to-[#14201a] lg:h-[var(--fs-h)]">
        <div className="relative w-full" style={{ aspectRatio: `${VB.w} / ${VB.h}`, maxWidth: `calc(var(--fs-h) * ${(VB.w / VB.h).toFixed(4)})` }}>
          <svg viewBox={`0 0 ${VB.w} ${VB.h}`} className="fs-building absolute inset-0 h-full w-full" role="group" aria-label="The 537 Firm building">
            <StateGradients />
            <g onClick={() => setSelected(null)}>
              <Shell />
            </g>
            <Workstation id="cio" state={states.cio} selected={selected === "cio"} onSelect={pick("cio")} pending={space.cio.pending_proposals.length} />
            <RoomDecor id="tim" off={states.tim.key === "inactive"} />
            <Workstation id="tim" state={states.tim} selected={selected === "tim"} onSelect={pick("tim")} />
            <Partition x={12} />
            <RoomDecor id="alex" off={states.alex.key === "inactive"} />
            <Workstation id="alex" state={states.alex} selected={selected === "alex"} onSelect={pick("alex")} />
            <Partition x={24} />
            <RoomDecor id="max" off={states.max.key === "inactive"} />
            <Workstation id="max" state={states.max} selected={selected === "max"} onSelect={pick("max")} />
            <Foreground />
          </svg>
          {IDS.map((id) => (
            <Indicator key={id} id={id} state={states[id]} active={selected === id} onClick={pick(id)} />
          ))}
          {selected && <SummaryCard id={selected} state={states[selected]} space={space} onClose={() => setSelected(null)} />}
          <p className="pointer-events-none absolute bottom-2 left-3 hidden text-[10px] text-slate-400/70 sm:block">
            Indicators come from live records. Breathing, plants, steam, and lighting are decorative.
          </p>
        </div>
      </div>
      <ul className="divide-y divide-ink-800 border-t border-ink-800 bg-ink-950 lg:hidden" aria-label="What each person is doing">
        {IDS.map((id) => {
          const st = states[id];
          const m = STATE_META[st.key];
          return (
            <li key={id}>
              <button onClick={pick(id)} className="flex w-full items-start gap-3 px-4 py-3 text-left">
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.working || st.key === "error" ? m.color : AGENTS[id].color }} />
                <span className="min-w-0 flex-1">
                  <span className="text-sm font-semibold text-slate-100">{NAMES[id]}</span>
                  <span className="block text-sm text-slate-300">{st.headline}</span>
                  {st.sub && <span className="block truncate text-xs text-slate-500">{st.sub}</span>}
                </span>
                <span className="text-slate-500" aria-hidden="true">›</span>
              </button>
            </li>
          );
        })}
      </ul>
      <OverviewPanel space={space} events={events} />
    </div>
  );
}

function Indicator({ id, state, active, onClick }: { id: AgentId; state: AgentState; active: boolean; onClick: () => void }) {
  const [x, y] = headPoint(id);
  const m = STATE_META[state.key];
  const color = AGENTS[id].color;
  return (
    <button
      onClick={onClick}
      aria-label={`${NAMES[id]}: ${state.headline}${state.sub ? `. ${state.sub}` : ""}. Open summary.`}
      className={`absolute z-10 max-w-[44%] -translate-x-1/2 -translate-y-full rounded-lg border bg-ink-950/85 px-1.5 py-1 text-left shadow-lg backdrop-blur transition hover:border-gold-500/60 sm:max-w-[240px] sm:px-2.5 sm:py-1.5 ${active ? "border-gold-500/70" : "border-white/10"}`}
      style={{ left: `${(x / VB.w) * 100}%`, top: `${(y / VB.h) * 100}%` }}
    >
      <span className="flex items-center gap-1.5">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full sm:h-2 sm:w-2 ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.working || state.key === "error" ? m.color : color }} />
        <span className="text-[10px] font-semibold text-slate-50 sm:text-xs">{NAMES[id]}</span>
        <span className="ml-auto hidden pl-2 text-slate-500 sm:inline" aria-hidden="true">›</span>
      </span>
      <span className="mt-0.5 hidden text-[11px] leading-snug text-slate-200 sm:line-clamp-2">{state.headline}</span>
      {state.sub && <span className="hidden text-[10px] leading-snug text-slate-400 md:line-clamp-1">{state.sub}</span>}
      <span className="absolute left-1/2 top-full h-2 w-2 -translate-x-1/2 -translate-y-1 rotate-45 border-b border-r border-white/10 bg-ink-950" aria-hidden="true" />
    </button>
  );
}

function SummaryCard({ id, state, space, onClose }: { id: AgentId; state: AgentState; space: Space; onClose: () => void }) {
  const { overview, bump } = useLive();
  const wide = useWide();
  const detail = useResource<ManagerDetail>(id === "cio" ? null : `/api/managers/${id}`, { every: 30000, bump });
  const usage = useResource<Usage>(id === "cio" ? "/api/cio/usage" : null, { every: 60000 });
  const [x, y] = headPoint(id);
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  // Beside the person, on whichever side has room, clamped inside the building view.
  useLayoutEffect(() => {
    const el = ref.current;
    const box = el?.parentElement;
    if (!wide || !el || !box) return setPos(null);
    const place = () => {
      const W = box.clientWidth, H = box.clientHeight, w = el.offsetWidth, h = el.offsetHeight;
      const ax = (x / VB.w) * W, ay = (y / VB.h) * H + 40;
      const left = ax + 30 + w < W - 8 ? ax + 30 : Math.max(8, ax - 30 - w);
      setPos({ left, top: Math.min(Math.max(8, ay - h / 2), Math.max(8, H - h - 8)) });
    };
    place();
    const ro = new ResizeObserver(place);
    ro.observe(box);
    ro.observe(el);
    return () => ro.disconnect();
  }, [wide, x, y, id]);
  const m = STATE_META[state.key];
  const card = overview?.managers.find((c) => c.id === id);
  const d = detail.data;
  const book = d?.active_book ?? "LIVE";
  const b = d?.books[book];
  const perf = b?.performance;
  const positions = b?.positions.filter((p) => p.quantity > 0 || p.target > 0) ?? [];
  const to = id === "cio" ? "/cio" : `/managers/${id}`;

  return (
    <div
      role="dialog"
      aria-label={`${NAMES[id]} summary`}
      ref={ref}
      className="fixed inset-x-2 bottom-2 z-30 max-h-[72vh] overflow-y-auto rounded-2xl border border-ink-600 bg-ink-900/95 p-4 shadow-2xl backdrop-blur lg:absolute lg:inset-x-auto lg:bottom-auto lg:max-h-[calc(100%-16px)] lg:w-[340px]"
      style={wide ? (pos ? { left: pos.left, top: pos.top } : { visibility: "hidden" }) : undefined}
    >
      <header className="flex items-start gap-3">
        <Avatar id={id} size={44} working={m.working} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-display text-lg text-slate-50">{NAMES[id]}</span>
            {card && <Badge tone={card.mode}>{card.mode}</Badge>}
          </div>
          <div className="truncate text-xs text-slate-400">{AGENTS[id].title}</div>
        </div>
        <button onClick={onClose} className="rounded-md px-2 py-1 text-slate-500 hover:bg-ink-800 hover:text-slate-200" aria-label="Close summary">✕</button>
      </header>

      <section className="mt-4 rounded-xl border border-ink-700 bg-ink-850 p-3">
        <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-wider" style={{ color: m.color }}>
          <span className={`h-1.5 w-1.5 rounded-full ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.color }} />
          Right now
        </div>
        <p className="mt-1.5 text-sm font-medium text-slate-100">{state.headline}</p>
        {state.detail && <p className="mt-1 text-xs leading-relaxed text-slate-400">{state.detail}</p>}
        {state.symbols.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {state.symbols.slice(0, 6).map((s, i) => (
              <span key={`${s.symbol}-${i}`} className="rounded border border-ink-600 px-1.5 py-0.5 text-[10px] font-semibold text-slate-200">
                {s.side && <span className={s.side === "sell" ? "text-rose-300" : "text-emerald-300"}>{s.side.toUpperCase()} </span>}
                {s.symbol}
              </span>
            ))}
            {state.book === "SIM" && <span className="rounded bg-sky-500/10 px-1.5 py-0.5 text-[10px] text-sky-300">simulation</span>}
          </div>
        )}
        <p className="mt-2 text-[10px] text-slate-500" title={when(state.at)}>
          {state.at ? `${ago(state.at)} · ` : ""}source: {state.source}
        </p>
      </section>

      {id === "cio" ? (
        <>
          <section className="mt-4">
            <h3 className="label mb-2">Awaiting your decision</h3>
            {space.cio.pending_proposals.length ? (
              <ul className="space-y-1.5 text-sm">
                {space.cio.pending_proposals.map((p) => <li key={p.id} className="text-slate-200">#{p.id} {p.title}</li>)}
              </ul>
            ) : (
              <p className="text-sm text-slate-500">No open proposals.</p>
            )}
          </section>
          <section className="mt-4">
            <h3 className="label mb-2">Recent decisions</h3>
            <EventList events={space.cio.recent.slice(0, 3)} empty="No proposals or memories recorded yet." />
          </section>
          {usage.data && (
            <p className="mt-4 text-xs text-slate-500">
              AI spend this month: {money(usage.data.agents.cio?.spent ?? 0)} of {money(usage.data.agents.cio?.cap ?? usage.data.monthly_usd)}
            </p>
          )}
        </>
      ) : !d ? (
        <p className="mt-4 text-sm text-slate-500">{detail.error ? detail.error.message : "Loading the portfolio…"}</p>
      ) : !d.implemented ? (
        <section className="mt-4 text-sm text-slate-300">
          <h3 className="label mb-2">Portfolio</h3>
          Allocated cash held for {NAMES[id]}: <span className="tabular-nums text-slate-100">{money(d.books.LIVE.cash)}</span>
        </section>
      ) : (
        <>
          <section className="mt-4">
            <h3 className="label mb-2">{book === "SIM" ? "Simulated portfolio" : "Portfolio"}</h3>
            <div className="grid grid-cols-3 gap-2 text-sm">
              <Mini label="Value" value={money(b?.nav)} />
              <Mini label="Cash" value={money(b?.cash)} />
              <Mini label="Return" value={<span className={tone(perf?.twr)}>{signedPct(perf?.twr)}</span>} />
            </div>
            {book === "SIM" && <p className="mt-1.5 text-[11px] text-sky-300/90">Simulation at live prices · {money(d.books.LIVE.cash)} of real cash allocated and waiting</p>}
            {positions.length > 0 && (
              <table className="mt-2 w-full text-xs">
                <tbody>
                  {positions.slice(0, 5).map((p) => (
                    <tr key={p.symbol} className="border-t border-ink-800">
                      <td className="py-1 font-semibold text-slate-200">{p.symbol}</td>
                      <td className="py-1 text-right tabular-nums text-slate-300">{money(p.value)}</td>
                      <td className="py-1 text-right tabular-nums text-slate-500">{pct(p.weight, 0)} of {pct(p.target, 0)} target</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section className="mt-4">
            <h3 className="label mb-2">Recent decisions</h3>
            <EventList events={d.runs.slice(0, 3)} strip={`${NAMES[id]}: `} empty="No decisions recorded yet." />
          </section>
        </>
      )}

      <Link to={to} className="btn-primary mt-4 w-full">
        {id === "cio" ? "Enter the CIO Office" : `Enter ${NAMES[id]}'s workspace`} →
      </Link>
    </div>
  );
}

function Mini({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg bg-ink-850 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-0.5 font-semibold tabular-nums text-slate-100">{value}</div>
    </div>
  );
}

function EventList({ events, strip = "", empty }: { events: FirmEvent[]; strip?: string; empty: string }) {
  if (!events.length) return <p className="text-sm text-slate-500">{empty}</p>;
  return (
    <ul className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="text-xs">
          <p className="text-slate-200">{e.summary.replace(strip, "")}</p>
          <p className="mt-0.5 text-slate-500" title={when(e.ts)}>Event #{e.id} · {ago(e.ts)}</p>
        </li>
      ))}
    </ul>
  );
}

function ClockFace({ now }: { now: number }) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(now);
  const h = Number(parts.find((p) => p.type === "hour")?.value ?? 0) % 12;
  const mn = Number(parts.find((p) => p.type === "minute")?.value ?? 0);
  return (
    <svg viewBox="-20 -20 40 40" className="h-9 w-9" aria-hidden="true">
      <circle r="18" fill="none" stroke="#cdb27a" strokeWidth="1.5" />
      <path d="M0 0 V-9" stroke="#e5e7eb" strokeWidth="2" strokeLinecap="round" transform={`rotate(${h * 30 + mn / 2})`} />
      <path d="M0 0 V-14" stroke="#e5e7eb" strokeWidth="1.3" strokeLinecap="round" transform={`rotate(${mn * 6})`} />
    </svg>
  );
}

function Spark({ closes }: { closes: { close: number }[] }) {
  const xs = closes.map((c) => c.close);
  const lo = Math.min(...xs), hi = Math.max(...xs);
  const d = xs.map((v, i) => `${i ? "L" : "M"}${((i / (xs.length - 1)) * 100).toFixed(1)} ${(26 - ((v - lo) / (hi - lo || 1)) * 24).toFixed(1)}`).join(" ");
  const up = xs[xs.length - 1] >= xs[0];
  return (
    <svg viewBox="0 0 100 28" className="h-7 w-20 shrink-0" preserveAspectRatio="none" aria-hidden="true">
      <path d={d} fill="none" stroke={up ? "#34d399" : "#fb7185"} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function OverviewPanel({ space, events }: { space: Space; events: FirmEvent[] }) {
  const { overview: o } = useLive();
  const now = useNow(15000);
  const c = space.controls;
  const flags = [c.kill_switch && "Kill switch engaged", c.reconcile_hold && "Buying paused (reconcile)", c.drawdown_brake && "Drawdown brake engaged"].filter(Boolean) as string[];
  const open = !!space.market.regular_hours_open;
  return (
    <aside className="border-t border-ink-800 bg-ink-950/95 p-5 lg:h-[var(--fs-h)] lg:overflow-y-auto lg:border-l lg:border-t-0">
      <header className="flex items-center justify-between gap-3">
        <h1 className="text-sm font-semibold uppercase tracking-[0.22em] text-gold-300">Firm Overview</h1>
        <div className="flex items-center gap-2">
          <ClockFace now={now} />
          <div className="text-right text-[11px] leading-tight text-slate-400">
            New York
            <br />
            <span className="text-slate-200">{new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" }).format(now)}</span>
            <br />
            <span className="text-slate-200">{etTime(new Date(now).toISOString(), false)}</span>
          </div>
        </div>
      </header>
      <div className="mt-3 h-px bg-gradient-to-r from-gold-500/50 to-transparent" />

      {o ? (
        <section className="mt-4 flex items-center gap-4">
          <div className="min-w-0 flex-1 space-y-3">
            <div>
              <div className="text-xs text-slate-500">Total capital</div>
              <div className="font-display text-2xl tabular-nums text-slate-50">{money(o.firm.nav)}</div>
            </div>
            <ul className="space-y-1 text-xs">
              {o.allocation.map((a) => (
                <li key={a.id} className="flex items-center gap-2">
                  <span className="h-2 w-2 rounded-sm" style={{ background: ALLOC_COLORS[a.id] ?? "#64748b" }} />
                  <span className="flex-1 text-slate-300">{a.name}</span>
                  <span className="tabular-nums text-slate-200">{money(a.nav)}</span>
                  <span className="w-9 text-right tabular-nums text-slate-500">{pct(a.pct, 0)}</span>
                </li>
              ))}
            </ul>
          </div>
          <Donut size={112} slices={o.allocation.map((a) => ({ label: a.name, value: a.nav, color: ALLOC_COLORS[a.id] ?? "#64748b" }))} />
        </section>
      ) : (
        <p className="mt-4 text-sm text-slate-500">Loading capital…</p>
      )}

      <section className="mt-5 grid grid-cols-3 gap-3 border-y border-ink-800 py-4 text-xs">
        <Status label="Market" value={open ? "Open" : "Closed"} ok={open ? true : null} sub={open ? "Regular hours" : space.market.next_open ? `Opens ${etTime(space.market.next_open)}` : undefined} />
        <Status label="Broker" value={space.broker_connected ? "Connected" : "Offline"} ok={!!space.broker_connected} sub={c.live_writes_enabled ? "Live orders on" : "Live orders off"} />
        <Status label="Safeguards" value={flags.length ? "Engaged" : "All clear"} ok={!flags.length} sub={flags.join(" · ") || "No holds or brakes"} />
      </section>

      <section className="mt-5">
        <header className="mb-3 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-gold-300">Recent activity</h2>
          <Link to="/activity" className="text-xs text-gold-400 hover:underline">All activity →</Link>
        </header>
        {events.length ? (
          <ul className="space-y-3">
            {events.slice(0, 6).map((e) => {
              const who = e.manager_id ?? (CIO_TYPES.has(e.type) ? "cio" : null);
              return (
                <li key={e.id} className="flex gap-2.5">
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: who ? AGENTS[who]?.color : e.severity === "critical" ? "#f43f5e" : "#64748b" }} />
                  <div className="min-w-0">
                    <p className={`text-xs ${e.severity === "critical" ? "text-rose-300" : "text-slate-200"}`}>{e.summary}</p>
                    <p className="mt-0.5 text-[10px] text-slate-500" title={when(e.ts)}>Event #{e.id} · {ago(e.ts)}</p>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-xs text-slate-500">Nothing recorded yet.</p>
        )}
      </section>

      <section className="mt-5 rounded-xl border border-ink-800 bg-ink-900/70 p-4">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-[0.2em] text-gold-300">Market</h2>
        {space.benchmarks.length ? (
          <ul className="space-y-3">
            {space.benchmarks.map((bm) => {
              const last = bm.closes[bm.closes.length - 1], prev = bm.closes[bm.closes.length - 2];
              const chg = last.close / prev.close - 1;
              return (
                <li key={bm.symbol} className="flex items-center gap-3 text-xs">
                  <div className="min-w-0 flex-1">
                    <div className="text-slate-300">{bm.label}</div>
                    <div className="text-[10px] text-slate-500">close of {last.date}</div>
                  </div>
                  <span className="tabular-nums text-slate-100">{last.close.toFixed(2)}</span>
                  <span className={`w-14 text-right tabular-nums ${tone(chg)}`}>{signedPct(chg)}</span>
                  <Spark closes={bm.closes} />
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-xs text-slate-500">No stored daily closes yet; they are recorded after each session.</p>
        )}
      </section>
    </aside>
  );
}

function Status({ label, value, ok, sub }: { label: string; value: string; ok: boolean | null; sub?: string }) {
  return (
    <div className="min-w-0">
      <div className="text-slate-500">{label}</div>
      <div className="mt-1 flex items-center gap-1.5 font-medium text-slate-100">
        <span className={`h-2 w-2 shrink-0 rounded-full ${ok === null ? "bg-slate-500" : ok ? "bg-emerald-400" : "bg-amber-400"}`} />
        {value}
      </div>
      {sub && <div className="mt-0.5 text-[10px] leading-snug text-slate-500">{sub}</div>}
    </div>
  );
}
