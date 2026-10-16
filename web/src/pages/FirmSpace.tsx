// The Firm: the whole company on one map you can drag and zoom. Click a person for what they
// are doing and their numbers, click a room for its department and its folders (the pages
// that hold its records), click a floor tag to fly there. Everything shown comes from live
// records; furniture, plants, and the city are decoration.

import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useLive } from "../App";
import { type FirmEvent, type ManagerDetail, type Usage } from "../api";
import { Donut } from "../components/charts";
import { AGENTS, Avatar, Badge, Empty } from "../components/ui";
import { useHeaderHeight, usePageVisible, useResource } from "../hooks";
import { ago, money, pct, signedPct, tone, when } from "../format";
import { AcademyOffice, Office } from "../space/Office";
import { type Camera, PanZoom } from "../space/PanZoom";
import { ALLOC_COLORS, type AgentId, FirmMap, NAMES, type Selection, signedPctOf, traineeDoing, traineeLine, weekOnFloor } from "../space/FirmMap";
import { ACADEMY, DEPTS, GROUND, type Dept, type DeptId, FLOORS, type Floor, type FloorId, type Folder, type Rect, TOWER, WORLD, deptById, deptOf, floorById, fxMarketOpen } from "../space/departments";
import { type AcademySpaceAgent, type AgentState, CIO_TYPES, MOOD_LABEL, type Mood, type Space, STATE_META, cioState, etTime, managerState, moodOf } from "../space/state";

const IDS: AgentId[] = ["cio", "tim", "alex", "max"];

/** Engine timestamps arrive as epoch seconds or ISO strings. */
function tsIso(ts: string | number): string | null {
  const ms = typeof ts === "number" || /^\d+(\.\d+)?$/.test(ts) ? Number(ts) * 1000 : Date.parse(ts);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null;
}
const PANEL_W = 384;

function useNow(every: number) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), every);
    return () => window.clearInterval(t);
  }, [every]);
  return now;
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

const ACADEMY_RECT: Rect = [ACADEMY.x0 - 20, ACADEMY.top - 100, ACADEMY.x1 - ACADEMY.x0 + 40, GROUND - ACADEMY.top + 120];
const TOWER_RECT: Rect = [TOWER.x0 - 20, TOWER.top - 110, TOWER.x1 - TOWER.x0 + 40, GROUND - TOWER.top + 130];

