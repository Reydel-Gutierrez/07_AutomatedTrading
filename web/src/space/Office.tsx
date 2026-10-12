// A 1-on-1 at someone's desk, inside The Firm. The person speaks first (their real report);
// everything else is a tile you open when you want more. Graham's desk is a conversation.

import { type ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useLive } from "../App";
import { api, type ManagerDetail, type Memory, type PoolStatus, type Proposal, type Usage } from "../api";
import { PerformanceChart, WeightBars } from "../components/charts";
import { AGENTS, Badge, Empty, ErrorLine, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { ago, money, pct, signedPct, tone, until, when } from "../format";
import { ChatThread, MemoryPanel, ProposalCard, useCioChat } from "../pages/CIO";
import { DecisionLog, MandateCard, ModeControl, Orders, Positions, ResearchDesk, RunNow } from "../pages/Manager";
import { Body, Hands, LOOKS } from "./Character";
import { poseFor } from "./pose";
import { type AgentState, type Space, STATE_META } from "./state";

const NAMES: Record<string, string> = { cio: "Graham", tim: "Tim", alex: "Alex", max: "Max" };
const WALL: Record<string, [string, string]> = { cio: ["#2a211b", "#1b1612"], tim: ["#18233a", "#111a2b"], alex: ["#211c33", "#161326"], max: ["#1d2028", "#14161c"] };

/** The close-up: the person at their desk, drawn larger, posed by their real state. */
function DeskScene({ id, state }: { id: string; state: AgentState }) {
  const look = LOOKS[id] ?? LOOKS.tim;
  const pose = poseFor(state.key);
  const meta = STATE_META[state.key];
  const [top, low] = WALL[id] ?? WALL.tim;
  const off = state.key === "inactive";
  return (
    <svg viewBox="0 0 420 280" className="h-full w-full" preserveAspectRatio="xMidYMax slice" aria-hidden="true">
      <defs>
        <linearGradient id={`desk-wall-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={top} />
          <stop offset="1" stopColor={low} />
        </linearGradient>
        <radialGradient id="desk-lamp">
          <stop offset="0" stopColor="#ffd9a0" stopOpacity=".35" />
          <stop offset="1" stopColor="#ffd9a0" stopOpacity="0" />
        </radialGradient>
      </defs>
      <rect width="420" height="280" fill={`url(#desk-wall-${id})`} />
      {Array.from({ length: 14 }, (_, i) => <rect key={i} x={i * 30} y="0" width="1.5" height="200" fill="#000" opacity=".18" />)}
      {id === "cio" ? (
        <g>
          <rect x="250" y="26" width="140" height="96" fill="#0a1226" stroke="#4a3b30" strokeWidth="5" />
          {[[256, 70, 16, 52], [276, 50, 18, 72], [298, 80, 14, 42], [316, 44, 20, 78], [340, 66, 16, 56], [360, 56, 22, 66]].map(([x, y, w, h], i) => <rect key={i} x={x} y={y} width={w} height={h} fill="url(#fs-lit)" opacity=".9" />)}
          <rect x="24" y="30" width="110" height="150" fill="#2a1f19" stroke="#4a3a2e" strokeWidth="4" />
          {[0, 1, 2].map((r) => Array.from({ length: 9 }, (_, i) => <rect key={`${r}-${i}`} x={30 + i * 11} y={38 + r * 48} width="8" height="40" fill={["#6b2d2d", "#2d4a6b", "#8a7448", "#33553f"][(i + r) % 4]} />))}
        </g>
      ) : (
        <g>
          <rect x="24" y="34" width="120" height="74" rx="4" fill="#0a0d13" stroke="#2b3242" strokeWidth="3" />
          <text x="34" y="54" fontSize="9" fontWeight="600" fill={meta.color} fontFamily="Inter, sans-serif" letterSpacing=".6">{meta.label.toUpperCase()}</text>
          {state.symbols.slice(0, 3).map((sy, i) => (
            <text key={sy.symbol + i} x="34" y={72 + i * 14} fontSize="10" fontWeight="600" fill="#e5e7eb" fontFamily="Inter, sans-serif">
              {sy.side ? `${sy.side.toUpperCase()} ` : ""}{sy.symbol}
            </text>
          ))}
          <rect x="300" y="40" width="96" height="6" fill="#4a3628" />
          {[0, 1, 2, 3, 4].map((i) => <rect key={i} x={306 + i * 16} y={i % 2 ? 18 : 22} width="11" height={i % 2 ? 22 : 18} fill={look.accent} opacity={0.35 + i * 0.1} />)}
        </g>
      )}
      <ellipse cx="210" cy="190" rx="170" ry="70" fill="url(#desk-lamp)" />
      <rect y="200" width="420" height="80" fill="#1b1612" />
      <g transform="translate(210 196) scale(1.55)" className={off ? "fs-off" : undefined}>
        <rect x={-34} y={-2} width={68} height={10} fill={look.top} />
        <Body look={look} pose={pose} working={meta.working} />
      </g>
      <rect x="40" y="190" width="340" height="14" rx="2" fill={id === "max" ? "#3d4452" : "#6b4430"} />
      <rect x="48" y="204" width="324" height="70" fill={id === "max" ? "#2a2f3a" : "#4a2f22"} />
      <rect x="150" y="222" width="120" height="26" rx="3" fill="#14110f" stroke="#cdb27a" strokeWidth="1.2" />
      <text x="210" y="234" textAnchor="middle" fontSize="10" fontFamily="Fraunces, Georgia, serif" fill="#e4cf9f" letterSpacing="2">{NAMES[id].toUpperCase()}</text>
      <text x="210" y="244" textAnchor="middle" fontSize="5.5" fontFamily="Inter, sans-serif" fill="#a8a29e" letterSpacing="1.2">{AGENTS[id].title.toUpperCase()}</text>
      <g transform="translate(318 150)">
        <rect x="-4" y="28" width="8" height="14" fill="#2b3242" />
        <rect x="-36" y="-6" width="72" height="44" rx="3" fill="#0d1017" stroke="#2b3242" strokeWidth="2" />
        <rect x="-32" y="-2" width="64" height="36" fill={off ? "#07090d" : state.key === "error" ? "#4a1020" : "#13243c"} className={meta.working ? "fs-pulse" : undefined} />
        {!off && <path d="M-26 22 l10 -7 l9 4 l10 -11 l9 5 l12 -9" stroke={look.accent} strokeWidth="1.6" fill="none" />}
      </g>
      <g transform="translate(110 186)">
        <rect x="-6" y="-14" width="12" height="14" rx="2" fill="#ece6da" />
        {!off && <path className="fs-steam" d="M-1 -18 q-3 -4 0 -8 q3 -4 0 -8" stroke="#e2e8f0" strokeWidth="1" fill="none" opacity=".45" />}
      </g>
      <g transform="translate(210 196) scale(1.55)" className={off ? "fs-off" : undefined}>
        <Hands look={look} pose={pose} />
      </g>
    </svg>
  );
}

/** What they say when you sit down: only facts from their state. */
function Report({ id, state, extra }: { id: string; state: AgentState; extra?: ReactNode }) {
  const m = STATE_META[state.key];
  return (
    <div className="relative rounded-2xl border border-ink-700 bg-ink-900/95 p-4 shadow-xl">
      <span className="absolute -top-2 left-10 h-4 w-4 rotate-45 border-l border-t border-ink-700 bg-ink-900" aria-hidden="true" />
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: m.color }}>
        <span className={`h-1.5 w-1.5 rounded-full ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.color }} />
        {NAMES[id]} · right now
      </div>
      <p className="mt-1.5 text-[15px] font-medium leading-snug text-slate-50">{state.headline}</p>
      {state.detail && <p className="mt-1 text-sm leading-relaxed text-slate-400">{state.detail}</p>}
      {extra}
      <p className="mt-2 text-[10px] text-slate-500" title={when(state.at)}>{state.at ? `${ago(state.at)} · ` : ""}source: {state.source}</p>
    </div>
  );
}

type Tile = { key: string; label: string; icon: string; teaser: string; body: ReactNode; wide?: boolean };

function Tiles({ tiles, open, setOpen }: { tiles: Tile[]; open: string | null; setOpen: (k: string | null) => void }) {
  const current = tiles.find((t) => t.key === open);
  if (current) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="mb-3 flex items-center gap-3">
          <button className="btn-ghost px-2.5 py-1 text-xs" onClick={() => setOpen(null)}>← Back</button>
          <h3 className="text-sm font-semibold text-slate-100">{current.icon} {current.label}</h3>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">{current.body}</div>
      </div>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3">
      {tiles.map((t) => (
        <button key={t.key} onClick={() => setOpen(t.key)} className="group rounded-xl border border-ink-700 bg-ink-850/80 p-3 text-left transition hover:border-gold-500/50 hover:bg-ink-800">
          <div className="text-lg" aria-hidden="true">{t.icon}</div>
          <div className="mt-1 text-sm font-medium text-slate-100">{t.label}</div>
          <div className="mt-0.5 truncate text-xs text-slate-500 group-hover:text-slate-400">{t.teaser}</div>
        </button>
      ))}
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-xl bg-ink-850 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="mt-0.5 text-lg font-semibold tabular-nums text-slate-50">{value}</div>
      {sub && <div className="text-[11px] text-slate-500">{sub}</div>}
    </div>
  );
}

function CapitalTile({ m, onDone }: { m: ManagerDetail; onDone: () => void }) {
  const { overview, reloadOverview } = useLive();
  const [amount, setAmount] = useState("");
  const done = () => {
    setAmount("");
    onDone();
    reloadOverview();
  };
  const give = useAction(() => api.post("/api/capital/allocate", { manager: m.id, amount, note: `from ${m.name}'s desk` }), done);
  const take = useAction(() => api.post("/api/capital/deallocate", { manager: m.id, amount, note: `from ${m.name}'s desk` }), done);
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-3 gap-2">
        <Kpi label="His cash" value={money(m.books.LIVE.cash)} sub={`${money(m.books.LIVE.free_cash)} free`} />
        <Kpi label="Invested" value={money(m.books.LIVE.invested)} />
        <Kpi label="Unallocated" value={money(overview?.firm.unallocated)} sub="firm cash you can give" />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className="label">Amount ($)</span>
          <input className="input mt-1 w-32" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="50.00" />
        </label>
        <button className="btn-primary" disabled={!amount || give.pending} onClick={() => give.run()}>Give to {m.name}</button>
        <button className="btn-ghost" disabled={!amount || take.pending} onClick={() => take.run()}>Take back</button>
      </div>
      <p className="text-xs text-slate-500">Taking back moves free cash only; to free invested money, lower it and {m.name} raises cash on his next run.</p>
      <ErrorLine error={give.error ?? take.error} />
    </div>
  );
}

function ManagerDesk({ id }: { id: string }) {
  const { bump } = useLive();
  const { data: m, reload } = useResource<ManagerDetail>(`/api/managers/${id}`, { every: 30000, bump });
  const [open, setOpen] = useState<string | null>(null);
  if (!m) return <p className="text-sm text-slate-500">Pulling up the books…</p>;
  const book = m.active_book ?? "LIVE";
  const b = m.books[book];
  const held = b.positions.filter((p) => p.quantity > 0);
  const today = new Date().toISOString().slice(0, 10);
  const tiles: Tile[] = [
    {
      key: "portfolio", label: "Portfolio", icon: "◔", teaser: `${money(b.nav)} · ${signedPct(b.performance.twr)}`,
      body: (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Kpi label="Value" value={money(b.nav)} />
            <Kpi label="Cash" value={money(b.cash)} sub={`${money(b.free_cash)} free`} />
            <Kpi label="Invested" value={money(b.invested)} />
            <Kpi label="Realized" value={<span className={tone(b.realized_pnl)}>{money(b.realized_pnl)}</span>} />
          </div>
          <PerformanceChart series={b.performance.series} color={AGENTS[id].color} />
          {b.positions.some((p) => p.target > 0) && <WeightBars rows={b.positions.map((p) => ({ symbol: p.symbol, weight: p.weight, target: p.target }))} color={AGENTS[id].color} />}
        </div>
      ),
    },
    { key: "positions", label: "Positions", icon: "▤", teaser: held.length ? `${held.length} holding${held.length > 1 ? "s" : ""}: ${held.map((p) => p.symbol).slice(0, 4).join(", ")}` : "No holdings yet", body: <Positions b={b} /> },
    { key: "strategy", label: "Strategy", icon: "♟", teaser: `Mandate v${m.mandate.version}`, body: <MandateCard m={m} onDone={reload} /> },
    { key: "decisions", label: "Decisions", icon: "✎", teaser: m.last_run ? `Last ${ago(m.last_run.at)}` : "None yet", body: <DecisionLog m={m} /> },
    { key: "orders", label: "Orders", icon: "⇄", teaser: `${m.orders.filter((o) => o.book === book && String(o.created_at).startsWith(today)).length} today · ${m.orders.filter((o) => o.book === book).length} total`, body: <Orders m={m} /> },
    { key: "capital", label: "Capital", icon: "$", teaser: `${money(m.books.LIVE.free_cash)} free`, body: <CapitalTile m={m} onDone={reload} /> },
    ...(id === "alex" ? [{ key: "research", label: "Research", icon: "⌕", teaser: "Notes with sources", body: <ResearchDesk /> }] : []),
    {
      key: "controls", label: "Controls", icon: "⏻", teaser: `${m.mode}${m.mode === "LIVE" ? " · real money" : ""}`,
      body: (
        <div className="space-y-4 text-sm">
          <p className="text-slate-400">{m.name} is <Badge tone={m.mode}>{m.mode}</Badge> since {when(m.mode_changed_at)} ({m.mode_changed_by}). Next decision {until(m.next_run)}.</p>
          {m.implemented && <ModeControl m={m} onDone={reload} />}
          <div className="flex items-center gap-3">
            <RunNow id={m.id} onDone={reload} />
            <span className="text-xs text-slate-500">Orders only go out inside market hours, through every desk check.</span>
          </div>
        </div>
      ),
    },
  ];
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      {!open && (
        <div className="grid grid-cols-3 gap-2">
          <Kpi label={book === "SIM" ? "Simulated value" : "Value"} value={money(b.nav)} />
          <Kpi label="Return" value={<span className={tone(b.performance.twr)}>{signedPct(b.performance.twr)}</span>} sub={`S&P ${signedPct(b.performance.benchmark)}`} />
          <Kpi label="Next decision" value={<span className="text-base">{until(m.next_run)}</span>} />
        </div>
      )}
      <Tiles tiles={tiles} open={open} setOpen={setOpen} />
    </div>
  );
}

const GRAHAM_STARTERS = [
  "How did today go for the firm?",
  "Who is doing best so far, and is it meaningful yet?",
  "What should I do with the unallocated cash?",
  "Walk me through Alex's research notes.",
  "Are any risk limits close to triggering?",
];

function GrahamDesk({ space }: { space: Space }) {
  const { bump, reloadOverview } = useLive();
  const proposals = useResource<{ proposals: Proposal[] }>("/api/cio/proposals", { every: 30000, bump });
  const memory = useResource<{ memories: Memory[] }>("/api/cio/memory", { every: 0, bump });
  const usage = useResource<Usage>("/api/cio/usage", { every: 60000 });
  const pool = useResource<{ pool: PoolStatus }>("/api/capital", { every: 60000, bump });
  const refresh = () => {
    proposals.reload();
    memory.reload();
    usage.reload();
    pool.reload();
    reloadOverview();
  };
  const chat = useCioChat(refresh);
  const [panel, setPanel] = useState<string | null>(null);
  const all = proposals.data?.proposals ?? [];
  const byId = Object.fromEntries(all.map((p) => [p.id, p]));
  const pending = all.filter((p) => p.status === "PENDING");
  const live = (memory.data?.memories ?? []).filter((m) => m.active);
  const spend = usage.data?.agents.cio;
  const p = pool.data?.pool;
  const chips: { key: string; label: string; value: string; body: ReactNode }[] = [
    {
      key: "proposals", label: "Proposals", value: pending.length ? `${pending.length} to decide` : "none open",
      body: pending.length ? <div className="space-y-3">{pending.map((x) => <ProposalCard key={x.id} p={x} onDecided={refresh} />)}</div> : <Empty>Nothing waiting for you. Graham files proposals here; nothing changes until you approve.</Empty>,
    },
    { key: "memory", label: "Memory", value: `${live.length} notes`, body: <MemoryPanel memories={memory.data?.memories ?? []} onChange={memory.reload} /> },
    {
      key: "pool", label: "His pool", value: p ? money(Number(p.cash)) : "—",
      body: p ? (
        <div className="space-y-3 text-sm">
          <p className="text-slate-400">Money you give Graham to distribute by performance. He can move it only to managers trading real money, never past your caps, and can pull back only what he gave.</p>
          <table className="tbl">
            <thead><tr><th>Manager</th><th className="text-right">Share</th><th className="text-right">Cap</th><th className="text-right">From Graham</th></tr></thead>
            <tbody>{p.managers.map((m) => <tr key={m.id}><td>{m.name} <span className="text-xs text-slate-500">{m.mode}</span></td><td className="text-right">{pct(m.share, 1)}</td><td className="text-right">{pct(m.cap, 0)}</td><td className="text-right">{money(Number(m.granted))}</td></tr>)}</tbody>
          </table>
          <Link to="/capital" className="text-xs text-gold-400 hover:underline">Fund the pool or change caps in Capital →</Link>
        </div>
      ) : <p className="text-sm text-slate-500">Loading…</p>,
    },
    {
      key: "budget", label: "AI budget", value: spend ? `${money(spend.spent)} of ${money(spend.cap)}` : "—",
      body: spend ? (
        <div className="text-sm">
          <div className="flex justify-between"><span className="text-slate-300">{money(spend.spent)} used</span><span className="text-slate-500">of {money(spend.cap)} · {usage.data?.month}</span></div>
          <div className="mt-2 h-2 rounded-full bg-ink-800"><div className="h-2 rounded-full bg-gold-500" style={{ width: `${Math.min(100, (spend.spent / Math.max(spend.cap, 0.0001)) * 100)}%` }} /></div>
          <p className="mt-2 text-xs text-slate-500">Change it in <Link className="text-gold-400" to="/system">System</Link>.</p>
        </div>
      ) : null,
    },
  ];
  const current = chips.find((c) => c.key === panel);
  const convs = chat.convs.data?.conversations ?? [];
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap gap-2">
        {chips.map((c) => (
          <button key={c.key} onClick={() => setPanel(panel === c.key ? null : c.key)} className={`rounded-full border px-3 py-1.5 text-xs transition ${panel === c.key ? "border-gold-500/70 bg-ink-800 text-slate-100" : "border-ink-700 text-slate-300 hover:border-ink-600"}`}>
            <span className="text-slate-500">{c.label} · </span>{c.value}
          </button>
        ))}
      </div>
      <div className="mt-3 flex items-center gap-2">
        <select className="input py-1.5 text-xs" value={chat.active ?? ""} onChange={(e) => chat.setActive(Number(e.target.value))} aria-label="Conversation">
          {!convs.length && <option value="">New conversation</option>}
          {convs.map((c) => <option key={c.id} value={c.id}>{c.title} · {ago(c.updated_at)}</option>)}
        </select>
        <button className="btn-ghost shrink-0 px-2.5 py-1.5 text-xs" onClick={chat.newConversation}>New</button>
      </div>
      <div className="mt-3 flex min-h-0 flex-1 flex-col overflow-hidden rounded-2xl border border-ink-700 bg-ink-900/70">
        <ChatThread chat={chat} proposalsById={byId} onDecided={refresh} greeting="Pull up a chair. What's on your mind?" starters={GRAHAM_STARTERS} />
      </div>
      {current && (
        <div className="absolute inset-0 top-10 z-10 flex flex-col rounded-2xl border border-ink-600 bg-ink-900 p-4 shadow-2xl">
          <div className="mb-3 flex items-center gap-3">
            <button className="btn-ghost px-2.5 py-1 text-xs" onClick={() => setPanel(null)}>← Back to the conversation</button>
            <h3 className="text-sm font-semibold text-slate-100">{current.label}</h3>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto pr-1">{current.body}</div>
        </div>
      )}
      {space.cio.pending_proposals.length > 0 && panel !== "proposals" && (
        <p className="mt-2 text-xs text-gold-300">{space.cio.pending_proposals.length} proposal(s) waiting: open Proposals above to decide.</p>
      )}
    </div>
  );
}

