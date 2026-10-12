// A full-screen 1-on-1 in someone's office, inside The Firm. The room fills the screen; the
// person sits at their desk and speaks first (their real report). Questions on the left get
// answers in their voice, assembled only from their records; widgets on the right open
// panels with the full detail. Graham's office centers on the conversation.

import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useLive } from "../App";
import { api, type ManagerDetail, type Memory, type PoolStatus, type Proposal, type Usage } from "../api";
import { PerformanceChart, WeightBars } from "../components/charts";
import { AGENTS, Badge, Empty, ErrorLine, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { ago, money, pct, signedPct, tone, until, when } from "../format";
import { ChatThread, MemoryPanel, ProposalCard, useCioChat } from "../pages/CIO";
import { DecisionLog, MandateCard, ModeControl, Orders, Positions, ResearchDesk, RunNow } from "../pages/Manager";
import { Body, Chair, Hands, LOOKS, Zzz } from "./Character";
import { poseFor } from "./pose";
import { type AgentState, MOOD_LABEL, type Mood, type Space, STATE_META } from "./state";

const NAMES: Record<string, string> = { cio: "Graham", tim: "Tim", alex: "Alex", max: "Max" };
const WALL: Record<string, [string, string, string]> = {
  cio: ["#2b2119", "#1a140f", "#5a3d2b"],
  tim: ["#1a2740", "#0f182a", "#3b2b22"],
  alex: ["#241e38", "#141127", "#3b2b22"],
  max: ["#1e2129", "#121419", "#2a2f3a"],
};
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

const glass = "rounded-2xl border border-white/10 bg-ink-950/75 shadow-2xl backdrop-blur-md";

// -- the room ----------------------------------------------------------------------------
function Room({ id, state, mood, shifted = false }: { id: string; state: AgentState; mood: Mood; shifted?: boolean }) {
  const look = LOOKS[id] ?? LOOKS.tim;
  const pose = poseFor(state.key);
  const meta = STATE_META[state.key];
  const [top, low, wood] = WALL[id] ?? WALL.tim;
  const off = state.key === "inactive";
  const asleep = mood.kind === "asleep" && !off;
  const stressed = mood.kind === "stressed";
  const feeling = stressed ? "stressed" : mood.kind === "pleased" ? "pleased" : "calm";
  const gaze = meta.working ? "monitor" : mood.kind === "pleased" ? "viewer" : "wander";
  const cx = 1200;
  const deskY = 640;
  const s = 3;
  return (
    <svg viewBox="0 0 2400 1000" preserveAspectRatio={shifted ? "xMaxYMid slice" : "xMidYMid slice"} className="fs-scene absolute inset-0 h-full w-full" aria-hidden="true">
      <defs>
        <linearGradient id={`room-wall-${id}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={low} />
          <stop offset=".55" stopColor={top} />
          <stop offset="1" stopColor={low} />
        </linearGradient>
        <linearGradient id="room-floor" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2a1f18" />
          <stop offset="1" stopColor="#120d0a" />
        </linearGradient>
        <linearGradient id="room-night" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#070c1c" />
          <stop offset="1" stopColor="#1d2240" />
        </linearGradient>
        <radialGradient id="room-glow">
          <stop offset="0" stopColor="#ffd9a0" stopOpacity=".32" />
          <stop offset="1" stopColor="#ffd9a0" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="room-stress">
          <stop offset="0" stopColor="#f43f5e" stopOpacity=".28" />
          <stop offset="1" stopColor="#f43f5e" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="room-state">
          <stop offset="0" stopColor={meta.color} stopOpacity=".22" />
          <stop offset="1" stopColor={meta.color} stopOpacity="0" />
        </radialGradient>
        <pattern id="room-lit" width="16" height="18" patternUnits="userSpaceOnUse">
          <rect width="16" height="18" fill="#0d1324" />
          <rect x="3" y="3" width="4" height="5" fill="#f6c979" opacity=".7" />
          <rect x="9" y="3" width="4" height="5" fill="#f6c979" opacity=".12" />
          <rect x="3" y="11" width="4" height="5" fill="#9fc3ff" opacity=".2" />
          <rect x="9" y="11" width="4" height="5" fill="#f6c979" opacity=".5" />
        </pattern>
      </defs>
      {/* walls with slats */}
      <rect width="2400" height="1000" fill={`url(#room-wall-${id})`} />
      {Array.from({ length: 60 }, (_, i) => <rect key={i} x={i * 40} y="0" width="2" height="780" fill="#000" opacity=".16" />)}
      <rect y="560" width="2400" height="220" fill="#000" opacity=".12" />
      <rect y="556" width="2400" height="6" fill={wood} opacity=".7" />

      {/* window onto the city */}
      <g>
        <rect x="300" y="130" width="460" height="400" fill="url(#room-night)" />
        {[[310, 300, 50, 230], [370, 250, 44, 280], [420, 330, 60, 200], [490, 220, 40, 310], [540, 290, 66, 240], [616, 260, 46, 270], [670, 320, 80, 210]].map(([x, y, w, h], i) => (
          <rect key={i} x={x} y={y} width={w} height={h} fill="url(#room-lit)" />
        ))}
        <circle cx="690" cy="200" r="22" fill="#f3ead2" opacity=".85" />
        <path d="M530 130 V530 M300 330 H760" stroke={wood} strokeWidth="8" />
        <rect x="300" y="130" width="460" height="400" fill="none" stroke={wood} strokeWidth="14" />
        <rect x="286" y="530" width="488" height="18" fill={wood} />
      </g>

      {/* shelves */}
      <g>
        <rect x="1640" y="150" width="360" height="560" fill="#1d1612" stroke={wood} strokeWidth="10" />
        {[0, 1, 2, 3].map((r) => (
          <g key={r}>
            <rect x="1645" y={150 + (r + 1) * 140 - 8} width="350" height="10" fill={wood} />
            {Array.from({ length: 14 }, (_, i) => (
              <rect key={i} x={1660 + i * 23} y={150 + r * 140 + (i % 5 === 2 ? 50 : 30)} width="17" height={i % 5 === 2 ? 82 : 102} fill={[look.accent, "#6b2d2d", "#2d4a6b", "#8a7448", "#33553f"][(i + r) % 5]} opacity={i % 5 === 0 ? 0.75 : 0.9} />
            ))}
          </g>
        ))}
      </g>

      {/* the wall display behind the desk shows only real state */}
      {id === "alex" ? (
        <g>
          <rect x="840" y="200" width="300" height="190" fill="#3a3046" stroke={wood} strokeWidth="8" />
          {[[860, 220, "#a78bfa"], [930, 230, "#fde68a"], [1010, 215, "#93c5fd"], [1070, 240, "#fca5a5"], [880, 300, "#a7f3d0"], [960, 310, "#fde68a"], [1040, 300, "#c4b5fd"]].map(([x, y, c], i) => (
            <rect key={i} x={x as number} y={y as number} width="54" height="44" fill={c as string} opacity=".85" transform={`rotate(${i % 2 ? 3 : -3} ${x} ${y})`} />
          ))}
        </g>
      ) : id !== "cio" ? (
        <g fontFamily="Inter, sans-serif">
          <rect x="820" y="210" width="300" height="170" rx="8" fill="#07090d" stroke="#2b3242" strokeWidth="6" />
          <text x="845" y="250" fontSize="20" fontWeight="700" fill={meta.color} letterSpacing="2">{meta.label.toUpperCase()}</text>
          {state.symbols.slice(0, 3).map((sy, i) => (
            <text key={sy.symbol + i} x="845" y={292 + i * 30} fontSize="24" fontWeight="700" fill="#e5e7eb">
              <tspan fill={sy.side === "sell" ? "#fb7185" : "#34d399"} fontSize="16">{sy.side ? `${sy.side.toUpperCase()} ` : ""}</tspan>
              {sy.symbol}
            </text>
          ))}
          {!state.symbols.length && <text x="845" y="320" fontSize="44" fill="#cdb27a" opacity=".3" fontFamily="Fraunces, serif">537</text>}
        </g>
      ) : (
        <g>
          <rect x="860" y="200" width="200" height="150" fill="#14110f" stroke="#b8975a" strokeWidth="8" />
          <text x="960" y="292" textAnchor="middle" fontSize="54" fill="#e4cf9f" fontFamily="Fraunces, serif">537</text>
        </g>
      )}

      {/* light */}
      <path d={`M${cx} 0 V120`} stroke="#3a4560" strokeWidth="4" />
      <path d={`M${cx - 70} 170 L${cx - 34} 118 h68 L${cx + 70} 170 Z`} fill="#2a3245" />
      <ellipse cx={cx} cy="170" rx="70" ry="10" fill="#ffe2a8" opacity={off || asleep ? 0.25 : 0.9} />
      {!off && !asleep && <path d={`M${cx - 70} 170 L${cx - 420} 1000 H${cx + 420} L${cx + 70} 170 Z`} fill="#ffe9c0" opacity=".07" />}
      <ellipse cx={cx} cy="700" rx="760" ry="300" fill="url(#room-glow)" />
      {meta.working && <ellipse cx={cx} cy="560" rx="420" ry="240" fill="url(#room-state)" className="fs-pulse" />}
      {stressed && <ellipse cx={cx} cy="520" rx="900" ry="420" fill="url(#room-stress)" className="fs-stress" />}
      {asleep && <rect width="2400" height="1000" fill="#020617" opacity=".35" />}

      {/* floor */}
      <rect y="780" width="2400" height="220" fill="url(#room-floor)" />
      {Array.from({ length: 9 }, (_, i) => <rect key={i} y={790 + i * 24} width="2400" height="1.5" fill="#000" opacity=".3" />)}
      <ellipse cx={cx} cy="860" rx="560" ry="70" fill="#3b2c24" opacity=".8" />

      {/* plants */}
      {[[760, 780, 1.6], [1580, 780, 1.4], [220, 790, 1.8]].map(([x, y, k], i) => (
        <g key={i} transform={`translate(${x} ${y}) scale(${k})`}>
          <g className="fs-sway" style={{ transformOrigin: "0px -18px" }}>
            {[[-14, -52, -26], [0, -64, 0], [14, -50, 24], [-8, -40, -44], [9, -38, 40], [-3, -74, 8]].map(([dx, dy, r], j) => (
              <ellipse key={j} cx={dx} cy={dy} rx={7} ry={20} fill={["#2f7d5b", "#3d9a6f", "#2a6e50"][j % 3]} transform={`rotate(${r} ${dx} ${dy})`} />
            ))}
          </g>
          <path d="M-14 -20 h28 l-4 20 h-20 Z" fill="#2a2522" />
        </g>
      ))}

      {/* the person and the desk */}
      <g transform={`translate(${cx} ${deskY}) scale(${s})`} className={off ? "fs-off" : undefined}>
        <Chair look={look} />
      </g>
      <g transform={`translate(${cx} ${deskY}) scale(${s})`} className={off ? "fs-off" : undefined}>
        <rect x={-34} y={-2} width={68} height={12} fill={look.top} />
        <Body look={look} pose={asleep ? "asleep" : pose} working={meta.working} feeling={feeling} gaze={gaze} chair={false} />
      </g>
      <rect x={cx - 450} y={deskY - 10} width="900" height="34" rx="4" fill={id === "max" ? "#3d4452" : "#6b4430"} />
      <rect x={cx - 430} y={deskY + 24} width="860" height="156" fill={id === "max" ? "#2a2f3a" : wood} />
      <rect x={cx - 430} y={deskY + 24} width="860" height="8" fill="#000" opacity=".3" />
      <g transform={`translate(${cx} ${deskY + 88})`}>
        <rect x="-150" y="-34" width="300" height="68" rx="6" fill="#14110f" stroke="#cdb27a" strokeWidth="3" />
        <text y="0" textAnchor="middle" fontSize="30" fontFamily="Fraunces, Georgia, serif" fill="#e4cf9f" letterSpacing="7">{NAMES[id].toUpperCase()}</text>
        <text y="24" textAnchor="middle" fontSize="12" fontFamily="Inter, sans-serif" fill="#a8a29e" letterSpacing="3">{AGENTS[id].title.toUpperCase()}</text>
      </g>
      {/* desk props */}
      {[...(id === "max" ? [cx - 330] : []), cx + 300].map((mx) => (
        <g key={mx} transform={`translate(${mx} ${deskY - 10})`}>
          <rect x="-10" y="-50" width="20" height="50" fill="#2b3242" />
          <rect x="-110" y="-190" width="220" height="140" rx="8" fill="#0d1017" stroke="#2b3242" strokeWidth="6" />
          <rect x="-100" y="-180" width="200" height="120" fill={off ? "#07090d" : state.key === "error" ? "#4a1020" : "#13243c"} className={meta.working ? "fs-pulse" : undefined} />
          {!off && <path d="M-84 -86 l34 -24 l30 14 l34 -38 l30 16 l40 -30" stroke={look.accent} strokeWidth="5" fill="none" />}
        </g>
      ))}
      {id !== "max" && (
        <g transform={`translate(${cx - 300} ${deskY - 10})`}>
          <rect x="-16" y="-40" width="32" height="40" rx="5" fill="#ece6da" />
          <path d="M16 -30 q16 0 16 12 q0 12 -16 12" stroke="#ece6da" strokeWidth="6" fill="none" />
          {!off && <path className="fs-steam" d="M-2 -52 q-8 -10 0 -20 q8 -10 0 -20" stroke="#e2e8f0" strokeWidth="3" fill="none" opacity=".45" />}
        </g>
      )}
      <g transform={`translate(${cx} ${deskY}) scale(${s})`} className={off ? "fs-off" : undefined}>
        <Hands look={look} pose={pose} />
      </g>
      {asleep && <Zzz x={cx + 60} y={deskY - 300} size={28} />}
    </svg>
  );
}

// -- shared pieces ---------------------------------------------------------------------------
function Speech({ id, state, mood, title, children }: { id: string; state: AgentState; mood?: Mood; title?: string; children?: ReactNode }) {
  const m = STATE_META[state.key];
  return (
    <div className={`${glass} relative p-4`}>
      <span className="absolute -bottom-2 right-12 hidden h-4 w-4 rotate-45 border-b border-r border-white/10 bg-ink-950/75 lg:block" aria-hidden="true" />
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider" style={{ color: m.color }}>
        <span className={`h-1.5 w-1.5 rounded-full ${m.working ? "fs-pulse-dot" : ""}`} style={{ background: m.color }} />
        {title ?? `${NAMES[id]} · right now`}
      </div>
      {children ?? (
        <>
          <p className="mt-1.5 text-base font-medium leading-snug text-slate-50">{state.headline}</p>
          {state.detail && <p className="mt-1 text-sm leading-relaxed text-slate-300">{state.detail}</p>}
          <p className="mt-2 text-[10px] text-slate-500" title={when(state.at)}>{state.at ? `${ago(state.at)} · ` : ""}source: {state.source}</p>
        </>
      )}
      {mood && mood.kind !== "calm" && mood.kind !== "focused" && (
        <p className={`mt-2 border-t border-white/10 pt-2 text-xs ${mood.kind === "stressed" ? "text-rose-300" : mood.kind === "pleased" ? "text-emerald-300" : "text-slate-400"}`}>
          {MOOD_LABEL[mood.kind]}{mood.reason ? ` · ${mood.reason}` : ""}
        </p>
      )}
    </div>
  );
}

function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="min-w-[7rem] px-4 py-2">
      <div className="text-[10px] uppercase tracking-wider text-slate-500">{label}</div>
      <div className="text-lg font-semibold tabular-nums text-slate-50">{value}</div>
      {sub && <div className="text-[11px] text-slate-500">{sub}</div>}
    </div>
  );
}

function Panel({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div className={`${glass} flex min-h-0 flex-col p-4`}>
      <div className="mb-3 flex items-center gap-3">
        <h3 className="text-sm font-semibold text-slate-100">{title}</h3>
        <button className="ml-auto rounded-md px-2 py-1 text-slate-400 hover:bg-white/5 hover:text-slate-100" onClick={onClose} aria-label="Close panel">✕</button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto pr-1">{children}</div>
    </div>
  );
}

type Widget = { key: string; label: string; icon: string; teaser: string; body: ReactNode };

function WidgetList({ widgets, open }: { widgets: Widget[]; open: (k: string) => void }) {
  return (
    <div className="space-y-2">
      {widgets.map((w) => (
        <button key={w.key} onClick={() => open(w.key)} className={`${glass} group flex w-full items-center gap-3 px-3.5 py-2.5 text-left transition hover:border-gold-500/50`}>
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/5 text-sm text-slate-200" aria-hidden="true">{w.icon}</span>
          <span className="min-w-0">
            <span className="block text-sm font-medium text-slate-100">{w.label}</span>
            <span className="block truncate text-xs text-slate-500 group-hover:text-slate-400">{w.teaser}</span>
          </span>
          <span className="ml-auto text-slate-600 group-hover:text-slate-300" aria-hidden="true">›</span>
        </button>
      ))}
    </div>
  );
}

function CapitalWidget({ m, onDone }: { m: ManagerDetail; onDone: () => void }) {
  const { overview, reloadOverview } = useLive();
  const [amount, setAmount] = useState("");
  const done = () => {
    setAmount("");
    onDone();
    reloadOverview();
  };
  const give = useAction(() => api.post("/api/capital/allocate", { manager: m.id, amount, note: `from ${m.name}'s office` }), done);
  const take = useAction(() => api.post("/api/capital/deallocate", { manager: m.id, amount, note: `from ${m.name}'s office` }), done);
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-3 gap-2 rounded-xl bg-white/5">
        <Kpi label="His cash" value={money(m.books.LIVE.cash)} sub={`${money(m.books.LIVE.free_cash)} free`} />
        <Kpi label="Invested" value={money(m.books.LIVE.invested)} />
        <Kpi label="Unallocated" value={money(overview?.firm.unallocated)} sub="you can give" />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className="label">Amount ($)</span>
          <input className="input mt-1 w-32" inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} placeholder="50.00" />
        </label>
        <button className="btn-primary" disabled={!amount || give.pending} onClick={() => give.run()}>Give to {m.name}</button>
        <button className="btn-ghost" disabled={!amount || take.pending} onClick={() => take.run()}>Take back</button>
      </div>
      <p className="text-xs text-slate-500">Taking back moves free cash only; invested money is freed when {m.name} sells on a later run.</p>
      <ErrorLine error={give.error ?? take.error} />
    </div>
  );
}

