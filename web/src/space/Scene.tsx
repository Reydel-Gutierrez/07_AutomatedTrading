// The office itself: one SVG per room so the floor plan can reflow from a two-storey
// building on desktop to a stacked column on phones. Rooms draw real state only; the
// lamp glow, plants, steam, clouds, and blinking are the cosmetic layer.

import { type ReactNode } from "react";
import { Body, Hands, LOOKS, type Pose } from "./Character";
import { type AgentState, type StateKey, STATE_META } from "./state";

const W = 400;
const H = 250;
const DESK_Y = 162;

export function poseFor(key: StateKey): Pose {
  switch (key) {
    case "analyzing":
    case "preparing":
    case "executing":
      return "typing";
    case "reviewing":
      return "reading";
    case "completed":
      return "pleased";
    case "error":
      return "alert";
    default:
      return "idle";
  }
}

function Glyph({ x, y, k }: { x: number; y: number; k: StateKey }) {
  if (k === "idle" || k === "inactive") return null;
  const m = STATE_META[k];
  const icon: Record<string, ReactNode> = {
    analyzing: <path d="M-3 -3 m-4 0 a4 4 0 1 0 8 0 a4 4 0 1 0 -8 0 M0 0 l4 4" stroke="#0b0e14" strokeWidth="1.8" fill="none" strokeLinecap="round" />,
    reviewing: <path d="M-4 -5 h6 l2 2 v8 h-8 Z M-2 -1 h4 M-2 2 h4" stroke="#0b0e14" strokeWidth="1.4" fill="none" />,
    preparing: <path d="M-5 -4 h10 v8 h-10 Z M-5 -1 h10" stroke="#0b0e14" strokeWidth="1.5" fill="none" />,
    executing: <path d="M1 -6 L-4 1 h3 L-1 6 L4 -1 h-3 Z" fill="#0b0e14" />,
    completed: <path d="M-4 0 l3 3 l5 -6" stroke="#0b0e14" strokeWidth="2" fill="none" strokeLinecap="round" />,
    error: <path d="M0 -5 v5 M0 3.5 v.5" stroke="#0b0e14" strokeWidth="2.2" strokeLinecap="round" />,
    waiting: <path d="M-3.5 -5 h7 M-3.5 5 h7 M-3 -5 L3 5 M3 -5 L-3 5" stroke="#0b0e14" strokeWidth="1.4" fill="none" />,
    paused: <path d="M-2.5 -4 v8 M2.5 -4 v8" stroke="#0b0e14" strokeWidth="2" />,
  };
  return (
    <g transform={`translate(${x} ${y})`}>
      {m.working && <circle r="12" fill={m.color} opacity=".35" className="fs-ping" />}
      <circle r="10" fill={m.color} />
      {icon[k]}
    </g>
  );
}

function Lamp({ x, k, color = "#fff3d6" }: { x: number; k: StateKey; color?: string }) {
  const glow = k === "inactive" ? 0 : STATE_META[k].working ? 0.16 : 0.08;
  return (
    <g>
      <path d={`M${x} 0 V16`} stroke="#3a4560" strokeWidth="1.5" />
      <path d={`M${x - 14} 28 L${x - 7} 16 h14 L${x + 14} 28 Z`} fill="#2a3245" />
      {glow > 0 && <path d={`M${x - 14} 28 L${x - 80} ${H} H${x + 80} L${x + 14} 28 Z`} fill={color} opacity={glow} />}
      <ellipse cx={x} cy={28} rx={14} ry={2.5} fill={glow ? color : "#4b5563"} opacity={glow ? 0.9 : 0.6} />
    </g>
  );
}

