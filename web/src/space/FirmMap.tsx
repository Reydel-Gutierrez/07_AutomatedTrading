// The whole firm on one map: the HQ Tower (four floors) and the Academy, drawn as a
// cutaway seen from the front. Every room is a department you can click; every person
// at a desk can be clicked. Status chips, screens, and boards show only live records;
// furniture, plants, lights, and the city are decoration.

import { type KeyboardEvent, type ReactNode, memo } from "react";
import type { FirmEvent, Overview } from "../api";
import { AGENTS } from "../components/ui";
import { money } from "../format";
import { Body, Chair, Hands, LOOKS, type Look, type Pose, Zzz } from "./Character";
import { ACADEMY, DEPTS, type Dept, type DeptId, FLOORS, type Floor, type FloorId, GROUND, type Rect, SESSIONS, SLAB, TOWER, WORLD, fxMarketOpen, localTime, sessionOpen } from "./departments";
import { poseFor } from "./pose";
import { type AcademySpaceAgent, type AgentState, type Mood, PHASE, type Space, STATE_META } from "./state";

export type AgentId = "cio" | "tim" | "alex" | "max";
export type Selection = { kind: "person"; id: AgentId } | { kind: "dept"; id: DeptId } | { kind: "trainee"; id: string } | { kind: "floor"; id: FloorId };

export const NAMES: Record<AgentId, string> = { cio: "Graham", tim: "Tim", alex: "Alex", max: "Max" };
export const ALLOC_COLORS: Record<string, string> = { tim: AGENTS.tim.color, alex: AGENTS.alex.color, max: AGENTS.max.color, cio: AGENTS.cio.color, unallocated: "#475569" };

type Ctx = {
  space: Space;
  states: Record<AgentId, AgentState>;
  moods: Record<AgentId, Mood>;
  overview: Overview | null;
  events: FirmEvent[];
  now: number;
  sel: Selection | null;
  onSelect: (s: Selection) => void;
  onFocus: (r: Rect) => void;
  onOpen?: (path: string) => void; // open a page over the map (a folder)
};

const cut = (s: string, n: number) => (s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s);
/** Word-wrap into at most `lines` lines of about `n` characters; the last line is cut with an ellipsis. */
function wrap(s: string, n: number, lines: number): string[] {
  const out: string[] = [];
  let cur = "";
  for (const w of s.split(/\s+/)) {
    if (cur && (cur + " " + w).length > n) {
      out.push(cur);
      cur = w;
    } else cur = cur ? `${cur} ${w}` : w;
  }
  if (cur) out.push(cur);
  return out.length > lines ? [...out.slice(0, lines - 1), cut(out.slice(lines - 1).join(" "), n)] : out;
}
const activate = (fn: () => void) => (e: KeyboardEvent) => {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    e.stopPropagation();
    fn();
  }
};
const fy = (r: Rect) => r[1] + r[3] - SLAB; // floor surface of a room

// -- shared art ---------------------------------------------------------------------------------

const Defs = memo(function Defs() {
  return (
    <defs>
      <linearGradient id="fm-sky" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#050915" />
        <stop offset=".6" stopColor="#121a30" />
        <stop offset="1" stopColor="#2b2230" />
      </linearGradient>
      <radialGradient id="fm-warm">
        <stop offset="0" stopColor="#ffcf8a" stopOpacity=".5" />
        <stop offset="1" stopColor="#ffcf8a" stopOpacity="0" />
      </radialGradient>
      <radialGradient id="fm-bulb">
        <stop offset="0" stopColor="#ffe2a8" stopOpacity=".8" />
        <stop offset="1" stopColor="#ffe2a8" stopOpacity="0" />
      </radialGradient>
      <linearGradient id="fm-night" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#0a1226" />
        <stop offset="1" stopColor="#23263f" />
      </linearGradient>
      <linearGradient id="fm-glass" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stopColor="#bcd3ff" stopOpacity=".16" />
        <stop offset="1" stopColor="#bcd3ff" stopOpacity=".04" />
      </linearGradient>
      <pattern id="fm-lit" width="14" height="16" patternUnits="userSpaceOnUse">
        <rect width="14" height="16" fill="#0e1424" />
        <rect x="2" y="3" width="3" height="4" fill="#f6c979" opacity=".7" />
        <rect x="8" y="3" width="3" height="4" fill="#f6c979" opacity=".15" />
        <rect x="2" y="10" width="3" height="4" fill="#9fc3ff" opacity=".2" />
        <rect x="8" y="10" width="3" height="4" fill="#f6c979" opacity=".55" />
      </pattern>
      <pattern id="fm-lit2" width="18" height="14" patternUnits="userSpaceOnUse">
        <rect width="18" height="14" fill="#111829" />
        <rect x="3" y="3" width="4" height="3" fill="#f6c979" opacity=".2" />
        <rect x="11" y="3" width="4" height="3" fill="#f6c979" opacity=".65" />
        <rect x="3" y="9" width="4" height="3" fill="#f6c979" opacity=".5" />
      </pattern>
      <pattern id="fm-slats" width="22" height="10" patternUnits="userSpaceOnUse">
        <rect width="1.5" height="10" fill="#000" opacity=".2" />
      </pattern>
      {(Object.keys(STATE_META) as AgentState["key"][]).map((k) => (
        <radialGradient key={k} id={`fm-state-${k}`}>
          <stop offset="0" stopColor={STATE_META[k].color} stopOpacity=".45" />
          <stop offset="1" stopColor={STATE_META[k].color} stopOpacity="0" />
        </radialGradient>
      ))}
    </defs>
  );
});