export default function FirmSpace() {
  const { events, bump } = useLive();
  const visible = usePageVisible();
  // Poll only while the tab is visible (refreshes on return); live events refresh immediately via `bump`.
  const { data: space, error } = useResource<Space>("/api/space", { every: visible ? 5000 : 0, bump });
  const { overview } = useLive();
  const now = useNow(5000);
  const header = useHeaderHeight();
  const params = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const wide = useWide();
  const camera = useRef<Camera>(null);
  const [sel, setSel] = useState<Selection | null>(null);
  const [panel, setPanel] = useState(() => window.matchMedia("(min-width: 1024px)").matches);
  const office = (IDS as string[]).includes(params.office ?? "") ? (params.office as AgentId) : null;

  const states = useMemo(() => {
    if (!space) return null;
    const out = { cio: cioState(space.cio, events, now) } as Record<AgentId, AgentState>;
    for (const a of space.agents) out[a.id as AgentId] = managerState(a, events, now);
    return out;
  }, [space, events, now]);
  const moods = useMemo(() => {
    if (!space || !states) return null;
    const open = !!space.market.regular_hours_open;
    const out = { cio: moodOf(states.cio, null, open, now) } as Record<AgentId, Mood>;
    for (const a of space.agents) out[a.id as AgentId] = moodOf(states[a.id as AgentId], a, open, now);
    return out;
  }, [space, states, now]);

  const select = useCallback((s: Selection | null) => {
    setSel(s);
    if (s) setPanel(true);
  }, []);
  const focus = useCallback((r: Rect) => camera.current?.flyTo(r), []);
  const goTo = useCallback(
    (s: Selection) => {
      select(s);
      if (s.kind === "dept") focus(deptById(s.id).rect);
      else if (s.kind === "floor") focus(floorById(s.id).rect);
      else if (s.kind === "person") focus(deptOf(s.id)!.rect);
      else {
        const stage = space?.academy?.agents?.find((a) => a.id === s.id)?.stage;
        focus(deptById(stage === "PAPER" ? "hall" : stage === "DEMO" ? "demo-desk" : stage === "RETIRED" ? "alumni" : "studio").rect);
      }
    },
    [select, focus, space],
  );
  // Offices (/firm/<someone>) replace the map; every other folder opens over it.
  const openFolder = useCallback((path: string) => (path.startsWith("/firm/") ? navigate(path) : navigate(`${location.pathname}?open=${encodeURIComponent(path)}`)), [navigate, location.pathname]);
  const sit = useCallback((id: string) => navigate(`/firm/${id}`), [navigate]);

  // Old floor links (/firm/forex, /firm/academy) open on that part of the map.
  const start = useRef(params.office === "forex" ? floorById("forex").rect : params.office === "academy" ? ACADEMY_RECT : undefined).current;
  useEffect(() => {
    if (params.office === "forex") select({ kind: "floor", id: "forex" });
    if (params.office === "academy") select({ kind: "floor", id: "academy-research" });
  }, [params.office, select]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !office && !new URLSearchParams(location.search).get("open")) setSel(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [office, location.search]);

  const trainee = !office && params.office ? space?.academy?.agents?.find((a) => a.id === params.office) ?? null : null;

  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!space || !states || !moods) return <div className="p-6 text-slate-500">Opening the building…</div>;

  const sheet = !wide && panel;
  return (
    <div className="relative overflow-hidden" style={{ height: `calc(100dvh - ${header}px)` }}>
      <PanZoom ref={camera} world={WORLD} start={start} label="Map of The 537 Firm" insetRight={wide && panel ? PANEL_W : 0} insetBottom={sheet ? Math.round(window.innerHeight * 0.45) : 0}>
        <FirmMap space={space} states={states} moods={moods} overview={overview} events={events} now={now} sel={sel} onSelect={select} onFocus={focus} onOpen={openFolder} onSit={sit} onBackground={() => setSel(null)} />
      </PanZoom>

      <Controls camera={camera} onJump={goTo} onTower={() => focus(TOWER_RECT)} onAcademy={() => focus(ACADEMY_RECT)} />

      {!panel && (
        <button onClick={() => setPanel(true)} className="absolute bottom-8 right-3 z-20 rounded-xl lg:bottom-auto lg:top-3 border border-white/10 bg-ink-950/90 px-3 py-2 text-xs font-medium text-slate-200 shadow-lg backdrop-blur hover:border-gold-500/50">
          {sel ? "Show details" : "Firm overview"} ‹
        </button>
      )}
      {panel && (
        <aside
          className={`absolute z-20 flex flex-col border-ink-700 bg-ink-950/95 shadow-2xl backdrop-blur ${wide ? "bottom-0 right-0 top-0 border-l" : "inset-x-0 bottom-0 max-h-[45dvh] rounded-t-2xl border-t"}`}
          style={wide ? { width: PANEL_W } : undefined}
          aria-label={sel ? "Details" : "Firm overview"}
        >
          <div className="flex items-center gap-2 border-b border-ink-800 px-4 py-2">
            {sel ? (
              <button onClick={() => setSel(null)} className="text-xs text-slate-400 hover:text-slate-200">← Firm overview</button>
            ) : (
              <span className="text-xs font-semibold uppercase tracking-[0.22em] text-gold-300">Firm overview</span>
            )}
            <button onClick={() => setPanel(false)} className="ml-auto rounded-md px-2 py-1 text-xs text-slate-500 hover:bg-ink-800 hover:text-slate-200" aria-label="Hide panel">
              Hide ›
            </button>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4">
            {sel?.kind === "person" && <PersonPanel key={sel.id} id={sel.id} state={states[sel.id]} mood={moods[sel.id]} space={space} onFolder={openFolder} onSit={() => navigate(`/firm/${sel.id}`)} />}
            {sel?.kind === "trainee" && <TraineePanel a={space.academy?.agents?.find((a) => a.id === sel.id)} space={space} onFolder={openFolder} onSit={() => sit(sel.id)} />}
            {sel?.kind === "dept" && <DeptPanel d={deptById(sel.id)} space={space} states={states} events={events} now={now} onPick={goTo} onFolder={openFolder} />}
            {sel?.kind === "floor" && <FloorPanel f={floorById(sel.id)} onPick={goTo} />}
            {!sel && <OverviewPanel space={space} events={events} onPick={goTo} onFolder={openFolder} />}
          </div>
        </aside>
      )}

      <p className="pointer-events-none absolute bottom-2 left-3 z-10 hidden text-[10px] text-slate-400/70 sm:block" style={wide && panel ? { right: PANEL_W } : undefined}>
        People, screens, and boards show live records. Furniture, plants, lighting, and the city are decorative.
      </p>
      {office && <Office id={office} state={states[office]} mood={moods[office]} space={space} top={header} onClose={() => navigate("/firm")} />}
      {trainee && <AcademyOffice a={trainee} space={space} top={header} onClose={() => navigate("/firm")} />}
    </div>
  );
}