function Plant({ x, y = 222, s = 1 }: { x: number; y?: number; s?: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${s})`}>
      <g className="fs-sway" style={{ transformOrigin: "0px -18px" }}>
        {[[-14, -52, -26], [0, -62, 0], [14, -50, 24], [-8, -40, -40], [9, -38, 38]].map(([dx, dy, r], i) => (
          <ellipse key={i} cx={dx} cy={dy} rx={6} ry={16} fill={i % 2 ? "#2f7d5b" : "#3d9a6f"} transform={`rotate(${r} ${dx} ${dy})`} />
        ))}
      </g>
      <path d="M-13 -20 h26 l-3 20 h-20 Z" fill="#6b4a3a" />
      <rect x={-14} y={-22} width={28} height={4} rx={1} fill="#7d5845" />
    </g>
  );
}

function Mug({ x, steam }: { x: number; steam: boolean }) {
  return (
    <g transform={`translate(${x} ${DESK_Y - 4})`}>
      {steam && (
        <g stroke="#cbd5e1" strokeWidth="1.2" fill="none" opacity=".5">
          <path className="fs-steam" d="M-2 -14 q-3 -4 0 -8 q3 -4 0 -8" />
          <path className="fs-steam fs-delay" d="M3 -14 q-3 -4 0 -8 q3 -4 0 -8" />
        </g>
      )}
      <rect x={-5} y={-12} width={11} height={12} rx={2} fill="#e9e4da" />
      <path d="M6 -9 q5 0 5 3.5 q0 3.5 -5 3.5" stroke="#e9e4da" strokeWidth="2" fill="none" />
    </g>
  );
}

function Desk({ cx, half = 96, wood = "#3b2b22", top = "#5a4232" }: { cx: number; half?: number; wood?: string; top?: string }) {
  return (
    <g>
      <rect x={cx - half} y={DESK_Y - 6} width={half * 2} height={12} rx={2} fill={top} />
      <rect x={cx - half + 4} y={DESK_Y + 6} width={half * 2 - 8} height={56} fill={wood} />
      <rect x={cx - half + 4} y={DESK_Y + 6} width={half * 2 - 8} height={3} fill="#000" opacity=".25" />
      <rect x={cx - half + 18} y={DESK_Y + 20} width={half * 2 - 36} height={30} rx={2} fill="none" stroke="#000" strokeOpacity=".18" />
    </g>
  );
}

function Keyboard({ cx }: { cx: number }) {
  return <rect x={cx - 20} y={DESK_Y - 4} width={40} height={4} rx={1} fill="#c8ced8" opacity=".85" />;
}

function Monitor({ x, k, accent, off = false }: { x: number; k: StateKey; accent: string; off?: boolean }) {
  const color = STATE_META[k].color;
  const busy = STATE_META[k].working;
  return (
    <g transform={`translate(${x} 0)`}>
      <rect x={-4} y={DESK_Y - 22} width={8} height={18} fill="#2b3242" />
      <rect x={-14} y={DESK_Y - 6} width={28} height={3} rx={1} fill="#2b3242" />
      <rect x={-29} y={DESK_Y - 60} width={58} height={40} rx={3} fill="#0d1017" stroke="#2b3242" strokeWidth="2" />
      {off ? (
        <path d={`M-20 ${DESK_Y - 55} l12 0 l-18 26`} stroke="#fff" strokeOpacity=".06" strokeWidth="6" />
      ) : (
        <g>
          <clipPath id={`scr-${x}`}>
            <rect x={-27} y={DESK_Y - 58} width={54} height={36} rx={2} />
          </clipPath>
          <g clipPath={`url(#scr-${x})`}>
            <rect x={-27} y={DESK_Y - 58} width={54} height={36} fill={k === "error" ? "#3b0d16" : "#0f1a2b"} />
            {busy ? (
              <g className="fs-scan">
                {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
                  <rect key={i} x={-23} y={DESK_Y - 56 + i * 7 - 28} width={12 + ((i * 17) % 30)} height={2.5} rx={1} fill={i % 3 ? accent : color} opacity=".8" />
                ))}
              </g>
            ) : k === "error" ? (
              <text x={0} y={DESK_Y - 35} textAnchor="middle" fontSize="14" fontWeight="700" fill="#f43f5e">!</text>
            ) : (
              <g opacity=".55">
                <path d={`M-22 ${DESK_Y - 30} l10 -6 l8 3 l10 -9 l8 4 l9 -6`} stroke={accent} strokeWidth="1.5" fill="none" />
                <text x={0} y={DESK_Y - 44} textAnchor="middle" fontSize="7" fill="#cdb27a" fontFamily="Fraunces, serif" letterSpacing=".5">537</text>
              </g>
            )}
          </g>
        </g>
      )}
    </g>
  );
}