export function Office({ id, state, space, onClose }: { id: string; state: AgentState; space: Space; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const agent = space.agents.find((a) => a.id === id);
  const next = agent?.next_run ? `Next decision ${until(agent.next_run)}.` : null;
  return (
    <div className="fixed inset-0 z-50 overflow-y-auto bg-ink-950/80 backdrop-blur-sm lg:absolute lg:z-20 lg:overflow-hidden" role="dialog" aria-label={`1-on-1 with ${NAMES[id]}`}>
      <div className="mx-auto flex min-h-full max-w-[1400px] flex-col p-3 lg:h-full lg:p-5">
        <header className="mb-3 flex items-center gap-3">
          <button onClick={onClose} className="btn-ghost px-3 py-1.5 text-xs">← The Firm</button>
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 rounded-full" style={{ background: AGENTS[id].color }} />
            <span className="font-display text-xl text-slate-50">{NAMES[id]}</span>
            <span className="hidden text-xs text-slate-400 sm:inline">{AGENTS[id].title}</span>
            {agent && <Badge tone={agent.mode}>{agent.mode}</Badge>}
          </div>
          <Link to={id === "cio" ? "/cio" : `/managers/${id}`} className="ml-auto text-xs text-slate-500 hover:text-slate-300">Full workspace ↗</Link>
        </header>
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(300px,0.85fr)_1.4fr]">
          <div className="flex min-h-0 flex-col gap-3">
            <div className="relative aspect-[3/2] overflow-hidden rounded-2xl border border-ink-700 lg:aspect-auto lg:min-h-[240px] lg:flex-1">
              <DeskScene id={id} state={state} />
            </div>
            <Report id={id} state={state} extra={id !== "cio" && next ? <p className="mt-1 text-xs text-slate-500">{next}</p> : null} />
          </div>
          <div className="flex min-h-[60vh] flex-col rounded-2xl border border-ink-700 bg-ink-900/90 p-4 lg:min-h-0">
            {id === "cio" ? <GrahamDesk space={space} /> : <ManagerDesk id={id} />}
          </div>
        </div>
      </div>
    </div>
  );
}