// -- answers in the manager's voice, from records only ------------------------------------------
type Note = { symbol: string; verdict: string; conviction: number; thesis: string; created_at: string };

function strategyLine(m: ManagerDetail): string {
  const p = m.mandate.params;
  if (m.id === "tim") return `I keep a diversified ETF portfolio: ${Object.entries(p.targets ?? {}).map(([k, v]) => `${pct(Number(v), 0)} ${k}`).join(", ")}. I invest new cash toward those targets and rebalance when a fund drifts more than ${pct(p.abs_band, 0)}, plus a quarterly check. No predictions, no AI.`;
  if (m.id === "alex") return `I rank ${(p.universe ?? []).length} large US stocks by 12-month momentum adjusted for volatility, only above their 200-day average. My research desk checks each candidate; I hold the top ${p.positions} approved names in equal amounts and rebalance monthly. Broken trends or a research reject get sold right away.`;
  return `I buy sharp dips (RSI(${p.rsi_days}) below ${p.rsi_entry_below}) in names that are still in an uptrend, up to ${p.max_positions} positions of ${pct(p.position_pct, 0)} each. Every trade has a stop ${pct(p.stop_pct, 0)} below entry; I sell when price recovers above the ${p.exit_sma_days}-day average or after ${p.max_hold_days} sessions.`;
}