/** The big wall display: the only place text appears inside a scene, and it shows facts only. */
function WallBoard({ x, y, w, h, state, nextRun, accent, off }: { x: number; y: number; w: number; h: number; state: AgentState; nextRun?: string | null; accent: string; off?: boolean }) {
  const m = STATE_META[state.key];
  const syms = state.symbols.slice(0, 3);
  return (
    <g transform={`translate(${x} ${y})`}>
      <rect width={w} height={h} rx={4} fill="#0a0d13" stroke="#2b3242" strokeWidth="2.5" />
      {off ? (
        <path d={`M10 6 l26 0 l-34 ${h - 14}`} stroke="#fff" strokeOpacity=".04" strokeWidth="10" />
      ) : (
        <g fontFamily="Inter, sans-serif">
          <rect x={2} y={2} width={w - 4} height={13} rx={2} fill={m.color} opacity=".2" />
          <circle cx={9} cy={8.5} r={2.6} fill={m.color} className={m.working ? "fs-pulse" : ""} />
          <text x={16} y={11.5} fontSize="8" fontWeight="600" fill={m.color} letterSpacing=".6">
            {m.label.toUpperCase()}
          </text>
          {syms.length > 0 ? (
            syms.map((s, i) => (
              <g key={`${s.symbol}-${i}`} transform={`translate(8 ${26 + i * 15})`}>
                {s.side && (
                  <text fontSize="7.5" fontWeight="700" fill={s.side === "sell" ? "#fb7185" : "#34d399"}>
                    {s.side.toUpperCase()}
                  </text>
                )}
                <text x={s.side ? 26 : 0} fontSize="9.5" fontWeight="600" fill="#e5e7eb">
                  {s.symbol}
                </text>
              </g>
            ))
          ) : state.key === "analyzing" ? (
            <g className="fs-pulse">
              {[0, 1, 2].map((i) => (
                <rect key={i} x={8} y={22 + i * 12} width={w - 30 - i * 14} height={4} rx={2} fill={accent} opacity=".5" />
              ))}
            </g>
          ) : nextRun ? (
            <g>
              <text x={8} y={30} fontSize="7" fill="#64748b" letterSpacing=".8">NEXT DECISION</text>
              <text x={8} y={44} fontSize="10" fontWeight="600" fill="#cbd5e1">{nextRun}</text>
            </g>
          ) : (
            <text x={w / 2} y={h / 2 + 8} textAnchor="middle" fontSize="13" fill="#cdb27a" opacity=".35" fontFamily="Fraunces, serif">537</text>
          )}
        </g>
      )}
    </g>
  );
}

function Room({ wall, children }: { wall: string; children: ReactNode }) {
  return (
    <>
      <rect width="100%" height={H} fill={wall} />
      <rect y={196} width="100%" height={54} fill="#1b1612" />
      {Array.from({ length: 14 }, (_, i) => (
        <path key={i} d={`M${i * 62} 196 l-18 54`} stroke="#000" strokeOpacity=".22" />
      ))}
      <rect y={192} width="100%" height={5} fill="#0f0c0a" opacity=".6" />
      {children}
    </>
  );
}

export function nextLabel(iso: string | null | undefined): string | null {
  if (!iso) return null;
  return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "numeric", minute: "2-digit" }).format(new Date(iso)) + " ET";
}