function Plant({ x, y, s = 1 }: { x: number; y: number; s?: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${s})`} pointerEvents="none">
      <g className="fs-sway" style={{ transformOrigin: "0px -14px" }}>
        {[[-11, -40, -28], [0, -50, 0], [11, -38, 26], [-6, -30, -48], [7, -28, 46], [-2, -58, 8]].map(([dx, dy, r], i) => (
          <ellipse key={i} cx={dx} cy={dy} rx={5.5} ry={15} fill={["#2f7d5b", "#3f9a6b", "#2a6e50"][i % 3]} transform={`rotate(${r} ${dx} ${dy})`} />
        ))}
      </g>
      <path d="M-10 -16 h20 l-3 16 h-14 Z" fill="#2a2522" />
      <ellipse cx={0} cy={-16} rx={10} ry={3} fill="#3a332e" />
    </g>
  );
}

function CityWindow({ x, y, w, h, frame = "#3a2e26" }: { x: number; y: number; w: number; h: number; frame?: string }) {
  const towers = Array.from({ length: Math.max(3, Math.round(w / 26)) }, (_, i) => {
    const bw = 14 + ((i * 7 + x) % 12);
    const top = y + h * (0.25 + (((i * 37 + x) % 40) / 100));
    return { x: x + 4 + i * ((w - 8) / Math.max(3, Math.round(w / 26))), w: bw, top };
  });
  return (
    <g pointerEvents="none">
      <rect x={x} y={y} width={w} height={h} fill="url(#fm-night)" />
      {towers.map((t, i) => (
        <rect key={i} x={t.x} y={t.top} width={Math.min(t.w, x + w - t.x)} height={y + h - t.top} fill={i % 2 ? "url(#fm-lit)" : "url(#fm-lit2)"} />
      ))}
      <path d={`M${x + w / 2} ${y} V${y + h} M${x} ${y + h / 2} H${x + w}`} stroke={frame} strokeWidth="3" />
      <rect x={x} y={y} width={w} height={h} fill="none" stroke={frame} strokeWidth="5" />
    </g>
  );
}

function Shelf({ x, y, w, h, colors }: { x: number; y: number; w: number; h: number; colors: string[] }) {
  const rows = Math.max(1, Math.floor(h / 34));
  const per = Math.floor((w - 8) / 9);
  return (
    <g pointerEvents="none">
      <rect x={x} y={y} width={w} height={h} fill="#2a1f19" stroke="#4a3a2e" strokeWidth="3" />
      {Array.from({ length: rows }, (_, r) => (
        <g key={r}>
          <rect x={x} y={y + (r + 1) * (h / rows) - 3} width={w} height={3} fill="#4a3a2e" />
          {Array.from({ length: per }, (_, i) => (
            <rect key={i} x={x + 5 + i * 9} y={y + r * (h / rows) + (i % 5 === 2 ? 10 : 5)} width={6.5} height={h / rows - (i % 5 === 2 ? 13 : 8)} fill={colors[(i + r) % colors.length]} />
          ))}
        </g>
      ))}
    </g>
  );
}

function Board({ x, y, w, h, children, light = false }: { x: number; y: number; w: number; h: number; children?: ReactNode; light?: boolean }) {
  return (
    <g fontFamily="Inter, sans-serif" pointerEvents="none">
      <rect x={x} y={y} width={w} height={h} rx={4} fill={light ? "#eef0ea" : "#07090d"} stroke={light ? "#c9bfa9" : "#2b3242"} strokeWidth="3" opacity={light ? 0.92 : 1} />
      {children}
    </g>
  );
}

/** A label on the wall for a room; full when near, title only when zoomed out. */
function Plate({ x, y, title, sub, muted = false }: { x: number; y: number; title: string; sub?: string; muted?: boolean }) {
  const t = title.toUpperCase();
  const wt = t.length * 7.3 + 18;
  const ws = sub ? sub.length * 5.4 + 18 : 0;
  const color = muted ? "#cbd5e1" : "#e4cf9f";
  const stroke = muted ? "#64748b" : "#cdb27a";
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none" fontFamily="Inter, sans-serif">
      <g className="fm-lbl">
        <g className="fm-near">
          <rect width={Math.max(wt, ws)} height={sub ? 34 : 21} rx={5} fill="#090d18" fillOpacity=".9" stroke={stroke} strokeOpacity=".55" />
          <text x={9} y={14.5} fontSize="10.5" fontWeight="700" letterSpacing="1.4" fill={color}>{t}</text>
          {sub && <text x={9} y={27.5} fontSize="9.5" fill="#94a3b8">{sub}</text>}
        </g>
        <g className="fm-compact">
          <rect width={wt} height={21} rx={5} fill="#090d18" fillOpacity=".9" stroke={stroke} strokeOpacity=".55" />
          <text x={9} y={14.5} fontSize="10.5" fontWeight="700" letterSpacing="1.4" fill={color}>{t}</text>
        </g>
      </g>
    </g>
  );
}

/** A status chip over a person's head, anchored at its bottom centre. Zoomed out, only `compact` chips stay (as a name). */
function Chip({ x, y, name, line, dot, pulse, alert, selected, compact = true }: { x: number; y: number; name: string; line: string; dot: string; pulse: boolean; alert: boolean; selected: boolean; compact?: boolean }) {
  const l = cut(line, 38);
  const w = Math.max(name.length * 7.4 + 34, l.length * 5.6 + 22);
  const wc = name.length * 7.4 + 30;
  const stroke = selected ? "#cdb27a" : alert ? "#f43f5e" : "#ffffff30";
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none" fontFamily="Inter, sans-serif">
      <g className="fm-lbl">
        <g className="fm-near">
          <rect x={-w / 2} y={-36} width={w} height={32} rx={7} fill="#090d18" fillOpacity=".92" stroke={stroke} />
          <path d="M-5 -4.5 L0 1 L5 -4.5 Z" fill="#090d18" />
          <circle cx={-w / 2 + 11} cy={-25} r={3.6} fill={dot} className={pulse ? "fs-pulse-dot" : undefined} />
          <text x={-w / 2 + 20} y={-21} fontSize="11.5" fontWeight="700" fill="#f8fafc">{name}</text>
          <text x={-w / 2 + 11} y={-9.5} fontSize="9.5" fill="#cbd5e1">{l}</text>
        </g>
        {compact && <g className="fm-compact">
          <rect x={-wc / 2} y={-24} width={wc} height={20} rx={7} fill="#090d18" fillOpacity=".92" stroke={stroke} />
          <path d="M-5 -4.5 L0 1 L5 -4.5 Z" fill="#090d18" />
          <circle cx={-wc / 2 + 11} cy={-14} r={3.6} fill={dot} className={pulse ? "fs-pulse-dot" : undefined} />
          <text x={-wc / 2 + 20} y={-10} fontSize="11.5" fontWeight="700" fill="#f8fafc">{name}</text>
        </g>}
      </g>
    </g>
  );
}

function Monitor({ x, y, color, chart, off, pulse }: { x: number; y: number; color: string; chart?: string; off: boolean; pulse: boolean }) {
  return (
    <g pointerEvents="none">
      <rect x={x - 3} y={y - 10} width={6} height={10} fill="#2b3242" />
      <rect x={x - 24} y={y - 44} width={48} height={34} rx={3} fill="#0d1017" stroke="#2b3242" strokeWidth="2.5" />
      <rect x={x - 20} y={y - 40} width={40} height={26} fill={off ? "#07090d" : color} opacity={off ? 1 : 0.8} className={pulse ? "fs-pulse" : undefined} />
      {chart && !off && <polyline points={`${x - 16},${y - 20} ${x - 8},${y - 28} ${x},${y - 24} ${x + 8},${y - 34} ${x + 16},${y - 30}`} fill="none" stroke={chart} strokeWidth="1.8" />}
    </g>
  );
}

function Mug({ x, y, steam }: { x: number; y: number; steam: boolean }) {
  return (
    <g transform={`translate(${x} ${y})`} pointerEvents="none">
      {steam && <path className="fs-steam" d="M0 -10 q-2 -3 0 -6 q2 -3 0 -6" stroke="#e2e8f0" strokeWidth="1" fill="none" opacity=".45" />}
      <rect x={-4} y={-9} width={8} height={9} rx={1.5} fill="#ece6da" />
    </g>
  );
}

/** A desk with nobody at it: chair tucked in, dark screens, a plate on the front. */
function EmptyDesk({ x, y, w, plate, screens = 1, tone = "#3d4452" }: { x: number; y: number; w: number; plate?: string; screens?: number; tone?: string }) {
  const top = y - 58;
  return (
    <g pointerEvents="none" className="fs-off-soft">
      <rect x={x - 26} y={top - 52} width={52} height={70} rx={10} fill="#232834" />
      {Array.from({ length: screens }, (_, i) => {
        const mx = x + (i - (screens - 1) / 2) * 52;
        return <Monitor key={i} x={mx} y={top} color="#0b0e14" off pulse={false} />;
      })}
      <rect x={x - w / 2} y={top} width={w} height={y - top} fill="#2a2f3a" />
      <rect x={x - w / 2 - 4} y={top - 6} width={w + 8} height={8} rx={2} fill={tone} />
      {plate && (
        <g fontFamily="Inter, sans-serif">
          <rect x={x - 38} y={top + 18} width={76} height={15} rx={3} fill="#14110f" stroke="#cdb27a" strokeOpacity=".5" />
          <text x={x} y={top + 29} textAnchor="middle" fontSize="8" fontWeight="700" letterSpacing="1.2" fill="#cdb27a">{plate}</text>
        </g>
      )}
    </g>
  );
}

// -- people -----------------------------------------------------------------------------------------

const WOOD: Record<AgentId, [string, string]> = { cio: ["#7a5038", "#4a2f22"], tim: ["#6a4e3a", "#3e2c22"], alex: ["#6a4e3a", "#3e2c22"], max: ["#4a5262", "#2a2f3a"] };

/** One of the HQ people at their desk. Clickable. */
function Seat({ id, x, floorY, c, phase, pending = 0 }: { id: AgentId; x: number; floorY: number; c: Ctx; phase?: number; pending?: number }) {
  const state = c.states[id];
  const mood = c.moods[id];
  const look: Look = LOOKS[id];
  const meta = STATE_META[state.key];
  const off = state.key === "inactive";
  const asleep = mood.kind === "asleep" && !off;
  const pose: Pose = asleep ? "asleep" : poseFor(state.key);
  const feeling = mood.kind === "stressed" ? "stressed" : mood.kind === "pleased" ? "pleased" : "calm";
  const gaze = meta.working ? "monitor" : mood.kind === "pleased" ? "viewer" : "wander";
  const s = 0.85;
  const top = floorY - 62;
  const selected = c.sel?.kind === "person" && c.sel.id === id;
  const [wood, woodDark] = WOOD[id];
  const select = () => c.onSelect({ kind: "person", id });
  const mode = c.space.agents.find((a) => a.id === id)?.mode;
  return (
    <g
      role="button"
      tabIndex={0}
      data-fm-hit
      aria-label={`${NAMES[id]}, ${AGENTS[id].title}: ${state.headline}. Open details.`}
      className="fm-person outline-none"
      onClick={(e) => {
        e.stopPropagation();
        select();
      }}
      onKeyDown={activate(select)}
    >
      <rect x={x - 95} y={floorY - 200} width={190} height={200} fill="transparent" />
      <ellipse cx={x} cy={floorY - 3} rx={92} ry={9} fill="none" stroke={selected ? meta.color : "transparent"} strokeWidth="2.5" strokeDasharray="6 5" className={selected ? "fs-flow" : "fm-ring"} />
      {meta.working && <ellipse cx={x} cy={top} rx={110} ry={70} fill={`url(#fm-state-${state.key})`} className="fs-pulse" pointerEvents="none" />}
      {mood.kind === "stressed" && <ellipse cx={x} cy={top} rx={130} ry={90} fill="url(#fm-state-error)" className="fs-stress" pointerEvents="none" />}
      <g className={off ? "fs-off" : undefined}>
        <g transform={`translate(${x} ${top}) scale(${s})`}>
          <Chair look={look} />
          <Body look={look} pose={pose} working={meta.working} feeling={feeling} gaze={gaze} chair={false} />
        </g>
        {id === "cio" &&
          Array.from({ length: Math.min(pending, 6) }, (_, i) => <rect key={i} x={x + 22} y={top - 3 - i * 2.5} width={26} height={3} fill="#f4f1e8" stroke="#cbbf9f" strokeWidth=".5" />)}
        <rect x={x - 80} y={top} width={160} height={floorY - top} fill={woodDark} />
        <rect x={x - 84} y={top - 6} width={168} height={9} rx={2} fill={wood} />
        <rect x={x - 80} y={top + 3} width={160} height={4} fill="#000" opacity=".25" />
        {id !== "max" && <Mug x={x - 58} y={top - 6} steam={!off} />}
        <Monitor x={x + 54} y={top - 6} color={state.key === "error" ? "#4a1020" : meta.working ? look.accent : "#1d3350"} chart={meta.working || state.key === "error" ? undefined : look.accent} off={off} pulse={meta.working} />
        {id === "max" && <Monitor x={x - 54} y={top - 6} color={state.key === "error" ? "#4a1020" : meta.working ? look.accent : "#1d3350"} chart={meta.working || state.key === "error" ? undefined : look.accent} off={off} pulse={meta.working} />}
        <g transform={`translate(${x} ${top}) scale(${s})`}>
          <Hands look={look} pose={pose} />
        </g>
        {asleep && <Zzz x={x + 18} y={top - 82} size={8} />}
        <g fontFamily="Fraunces, Georgia, serif">
          <rect x={x - 34} y={top + 18} width={68} height={16} rx={2} fill="#14110f" stroke="#cdb27a" strokeOpacity=".5" />
          <text x={x} y={top + 30} textAnchor="middle" fontSize="10" letterSpacing="2.5" fill="#e4cf9f">{NAMES[id].toUpperCase()}</text>
        </g>
      </g>
      {off && phase && (
        <g fontFamily="Inter, sans-serif">
          <rect x={x - 48} y={top + 40} width={96} height={14} rx={2} fill="#d4a017" />
          <text x={x} y={top + 50} textAnchor="middle" fontSize="7.5" fontWeight="700" fill="#141414" letterSpacing="1">OPENS IN PHASE {phase}</text>
        </g>
      )}
      <Chip
        x={x}
        y={floorY - 168}
        name={`${NAMES[id]}${mode && mode !== "LIVE" && id !== "cio" ? ` · ${mode}` : ""}`}
        line={state.headline}
        dot={meta.working || state.key === "error" ? meta.color : AGENTS[id].color}
        pulse={meta.working}
        alert={mood.kind === "stressed"}
        selected={selected}
        compact={false}
      />
    </g>
  );
}