function useAnswers(m: ManagerDetail | null, notes: Note[]) {
  return useMemo(() => {
    if (!m) return [];
    const book = m.active_book ?? "LIVE";
    const b = m.books[book];
    const held = b.positions.filter((x) => x.quantity > 0);
    const twr = b.performance.twr;
    const real = book === "LIVE" ? "real money" : "the simulation";
    const qa: { q: string; a: string; source: string }[] = [
      {
        q: "How are we doing?",
        a: `My ${real} book is worth ${money(b.nav)}: ${money(b.invested)} invested and ${money(b.cash)} in cash. ` +
          (twr === null ? "It's too early for a return figure; it appears after two daily closes." : `Since I started I'm ${signedPct(twr)} against ${signedPct(b.performance.benchmark)} for the S&P 500.`),
        source: "Ledger and daily marks",
      },
      { q: "What did you decide last?", a: m.last_run ? `${ago(m.last_run.at)}: ${m.last_run.summary.replace(`${m.name}: `, "")}` : "I haven't made a decision yet.", source: m.runs[0] ? `Event #${m.runs[0].id}` : "Event log" },
      { q: "What do you own?", a: held.length ? `${held.map((x) => `${x.symbol} ${money(x.value)} (${pct(x.weight, 0)})`).join(", ")}, plus ${money(b.cash)} cash.` : `Nothing yet; I'm holding ${money(b.cash)} in cash.`, source: "Ledger positions" },
      { q: "What's next?", a: m.next_run ? `My next decision is ${until(m.next_run)} (${when(m.next_run)}). ${m.mode === "LIVE" ? "Anything I decide goes through every desk check before it reaches Robinhood." : ""}` : `I'm ${m.mode.toLowerCase()}, so nothing is scheduled.`, source: "Scheduler" },
      { q: "How do you invest?", a: strategyLine(m), source: `Mandate v${m.mandate.version}` },
    ];
    if (m.id === "alex") {
      qa.push({
        q: "What did your research find?",
        a: notes.length ? notes.slice(0, 4).map((n) => `${n.symbol}: ${n.verdict === "approve" ? "approved" : "rejected"} (${n.conviction}/5). ${n.thesis.split(". ")[0]}.`).join(" ") : "No research notes yet.",
        source: "Research notes",
      });
    }
    return qa;
  }, [m, notes]);
}