export function ManagerScene({ id, state, nextRun, phase }: { id: string; state: AgentState; nextRun: string | null; phase?: number }) {
  const look = LOOKS[id] ?? LOOKS.tim;
  const cx = W / 2;
  const off = state.key === "inactive";
  const k = state.key;
  const pose = poseFor(k);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMax slice" className="h-full w-full" role="img" aria-label={`${id}'s office`}>
      <g className={off ? "fs-off" : ""}>
        <Room wall={id === "max" ? "#171a22" : id === "alex" ? "#181826" : "#141b28"}>
          <rect x={0} y={150} width={W} height={42} fill="#ffffff" opacity=".015" />
          <Lamp x={cx} k={k} />
          <WallBoard x={24} y={36} w={118} h={74} state={state} nextRun={nextLabel(nextRun)} accent={look.accent} off={off} />
          {id === "tim" && (
            <g>
              <rect x={276} y={44} width={100} height={6} fill="#4a3628" />
              <rect x={276} y={96} width={100} height={6} fill="#4a3628" />
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <rect key={i} x={282 + i * 15} y={i % 3 === 1 ? 18 : 22} width={11} height={i % 3 === 1 ? 26 : 22} rx={1} fill={["#2f6bd0", "#1f4f9e", "#8aa9d6", "#2f6bd0", "#3b4a63", "#5d82c4"][i]} />
              ))}
              <rect x={284} y={74} width={34} height={22} rx={2} fill="#2b3242" />
              <Plant x={350} y={96} s={0.55} />
              <Plant x={372} />
            </g>
          )}
          {id === "alex" && (
            <g>
              <rect x={262} y={30} width={112} height={78} rx={3} fill="#3a3046" />
              {[[272, 40, "#a78bfa"], [302, 44, "#fde68a"], [334, 38, "#93c5fd"], [282, 72, "#fca5a5"], [318, 70, "#a7f3d0"]].map(([x, y, c], i) => (
                <rect key={i} x={x as number} y={y as number} width={24} height={20} fill={c as string} opacity=".8" transform={`rotate(${(i % 2 ? 4 : -3)} ${x} ${y})`} />
              ))}
              <path d="M290 50 L314 54 L344 48 M296 82 L330 80" stroke="#f43f5e" strokeWidth="1" opacity=".6" />
              <rect x={320} y={206} width={44} height={30} fill="#8a6b4a" />
              <rect x={332} y={184} width={36} height={24} fill="#9c7b57" />
              <path d="M320 216 h44 M332 194 h36" stroke="#5c4630" strokeWidth="1.5" />
            </g>
          )}
          {id === "max" && (
            <g>
              <rect x={0} y={14} width={W} height={14} fill="#0a0c10" />
              <text x={cx} y={24} textAnchor="middle" fontSize="8" fill="#f59e0b" opacity=".35" fontFamily="Inter, sans-serif" letterSpacing="2">
                MARKET TAPE OFFLINE
              </text>
              <rect x={290} y={44} width={86} height={58} rx={3} fill="#0a0d13" stroke="#2b3242" strokeWidth="2" />
              <rect x={20} y={206} width={36} height={28} fill="#8a6b4a" />
            </g>
          )}
          <g transform={`translate(${cx} ${DESK_Y})`}>
            <Body look={look} pose={pose} working={STATE_META[k].working} />
          </g>
          <Desk cx={cx} wood={id === "max" ? "#2a2f3a" : "#3b2b22"} top={id === "max" ? "#3d4452" : "#5a4232"} />
          <Keyboard cx={cx} />
          <Monitor x={cx + 64} k={k} accent={look.accent} off={off} />
          {id === "max" && <Monitor x={cx - 64} k={k} accent={look.accent} off={off} />}
          {id !== "max" && <Mug x={cx - 70} steam={!off} />}
          <g transform={`translate(${cx} ${DESK_Y})`}>
            <Hands look={look} pose={pose} />
          </g>
          {off && (
            <g>
              <path d={`M${cx + 30} ${DESK_Y - 66} q34 -8 68 0 l4 48 q-38 6 -76 0 Z`} fill="#cfd4dc" opacity=".3" />
            </g>
          )}
          {k === "executing" && (
            <g>
              <path d={`M${cx + 93} ${DESK_Y - 40} C ${cx + 140} ${DESK_Y - 40}, ${W - 40} 120, ${W - 18} 120`} stroke={STATE_META.executing.color} strokeWidth="2" strokeDasharray="4 8" fill="none" className="fs-flow" />
              <rect x={W - 22} y={106} width={16} height={28} rx={3} fill="#2b3242" />
              <circle cx={W - 14} cy={114} r={2.5} fill={STATE_META.executing.color} className="fs-pulse" />
            </g>
          )}
          <Glyph x={cx + 30} y={50} k={k} />
        </Room>
      </g>
      {off && phase && (
        <g transform={`translate(${cx} 214)`} fontFamily="Inter, sans-serif">
          <path d="M-62 -6 L-70 30 M62 -6 L70 30" stroke="#d4a017" strokeWidth="4" />
          <rect x={-74} y={-14} width={148} height={20} rx={2} fill="#d4a017" />
          <path d="M-74 -14 h148 v20 h-148 Z" fill="url(#hazard)" opacity=".35" />
          <text y={0} textAnchor="middle" fontSize="9.5" fontWeight="700" fill="#141414" letterSpacing="1.4">
            OPENS IN PHASE {phase}
          </text>
          <defs>
            <pattern id="hazard" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect width="6" height="12" fill="#141414" />
            </pattern>
          </defs>
        </g>
      )}
    </svg>
  );
}