const TRAINEE_POSE: Record<string, Pose> = { trading: "typing", analyzing: "typing", watching: "reading", error: "alert" };

export function traineeDoing(a: AcademySpaceAgent, dataReady: boolean): string {
  if (a.stage === "RETIRED") return "retired";
  if (a.stage === "RESEARCH") {
    if (a.sprinting) return `sprinting: ${a.sprinting}`;
    return a.last_sprint ? `in research · last sprint ${(a.last_sprint.outcome || "").replace(/_/g, " ").toLowerCase()}` : "in research · first sprint at 1:00 AM";
  }
  if (a.paused || a.status === "PAUSED") return "paused";
  if (a.positions) return `${a.positions} open position${a.positions > 1 ? "s" : ""}`;
  if (a.activity === "watching") return "watching the market";
  return dataReady ? "waiting for a setup" : "waiting for market history";
}
export const signedPctOf = (x: number) => `${x >= 0 ? "+" : ""}${(x * 100).toFixed(2)}%`;
/** The one-line status under an Academy agent's name: research agents have no returns to show. */
export const traineeLine = (a: AcademySpaceAgent, dataReady: boolean) => (a.stage === "RESEARCH" ? `v${a.version} · ${traineeDoing(a, dataReady)}` : `${signedPctOf(a.return_pct)} · ${traineeDoing(a, dataReady)}`);

/** An Academy agent at its desk in the Trading Hall. */
function Trainee({ a, x, floorY, c }: { a: AcademySpaceAgent; x: number; floorY: number; c: Ctx }) {
  const look = { chair: "#1f2633", skin: "#d9a67c", hair: "#2b2523", hairStyle: "neat", top: "#334155", inner: "#e2e8f0", accent: a.persona.color, ...(a.persona.look ?? {}) } as Look;
  const paused = !!a.paused || a.status === "PAUSED";
  const pose: Pose = paused ? "idle" : TRAINEE_POSE[a.activity ?? ""] ?? "idle";
  const feeling = a.return_pct <= -0.05 ? "stressed" : a.return_pct >= 0.02 ? "pleased" : "calm";
  const busy = pose === "typing" || (a.stage === "RESEARCH" && !!a.sprinting);
  const s = 0.72;
  const top = floorY - 54;
  const selected = c.sel?.kind === "trainee" && c.sel.id === a.id;
  const select = () => c.onSelect({ kind: "trainee", id: a.id });
  return (
    <g role="button" tabIndex={0} data-fm-hit aria-label={`${a.name}, Academy ${a.style} trader: ${traineeDoing(a, !!c.space.academy?.engine?.data_ready)}. Open details.`} className="fm-person outline-none" onClick={(e) => (e.stopPropagation(), select())} onKeyDown={activate(select)}>
      <rect x={x - 62} y={floorY - 170} width={124} height={170} fill="transparent" />
      <ellipse cx={x} cy={floorY - 3} rx={62} ry={7} fill="none" stroke={selected ? a.persona.color : "transparent"} strokeWidth="2.5" strokeDasharray="6 5" className={selected ? "fs-flow" : "fm-ring"} />
      {busy && <ellipse cx={x} cy={top} rx={80} ry={50} fill="url(#fm-warm)" className="fs-pulse" pointerEvents="none" />}
      <g className={paused ? "fs-off" : undefined}>
        <g transform={`translate(${x} ${top}) scale(${s})`}>
          <Chair look={look} />
          <Body look={look} pose={pose} working={busy} feeling={feeling} gaze={busy ? "monitor" : "wander"} chair={false} />
        </g>
        <rect x={x - 56} y={top} width={112} height={floorY - top} fill="#33281f" />
        <rect x={x - 59} y={top - 5} width={118} height={8} rx={2} fill="#4a3b30" />
        <Monitor x={x + 38} y={top - 5} color={busy ? a.persona.color : "#13243c"} off={false} pulse={busy} chart={busy ? undefined : a.persona.color} />
        <g transform={`translate(${x} ${top}) scale(${s})`}>
          <Hands look={look} pose={pose} />
        </g>
        <g fontFamily="Inter, sans-serif">
          <rect x={x - 32} y={top + 16} width={64} height={14} rx={2} fill="#14110f" stroke={a.persona.color} strokeOpacity=".7" />
          <text x={x} y={top + 26} textAnchor="middle" fontSize="8" fontWeight="700" letterSpacing="1.2" fill={a.persona.color}>{a.name.toUpperCase()}</text>
        </g>
      </g>
      <Chip x={x} y={floorY - 140} name={a.name} line={traineeLine(a, !!c.space.academy?.engine?.data_ready)} dot={a.persona.color} pulse={busy} alert={a.stage !== "RESEARCH" && feeling === "stressed"} selected={selected} />
    </g>
  );
}

// -- rooms ------------------------------------------------------------------------------------------