// -- map controls ---------------------------------------------------------------------------------

function Controls({ camera, onJump, onTower, onAcademy }: { camera: React.RefObject<Camera>; onJump: (s: Selection) => void; onTower: () => void; onAcademy: () => void }) {
  const [floors, setFloors] = useState(false);
  const btn = "grid h-9 w-9 shrink-0 place-items-center rounded-lg text-base text-slate-200 hover:bg-ink-800";
  return (
    <div className="absolute left-3 right-3 top-3 z-20 flex flex-col items-start gap-2 lg:right-auto">
      <div className="flex max-w-full shrink-0 items-center gap-1 overflow-x-auto whitespace-nowrap rounded-xl border border-white/10 bg-ink-950/90 p-1 shadow-lg backdrop-blur">
        <button className={btn} onClick={() => camera.current?.zoomBy(1 / 1.3)} aria-label="Zoom out" title="Zoom out (−)">−</button>
        <button className={btn} onClick={() => camera.current?.zoomBy(1.3)} aria-label="Zoom in" title="Zoom in (+)">+</button>
        <button className="h-9 rounded-lg px-2.5 text-xs text-slate-200 hover:bg-ink-800" onClick={() => camera.current?.fit()} title="See everything (0)">Everything</button>
        <span className="mx-0.5 hidden h-5 w-px bg-white/10 sm:block" aria-hidden="true" />
        <button className="h-9 rounded-lg px-2.5 text-xs text-slate-200 hover:bg-ink-800" onClick={onTower}>HQ Tower</button>
        <button className="h-9 rounded-lg px-2.5 text-xs text-slate-200 hover:bg-ink-800" onClick={onAcademy}>Academy</button>
        <button className={`h-9 rounded-lg px-2.5 text-xs hover:bg-ink-800 ${floors ? "bg-ink-800 text-gold-200" : "text-slate-200"}`} onClick={() => setFloors((v) => !v)} aria-expanded={floors}>
          Floors ▾
        </button>
      </div>
      {floors && (
        <ul className="w-56 rounded-xl border border-white/10 bg-ink-950/95 p-1 text-sm shadow-lg backdrop-blur">
          {FLOORS.map((f) => (
            <li key={f.id}>
              <button
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left hover:bg-ink-800"
                onClick={() => {
                  setFloors(false);
                  onJump({ kind: "floor", id: f.id });
                }}
              >
                <span className="w-7 font-semibold text-gold-300">{f.level}</span>
                <span className="text-slate-200">{f.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="hidden rounded-lg bg-ink-950/70 px-2.5 py-1.5 text-[11px] text-slate-300 backdrop-blur sm:block">Drag to look around · scroll or pinch to zoom · click anyone or any room</p>
    </div>
  );
}

// -- inspector: people ---------------------------------------------------------------------------

function FolderButtons({ folders, onFolder }: { folders: Folder[]; onFolder: (p: string) => void }) {
  if (!folders.length) return null;
  return (
    <section className="mt-4">
      <h3 className="label mb-2">Folders</h3>
      <ul className="space-y-1.5">
        {folders.map((f) => (
          <li key={f.path + f.label}>
            <button onClick={() => onFolder(f.path)} className="group flex w-full items-center gap-3 rounded-xl border border-ink-700 bg-ink-900 px-3 py-2 text-left transition hover:border-gold-500/50">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-gold-500/10 text-gold-300" aria-hidden="true">▤</span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm font-medium text-slate-100">{f.label}</span>
                <span className="block truncate text-xs text-slate-500 group-hover:text-slate-400">{f.note}</span>
              </span>
              <span className="text-slate-600 group-hover:text-slate-300" aria-hidden="true">›</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function RightNow({ state }: { state: AgentState }) {
  const m = STATE_META[state.key];
  return (
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
  );
}

function PersonPanel({ id, state, mood, space, onFolder, onSit }: { id: AgentId; state: AgentState; mood: Mood; space: Space; onFolder: (p: string) => void; onSit: () => void }) {
  const { overview, bump } = useLive();
  const detail = useResource<ManagerDetail>(id === "cio" ? null : `/api/managers/${id}`, { every: 30000, bump });
  const usage = useResource<Usage>(id === "cio" ? "/api/cio/usage" : null, { every: 60000 });
  const m = STATE_META[state.key];
  const card = overview?.managers.find((c) => c.id === id);
  const d = detail.data;
  const book = d?.active_book ?? "LIVE";
  const b = d?.books[book];
  const perf = b?.performance;
  const positions = b?.positions.filter((p) => p.quantity > 0 || p.target > 0) ?? [];
  const dept = deptOf(id)!;
  return (
    <div>
      <header className="flex items-start gap-3">
        <Avatar id={id} size={48} working={m.working} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-display text-xl text-slate-50">{NAMES[id]}</span>
            {card && <Badge tone={card.mode}>{card.mode}</Badge>}
          </div>
          <div className="truncate text-xs text-slate-400">{AGENTS[id].title}</div>
          <div className="text-[11px] text-slate-500">{floorById(dept.floor).level} · {dept.name}</div>
        </div>
      </header>
      <RightNow state={state} />
      {mood.kind !== "calm" && mood.kind !== "focused" && (
        <p className={`mt-2 text-xs ${mood.kind === "stressed" ? "text-rose-300" : mood.kind === "pleased" ? "text-emerald-300" : "text-slate-400"}`}>
          {MOOD_LABEL[mood.kind]}{mood.reason ? ` · ${mood.reason}` : ""}
        </p>
      )}

      {id === "cio" ? (
        <>
          <section className="mt-4">
            <h3 className="label mb-2">Awaiting your decision</h3>
            {space.cio.pending_proposals.length ? (
              <ul className="space-y-1.5 text-sm">{space.cio.pending_proposals.map((p) => <li key={p.id} className="text-slate-200">#{p.id} {p.title}</li>)}</ul>
            ) : (
              <p className="text-sm text-slate-500">No open proposals.</p>
            )}
          </section>
          <section className="mt-4">
            <h3 className="label mb-2">Recent decisions</h3>
            <EventList events={space.cio.recent.slice(0, 3)} empty="No proposals or memories recorded yet." />
          </section>
          {usage.data && <p className="mt-4 text-xs text-slate-500">AI spend this month: {money(usage.data.agents.cio?.spent ?? 0)} of {money(usage.data.agents.cio?.cap ?? usage.data.monthly_usd)}</p>}
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

      <button onClick={onSit} className="btn-primary mt-4 w-full">Sit down with {NAMES[id]} →</button>
      <FolderButtons folders={dept.folders} onFolder={onFolder} />
    </div>
  );
}

function TraineePanel({ a, space, onFolder, onSit }: { a: AcademySpaceAgent | undefined; space: Space; onFolder: (p: string) => void; onSit: () => void }) {
  if (!a) return <p className="text-sm text-slate-500">This agent is no longer in the Academy's records.</p>;
  const paused = !!a.paused || a.status === "PAUSED";
  return (
    <div>
      <header className="flex items-start gap-3">
        <span className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl text-lg font-semibold text-ink-950" style={{ background: a.persona.color }}>{a.name[0]}</span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-display text-xl text-slate-50">{a.name}</span>
            <span className="rounded-full bg-slate-500/10 px-2 py-0.5 text-[11px] text-slate-300">{a.style === "day" ? "Day trader" : "Swing trader"}</span>
          </div>
          <div className="text-xs text-slate-400">{a.persona.tagline}</div>
          <div className="text-[11px] text-slate-500">{{ RESEARCH: "A1 · Research floor · no trading", PAPER: "A2 · Paper Trading Hall", DEMO: "A3 · Demo Desk · OANDA practice", RETIRED: "A3 · Alumni wall" }[a.stage] ?? ""} · v{a.version}</div>
        </div>
      </header>
      <section className="mt-4 rounded-xl border border-ink-700 bg-ink-850 p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-slate-400">Right now</div>
        <p className="mt-1.5 text-sm font-medium text-slate-100">{paused ? `Paused${a.paused ? `: ${a.paused}` : ""}` : traineeDoing(a, !!space.academy?.engine?.data_ready).replace(/^\w/, (ch) => ch.toUpperCase())}</p>
        {a.last_event && (
          <p className="mt-1 text-xs leading-relaxed text-slate-400">
            {a.last_event.summary} {tsIso(a.last_event.ts) && <span className="text-slate-500">· {ago(tsIso(a.last_event.ts))}</span>}
          </p>
        )}
        <p className="mt-2 text-[10px] text-slate-500">source: Forex engine records</p>
      </section>
      {a.stage === "RESEARCH" ? (
      <section className="mt-4 text-sm text-slate-300">
        <h3 className="label mb-2">Research</h3>
        <p>{a.last_sprint ? `Last sprint ${a.last_sprint.day}: ${a.last_sprint.outcome.replace(/_/g, " ").toLowerCase()}.` : "First research sprint tonight at 1:00 AM New York."}</p>
        {a.validation && <p className="mt-1 text-xs text-slate-400">Latest validation: {a.validation.toLowerCase()}</p>}
      </section>
      ) : (
      <section className="mt-4">
        <h3 className="label mb-2">{a.stage === "DEMO" ? "Practice account" : "Virtual account"}</h3>
        <div className="grid grid-cols-3 gap-2 text-sm">
          <Mini label="Equity" value={money(a.equity)} />
          <Mini label="Return" value={<span className={tone(a.return_pct)}>{signedPctOf(a.return_pct)}</span>} />
          <Mini label="Open" value={a.positions} />
        </div>
        {a.validation && <p className="mt-2 text-xs text-slate-400">Validation: {a.validation}</p>}
      </section>
      )}
      {a.stage !== "RESEARCH" && a.stage !== "RETIRED" && a.stage_since && (
        <p className="mt-2 text-xs text-slate-400">Week {weekOnFloor(a.stage_since)} on this floor · {a.floor_trades ?? 0} trades here{a.stage === "PAPER" ? " · you decide on a Demo Desk seat after 4 profitable weeks" : ""}</p>
      )}
      <button onClick={onSit} className="btn-primary mt-4 w-full">Sit down with {a.name} →</button>
      <FolderButtons folders={[{ label: `${a.name}'s record`, path: `/academy/${a.id}`, note: "Trades, validation evidence, revisions" }, { label: "The 537 Academy", path: "/academy", note: "Floors, journal, and rates" }, { label: "Sprint reports", path: "/academy/reports", note: "Every night's research results" }]} onFolder={onFolder} />
    </div>
  );
}

// -- inspector: departments and floors -----------------------------------------------------------

function deptFacts(d: Dept, space: Space, states: Record<AgentId, AgentState>, events: FirmEvent[], now: number, overview: ReturnType<typeof useLive>["overview"]): string[] {
  const ac = space.academy;
  const c = space.controls;
  switch (d.id) {
    case "cio": case "tim": case "alex": case "max":
      return [`${NAMES[d.id]}: ${states[d.id].headline}`];
    case "treasury":
      return overview ? [`Firm capital ${money(overview.firm.nav)}, of which ${money(overview.firm.unallocated)} is unallocated.`, ...overview.allocation.map((a) => `${a.name}: ${money(a.nav)} (${pct(a.pct, 0)})`)] : ["Loading capital…"];
    case "market-room": {
      const prices = ac?.prices ?? [];
      const fresh = prices.length > 0 && now / 1000 - Math.max(...prices.map((p) => p.time)) < 900;
      return [`FX market by schedule: ${fxMarketOpen(now) ? "open" : "closed"} (Sunday 5 PM to Friday 5 PM New York).`, !ac?.available ? "The Forex engine is not running." : prices.length ? `${prices.length} OANDA rates on the board${fresh ? ", live" : ", from before the close"}.` : "Waiting for OANDA prices."];
    }
    case "day-1": case "day-2": case "swing-1": case "swing-2":
      return ["Open. No agent has been promoted to live forex trading."];
    case "lobby":
      return overview ? [`Firm capital ${money(overview.firm.nav)}.`, ...overview.managers.map((m) => `${m.name}: ${m.mode.toLowerCase()}${m.status !== "OK" && m.status ? ` · ${m.status.toLowerCase()}` : ""}`)] : ["Loading…"];
    case "records":
      return events.length ? [`Latest: ${events[0].summary}`, `Event #${events[0].id} · ${ago(events[0].ts)}`] : ["Nothing recorded yet."];
    case "operations": {
      const flags = [c.kill_switch && "kill switch engaged", c.reconcile_hold && "buying paused (reconcile)", c.drawdown_brake && "drawdown brake engaged"].filter(Boolean);
      return [`Broker ${space.broker_connected ? "connected" : "offline"} · live orders ${c.live_writes_enabled ? "on" : "off"}.`, flags.length ? `Safeguards: ${flags.join(", ")}.` : "Safeguards: all clear.", `US market ${space.market.regular_hours_open ? "open" : `closed${space.market.next_open ? `, opens ${etTime(space.market.next_open)}` : ""}`}.`];
    }
    case "lab": {
      const running = ac?.validations_running ?? [];
      const job = ac?.engine?.job?.name;
      return running.length ? running.map((r) => `${ac?.agents?.find((a) => a.id === r.agent_id)?.name ?? r.agent_id}: ${r.status.replace("RUNNING: ", "")}`) : [job ? `Engine job: ${job}.` : "Idle. Sprints run every night at 1:00 AM New York; the report is ready by 8:00 AM."];
    }
    case "research":
      return ["Unoccupied."];
    case "stock-research": case "stock-hall": {
      const eq = space.equity_class;
      if (!eq?.available) return ["The Stock Swing Class engine is not running yet."];
      const here = (eq.agents ?? []).filter((a) => a.stage === (d.id === "stock-hall" ? "PAPER" : "RESEARCH"));
      if (!here.length) return [d.id === "stock-hall" ? "No stock agent has passed research yet." : "Nobody in research."];
      return here.map((a) => `${a.name}: ${a.status !== "ACTIVE" ? a.status.toLowerCase() : a.sprinting ? `sprinting (${a.sprinting})` : a.stage === "PAPER" ? `paper ${signedPct(a.return_pct)}` : a.last_sprint ? `last sprint ${a.last_sprint.day}: ${a.last_sprint.outcome.toLowerCase().replace("_", " ")}` : "waiting for history"}`);
    }
    case "core-board": case "trading-board":
      return overview ? overview.managers.filter((m) => (d.id === "trading-board" ? m.id === "max" : m.id !== "max")).map((m) => {
        const p = m.mode === "SIM" ? m.performance.SIM : m.performance.LIVE;
        return `${m.name} (${m.mode.toLowerCase()}): ${m.mode === "INACTIVE" ? "not trading" : `${money(m.mode === "SIM" ? m.sim.nav : m.live.nav)}, ${signedPct(p.twr)} vs S&P ${signedPct(p.benchmark)}`}`;
      }) : ["Loading…"];
    case "forex-board": {
      const n = (st: string) => (ac?.agents ?? []).filter((a) => a.stage === st).length;
      return ["0 of 4 live chairs filled. Live forex trading is not authorized.", ac?.available ? `Academy pipeline: ${n("DEMO")} on the Demo Desk, ${n("PAPER")} paper trading, ${n("RESEARCH")} in research.` : "The Forex engine is not running."];
    }
    case "ops-board": {
      const t = space.today;
      return t ? [`Today: ${t.runs} manager decisions, ${t.orders} orders (${t.fills} filled).`, `${t.events} events recorded · ${t.warnings + t.critical} warnings · ${t.cio} by Graham.`] : ["Loading…"];
    }
    case "demo-board": case "hall-board": case "research-board": {
      if (!ac?.available) return ["The Forex engine is not running."];
      const stage = d.id === "demo-board" ? "DEMO" : d.id === "hall-board" ? "PAPER" : "RESEARCH";
      const agents = (ac.agents ?? []).filter((a) => a.stage === stage);
      if (stage === "RESEARCH") return [ac.latest_report ? `Last sprint ${ac.latest_report.day}: passed ${ac.latest_report.passed.join(", ") || "none"}; improved ${ac.latest_report.improved.join(", ") || "none"}.` : "No sprint yet.", `${agents.filter((a) => a.validation === "PASSED").length} of ${agents.length} pass validation. Next sprint 1:00 AM New York.`];
      if (!agents.length) return [stage === "DEMO" ? `No agent on the Demo Desk. Practice account ${ac.engine?.demo?.configured ? "connected" : "not set up yet"}.` : "No agent is paper trading yet."];
      return agents.map((a) => `${a.name}: week ${weekOnFloor(a.stage_since) ?? 1}, ${signedPctOf(a.return_pct)}, ${a.floor_trades ?? 0} trades`);
    }
    case "alumni": {
      const retired = (ac?.agents ?? []).filter((a) => a.stage === "RETIRED");
      return retired.length ? [`${retired.map((a) => a.name).join(", ")} (records kept).`] : ["Empty. No agent has graduated or retired."];
    }
    case "studio": case "hall": case "demo-desk": {
      const stage = d.id === "studio" ? "RESEARCH" : d.id === "hall" ? "PAPER" : "DEMO";
      const agents = (ac?.agents ?? []).filter((a) => a.stage === stage);
      if (!ac?.available) return ["The Forex engine is not running."];
      const head = d.id === "studio" ? `${agents.length} agents in research (no trading).` : d.id === "hall" ? (agents.length ? `${agents.length} agents paper trading $10,000 virtual accounts.` : "Empty: no agent has passed research yet.") : (agents.length ? `${agents.length} of 2 seats taken.` : "Both seats open.");
      const extra = d.id === "demo-desk" ? (ac.engine?.demo?.configured ? "Practice account connected." : "The OANDA practice account is not set up yet.") : `Engine ${ac.engine?.online ? "online" : "offline"}${ac.engine?.market_open ? " · market open" : ""}.`;
      return [head, extra];
    }
  }
}

function DeptPanel({ d, space, states, events, now, onPick, onFolder }: { d: Dept; space: Space; states: Record<AgentId, AgentState>; events: FirmEvent[]; now: number; onPick: (s: Selection) => void; onFolder: (p: string) => void }) {
  const { overview } = useLive();
  const f = floorById(d.floor);
  const facts = deptFacts(d, space, states, events, now, overview);
  const stage = d.id === "studio" ? "RESEARCH" : d.id === "hall" ? "PAPER" : d.id === "demo-desk" ? "DEMO" : d.id === "alumni" ? "RETIRED" : null;
  const trainees = stage ? (space.academy?.agents ?? []).filter((a) => a.stage === stage) : [];
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-slate-500">{f.building} · {f.level} {f.name}</div>
      <h2 className="font-display text-xl text-slate-50">{d.name}</h2>
      <p className="mt-2 text-sm leading-relaxed text-slate-300">{d.purpose}</p>
      <section className="mt-4 rounded-xl border border-ink-700 bg-ink-850 p-3">
        <div className="text-[11px] font-medium uppercase tracking-wider text-slate-400">Right now</div>
        <ul className="mt-1.5 space-y-1">
          {facts.map((t, i) => <li key={i} className={i ? "text-xs text-slate-400" : "text-sm text-slate-100"}>{t}</li>)}
        </ul>
      </section>
      {(d.people?.length || trainees.length) ? (
        <section className="mt-4">
          <h3 className="label mb-2">People here</h3>
          <ul className="space-y-1.5">
            {(d.people ?? []).map((id) => (
              <li key={id}>
                <PersonRow color={AGENTS[id].color} name={NAMES[id as AgentId]} line={states[id as AgentId].headline} onClick={() => onPick({ kind: "person", id: id as AgentId })} />
              </li>
            ))}
            {trainees.map((a) => (
              <li key={a.id}>
                <PersonRow color={a.persona.color} name={a.name} line={traineeLine(a, !!space.academy?.engine?.data_ready)} onClick={() => onPick({ kind: "trainee", id: a.id })} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {d.later && <p className="mt-3 text-xs leading-relaxed text-slate-400">{d.later}</p>}
      <FolderButtons folders={d.folders} onFolder={onFolder} />
    </div>
  );
}

function PersonRow({ color, name, line, onClick }: { color: string; name: string; line: string; onClick: () => void }) {
  return (
    <button onClick={onClick} className="flex w-full items-start gap-2.5 rounded-lg px-2 py-1.5 text-left hover:bg-ink-850">
      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
      <span className="min-w-0 flex-1">
        <span className="block text-sm font-medium text-slate-100">{name}</span>
        <span className="block truncate text-xs text-slate-400">{line}</span>
      </span>
      <span className="text-slate-600" aria-hidden="true">›</span>
    </button>
  );
}

function FloorPanel({ f, onPick }: { f: Floor; onPick: (s: Selection) => void }) {
  const depts = DEPTS.filter((d) => d.floor === f.id);
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wider text-slate-500">{f.building} · {f.level}</div>
      <h2 className="font-display text-xl text-slate-50">{f.name}</h2>
      <p className="mt-1 text-sm text-slate-400">{f.division}</p>
      <DeptList depts={depts} onPick={onPick} />
    </div>
  );
}

function DeptList({ depts, onPick }: { depts: Dept[]; onPick: (s: Selection) => void }) {
  return (
    <ul className="mt-3 space-y-1">
      {depts.map((d) => (
        <li key={d.id}>
          <button onClick={() => onPick({ kind: "dept", id: d.id as DeptId })} className="flex w-full items-center gap-2 rounded-lg border border-ink-800 px-3 py-2 text-left hover:border-ink-600">
            <span className="min-w-0 flex-1">
              <span className="block text-sm font-medium text-slate-100">{d.name}</span>
              <span className="block truncate text-[11px] text-slate-500">{d.people?.map((p) => NAMES[p as AgentId]).join(" · ") || d.folders.map((x) => x.label).join(" · ") || "—"}</span>
            </span>
            <span className="text-slate-600" aria-hidden="true">›</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

// -- overview (nothing selected) -----------------------------------------------------------------------

function OverviewPanel({ space, events, onPick, onFolder }: { space: Space; events: FirmEvent[]; onPick: (s: Selection) => void; onFolder: (p: string) => void }) {
  const { overview: o } = useLive();
  const now = useNow(15000);
  const c = space.controls;
  const flags = [c.kill_switch && "Kill switch engaged", c.reconcile_hold && "Buying paused (reconcile)", c.drawdown_brake && "Drawdown brake engaged"].filter(Boolean) as string[];
  const open = !!space.market.regular_hours_open;
  const [floor, setFloor] = useState<FloorId | null>(null);
  return (
    <div>
      <header className="flex items-center justify-between gap-3">
        <div className="text-[11px] leading-tight text-slate-400">
          New York
          <br />
          <span className="text-slate-200">{new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" }).format(now)}</span>
          <span className="text-slate-200"> · {etTime(new Date(now).toISOString(), false)}</span>
        </div>
        <ClockFace now={now} />
      </header>

      {o ? (
        <section className="mt-3 flex items-center gap-4">
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
          <Donut size={104} slices={o.allocation.map((a) => ({ label: a.name, value: a.nav, color: ALLOC_COLORS[a.id] ?? "#64748b" }))} />
        </section>
      ) : (
        <p className="mt-4 text-sm text-slate-500">Loading capital…</p>
      )}

      <section className="mt-4 grid grid-cols-3 gap-3 border-y border-ink-800 py-3 text-xs">
        <Status label="Market" value={open ? "Open" : "Closed"} ok={open ? true : null} sub={open ? "Regular hours" : space.market.next_open ? `Opens ${etTime(space.market.next_open)}` : undefined} />
        <Status label="Broker" value={space.broker_connected ? "Connected" : "Offline"} ok={!!space.broker_connected} sub={c.live_writes_enabled ? "Live orders on" : "Live orders off"} />
        <Status label="Safeguards" value={flags.length ? "Engaged" : "All clear"} ok={!flags.length} sub={flags.join(" · ") || "No holds or brakes"} />
      </section>

      <section className="mt-4">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-gold-300">Departments</h2>
        <ul className="space-y-1">
          {FLOORS.map((f) => (
            <li key={f.id}>
              <button onClick={() => setFloor((cur) => (cur === f.id ? null : f.id))} aria-expanded={floor === f.id} className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-ink-850">
                <span className="w-7 text-xs font-semibold text-gold-300">{f.level}</span>
                <span className="flex-1 text-sm text-slate-200">{f.name}</span>
                <span className="text-[10px] text-slate-500">{f.building === "HQ Tower" ? "HQ" : "Academy"}</span>
                <span className="text-slate-600" aria-hidden="true">{floor === f.id ? "▾" : "▸"}</span>
              </button>
              {floor === f.id && <div className="pb-2 pl-9"><DeptList depts={DEPTS.filter((d) => d.floor === f.id)} onPick={onPick} /></div>}
            </li>
          ))}
        </ul>
      </section>

      <section className="mt-4">
        <header className="mb-2 flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-[0.2em] text-gold-300">Recent activity</h2>
          <button onClick={() => onFolder("/activity")} className="text-xs text-gold-400 hover:underline">All activity →</button>
        </header>
        {events.length ? (
          <ul className="space-y-2.5">
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

      <section className="mt-4 rounded-xl border border-ink-800 bg-ink-900/70 p-3">
        <h2 className="mb-2 text-xs font-semibold uppercase tracking-[0.2em] text-gold-300">Market</h2>
        {space.benchmarks.length ? (
          <ul className="space-y-2.5">
            {space.benchmarks.map((bm) => {
              const last = bm.closes[bm.closes.length - 1], prev = bm.closes[bm.closes.length - 2];
              const chg = prev ? last.close / prev.close - 1 : null;
              return (
                <li key={bm.symbol} className="flex items-center gap-3 text-xs">
                  <div className="min-w-0 flex-1">
                    <div className="text-slate-300">{bm.label}</div>
                    <div className="text-[10px] text-slate-500">close of {last.date}</div>
                  </div>
                  <span className="tabular-nums text-slate-100">{last.close.toFixed(2)}</span>
                  <span className={`w-14 text-right tabular-nums ${tone(chg)}`}>{signedPct(chg)}</span>
                  {bm.closes.length > 1 && <Spark closes={bm.closes} />}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="text-xs text-slate-500">No stored daily closes yet; they are recorded after each session.</p>
        )}
      </section>
      <p className="mt-3 text-[11px] text-slate-500">
        Prefer the classic pages? <Link to="/" className="text-gold-400 hover:underline">Headquarters</Link>
      </p>
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