function skyFor(hourEt: number): [string, string, boolean] {
  if (hourEt < 6 || hourEt >= 20) return ["#060a18", "#14213f", true];
  if (hourEt < 8) return ["#3b4d7a", "#e9a17a", false];
  if (hourEt < 17) return ["#4a86c5", "#a9d0ee", false];
  return ["#3a3f73", "#e38f6b", false];
}

export function CioScene({ state, pending, hourEt }: { state: AgentState; pending: number; hourEt: number }) {
  const look = LOOKS.cio;
  const cx = 400;
  const k = state.key;
  const off = k === "inactive";
  const pose = poseFor(k);
  const [skyTop, skyLow, night] = skyFor(hourEt);
  return (
    <svg viewBox={`0 0 800 ${H}`} preserveAspectRatio="xMidYMax slice" className="h-full w-full" role="img" aria-label="The CIO's executive office">
      <defs>
        <linearGradient id="cio-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor={skyTop} />
          <stop offset="1" stopColor={skyLow} />
        </linearGradient>
        <clipPath id="cio-win">
          <rect x={566} y={28} width={200} height={146} />
        </clipPath>
      </defs>
      <g className={off ? "fs-off" : ""}>
        <Room wall="#211a16">
          {Array.from({ length: 9 }, (_, i) => (
            <rect key={i} x={i * 92 + 6} y={118} width={80} height={70} rx={2} fill="none" stroke="#000" strokeOpacity=".22" />
          ))}
          {/* window with the real time of day in New York */}
          <g clipPath="url(#cio-win)">
            <rect x={566} y={28} width={200} height={146} fill="url(#cio-sky)" />
            {night
              ? [[590, 44], [640, 60], [700, 40], [742, 70], [612, 84], [726, 52]].map(([x, y], i) => <circle key={i} cx={x} cy={y} r={1} fill="#fff" opacity=".7" className={i % 2 ? "fs-pulse" : ""} />)
              : (
                <g className="fs-drift" opacity=".75">
                  <ellipse cx={600} cy={56} rx={22} ry={6} fill="#fff" />
                  <ellipse cx={614} cy={52} rx={14} ry={6} fill="#fff" />
                </g>
              )}
            {[[566, 120, 24, 54], [592, 96, 20, 78], [614, 130, 28, 44], [644, 84, 18, 90], [664, 112, 30, 62], [696, 70, 22, 104], [720, 118, 26, 56], [748, 100, 20, 74]].map(([x, y, w, h], i) => (
              <g key={i}>
                <rect x={x} y={y} width={w} height={h} fill={night ? "#0d1426" : "#2c3c57"} opacity=".95" />
                {night && Array.from({ length: Math.floor(h / 14) }, (_, j) => <rect key={j} x={x + 4 + ((i + j) % 2) * 8} y={y + 6 + j * 14} width={4} height={4} fill="#f8d48a" opacity={(i + j) % 3 ? 0.55 : 0.15} />)}
              </g>
            ))}
          </g>
          <rect x={566} y={28} width={200} height={146} fill="none" stroke="#4a3a2e" strokeWidth="6" />
          <path d="M666 28 V174 M566 100 H766" stroke="#4a3a2e" strokeWidth="3" />
          <rect x={558} y={174} width={216} height={8} fill="#4a3a2e" />
          {/* bookshelf */}
          <rect x={36} y={22} width={156} height={170} fill="#2e231c" stroke="#4a3a2e" strokeWidth="4" />
          {[60, 100, 140].map((y) => (
            <rect key={y} x={38} y={y} width={152} height={4} fill="#4a3a2e" />
          ))}
          {[26, 64, 104, 144].map((y, r) =>
            Array.from({ length: 9 }, (_, i) => (
              <rect key={`${r}-${i}`} x={44 + i * 16} y={y + (i % 4 === 2 ? 10 : 4)} width={12} height={i % 4 === 2 ? 24 : 30} fill={["#6b2d2d", "#2d4a6b", "#7a6a3a", "#33553f", "#5a3a6b"][(i + r) % 5]} opacity=".9" />
            )),
          )}
          {/* globe and art */}
          <g transform="translate(236 222)">
            <path d="M-10 0 h20 l-4 -8 h-12 Z" fill="#6b5a3a" />
            <rect x={-1.5} y={-26} width={3} height={18} fill="#8a7448" />
            <circle cy={-40} r={16} fill="#2d5a7b" />
            <path d="M-10 -48 q6 4 4 10 q-4 4 2 8 M4 -54 q4 6 10 4" stroke="#5f9a6a" strokeWidth="4" fill="none" />
            <circle cy={-40} r={18} fill="none" stroke="#b8975a" strokeWidth="1.5" />
          </g>
          <g transform="translate(468 46)">
            <rect width={64} height={50} fill="#14110f" stroke="#b8975a" strokeWidth="3" />
            <text x={32} y={31} textAnchor="middle" fontSize="15" fill="#e4cf9f" fontFamily="Fraunces, serif">537</text>
            <path d="M18 38 h28" stroke="#b8975a" strokeWidth=".8" />
          </g>
          <text x={226} y={18} fontSize="7" fill="#b8975a" opacity=".7" letterSpacing="2.5" fontFamily="Inter, sans-serif">OFFICE OF THE CIO</text>
          <Lamp x={cx} k={k} color="#ffe9b8" />
          <g transform={`translate(${cx} ${DESK_Y})`}>
            <Body look={look} pose={pose} working={STATE_META[k].working} />
          </g>
          <Desk cx={cx} half={128} wood="#4a2f22" top="#6b4430" />
          <rect x={cx - 120} y={DESK_Y + 14} width={240} height={2} fill="#b8975a" opacity=".5" />
          {/* brass desk lamp */}
          <g transform={`translate(${cx - 96} ${DESK_Y - 6})`}>
            <rect x={-10} y={-3} width={20} height={3} fill="#b8975a" />
            <path d="M0 -3 L-4 -30 L12 -40" stroke="#b8975a" strokeWidth="2.5" fill="none" />
            <path d="M4 -46 l18 4 l-6 10 Z" fill="#2f5a3f" />
            {!off && <path d="M14 -36 L4 0 H34 Z" fill="#ffe9b8" opacity=".12" />}
          </g>
          {/* the in-tray holds exactly the proposals waiting for the owner */}
          <g transform={`translate(${cx + 92} ${DESK_Y - 6})`}>
            <rect x={-18} y={-4} width={36} height={4} fill="#5a4232" />
            {Array.from({ length: Math.min(pending, 6) }, (_, i) => (
              <rect key={i} x={-15 + (i % 2)} y={-7 - i * 3} width={30} height={3} fill="#f4f1e8" stroke="#cbbf9f" strokeWidth=".5" />
            ))}
            {pending > 0 && <rect x={10} y={-10 - Math.min(pending, 6) * 3} width={5} height={7} fill="#cdb27a" />}
          </g>
          <Mug x={cx - 52} steam={!off} />
          <g transform={`translate(${cx} ${DESK_Y})`}>
            <Hands look={look} pose={pose} />
          </g>
          <Plant x={530} s={1.15} />
          <Glyph x={cx + 32} y={46} k={k} />
        </Room>
      </g>
    </svg>
  );
}