const WALLS: Partial<Record<DeptId, string>> = {
  cio: "#2b2119", treasury: "#232620",
  tim: "#1a2740", alex: "#241e38", max: "#1e2129",
  "market-room": "#18202e", "day-1": "#1b2230", "day-2": "#1b2230", "swing-1": "#231d30", "swing-2": "#231d30",
  lobby: "#2a231d", records: "#252019", operations: "#151b24",
  lab: "#1a2230", research: "#2a2119", alumni: "#251f1a", hall: "#211c19",
};

function Room({ d, c, sub, muted, open, children }: { d: Dept; c: Ctx; sub?: string; muted?: boolean; open?: string; children?: ReactNode }) {
  const [x, y, w, h] = d.rect;
  const floorY = fy(d.rect);
  const on = c.sel?.kind === "dept" && c.sel.id === d.id;
  const select = () => {
    c.onSelect({ kind: "dept", id: d.id });
    if (open) c.onOpen?.(open); // rooms whose click opens a page over the map
  };
  return (
    <g
      role="button"
      tabIndex={0}
      data-fm-hit
      aria-label={`${d.name}. Open details.`}
      className={`fm-room outline-none ${on ? "fm-room-on" : ""}`}
      onClick={(e) => {
        e.stopPropagation();
        select();
      }}
      onDoubleClick={(e) => {
        e.stopPropagation();
        c.onFocus(d.rect);
      }}
      onKeyDown={activate(select)}
    >
      <rect x={x} y={y} width={w} height={h - SLAB} fill={WALLS[d.id] ?? "#221d1a"} />
      <rect x={x} y={y} width={w} height={h - SLAB} fill="url(#fm-slats)" pointerEvents="none" />
      <rect x={x} y={floorY - 46} width={w} height={46} fill="#000" opacity=".16" pointerEvents="none" />
      <rect x={x} y={floorY - 48} width={w} height={2} fill="#fff" opacity=".05" pointerEvents="none" />
      <g pointerEvents="none">
        <rect x={x + w / 2 - 16} y={y} width={32} height={5} fill="#3a4560" />
        <path d={`M${x + w / 2 - 16} ${y + 5} L${x + w / 2 - 90} ${floorY} H${x + w / 2 + 90} L${x + w / 2 + 16} ${y + 5} Z`} fill="#ffe9c0" opacity=".035" />
        <ellipse cx={x + w / 2} cy={floorY} rx={Math.min(w / 2, 160)} ry={14} fill="url(#fm-warm)" opacity=".7" />
      </g>
      {children}
      <rect className="fm-hl" x={x + 3} y={y + 3} width={w - 6} height={h - SLAB - 6} rx={4} pointerEvents="none" />
      <Plate x={x + 9} y={y + 9} title={d.name} sub={sub} muted={muted} />
    </g>
  );
}

function CioRoom({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y] = d.rect;
  const f = fy(d.rect);
  return (
    <Room d={d} c={c} sub="Graham · Chief Investment Officer">
      <CityWindow x={x + 30} y={y + 58} w={170} h={110} />
      <Shelf x={x + 290} y={y + 56} w={96} h={140} colors={["#6b2d2d", "#2d4a6b", "#8a7448", "#33553f", "#5a3a6b"]} />
      <g transform={`translate(${x + 340} ${f})`} pointerEvents="none">
        <path d="M-9 0 h18 l-3 -7 h-12 Z" fill="#6b5a3a" />
        <rect x={-1.5} y={-26} width={3} height={19} fill="#8a7448" />
        <circle cy={-38} r={13} fill="#2d5a7b" />
        <circle cy={-38} r={14.5} fill="none" stroke="#b8975a" strokeWidth="1.2" />
      </g>
      <Plant x={x + 22} y={f} s={1.1} />
      <Seat id="cio" x={x + 170} floorY={f} c={c} pending={c.space.cio.pending_proposals.length} />
    </Room>
  );
}

const pctTxt = (x: number | null | undefined, d = 2) => (x === null || x === undefined ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(d)}%`);
/** Today's return: live value against the last close, or once today's close is marked, that day's return. */
export function todayReturn(nav: number | null, lastMark: { date: string; nav: number } | null, flowsSince: number, lastDay: { date: string; return: number } | null): number | null {
  const etToday = new Date().toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  if (lastDay && lastDay.date === etToday) return lastDay.return;
  return nav !== null && lastMark && lastMark.nav > 0 ? (nav - flowsSince) / lastMark.nav - 1 : null;
}
const toneOf = (x: number | null | undefined) => (x === null || x === undefined ? "#94a3b8" : x >= 0 ? "#34d399" : "#fb7185");

/** The firm's numbers in one room. Clicking it opens the full analytics over the map. */
function Treasury({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const o = c.overview;
  const t = c.space.treasury;
  const pending = c.space.cio.pending_proposals;
  const ac = c.space.academy;
  const nav = o?.firm.nav ?? null;
  const contributed = t?.contributed ?? null;
  const gain = nav !== null && contributed !== null ? nav - contributed : null;
  const gainPct = gain !== null && contributed ? gain / contributed : null;
  const today = todayReturn(nav, t?.last_mark ?? null, t?.flows_since_last_mark ?? 0, t?.last_day ?? null);
  const perf = o?.firm.performance;
  const total = o ? o.allocation.reduce((sum, a) => sum + Math.max(0, a.nav), 0) : 0;
  const spark = t?.spark ?? [];
  const floors = { RESEARCH: 0, PAPER: 0, DEMO: 0, RETIRED: 0 } as Record<string, number>;
  for (const a of ac?.agents ?? []) floors[a.stage] = (floors[a.stage] ?? 0) + 1;
  const paper = (ac?.agents ?? []).filter((a) => a.stage === "PAPER" || a.stage === "DEMO");
  let acc = 0;
  const A = x + 18, B = x + 286, C = x + 486; // three boards
  return (
    <g>
      <Room d={d} c={c} open="/analytics" sub={pending.length ? `${pending.length} awaiting you · click for analytics` : "Click for full analytics"}>
        {/* capital and returns */}
        <Board x={A} y={y + 48} w={256} h={206}>
          <text x={A + 12} y={y + 66} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">FIRM CAPITAL</text>
          <text x={A + 12} y={y + 94} fontSize="24" fontWeight="700" fill="#f8fafc">{nav !== null ? money(nav) : "…"}</text>
          <text x={A + 244} y={y + 94} textAnchor="end" fontSize="11" fontWeight="700" fill={toneOf(today)}>{today !== null ? `${pctTxt(today)} today` : ""}</text>
          {[
            ["Overall return", perf ? pctTxt(perf.twr) : "—", toneOf(perf?.twr)],
            ["S&P 500, same days", perf ? pctTxt(perf.benchmark) : "—", toneOf(perf?.benchmark)],
            ["Contributed", contributed !== null ? money(contributed) : "—", "#e2e8f0"],
            ["Gain on contributions", gain !== null ? `${gain >= 0 ? "+" : "-"}${money(Math.abs(gain))} (${pctTxt(gainPct)})` : "—", toneOf(gain)],
            ["Max drawdown", t ? `${(t.max_drawdown * 100).toFixed(2)}%` : "—", "#e2e8f0"],
          ].map(([k, v, col], i) => (
            <g key={k}>
              <text x={A + 12} y={y + 118 + i * 17} fontSize="9" fill="#94a3b8">{k}</text>
              <text x={A + 244} y={y + 118 + i * 17} textAnchor="end" fontSize="9.5" fontWeight="600" fill={col}>{v}</text>
            </g>
          ))}
          {o && total > 0 && o.allocation.map((a) => {
            const wPx = (Math.max(0, a.nav) / total) * 232;
            const el = <rect key={a.id} x={A + 12 + acc} y={y + 210} width={Math.max(0, wPx - 1)} height={8} fill={ALLOC_COLORS[a.id] ?? "#64748b"} />;
            acc += wPx;
            return el;
          })}
          <text x={A + 12} y={y + 234} fontSize="8" fill="#64748b">{o ? o.allocation.map((a) => `${a.name} ${(a.pct * 100).toFixed(0)}%`).join(" · ") : ""}</text>
        </Board>
        {/* growth and the boardroom */}
        <Board x={B} y={y + 48} w={190} h={206}>
          <text x={B + 12} y={y + 66} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">VALUE, LAST {Math.max(spark.length, 0)} CLOSES</text>
          {spark.length > 1 ? (() => {
            const lo = Math.min(...spark), hi = Math.max(...spark);
            const pts = spark.map((v, i) => `${B + 12 + (i / (spark.length - 1)) * 166},${y + 140 - ((v - lo) / (hi - lo || 1)) * 56}`).join(" ");
            const up = spark[spark.length - 1] >= spark[0];
            return <polyline points={pts} fill="none" stroke={up ? "#34d399" : "#fb7185"} strokeWidth="2" />;
          })() : <text x={B + 12} y={y + 112} fontSize="9" fill="#64748b">Builds up after a few daily closes</text>}
          <text x={B + 12} y={y + 162} fontSize="9" fill="#94a3b8">{t ? `${t.days_tracked} daily closes tracked` : ""}</text>
          <line x1={B + 12} x2={B + 178} y1={y + 174} y2={y + 174} stroke="#283044" />
          <text x={B + 12} y={y + 192} fontSize="8.5" fontWeight="700" letterSpacing="1.4" fill="#cdb27a">AWAITING YOUR DECISION</text>
          <text x={B + 12} y={y + 220} fontSize="20" fontWeight="700" fill={pending.length ? "#fbbf24" : "#64748b"}>{pending.length}</text>
          <text x={B + 38} y={y + 216} fontSize="8.5" fill="#cbd5e1">{pending.length ? cut(`#${pending[0].id} ${pending[0].title}`, 26) : "No open proposals"}</text>
        </Board>
        {/* the Academy */}
        <Board x={C} y={y + 48} w={w - (C - x) - 18} h={206}>
          <text x={C + 12} y={y + 66} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">THE 537 ACADEMY</text>
          {ac?.available ? (
            <g>
              {[["Floor 3 · Demo", floors.DEMO], ["Floor 2 · Paper", floors.PAPER], ["Floor 1 · Research", floors.RESEARCH]].map(([k, n], i) => (
                <g key={k as string}>
                  <text x={C + 12} y={y + 86 + i * 15} fontSize="9" fill="#94a3b8">{k}</text>
                  <text x={C + 164} y={y + 86 + i * 15} textAnchor="end" fontSize="9.5" fontWeight="600" fill="#e2e8f0">{n}</text>
                </g>
              ))}
              {paper.slice(0, 3).map((a, i) => (
                <g key={a.id}>
                  <circle cx={C + 15} cy={y + 133 + i * 14} r={3} fill={a.persona.color} />
                  <text x={C + 23} y={y + 136 + i * 14} fontSize="9" fill="#cbd5e1">{a.name}</text>
                  <text x={C + 164} y={y + 136 + i * 14} textAnchor="end" fontSize="9" fontWeight="600" fill={toneOf(a.return_pct)}>{pctTxt(a.return_pct)}</text>
                </g>
              ))}
              <text x={C + 12} y={y + 190} fontSize="8.5" fontWeight="700" letterSpacing="1.2" fill="#cdb27a">{ac.latest_report ? `SPRINT ${ac.latest_report.day.slice(5)}` : "NO SPRINT YET"}</text>
              {ac.latest_report && (
                <>
                  <text x={C + 12} y={y + 206} fontSize="8.5" fill="#cbd5e1">{cut(`Passed: ${ac.latest_report.passed.join(", ") || "none"}`, 30)}</text>
                  <text x={C + 12} y={y + 220} fontSize="8.5" fill="#94a3b8">{cut(`Improved: ${ac.latest_report.improved.join(", ") || "none"}`, 30)}</text>
                </>
              )}
            </g>
          ) : <text x={C + 12} y={y + 90} fontSize="9" fill="#64748b">Forex engine not running</text>}
        </Board>
        <Plant x={x + w - 14} y={f} s={0.8} />
      </Room>
    </g>
  );
}