function ManagerOffice({ id, state, mood, onPanel }: { id: string; state: AgentState; mood: Mood; onPanel: (open: boolean) => void }) {
  const { bump } = useLive();
  const navigate = useNavigate();
  const { data: m, reload } = useResource<ManagerDetail>(`/api/managers/${id}`, { every: 30000, bump });
  const notes = useResource<{ notes: Note[] }>(id === "alex" ? "/api/managers/alex/research" : null, { every: 60000, bump }).data?.notes ?? [];
  const answers = useAnswers(m, notes);
  const [asked, setAsked] = useState<number | null>(null);
  const [panel, setPanel] = useState<string | null>(null);
  const book = m?.active_book ?? "LIVE";
  const b = m?.books[book];
  const today = new Date().toISOString().slice(0, 10);
  const widgets: Widget[] = !m || !b ? [] : [
    {
      key: "portfolio", label: "Portfolio", icon: "◔", teaser: `${money(b.nav)} · ${signedPct(b.performance.twr)}`,
      body: (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-1 rounded-xl bg-white/5 sm:grid-cols-4">
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
    { key: "positions", label: "Positions", icon: "▤", teaser: b.positions.filter((p) => p.quantity > 0).map((p) => p.symbol).slice(0, 5).join(", ") || "No holdings yet", body: <Positions b={b} /> },
    { key: "strategy", label: "Strategy", icon: "♟", teaser: `Mandate v${m.mandate.version}`, body: <MandateCard m={m} onDone={reload} /> },
    { key: "decisions", label: "Decisions", icon: "✎", teaser: m.last_run ? `Last ${ago(m.last_run.at)}` : "None yet", body: <DecisionLog m={m} /> },
    { key: "orders", label: "Orders", icon: "⇄", teaser: `${m.orders.filter((o) => o.book === book && String(o.created_at).startsWith(today)).length} today · ${m.orders.filter((o) => o.book === book).length} total`, body: <Orders m={m} /> },
    ...(id === "alex" ? [{ key: "research", label: "Research", icon: "⌕", teaser: `${notes.length} notes with sources`, body: <ResearchDesk /> }] : []),
    { key: "capital", label: "Capital", icon: "$", teaser: `${money(m.books.LIVE.free_cash)} free cash`, body: <CapitalWidget m={m} onDone={reload} /> },
    {
      key: "controls", label: "Controls", icon: "⏻", teaser: `${m.mode}${m.mode === "LIVE" ? " · real money" : ""}`,
      body: (
        <div className="space-y-4 text-sm">
          <p className="text-slate-300">{m.name} is <Badge tone={m.mode}>{m.mode}</Badge> since {when(m.mode_changed_at)} ({m.mode_changed_by}). Next decision {until(m.next_run)}.</p>
          {m.implemented && <ModeControl m={m} onDone={reload} />}
          <div className="flex items-center gap-3">
            <RunNow id={m.id} onDone={reload} />
            <span className="text-xs text-slate-500">Orders only go out inside market hours, through every desk check.</span>
          </div>
        </div>
      ),
    },
  ];
  const current = widgets.find((w) => w.key === panel);
  useEffect(() => onPanel(!!current), [current, onPanel]);
  const answer = asked !== null ? answers[asked] : null;
  return (
    <>
      {/* left: speech + questions */}
      <div className="relative z-10 flex flex-col gap-3 p-4 lg:absolute lg:left-6 lg:top-20 lg:w-[min(24rem,28vw)] lg:p-0">
        {answer ? (
          <Speech id={id} state={state} title={`${NAMES[id]} · ${answer.q}`}>
            <p className="mt-1.5 text-[15px] leading-relaxed text-slate-100">“{answer.a}”</p>
            <div className="mt-2 flex items-center gap-3 text-[10px] text-slate-500">
              <span>source: {answer.source}</span>
              <button className="ml-auto text-slate-400 hover:text-slate-200" onClick={() => setAsked(null)}>What's happening now?</button>
            </div>
          </Speech>
        ) : (
          <Speech id={id} state={state} mood={mood} />
        )}
        <div className={`${glass} p-3`}>
          <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-slate-500">Ask {NAMES[id]}</div>
          <div className="flex flex-wrap gap-1.5">
            {answers.map((qa, i) => (
              <button key={qa.q} onClick={() => setAsked(i)} className={`rounded-full border px-3 py-1.5 text-xs transition ${asked === i ? "border-gold-500/70 bg-white/10 text-slate-50" : "border-white/10 text-slate-300 hover:border-white/25 hover:text-slate-100"}`}>
                {qa.q}
              </button>
            ))}
            <button onClick={() => navigate(`/firm/cio?ask=${encodeURIComponent(`Give me your honest assessment of ${NAMES[id]}: performance, risks, and what you would change.`)}`)} className="rounded-full border border-gold-500/40 px-3 py-1.5 text-xs text-gold-300 hover:border-gold-500">
              Ask Graham about {NAMES[id]} →
            </button>
          </div>
        </div>
      </div>

      {/* right: widgets or an open panel */}
      <div className="relative z-10 p-4 lg:absolute lg:bottom-24 lg:right-6 lg:top-20 lg:flex lg:w-[min(36rem,38vw)] lg:flex-col lg:p-0">
        {!m ? (
          <div className={`${glass} p-4 text-sm text-slate-400`}>Pulling up the books…</div>
        ) : current ? (
          <Panel title={`${current.icon}  ${current.label}`} onClose={() => setPanel(null)}>{current.body}</Panel>
        ) : (
          <div className="lg:ml-auto lg:w-72"><WidgetList widgets={widgets} open={setPanel} /></div>
        )}
      </div>

      {/* bottom: the numbers that matter */}
      {m && b && (
        <div className={`${glass} relative z-10 mx-4 mb-4 flex flex-wrap justify-center divide-x divide-white/10 lg:absolute lg:bottom-6 lg:left-1/2 lg:mx-0 lg:mb-0 lg:-translate-x-1/2`}>
          <Kpi label={book === "SIM" ? "Simulated value" : "Value"} value={money(b.nav)} />
          <Kpi label="Return" value={<span className={tone(b.performance.twr)}>{signedPct(b.performance.twr)}</span>} sub={`S&P ${signedPct(b.performance.benchmark)}`} />
          <Kpi label="Cash" value={money(b.cash)} />
          <Kpi label="Next decision" value={<span className="text-base">{until(m.next_run)}</span>} />
        </div>
      )}
    </>
  );
}

// -- Graham ----------------------------------------------------------------------------------
const GRAHAM_STARTERS = [
  "How did today go for the firm?",
  "Who is doing best so far, and is it meaningful yet?",
  "What should I do with the unallocated cash?",
  "Walk me through Alex's research notes.",
  "Are any risk limits close to triggering?",
];

function GrahamOffice({ state, mood, space }: { state: AgentState; mood: Mood; space: Space }) {
  const { bump, reloadOverview } = useLive();
  const [params, setParams] = useSearchParams();
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
  useEffect(() => {
    // Arriving from a manager's office with a question: put it in the composer, never send it unasked.
    const q = params.get("ask");
    if (q) {
      chat.setDraft(q);
      params.delete("ask");
      setParams(params, { replace: true });
    }
  }, [params, setParams, chat]);
  const all = proposals.data?.proposals ?? [];
  const byId = Object.fromEntries(all.map((p) => [p.id, p]));
  const pending = all.filter((p) => p.status === "PENDING");
  const live = (memory.data?.memories ?? []).filter((m) => m.active);
  const spend = usage.data?.agents.cio;
  const p = pool.data?.pool;
  const panels: Widget[] = [
    { key: "proposals", label: "Proposals", icon: "✓", teaser: pending.length ? `${pending.length} to decide` : "none open", body: pending.length ? <div className="space-y-3">{pending.map((x) => <ProposalCard key={x.id} p={x} onDecided={refresh} />)}</div> : <Empty>Nothing waiting. Graham files proposals here; nothing changes until you approve.</Empty> },
    { key: "memory", label: "Memory", icon: "✎", teaser: `${live.length} notes`, body: <MemoryPanel memories={memory.data?.memories ?? []} onChange={memory.reload} /> },
    {
      key: "pool", label: "His pool", icon: "$", teaser: p ? money(Number(p.cash)) : "—",
      body: p ? (
        <div className="space-y-3 text-sm">
          <p className="text-slate-400">Money you give Graham to distribute by performance: only to managers trading real money, never past your caps, and he can pull back only what he gave.</p>
          <table className="tbl">
            <thead><tr><th>Manager</th><th className="text-right">Share</th><th className="text-right">Cap</th><th className="text-right">From Graham</th></tr></thead>
            <tbody>{p.managers.map((m) => <tr key={m.id}><td>{m.name} <span className="text-xs text-slate-500">{m.mode}</span></td><td className="text-right">{pct(m.share, 1)}</td><td className="text-right">{pct(m.cap, 0)}</td><td className="text-right">{money(Number(m.granted))}</td></tr>)}</tbody>
          </table>
          <Link to="/capital" className="text-xs text-gold-400 hover:underline">Fund the pool or change caps in Capital →</Link>
        </div>
      ) : <p className="text-sm text-slate-500">Loading…</p>,
    },
    {
      key: "budget", label: "AI budget", icon: "◷", teaser: spend ? `${money(spend.spent)} of ${money(spend.cap)}` : "—",
      body: spend ? (
        <div className="text-sm">
          <div className="flex justify-between"><span className="text-slate-300">{money(spend.spent)} used</span><span className="text-slate-500">of {money(spend.cap)} · {usage.data?.month}</span></div>
          <div className="mt-2 h-2 rounded-full bg-ink-800"><div className="h-2 rounded-full bg-gold-500" style={{ width: `${Math.min(100, (spend.spent / Math.max(spend.cap, 0.0001)) * 100)}%` }} /></div>
          <p className="mt-2 text-xs text-slate-500">Change it in <Link className="text-gold-400" to="/system">System</Link>.</p>
        </div>
      ) : null,
    },
  ];
  const current = panels.find((w) => w.key === panel);
  const convs = chat.convs.data?.conversations ?? [];
  return (
    <>
      <div className="relative z-10 flex flex-col gap-3 p-4 lg:absolute lg:left-6 lg:top-20 lg:w-[min(22rem,26vw)] lg:p-0">
        <Speech id="cio" state={state} mood={mood} />
        {current ? (
          <Panel title={`${current.icon}  ${current.label}`} onClose={() => setPanel(null)}>{current.body}</Panel>
        ) : (
          <WidgetList widgets={panels} open={setPanel} />
        )}
        {space.cio.pending_proposals.length > 0 && panel !== "proposals" && <p className="text-xs text-gold-300">{space.cio.pending_proposals.length} proposal(s) waiting for you.</p>}
      </div>
      <div className={`${glass} relative z-10 m-4 flex h-[70vh] flex-col overflow-hidden lg:absolute lg:bottom-6 lg:right-6 lg:top-20 lg:m-0 lg:h-auto lg:w-[min(40rem,40vw)]`}>
        <div className="flex items-center gap-2 border-b border-white/10 p-3">
          <select className="input py-1.5 text-xs" value={chat.active ?? ""} onChange={(e) => chat.setActive(Number(e.target.value))} aria-label="Conversation">
            {!convs.length && <option value="">New conversation</option>}
            {convs.map((c) => <option key={c.id} value={c.id}>{c.title} · {ago(c.updated_at)}</option>)}
          </select>
          <button className="btn-ghost shrink-0 px-2.5 py-1.5 text-xs" onClick={chat.newConversation}>New</button>
        </div>
        <ChatThread chat={chat} proposalsById={byId} onDecided={refresh} greeting="Pull up a chair. What's on your mind?" starters={GRAHAM_STARTERS} />
      </div>
    </>
  );
}

// -- the office --------------------------------------------------------------------------------
export function Office({ id, state, mood, space, onClose, top = 57 }: { id: string; state: AgentState; mood: Mood; space: Space; onClose: () => void; top?: number }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  const agent = space.agents.find((a) => a.id === id);
  // You're sitting with Graham to talk: he's awake for it.
  if (id === "cio" && (mood.kind === "asleep" || mood.kind === "resting")) mood = { kind: "calm", reason: null };
  const wide = useWide();
  const [panelOpen, setPanelOpen] = useState(false);
  return (
    <div className="fixed inset-x-0 bottom-0 z-40 overflow-y-auto bg-ink-950 lg:overflow-hidden" style={{ top }} role="dialog" aria-label={`1-on-1 with ${NAMES[id]}`}>
      {/* the room: full screen on desktop, a banner on phones */}
      <div className="relative h-[46vh] lg:absolute lg:inset-0 lg:h-auto">
        <Room id={id} state={state} mood={mood} shifted={wide && (id === "cio" || panelOpen)} />
        <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-ink-950/50 via-transparent to-ink-950/60" />
      </div>
      <header className="absolute left-0 right-0 top-0 z-20 flex items-center gap-3 px-4 py-3 lg:px-6">
        <button onClick={onClose} className={`${glass} px-3 py-1.5 text-xs text-slate-200 hover:text-white`}>← The Firm</button>
        <div className={`${glass} flex items-center gap-2 px-3 py-1.5`}>
          <span className="h-2 w-2 rounded-full" style={{ background: AGENTS[id].color }} />
          <span className="font-display text-lg leading-none text-slate-50">{NAMES[id]}</span>
          <span className="hidden text-xs text-slate-400 sm:inline">{AGENTS[id].title}</span>
          {agent && <Badge tone={agent.mode}>{agent.mode}</Badge>}
        </div>
        <Link to={id === "cio" ? "/cio" : `/managers/${id}`} className={`${glass} ml-auto px-3 py-1.5 text-xs text-slate-400 hover:text-slate-200`}>Full workspace ↗</Link>
      </header>
      {id === "cio" ? <GrahamOffice state={state} mood={mood} space={space} /> : <ManagerOffice id={id} state={state} mood={mood} onPanel={setPanelOpen} />}
    </div>
  );
}