function ManagerRoom({ d, c, id }: { d: Dept; c: Ctx; id: "tim" | "alex" | "max" }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const unbuilt = c.space.agents.find((a) => a.id === id)?.implemented ? undefined : PHASE[id];
  const sub = { tim: "Core ETF portfolio", alex: "Strategic growth stocks", max: "Short-term swing trades" }[id];
  return (
    <Room d={d} c={c} sub={sub}>
      {id === "tim" && (
        <>
          <Shelf x={x + 20} y={y + 58} w={92} h={130} colors={["#2f6bd0", "#1f4f9e", "#8aa9d6", "#3b4a63"]} />
          <CityWindow x={x + w - 110} y={y + 58} w={90} h={90} />
          <Plant x={x + w - 22} y={f} s={1.1} />
        </>
      )}
      {id === "alex" && (
        <>
          <g pointerEvents="none">
            <rect x={x + 20} y={y + 58} width={120} height={70} fill="#3a3046" stroke="#4a3b30" strokeWidth="3" />
            {[[8, 8, "#a78bfa"], [36, 12, "#fde68a"], [66, 6, "#93c5fd"], [92, 14, "#fca5a5"], [14, 38, "#a7f3d0"], [48, 40, "#fde68a"], [82, 40, "#c4b5fd"]].map(([dx, dy, col], i) => (
              <rect key={i} x={x + 20 + (dx as number)} y={y + 58 + (dy as number)} width={20} height={16} fill={col as string} opacity=".85" />
            ))}
          </g>
          <CityWindow x={x + w - 110} y={y + 58} w={90} h={90} />
          <Plant x={x + 24} y={f} s={1} />
        </>
      )}
      {id === "max" && (
        <>
          <Board x={x + 150} y={y + 14} w={w - 170} h={18}>
            <text x={x + 150 + (w - 170) / 2} y={y + 27} textAnchor="middle" fontSize="8.5" fill="#f59e0b" letterSpacing="2.2" opacity={c.states.max.key === "inactive" ? 0.35 : 0.85}>
              {c.states.max.key === "inactive" ? "MARKET TAPE OFFLINE" : c.states.max.symbols.length ? c.states.max.symbols.map((s) => `${s.side ? `${s.side.toUpperCase()} ` : ""}${s.symbol}`).join(" · ") : "SWING DESK"}
            </text>
          </Board>
          <CityWindow x={x + 20} y={y + 90} w={80} h={80} />
          <Plant x={x + w - 22} y={f} s={1.1} />
        </>
      )}
      <Seat id={id} x={x + w / 2 + 10} floorY={f} c={c} phase={unbuilt} />
    </Room>
  );
}

const RATES = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "EUR_GBP"];

function MarketRoom({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y] = d.rect;
  const f = fy(d.rect);
  const fxOpen = fxMarketOpen(c.now);
  const prices = (c.space.academy?.prices ?? []).filter((p) => RATES.includes(p.instrument));
  const fresh = prices.length > 0 && c.now / 1000 - Math.max(...prices.map((p) => p.time)) < 900;
  return (
    <Room d={d} c={c} sub={fxOpen ? "FX market open" : "FX market closed"}>
      <Board x={x + 20} y={y + 58} w={196} h={104}>
        <text x={x + 30} y={y + 74} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#f59e0b">LIVE RATES · OANDA</text>
        {prices.length ? (
          prices.map((p, i) => (
            <g key={p.instrument}>
              <text x={x + 30 + (i % 2) * 94} y={y + 94 + Math.floor(i / 2) * 16} fontSize="8.5" fill={fresh ? "#cbd5e1" : "#64748b"}>{p.instrument.replace("_", "/")}</text>
              <text x={x + 76 + (i % 2) * 94} y={y + 94 + Math.floor(i / 2) * 16} fontSize="8.5" fill={fresh ? "#34d399" : "#64748b"}>{((p.bid + p.ask) / 2).toFixed(p.instrument.endsWith("JPY") ? 3 : 5)}</text>
            </g>
          ))
        ) : (
          <text x={x + 30} y={y + 96} fontSize="9" fill="#94a3b8">{c.space.academy?.available ? "Waiting for prices" : "Forex engine not running"}</text>
        )}
        {prices.length > 0 && !fresh && <text x={x + 30} y={y + 154} fontSize="7.5" fill="#94a3b8">last prices before the market closed</text>}
      </Board>
      {SESSIONS.map((s, i) => {
        const open = sessionOpen(c.now, s);
        const { hour, minute } = localTime(c.now, s.tz);
        const cx = x + 252 + i * 40, cy = y + 82;
        return (
          <g key={s.city} pointerEvents="none" fontFamily="Inter, sans-serif">
            <circle cx={cx} cy={cy} r={15} fill="#0f1218" stroke={open ? "#34d399" : "#4a3b30"} strokeWidth="2.5" />
            <line x1={cx} y1={cy} x2={cx} y2={cy - 8} stroke="#e5e7eb" strokeWidth="2" strokeLinecap="round" transform={`rotate(${(hour % 12) * 30 + minute / 2} ${cx} ${cy})`} />
            <line x1={cx} y1={cy} x2={cx} y2={cy - 12} stroke="#e5e7eb" strokeWidth="1.2" strokeLinecap="round" transform={`rotate(${minute * 6} ${cx} ${cy})`} />
            <text x={cx} y={cy + 27} textAnchor="middle" fontSize="7" fontWeight="700" letterSpacing=".6" fill={open ? "#34d399" : "#94a3b8"}>{s.city.toUpperCase()}</text>
          </g>
        );
      })}
      <EmptyDesk x={x + 130} y={f} w={140} screens={2} />
      <EmptyDesk x={x + 290} y={f} w={110} screens={1} tone="#4a3b5e" />
    </Room>
  );
}

function ChairRoom({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const day = d.id.startsWith("day");
  return (
    <Room d={d} c={c} sub="Open chair" muted>
      <Board x={x + 14} y={y + 66} w={w - 28} h={18}>
        <text x={x + w / 2} y={y + 78.5} textAnchor="middle" fontSize="7.5" letterSpacing="1.6" fill={day ? "#f59e0b" : "#c4b5fd"} opacity=".6">{day ? "INTRADAY · FLAT BY FRIDAY" : "MULTI-DAY · WEEKEND RULES"}</text>
      </Board>
      <EmptyDesk x={x + w / 2} y={f} w={120} screens={day ? 2 : 1} tone={day ? "#3d4452" : "#4a3b5e"} plate="OPEN CHAIR" />
    </Room>
  );
}

function Lobby({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const o = c.overview;
  const modes = (o?.managers ?? []).reduce<Record<string, number>>((m, a) => ((m[a.mode] = (m[a.mode] ?? 0) + 1), m), {});
  return (
    <Room d={d} c={c} sub="Headquarters · the firm at a glance">
      <rect x={x + 6} y={y + 60} width={56} height={f - y - 60} fill="url(#fm-glass)" stroke="#ffffff30" pointerEvents="none" />
      <path d={`M${x + 34} ${y + 60} V${f}`} stroke="#ffffff30" pointerEvents="none" />
      <g pointerEvents="none" fontFamily="Fraunces, Georgia, serif">
        <rect x={x + 96} y={y + 66} width={30} height={30} fill="none" stroke="#cdb27a" strokeWidth="2" />
        <text x={x + 111} y={y + 85} textAnchor="middle" fontSize="11" fill="#e4cf9f">537</text>
        <text x={x + 134} y={y + 86} fontSize="13" letterSpacing="2" fill="#e4cf9f">THE 537 FIRM</text>
      </g>
      <Board x={x + w - 172} y={y + 58} w={156} h={92}>
        <text x={x + w - 160} y={y + 76} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">HEADQUARTERS</text>
        <text x={x + w - 160} y={y + 100} fontSize="18" fontWeight="700" fill="#f8fafc">{o ? money(o.firm.nav) : "…"}</text>
        <text x={x + w - 160} y={y + 118} fontSize="8.5" fill="#94a3b8">{Object.entries(modes).map(([m, n]) => `${n} ${m.toLowerCase()}`).join(" · ") || "Loading managers"}</text>
        <text x={x + w - 160} y={y + 136} fontSize="8.5" fill={o?.broker.connected ? "#34d399" : "#fb7185"}>{o ? (o.broker.connected ? "Broker connected" : "Broker offline") : ""}</text>
      </Board>
      <g pointerEvents="none">
        <rect x={x + 120} y={f - 60} width={150} height={60} rx={6} fill="#3a2a22" />
        <rect x={x + 114} y={f - 66} width={162} height={10} rx={3} fill="#cdb27a" opacity=".7" />
        <rect x={x + 300} y={f - 40} width={90} height={26} rx={8} fill="#2d3a4f" />
        <rect x={x + 300} y={f - 62} width={90} height={26} rx={8} fill="#34445c" />
      </g>
      <Plant x={x + 86} y={f} s={1.1} />
      <Plant x={x + w - 22} y={f} s={1.15} />
    </Room>
  );
}

function Records({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const latest = c.events[0];
  return (
    <Room d={d} c={c} sub="The event log">
      <g pointerEvents="none">
        {[0, 1, 2].map((i) => (
          <g key={i}>
            <rect x={x + 18 + i * 42} y={f - 138} width={38} height={138} fill="#3d4452" stroke="#2a2f3a" strokeWidth="2" />
            {[0, 1, 2, 3].map((j) => (
              <g key={j}>
                <rect x={x + 22 + i * 42} y={f - 132 + j * 33} width={30} height={28} fill="#4a5262" />
                <rect x={x + 32 + i * 42} y={f - 121 + j * 33} width={10} height={3} fill="#cbd5e1" />
              </g>
            ))}
          </g>
        ))}
      </g>
      <Board x={x + 148} y={y + 58} w={w - 162} h={104}>
        <text x={x + 158} y={y + 75} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">LATEST EVENT</text>
        {latest ? (
          <>
            {wrap(latest.summary, 26, 2).map((line, i) => <text key={i} x={x + 158} y={y + 93 + i * 13} fontSize="8.5" fill="#e2e8f0">{line}</text>)}
            <text x={x + 158} y={y + 126} fontSize="8" fill="#94a3b8">#{latest.id} · {latest.type.toLowerCase().replace(/_/g, " ")}</text>
          </>
        ) : (
          <text x={x + 158} y={y + 95} fontSize="8.5" fill="#94a3b8">Nothing recorded yet</text>
        )}
      </Board>
    </Room>
  );
}

function Operations({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  const k = c.space.controls;
  const up = !!c.space.broker_connected;
  const flags = [k.kill_switch && "kill switch", k.reconcile_hold && "buys paused", k.drawdown_brake && "drawdown brake"].filter(Boolean) as string[];
  const lines: [string, string, string][] = [
    ["Broker", up ? "connected" : "offline", up ? "#34d399" : "#fb7185"],
    ["Live orders", k.live_writes_enabled ? "on" : "off", k.live_writes_enabled ? "#34d399" : "#94a3b8"],
    ["Safeguards", flags.length ? flags.join(", ") : "all clear", flags.length ? "#fbbf24" : "#34d399"],
    ["US market", c.space.market.regular_hours_open ? "open" : "closed", c.space.market.regular_hours_open ? "#34d399" : "#94a3b8"],
  ];
  return (
    <Room d={d} c={c} sub="Health, jobs, safeguards">
      <g pointerEvents="none">
        {[0, 1, 2].map((i) => (
          <g key={i}>
            <rect x={x + 18 + i * 42} y={f - 168} width={38} height={168} fill="#1f2430" stroke="#2b3242" strokeWidth="2" />
            {Array.from({ length: 10 }, (_, j) => (
              <g key={j}>
                <rect x={x + 22 + i * 42} y={f - 162 + j * 16} width={30} height={10} rx={1} fill="#121722" />
                <circle cx={x + 27 + i * 42} cy={f - 157 + j * 16} r={1.8} fill={up ? (j % 3 ? "#34d399" : "#38bdf8") : "#475569"} className={up ? "fs-pulse" : undefined} style={up ? { animationDelay: `${(i * 10 + j) * 0.13}s` } : undefined} />
              </g>
            ))}
          </g>
        ))}
      </g>
      <Board x={x + 148} y={y + 58} w={w - 162} h={92}>
        <text x={x + 158} y={y + 75} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#cdb27a">STATUS</text>
        {lines.map(([label, value, color], i) => (
          <g key={label}>
            <circle cx={x + 162} cy={y + 89 + i * 15} r={3} fill={color} />
            <text x={x + 170} y={y + 92 + i * 15} fontSize="8.5" fill="#cbd5e1">{label}:</text>
            <text x={x + 226} y={y + 92 + i * 15} fontSize="8.5" fill={color}>{cut(value, 20)}</text>
          </g>
        ))}
      </Board>
    </Room>
  );
}

function Lab({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y] = d.rect;
  const f = fy(d.rect);
  const ac = c.space.academy;
  const agents = ac?.agents ?? [];
  const running = ac?.validations_running ?? [];
  const job = ac?.engine?.job?.name ?? null;
  const busy = running.length > 0 || !!job?.startsWith("sprint") || !!job?.startsWith("validate");
  const lines = [
    ...running.map((r) => `${agents.find((a) => a.id === r.agent_id)?.name ?? r.agent_id}: ${r.status.replace("RUNNING: ", "")}`),
    ...(job === "backfill" ? ["Loading OANDA history"] : []),
  ];
  return (
    <Room d={d} c={c} sub={busy ? "Research sprint running" : "Idle until 1:00 AM"}>
      <g pointerEvents="none">
        {[0, 1].map((i) => (
          <g key={i}>
            <rect x={x + 14 + i * 40} y={f - 160} width={36} height={160} fill="#1f2430" stroke="#2b3242" strokeWidth="2" />
            {Array.from({ length: 9 }, (_, j) => (
              <g key={j}>
                <rect x={x + 18 + i * 40} y={f - 154 + j * 17} width={28} height={10} rx={1} fill="#121722" />
                <circle cx={x + 23 + i * 40} cy={f - 149 + j * 17} r={1.8} fill={busy ? (j % 3 ? "#34d399" : "#38bdf8") : "#334155"} className={busy ? "fs-pulse" : undefined} style={busy ? { animationDelay: `${(i * 9 + j) * 0.17}s` } : undefined} />
              </g>
            ))}
          </g>
        ))}
      </g>
      <Board x={x + 102} y={y + 58} w={90} h={84} light>
        <text x={x + 108} y={y + 72} fontSize="7" fontWeight="700" letterSpacing="1" fill="#334155">SPRINTS</text>
        {(lines.length ? lines : ["Queue empty"]).slice(0, 4).map((l, i) => (
          <text key={i} x={x + 108} y={y + 88 + i * 12} fontSize="7" fill={lines.length ? "#0f766e" : "#64748b"}>{cut(l, 18)}</text>
        ))}
      </Board>
    </Room>
  );
}

function Research({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y, w] = d.rect;
  const f = fy(d.rect);
  return (
    <Room d={d} c={c} sub="Unoccupied" muted>
      <Shelf x={x + 14} y={y + 56} w={64} h={100} colors={["#6b2d2d", "#2d4a6b", "#8a7448", "#33553f"]} />
      <EmptyDesk x={x + w / 2 + 14} y={f} w={96} plate="UNOCCUPIED" tone="#6b4430" />
    </Room>
  );
}

function Alumni({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y] = d.rect;
  const f = fy(d.rect);
  const retired = (c.space.academy?.agents ?? []).filter((a) => a.stage === "RETIRED");
  return (
    <Room d={d} c={c} sub={retired.length ? `${retired.length} retired` : "None yet"} muted={!retired.length}>
      <g pointerEvents="none">
        {Array.from({ length: 8 }, (_, i) => {
          const a = retired[i];
          return (
            <g key={i} transform={`translate(${x + 18 + (i % 4) * 44} ${y + 60 + Math.floor(i / 4) * 56})`}>
              <rect width={34} height={44} fill="#1a1512" stroke="#b8975a" strokeWidth="2.5" />
              <rect x={5} y={5} width={24} height={34} fill={a ? a.persona.color : "#0f0d0c"} opacity={a ? 0.55 : 1} />
              {a && <text x={17} y={28} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="14" fill="#14110f">{a.name[0]}</text>}
            </g>
          );
        })}
        <rect x={x + 30} y={f - 28} width={140} height={28} fill="#4a2f22" />
        <rect x={x + 26} y={f - 32} width={148} height={7} rx={2} fill="#6b4430" />
      </g>
    </Room>
  );
}

/** A floor of Academy desks for the agents at one stage (open seats fill the rest). */
function Desks({ d, c, stage, seats, sub, plate }: { d: Dept; c: Ctx; stage: AcademySpaceAgent["stage"]; seats: number; sub: string; plate: string }) {
  const [x, , w] = d.rect;
  const f = fy(d.rect);
  const agents = (c.space.academy?.agents ?? []).filter((a) => a.stage === stage);
  const n = Math.max(agents.length, seats);
  const step = (w - 20) / n;
  return (
    <Room d={d} c={c} sub={c.space.academy?.available ? sub.replace("{n}", String(agents.length)) : "Forex engine not running"}>
      {agents.map((a, i) => <Trainee key={a.id} a={a} x={x + 10 + step * (i + 0.5)} floorY={f} c={c} />)}
      {Array.from({ length: n - agents.length }, (_, i) => <EmptyDesk key={i} x={x + 10 + step * (agents.length + i + 0.5)} y={f} w={100} plate={plate} tone="#4a3b30" />)}
    </Room>
  );
}

function Hall({ d, c }: { d: Dept; c: Ctx }) {
  return <Desks d={d} c={c} stage="PAPER" seats={6} sub="{n} paper trading on live prices" plate="OPEN SEAT" />;
}

function Studio({ d, c }: { d: Dept; c: Ctx }) {
  return <Desks d={d} c={c} stage="RESEARCH" seats={7} sub="{n} agents in research" plate="OPEN DESK" />;
}

function DemoDesk({ d, c }: { d: Dept; c: Ctx }) {
  const [x, y] = d.rect;
  const demo = c.space.academy?.engine?.demo;
  return (
    <g>
      <Desks d={d} c={c} stage="DEMO" seats={2} sub="{n} of 2 seats taken" plate="DEMO SEAT" />
      <Board x={x + 150} y={y + 40} w={200} h={46}>
        <text x={x + 160} y={y + 58} fontSize="8.5" fontWeight="700" letterSpacing="1.6" fill="#f59e0b" opacity=".85">OANDA PRACTICE</text>
        <text x={x + 160} y={y + 74} fontSize="8" fill={demo?.configured ? "#34d399" : "#94a3b8"}>{demo?.configured ? `${demo.seats} seat account${demo.seats === 1 ? "" : "s"} connected` : "Practice account not set up yet"}</text>
      </Board>
    </g>
  );
}

function DeptRoom({ d, c }: { d: Dept; c: Ctx }) {
  switch (d.id) {
    case "cio": return <CioRoom d={d} c={c} />;
    case "treasury": return <Treasury d={d} c={c} />;
    case "tim": case "alex": case "max": return <ManagerRoom d={d} c={c} id={d.id} />;
    case "market-room": return <MarketRoom d={d} c={c} />;
    case "day-1": case "day-2": case "swing-1": case "swing-2": return <ChairRoom d={d} c={c} />;
    case "lobby": return <Lobby d={d} c={c} />;
    case "records": return <Records d={d} c={c} />;
    case "operations": return <Operations d={d} c={c} />;
    case "lab": return <Lab d={d} c={c} />;
    case "studio": return <Studio d={d} c={c} />;
    case "demo-desk": return <DemoDesk d={d} c={c} />;
    case "research": return <Research d={d} c={c} />;
    case "alumni": return <Alumni d={d} c={c} />;
    case "hall": return <Hall d={d} c={c} />;
  }
}

// -- the city and the buildings ----------------------------------------------------------------

function Tree({ x, s = 1 }: { x: number; s?: number }) {
  return (
    <g transform={`translate(${x} ${GROUND + 14}) scale(${s})`} pointerEvents="none">
      <ellipse rx={26} ry={6} fill="#000" opacity=".3" />
      <rect x={-3} y={-44} width={6} height={44} fill="#3b2a20" />
      {[[0, -82, 30], [-18, -64, 22], [18, -62, 22], [0, -104, 20], [-10, -92, 18], [12, -90, 18]].map(([cx, cy, r], i) => (
        <circle key={i} cx={cx} cy={cy} r={r} fill={["#1f4a35", "#245a3f", "#1b3f2e"][i % 3]} />
      ))}
    </g>
  );
}

function Lamp({ x }: { x: number }) {
  return (
    <g transform={`translate(${x} ${GROUND + 14})`} pointerEvents="none">
      <ellipse rx={50} ry={10} fill="url(#fm-warm)" />
      <rect x={-1.5} y={-90} width={3} height={90} fill="#1b1f27" />
      <rect x={-6} y={-98} width={12} height={9} rx={2} fill="#ffd48a" />
      <circle cy={-93} r={22} fill="url(#fm-bulb)" />
    </g>
  );
}

const STARS = Array.from({ length: 70 }, (_, i) => [((i * 389) % 2900) - 400, ((i * 211) % 560) + 10, i % 3 ? 0.9 : 1.5] as const);
const SKYLINE = Array.from({ length: 26 }, (_, i) => {
  const w = 60 + ((i * 53) % 70);
  const x = -500 + i * 125 + ((i * 31) % 40);
  const h = 260 + ((i * 97) % 420);
  return { x, w, h };
});

const City = memo(function City({ onBackground }: { onBackground: () => void }) {
  return (
    <g onClick={onBackground}>
      <rect x={-3000} y={-3000} width={WORLD.w + 6000} height={3000 + GROUND} fill="url(#fm-sky)" />
      {STARS.map(([x, y, r], i) => <circle key={i} cx={x} cy={y} r={r} fill="#fff" opacity=".55" className={i % 6 === 0 ? "fs-pulse" : undefined} />)}
      <circle cx={1760} cy={150} r={26} fill="#f3ead2" opacity=".9" />
      <circle cx={1760} cy={150} r={70} fill="url(#fm-bulb)" opacity=".25" />
      <g opacity=".5">
        {SKYLINE.map((b, i) => (
          <g key={i}>
            <rect x={b.x} y={GROUND - b.h} width={b.w} height={b.h} fill={i % 2 ? "url(#fm-lit)" : "url(#fm-lit2)"} />
            <rect x={b.x} y={GROUND - b.h} width={b.w} height={3} fill="#2a3350" />
          </g>
        ))}
      </g>
      <rect x={-3000} y={GROUND} width={WORLD.w + 6000} height={3000} fill="#14201a" />
      <rect x={-3000} y={GROUND} width={WORLD.w + 6000} height={24} fill="#2a2724" />
      <rect x={-3000} y={GROUND + 46} width={WORLD.w + 6000} height={50} fill="#191b20" />
      <path d={`M-3000 ${GROUND + 71} H${WORLD.w + 3000}`} stroke="#d6c08a" strokeOpacity=".35" strokeWidth="3" strokeDasharray="26 22" />
      <Tree x={50} s={1.1} />
      <Tree x={1268} s={0.95} />
      <Lamp x={1300} />
      <Lamp x={-10} />
      <Tree x={2540} s={1.15} />
      <Lamp x={2500} />
    </g>
  );
});

const Buildings = memo(function Buildings() {
  const hqSlabs = FLOORS.filter((f) => f.building === "HQ Tower");
  const acSlabs = FLOORS.filter((f) => f.building !== "HQ Tower");
  const walls = (b: string) => DEPTS.filter((d) => floorBuilding(d.floor) === b);
  return (
    <g pointerEvents="none">
      {/* HQ Tower */}
      <rect x={TOWER.x0 - 12} y={TOWER.top - 12} width={TOWER.x1 - TOWER.x0 + 24} height={GROUND - TOWER.top + 12} fill="#241d18" />
      <rect x={TOWER.x0 - 20} y={TOWER.top - 18} width={TOWER.x1 - TOWER.x0 + 40} height={14} fill="#3a2e26" />
      <rect x={TOWER.x0 + 30} y={TOWER.top - 46} width={70} height={28} fill="#2a2f3a" />
      <line x1={TOWER.x1 - 60} y1={TOWER.top - 18} x2={TOWER.x1 - 60} y2={TOWER.top - 120} stroke="#4a4f58" strokeWidth="3" />
      <circle cx={TOWER.x1 - 60} cy={TOWER.top - 122} r={4} fill="#f43f5e" className="fs-pulse" />
      <rect x={520} y={TOWER.top - 50} width={6} height={32} fill="#3a2e26" />
      <rect x={794} y={TOWER.top - 50} width={6} height={32} fill="#3a2e26" />
      <rect x={470} y={TOWER.top - 108} width={380} height={60} rx={4} fill="#14110f" stroke="#cdb27a" strokeOpacity=".7" strokeWidth="2" />
      <rect x={488} y={TOWER.top - 96} width={36} height={36} fill="none" stroke="#cdb27a" strokeWidth="2" />
      <text x={506} y={TOWER.top - 72} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="14" fill="#e4cf9f">537</text>
      <text x={540} y={TOWER.top - 66} fontFamily="Fraunces, Georgia, serif" fontSize="28" letterSpacing="5" fill="#e4cf9f">THE 537 FIRM</text>
      {hqSlabs.map((f) => <Slab key={f.id} f={f} x0={TOWER.x0 - 12} x1={TOWER.x1 + 12} />)}
      {walls("HQ Tower").filter((d) => d.rect[0] > TOWER.x0).map((d) => <rect key={d.id} x={d.rect[0] - 3} y={d.rect[1]} width={6} height={d.rect[3] - SLAB} fill="#2a221d" />)}
      <rect x={TOWER.x0 - 70} y={GROUND - 152} width={58} height={8} fill="#cdb27a" opacity=".7" />
      {/* The 537 Academy */}
      <rect x={ACADEMY.x0 - 12} y={ACADEMY.top - 12} width={ACADEMY.x1 - ACADEMY.x0 + 24} height={GROUND - ACADEMY.top + 12} fill="#241d18" />
      <rect x={ACADEMY.x0 - 20} y={ACADEMY.top - 18} width={ACADEMY.x1 - ACADEMY.x0 + 40} height={14} fill="#3a2e26" />
      <rect x={(ACADEMY.x0 + ACADEMY.x1) / 2 - 160} y={ACADEMY.top - 46} width={6} height={28} fill="#3a2e26" />
      <rect x={(ACADEMY.x0 + ACADEMY.x1) / 2 + 154} y={ACADEMY.top - 46} width={6} height={28} fill="#3a2e26" />
      <rect x={(ACADEMY.x0 + ACADEMY.x1) / 2 - 190} y={ACADEMY.top - 96} width={380} height={52} rx={4} fill="#14110f" stroke="#cdb27a" strokeOpacity=".7" strokeWidth="2" />
      <text x={(ACADEMY.x0 + ACADEMY.x1) / 2} y={ACADEMY.top - 61} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="24" letterSpacing="4" fill="#e4cf9f">THE 537 ACADEMY</text>
      {acSlabs.map((f) => <Slab key={f.id} f={f} x0={ACADEMY.x0 - 12} x1={ACADEMY.x1 + 12} />)}
      {walls("The 537 Academy").filter((d) => d.rect[0] > ACADEMY.x0).map((d) => <rect key={d.id} x={d.rect[0] - 3} y={d.rect[1]} width={6} height={d.rect[3] - SLAB} fill="#2a221d" />)}
    </g>
  );
});

const floorBuilding = (id: FloorId) => FLOORS.find((f) => f.id === id)!.building;

function Slab({ f, x0, x1 }: { f: Floor; x0: number; x1: number }) {
  const y = f.rect[1] + f.rect[3] - SLAB;
  return (
    <g>
      <rect x={x0} y={y} width={x1 - x0} height={SLAB} fill="#3a2e26" />
      <rect x={x0} y={y} width={x1 - x0} height={2} fill="#5a4636" />
    </g>
  );
}

/** The floor tags beside each building: click to fly to the floor. */
function FloorTag({ f, c }: { f: Floor; c: Ctx }) {
  const hq = f.building === "HQ Tower";
  const [, y, , h] = f.rect;
  const ax = hq ? TOWER.x0 - 22 : ACADEMY.x1 + 22;
  const ay = y + (h - SLAB) / 2;
  const words = f.name.split(/ (?=\S)/);
  const w = 92;
  const bh = 34 + words.length * 13;
  const on = c.sel?.kind === "floor" && c.sel.id === f.id;
  const pick = () => {
    c.onSelect({ kind: "floor", id: f.id });
    c.onFocus(f.rect);
  };
  return (
    <g role="button" tabIndex={0} data-fm-hit aria-label={`${f.building}, ${f.level} ${f.name}. Fly to this floor.`} className="fm-tag cursor-pointer outline-none" onClick={(e) => (e.stopPropagation(), pick())} onKeyDown={activate(pick)} transform={`translate(${ax} ${ay})`}>
      <g className="fm-lbl" style={{ transformOrigin: "0 0" }}>
        <g transform={`translate(${hq ? -w : 0} ${-bh / 2})`} fontFamily="Inter, sans-serif">
          <rect width={w} height={bh} rx={8} fill="#0b1020" stroke="#cdb27a" strokeOpacity={on ? 0.95 : 0.45} strokeWidth={on ? 2 : 1} />
          <text x={12} y={26} fontSize="20" fontWeight="700" fill="#e4cf9f">{f.level}</text>
          {words.map((wd, i) => <text key={i} x={12} y={44 + i * 13} fontSize="10.5" fill="#cbd5e1">{wd}</text>)}
        </g>
      </g>
    </g>
  );
}

// -- the map ----------------------------------------------------------------------------------------

export function FirmMap(props: Ctx & { onBackground: () => void }) {
  const { onBackground, ...c } = props;
  return (
    <g>
      <Defs />
      <City onBackground={onBackground} />
      <Buildings />
      {DEPTS.map((d) => <DeptRoom key={d.id} d={d} c={c} />)}
      {FLOORS.map((f) => <FloorTag key={f.id} f={f} c={c} />)}
    </g>
  );
}
