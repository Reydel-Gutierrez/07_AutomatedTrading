// The Firm as one isometric building, drawn in SVG with a 2:1 projection.
// World units: x runs down-right, y runs down-left, z is up. The upper floor sits
// on the back half so the investment floor in front stays visible (a cutaway).
// Static architecture is memoized; only the people and their desks re-render.

import { memo, type ReactNode } from "react";
import { Body, Hands, LOOKS, type Look } from "./Character";
import { poseFor } from "./pose";
import { type AgentState, STATE_META } from "./state";

export const K = 20;
const OX = 520;
const OY = 400;
export const VB = { w: 1280, h: 1010 };

export const P = (x: number, y: number, z = 0): [number, number] => [OX + (x - y) * K, OY + ((x + y) * K) / 2 - z * K];
const pts = (...ps: [number, number][]) => ps.map((p) => `${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");

// floor plan
const UP_Z = 8.6; // upper floor surface
const UP_H = 7; // upper wall height
const G_Y0 = 10; // investment floor starts in front of the upper block
const G_Y1 = 22;
export const ROOMS = {
  cio: { x0: 0, x1: 14, desk: { x: 4.1, y: 4.2, w: 6, d: 2 }, z: UP_Z },
  tim: { x0: 0, x1: 12, desk: { x: 3.1, y: 14.4, w: 5.6, d: 1.9 }, z: 0 },
  alex: { x0: 12, x1: 24, desk: { x: 15.1, y: 14.4, w: 5.6, d: 1.9 }, z: 0 },
  max: { x0: 24, x1: 36, desk: { x: 27.1, y: 14.4, w: 5.6, d: 1.9 }, z: 0 },
} as const;
export type AgentId = keyof typeof ROOMS;
const DESK_H = 1.45;

/** Screen point above an agent's head (for the HTML indicator), in viewBox units. */
export function headPoint(id: AgentId): [number, number] {
  const r = ROOMS[id];
  const [x, y] = P(r.desk.x + r.desk.w / 2, r.desk.y, r.z + DESK_H);
  return [x, y - (id === "cio" ? 100 : 90)];
}

function Poly({ p, fill, stroke, sw = 1, op, className }: { p: [number, number][]; fill: string; stroke?: string; sw?: number; op?: number; className?: string }) {
  return <polygon points={pts(...p)} fill={fill} stroke={stroke} strokeWidth={stroke ? sw : undefined} opacity={op} className={className} strokeLinejoin="round" />;
}
const floorQ = (x0: number, y0: number, x1: number, y1: number, z: number): [number, number][] => [P(x0, y0, z), P(x1, y0, z), P(x1, y1, z), P(x0, y1, z)];
const wallXQ = (x: number, y0: number, y1: number, z0: number, z1: number): [number, number][] => [P(x, y0, z0), P(x, y1, z0), P(x, y1, z1), P(x, y0, z1)];
const wallYQ = (y: number, x0: number, x1: number, z0: number, z1: number): [number, number][] => [P(x0, y, z0), P(x1, y, z0), P(x1, y, z1), P(x0, y, z1)];

function Box({ x, y, z, w, d, h, top, left, right, stroke = "#00000040" }: { x: number; y: number; z: number; w: number; d: number; h: number; top: string; left: string; right: string; stroke?: string }) {
  return (
    <g>
      <Poly p={[P(x, y + d, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x, y + d, z + h)]} fill={left} stroke={stroke} sw={0.6} />
      <Poly p={[P(x + w, y, z), P(x + w, y + d, z), P(x + w, y + d, z + h), P(x + w, y, z + h)]} fill={right} stroke={stroke} sw={0.6} />
      <Poly p={floorQ(x, y, x + w, y + d, z + h)} fill={top} stroke={stroke} sw={0.6} />
    </g>
  );
}

/** Content drawn flat on the plane y = const (text reads left to right). Origin at (x, y, z). */
function OnWallY({ x, y, z, children }: { x: number; y: number; z: number; children: ReactNode }) {
  const [sx, sy] = P(x, y, z);
  return <g transform={`matrix(1 0.5 0 1 ${sx} ${sy})`}>{children}</g>;
}
/** Content on the plane x = const, reading from front (large y) to back. Origin at (x, y, z). */
function OnWallX({ x, y, z, children }: { x: number; y: number; z: number; children: ReactNode }) {
  const [sx, sy] = P(x, y, z);
  return <g transform={`matrix(1 -0.5 0 1 ${sx} ${sy})`}>{children}</g>;
}

function Glow({ x, y, z, r = 3.2, color = "url(#fs-warm)", className }: { x: number; y: number; z: number; r?: number; color?: string; className?: string }) {
  const [cx, cy] = P(x, y, z);
  return <ellipse cx={cx} cy={cy} rx={r * K * 1.42} ry={r * K * 0.71} fill={color} className={className} />;
}

function Plant({ x, y, z, s = 1 }: { x: number; y: number; z: number; s?: number }) {
  const [sx, sy] = P(x, y, z);
  return (
    <g transform={`translate(${sx} ${sy}) scale(${s})`}>
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

function Tree({ x, y, s = 1 }: { x: number; y: number; s?: number }) {
  const [sx, sy] = P(x, y, -0.8);
  return (
    <g transform={`translate(${sx} ${sy}) scale(${s})`}>
      <ellipse cx={0} cy={0} rx={26} ry={9} fill="#000" opacity=".3" />
      <rect x={-3} y={-40} width={6} height={40} fill="#3b2a20" />
      {[[0, -78, 30], [-18, -60, 22], [18, -58, 22], [0, -100, 20], [-10, -88, 18], [12, -86, 18]].map(([cx, cy, r], i) => (
        <circle key={i} cx={cx} cy={cy} r={r} fill={["#1f4a35", "#245a3f", "#1b3f2e"][i % 3]} />
      ))}
      <circle cx={-8} cy={-92} r={9} fill="#2f6b4a" opacity=".6" />
    </g>
  );
}

function StreetLamp({ x, y }: { x: number; y: number }) {
  const [sx, sy] = P(x, y, -0.8);
  return (
    <g transform={`translate(${sx} ${sy})`}>
      <ellipse cx={0} cy={0} rx={40} ry={18} fill="url(#fs-warm)" opacity=".9" />
      <rect x={-1.5} y={-46} width={3} height={46} fill="#1b1f27" />
      <rect x={-5} y={-54} width={10} height={9} rx={2} fill="#ffd48a" />
      <circle cx={0} cy={-50} r={16} fill="url(#fs-bulb)" />
    </g>
  );
}

/** Night sky, skyline, grounds, and the building shell. Never re-renders. */
const Shell = memo(function Shell() {
  return (
    <g>
      <defs>
        <linearGradient id="fs-sky" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#070b17" />
          <stop offset=".55" stopColor="#141a2e" />
          <stop offset="1" stopColor="#3a2622" />
        </linearGradient>
        <radialGradient id="fs-warm">
          <stop offset="0" stopColor="#ffcf8a" stopOpacity=".55" />
          <stop offset="1" stopColor="#ffcf8a" stopOpacity="0" />
        </radialGradient>
        <radialGradient id="fs-bulb">
          <stop offset="0" stopColor="#ffe2a8" stopOpacity=".8" />
          <stop offset="1" stopColor="#ffe2a8" stopOpacity="0" />
        </radialGradient>
        <linearGradient id="fs-wall" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2b2420" />
          <stop offset="1" stopColor="#3a2e26" />
        </linearGradient>
        <linearGradient id="fs-wall2" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#221d1a" />
          <stop offset="1" stopColor="#2f2620" />
        </linearGradient>
        <linearGradient id="fs-glass" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#bcd3ff" stopOpacity=".1" />
          <stop offset="1" stopColor="#bcd3ff" stopOpacity=".03" />
        </linearGradient>
        <linearGradient id="fs-night" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#0a1226" />
          <stop offset="1" stopColor="#23263f" />
        </linearGradient>
        <pattern id="fs-lit" width="14" height="16" patternUnits="userSpaceOnUse">
          <rect width="14" height="16" fill="#0e1424" />
          <rect x="2" y="3" width="3" height="4" fill="#f6c979" opacity=".7" />
          <rect x="8" y="3" width="3" height="4" fill="#f6c979" opacity=".15" />
          <rect x="2" y="10" width="3" height="4" fill="#9fc3ff" opacity=".2" />
          <rect x="8" y="10" width="3" height="4" fill="#f6c979" opacity=".55" />
        </pattern>
        <pattern id="fs-lit2" width="18" height="14" patternUnits="userSpaceOnUse">
          <rect width="18" height="14" fill="#111829" />
          <rect x="3" y="3" width="4" height="3" fill="#f6c979" opacity=".2" />
          <rect x="11" y="3" width="4" height="3" fill="#f6c979" opacity=".65" />
          <rect x="3" y="9" width="4" height="3" fill="#f6c979" opacity=".5" />
        </pattern>
        <pattern id="fs-slat-y" width="14" height="20" patternUnits="userSpaceOnUse" patternTransform="matrix(1 0.5 0 1 0 0)">
          <rect width="14" height="20" fill="none" />
          <rect width="1.5" height="20" fill="#000" opacity=".22" />
          <rect x="1.5" width="1" height="20" fill="#fff" opacity=".04" />
        </pattern>
        <pattern id="fs-slat-x" width="14" height="20" patternUnits="userSpaceOnUse" patternTransform="matrix(1 -0.5 0 1 0 0)">
          <rect width="14" height="20" fill="none" />
          <rect width="1.5" height="20" fill="#000" opacity=".22" />
        </pattern>
        <linearGradient id="fs-wash" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffcf8a" stopOpacity=".16" />
          <stop offset=".6" stopColor="#ffcf8a" stopOpacity=".03" />
          <stop offset="1" stopColor="#000" stopOpacity=".15" />
        </linearGradient>
        <pattern id="fs-plank" width="20" height="10" patternUnits="userSpaceOnUse" patternTransform="matrix(1 0.5 -1 0.5 0 0)">
          <rect width="20" height="10" fill="#6a4a35" />
          <path d="M0 9.5 H20 M10 0 V10" stroke="#3d2a1e" strokeOpacity=".5" strokeWidth="1" />
        </pattern>
        <pattern id="fs-stone" width="24" height="24" patternUnits="userSpaceOnUse" patternTransform="matrix(1 0.5 -1 0.5 0 0)">
          <rect width="24" height="24" fill="#4a4038" />
          <path d="M0 23.5 H24 M23.5 0 V24" stroke="#2b241f" strokeOpacity=".6" strokeWidth="1" />
        </pattern>
      </defs>

      <rect width={VB.w} height={VB.h} fill="url(#fs-sky)" />
      {[[90, 60], [210, 34], [330, 90], [470, 40], [700, 70], [820, 30], [980, 54], [1120, 84], [1210, 40], [600, 110], [150, 140]].map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r={i % 3 ? 0.9 : 1.4} fill="#fff" opacity=".6" className={i % 4 === 0 ? "fs-pulse" : undefined} />
      ))}
      <circle cx={1150} cy={110} r={22} fill="#f3ead2" opacity=".9" />
      <circle cx={1150} cy={110} r={60} fill="url(#fs-bulb)" opacity=".25" />
      {/* skyline */}
      {[[600, 150, 50, 300], [660, 210, 40, 240], [710, 120, 60, 330], [780, 190, 44, 260], [830, 90, 54, 360], [895, 170, 46, 280], [950, 130, 64, 320], [1025, 200, 40, 250], [1070, 150, 56, 300], [1135, 220, 44, 230], [1185, 160, 60, 290], [20, 260, 50, 200], [80, 220, 44, 240], [130, 290, 60, 170]].map(([x, y, w, h], i) => (
        <g key={i}>
          <rect x={x} y={y} width={w} height={h} fill={i % 2 ? "url(#fs-lit)" : "url(#fs-lit2)"} />
          <rect x={x} y={y} width={w} height={3} fill="#2a3350" />
        </g>
      ))}
      <rect x={0} y={330} width={VB.w} height={180} fill="url(#fs-sky)" opacity=".5" />

      {/* grounds */}
      <Poly p={floorQ(-5, -2, 46, 40, -0.8)} fill="#14201a" />
      <Poly p={floorQ(-3, 23, 39, 30, -0.8)} fill="#2a2724" />
      <Poly p={floorQ(14, 23, 22, 40, -0.8)} fill="#2a2724" />
      <Poly p={floorQ(-2, -2, 38, 24, -0.8)} fill="#1b1714" />
      <Box x={-1} y={-1} w={38} d={24} z={-0.8} h={0.8} top="#2c2621" left="#211c18" right="#1a1613" />

      {/* the base under the executive floor; its front face is the investment floor's back wall */}
      <Poly p={wallXQ(36, 0, G_Y0, 0, UP_Z)} fill="#1c1916" />
      <OnWallX x={36} y={G_Y0} z={UP_Z}>
        {[0, 1, 2, 3].map((i) => (
          <rect key={i} x={14 + i * 52} y={34} width={30} height={80} fill="url(#fs-night)" stroke="#3a2e26" strokeWidth="3" />
        ))}
      </OnWallX>
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-wall)" />
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-slat-y)" />
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-wash)" />
      {[3, 9, 15, 21, 27, 33].map((x) => (
        <g key={x}>
          <Glow x={x} y={G_Y0 + 0.2} z={4.6} r={1.5} color="url(#fs-bulb)" />
          <Poly p={wallYQ(G_Y0 + 0.05, x - 0.2, x + 0.2, 5.6, 6.2)} fill="#ffd48a" />
        </g>
      ))}
      {/* executive floor slab */}
      <Box x={0} y={0} w={36} d={G_Y0} z={UP_Z - 0.6} h={0.6} top="#3a2e26" left="#2a221d" right="#211b17" />
      <Poly p={floorQ(0, 0, 36, G_Y0, UP_Z)} fill="url(#fs-stone)" />
      <Poly p={floorQ(14, 0, 36, G_Y0, UP_Z)} fill="#5a4c40" op={0.35} />
      {/* executive floor walls */}
      <Poly p={wallXQ(0, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-wall2)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-wall)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-slat-y)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-wash)" />
      <Poly p={wallXQ(0, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-wash)" />
      <Poly p={[P(0, 0, UP_Z + UP_H), P(36, 0, UP_Z + UP_H), P(36, -0.6, UP_Z + UP_H), P(0, -0.6, UP_Z + UP_H)]} fill="#4a3b30" />
      <Poly p={[P(0, 0, UP_Z + UP_H), P(0, G_Y0, UP_Z + UP_H), P(-0.6, G_Y0, UP_Z + UP_H), P(-0.6, 0, UP_Z + UP_H)]} fill="#4a3b30" />
      {/* CIO's window onto the city */}
      <OnWallY x={2.5} y={0} z={UP_Z + 6.2}>
        <rect width={8.5 * K} height={4.6 * K} fill="url(#fs-night)" />
        {[[6, 30, 16, 62], [26, 14, 20, 78], [50, 40, 14, 52], [68, 22, 22, 70], [94, 46, 18, 46], [116, 10, 20, 82], [140, 34, 18, 58]].map(([x, y, w, h], i) => (
          <rect key={i} x={x} y={y} width={w} height={h} fill={i % 2 ? "url(#fs-lit)" : "url(#fs-lit2)"} />
        ))}
        <path d={`M${4.25 * K} 0 V${4.6 * K} M0 ${2.3 * K} H${8.5 * K}`} stroke="#3a2e26" strokeWidth="3" />
        <rect width={8.5 * K} height={4.6 * K} fill="none" stroke="#4a3b30" strokeWidth="5" />
      </OnWallY>
      {/* bookshelf along the CIO's side wall */}
      <OnWallX x={0} y={8.4} z={UP_Z + 5.6}>
        <rect width={7 * K} height={5.4 * K} fill="#2a1f19" stroke="#4a3a2e" strokeWidth="3" />
        {[0, 1, 2, 3].map((r) => (
          <g key={r}>
            <rect y={r * 27 + 25} width={7 * K} height={3} fill="#4a3a2e" />
            {Array.from({ length: 13 }, (_, i) => (
              <rect key={i} x={6 + i * 10} y={r * 27 + (i % 5 === 2 ? 10 : 6)} width={7} height={i % 5 === 2 ? 15 : 19} fill={["#6b2d2d", "#2d4a6b", "#8a7448", "#33553f", "#5a3a6b"][(i + r) % 5]} />
            ))}
          </g>
        ))}
      </OnWallX>
      {/* the firm's name in the lounge */}
      <OnWallY x={23} y={0} z={UP_Z + 6.3}>
        <rect width={12 * K} height={4.2 * K} rx={2} fill="#14110f" />
        <rect x={14} y={14} width={50} height={50} fill="none" stroke="#cdb27a" strokeWidth="2.5" />
        <text x={39} y={47} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="22" fill="#e4cf9f">537</text>
        <text x={80} y={36} fontFamily="Fraunces, Georgia, serif" fontSize="19" letterSpacing="3" fill="#e4cf9f">THE 537</text>
        <text x={80} y={60} fontFamily="Fraunces, Georgia, serif" fontSize="19" letterSpacing="3" fill="#e4cf9f">FIRM</text>
        <rect y={4.2 * K - 3} width={12 * K} height={3} fill="#cdb27a" opacity=".5" />
      </OnWallY>
      {/* sconces */}
      {[[16, UP_Z + 5], [21, UP_Z + 5], [36 - 0.5, UP_Z + 5]].map(([x, z], i) => (
        <g key={i}>
          <Glow x={x} y={0.2} z={z - 1.5} r={1.4} color="url(#fs-bulb)" />
          <Poly p={wallYQ(0.05, x - 0.25, x + 0.25, z - 0.3, z + 0.3)} fill="#ffd48a" />
        </g>
      ))}
      {/* lounge */}
      <Poly p={floorQ(17, 3, 31, 8.4, UP_Z)} fill="#3b2c24" stroke="#cdb27a" sw={1} op={0.95} />
      <Box x={18.5} y={0.6} w={8} d={1.6} z={UP_Z} h={0.9} top="#d8c9b0" left="#b9a88c" right="#a08f74" />
      <Box x={18.5} y={0.4} w={8} d={0.6} z={UP_Z} h={2.1} top="#e2d4bc" left="#c7b699" right="#ad9c80" />
      <Box x={20.5} y={4.6} w={3.6} d={2} z={UP_Z} h={0.7} top="#2a1f19" left="#1e1612" right="#18120f" />
      <Box x={28} y={3.4} w={1.8} d={2} z={UP_Z} h={0.9} top="#d8c9b0" left="#b9a88c" right="#a08f74" />
      <Box x={29.2} y={3.4} w={0.6} d={2} z={UP_Z} h={1.9} top="#e2d4bc" left="#c7b699" right="#ad9c80" />
      <Box x={16.2} y={6.2} w={1.8} d={1.8} z={UP_Z} h={0.9} top="#d8c9b0" left="#b9a88c" right="#a08f74" />
      <Glow x={24} y={5} z={UP_Z} r={5} />
      {(() => {
        const [lx, ly] = P(33.5, 2, UP_Z);
        return (
          <g>
            <line x1={lx} y1={ly} x2={lx} y2={ly - 72} stroke="#1b1f27" strokeWidth="2" />
            <path d={`M${lx - 9} ${ly - 70} h18 l-4 -14 h-10 Z`} fill="#f1d9a8" />
            <circle cx={lx} cy={ly - 70} r={26} fill="url(#fs-bulb)" />
          </g>
        );
      })()}
      <Plant x={15.6} y={1.2} z={UP_Z} s={1.1} />
      <Plant x={34.6} y={8.4} z={UP_Z} s={1.2} />
      <Plant x={27.4} y={1} z={UP_Z} s={0.9} />
      {/* CIO's office furnishings */}
      <Poly p={floorQ(2.2, 2.6, 11.6, 8.8, UP_Z)} fill="#3a2a22" op={0.85} />
      <Plant x={1.2} y={9} z={UP_Z} s={1.15} />
      <Plant x={12.6} y={1.2} z={UP_Z} s={1} />
      {(() => {
        const [gx, gy] = P(12, 7.5, UP_Z);
        return (
          <g transform={`translate(${gx} ${gy})`}>
            <path d="M-8 0 h16 l-3 -6 h-10 Z" fill="#6b5a3a" />
            <rect x={-1.2} y={-20} width={2.4} height={14} fill="#8a7448" />
            <circle cy={-30} r={12} fill="#2d5a7b" />
            <path d="M-7 -36 q5 3 3 8 q-3 3 1 6 M3 -40 q3 4 7 3" stroke="#5f9a6a" strokeWidth="3" fill="none" />
            <circle cy={-30} r={13.5} fill="none" stroke="#b8975a" strokeWidth="1.2" />
          </g>
        );
      })()}
      <Glow x={7} y={5} z={UP_Z} r={4.2} />
      <Poly p={wallXQ(14, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-glass)" stroke="#ffffff22" />

      {/* investment floor */}
      <Poly p={floorQ(0, G_Y0, 36, G_Y1, 0)} fill="url(#fs-plank)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-wall2)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-slat-x)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-wash)" />
      <Poly p={[P(0, G_Y0, UP_Z - 0.6), P(0, G_Y1, UP_Z - 0.6), P(-0.6, G_Y1, UP_Z - 0.6), P(-0.6, G_Y0, UP_Z - 0.6)]} fill="#4a3b30" />
    </g>
  );
});

function RoomDecor({ id, off }: { id: "tim" | "alex" | "max"; off: boolean }) {
  const r = ROOMS[id];
  const mid = (r.x0 + r.x1) / 2;
  return (
    <g className={off ? "fs-off" : undefined}>
      <Glow x={mid} y={15.5} z={0} r={off ? 3 : 4.6} />
      {id === "tim" && (
        <g>
          <Box x={1} y={G_Y0} w={4.4} d={0.9} z={0} h={4.6} top="#3a2a20" left="#2e2119" right="#241a14" />
          <OnWallY x={1.1} y={G_Y0 + 0.9} z={4.5}>
            {[0, 1, 2].map((row) => (
              <g key={row}>
                <rect y={row * 28 + 26} width={86} height={3} fill="#4a3a2e" />
                {Array.from({ length: 9 }, (_, i) => <rect key={i} x={4 + i * 9} y={row * 28 + 8} width={6.5} height={18} fill={["#2f6bd0", "#1f4f9e", "#8aa9d6", "#3b4a63"][(i + row) % 4]} />)}
              </g>
            ))}
          </OnWallY>
          <Plant x={10.8} y={G_Y0 + 1} z={0} s={1.2} />
          <Plant x={1} y={20.8} z={0} s={1.1} />
        </g>
      )}
      {id === "alex" && (
        <g>
          <OnWallY x={13.4} y={G_Y0} z={6}>
            <rect width={6 * K} height={3.2 * K} fill="#3a3046" stroke="#4a3b30" strokeWidth="3" />
            {[[8, 8, "#a78bfa"], [34, 12, "#fde68a"], [62, 6, "#93c5fd"], [90, 14, "#fca5a5"], [16, 34, "#a7f3d0"], [48, 36, "#fde68a"], [80, 38, "#c4b5fd"]].map(([x, y, c], i) => (
              <rect key={i} x={x as number} y={y as number} width={20} height={16} fill={c as string} opacity=".85" />
            ))}
          </OnWallY>
          <Box x={21.6} y={19.6} w={1.4} d={1.2} z={0} h={1} top="#a07c56" left="#8a6b4a" right="#765a3d" />
          <Box x={21.8} y={19.8} w={1} d={0.9} z={1} h={0.8} top="#b08a62" left="#9c7b57" right="#86684a" />
          <Plant x={23} y={G_Y0 + 1} z={0} s={1} />
        </g>
      )}
      {id === "max" && (
        <g>
          <OnWallY x={25} y={G_Y0} z={6.6}>
            <rect width={10 * K} height={18} fill="#07090d" />
            <text x={10 * K / 2} y={13} textAnchor="middle" fontSize="9" fill="#f59e0b" opacity=".35" letterSpacing="2.5" fontFamily="Inter, sans-serif">MARKET TAPE OFFLINE</text>
          </OnWallY>
          <OnWallY x={30.5} y={G_Y0} z={5}>
            <rect width={4.5 * K} height={2.6 * K} rx={2} fill="#0a0d13" stroke="#2b3242" strokeWidth="3" />
          </OnWallY>
          <Box x={25} y={20} w={1.6} d={1.2} z={0} h={1} top="#a07c56" left="#8a6b4a" right="#765a3d" />
          <Plant x={35} y={G_Y0 + 1} z={0} s={1.1} />
        </g>
      )}
    </g>
  );
}

function Monitor({ x, y, z, w = 1.5, h = 1.05, k, accent, off }: { x: number; y: number; z: number; w?: number; h?: number; k: AgentState["key"]; accent: string; off: boolean }) {
  const busy = STATE_META[k].working;
  const screen = k === "error" ? "#4a1020" : off ? "#07090d" : busy ? accent : "#1d3350";
  return (
    <g>
      <Poly p={[P(x + w / 2 - 0.1, y, z), P(x + w / 2 + 0.1, y, z), P(x + w / 2 + 0.1, y, z + 0.35), P(x + w / 2 - 0.1, y, z + 0.35)]} fill="#2b3242" />
      <Poly p={wallYQ(y, x, x + w, z + 0.35, z + 0.35 + h)} fill="#11151d" stroke="#2b3242" sw={1.5} />
      <Poly p={wallYQ(y + 0.01, x + 0.1, x + w - 0.1, z + 0.45, z + 0.25 + h)} fill={screen} op={off ? 1 : busy ? 0.55 : 0.8} className={busy ? "fs-pulse" : undefined} />
      {!off && !busy && k !== "error" && (
        <polyline points={pts(P(x + 0.2, y + 0.02, z + 0.7), P(x + 0.55, y + 0.02, z + 0.95), P(x + 0.85, y + 0.02, z + 0.8), P(x + 1.25, y + 0.02, z + 1.15))} fill="none" stroke={accent} strokeWidth="1.4" />
      )}
    </g>
  );
}

function Mug({ x, y, z, steam }: { x: number; y: number; z: number; steam: boolean }) {
  const [sx, sy] = P(x, y, z);
  return (
    <g transform={`translate(${sx} ${sy})`}>
      {steam && (
        <g stroke="#e2e8f0" strokeWidth="1" fill="none" opacity=".45">
          <path className="fs-steam" d="M-1 -9 q-2 -3 0 -6 q2 -3 0 -6" />
          <path className="fs-steam fs-delay" d="M2 -9 q-2 -3 0 -6 q2 -3 0 -6" />
        </g>
      )}
      <rect x={-3.5} y={-8} width={7} height={8} rx={1.5} fill="#ece6da" />
    </g>
  );
}

/** One person at their desk. Clickable; keyboard reachable. */
export function Workstation({ id, state, selected, onSelect, pending = 0 }: { id: AgentId; state: AgentState; selected: boolean; onSelect: () => void; pending?: number }) {
  const r = ROOMS[id];
  const { x, y, w, d } = r.desk;
  const z = r.z;
  const look: Look = LOOKS[id];
  const k = state.key;
  const off = k === "inactive";
  const pose = poseFor(k);
  const [ax, ay] = P(x + w / 2, y, z + DESK_H);
  const s = id === "cio" ? 0.78 : 0.74;
  const meta = STATE_META[k];
  const wood = id === "cio" ? { top: "#6b4430", left: "#4a2f22", right: "#3b2519" } : id === "max" ? { top: "#3d4452", left: "#2a2f3a", right: "#222631" } : { top: "#5a4232", left: "#3e2c22", right: "#33241b" };
  return (
    <g
      role="button"
      tabIndex={0}
      aria-label={`${id === "cio" ? "CIO" : id[0].toUpperCase() + id.slice(1)}: ${state.headline}. Open summary.`}
      onClick={onSelect}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onSelect())}
      className="fs-station cursor-pointer outline-none"
    >
      {/* hit area + selection ring on the floor */}
      <Poly p={floorQ(x - 1.4, y - 2.6, x + w + 1.2, y + d + 1.4, z)} fill="transparent" />
      <ellipse cx={ax} cy={P(x + w / 2, y + d / 2, z)[1]} rx={w * K * 1.05} ry={w * K * 0.5} fill="none" stroke={selected ? meta.color : "transparent"} strokeWidth="2" strokeDasharray="5 5" className={selected ? "fs-flow" : "fs-ring"} />
      {meta.working && <Glow x={x + w / 2} y={y + d / 2} z={z} r={2.6} color={`url(#fs-state-${k})`} className="fs-pulse" />}
      <g className={off ? "fs-off" : undefined}>
        <g transform={`translate(${ax} ${ay}) scale(${s})`}>
          <rect x={-34} y={-2} width={68} height={22} fill={look.top} />
          <Body look={look} pose={pose} working={meta.working} />
        </g>
        <Box x={x} y={y} z={z} w={w} d={d} h={DESK_H} top={wood.top} left={wood.left} right={wood.right} />
        {id !== "max" && <Mug x={x + 0.9} y={y + 1.2} z={z + DESK_H} steam={!off} />}
        {id === "cio" && (
          <g>
            {Array.from({ length: Math.min(pending, 6) }, (_, i) => (
              <Poly key={i} p={floorQ(x + w - 1.3, y + 1.1, x + w - 0.3, y + 1.7, z + DESK_H + 0.06 + i * 0.07)} fill="#f4f1e8" stroke="#cbbf9f" sw={0.5} />
            ))}
            {(() => {
              const [lx, ly] = P(x + 0.5, y + 0.4, z + DESK_H);
              return (
                <g transform={`translate(${lx} ${ly})`}>
                  <path d="M0 0 L-3 -18 L8 -26" stroke="#b8975a" strokeWidth="2" fill="none" />
                  <path d="M4 -30 l13 3 l-5 8 Z" fill="#2f5a3f" />
                  <circle cx={10} cy={-20} r={14} fill="url(#fs-bulb)" opacity=".7" />
                </g>
              );
            })()}
          </g>
        )}
        <g transform={`translate(${ax} ${ay}) scale(${s})`}>
          <Hands look={look} pose={pose} />
        </g>
        <Monitor x={x + w - 1.85} y={y + 0.3} z={z + DESK_H} w={1.7} h={1.15} k={k} accent={look.accent} off={off} />
        {id === "max" && <Monitor x={x + 0.15} y={y + 0.3} z={z + DESK_H} w={1.7} h={1.15} k={k} accent={look.accent} off={off} />}
      </g>
      {off && (
        <g transform={`translate(${P(x + w / 2, y + d + 1.3, z)[0]} ${P(x + w / 2, y + d + 1.3, z)[1]})`} fontFamily="Inter, sans-serif">
          <path d="M-34 -2 L-38 16 M34 -2 L38 16" stroke="#d4a017" strokeWidth="2.5" />
          <rect x={-42} y={-12} width={84} height={14} rx={2} fill="#d4a017" />
          <text y={-2} textAnchor="middle" fontSize="7.5" fontWeight="700" fill="#141414" letterSpacing="1">
            OPENS IN PHASE {id === "alex" ? 3 : 4}
          </text>
        </g>
      )}
    </g>
  );
}

/** Glass wall between two investment-floor offices; drawn between their contents for correct depth. */
export function Partition({ x }: { x: number }) {
  return <Poly p={wallXQ(x, G_Y0, G_Y1, 0, UP_Z - 1.2)} fill="url(#fs-glass)" stroke="#ffffff1f" />;
}

/** Glass partitions, railings, entrance sign, trees: everything in front of the people. */
export const Foreground = memo(function Foreground() {
  return (
    <g pointerEvents="none">
      <Poly p={wallXQ(36, G_Y0, G_Y1, 0, UP_Z - 1.2)} fill="url(#fs-glass)" stroke="#ffffff26" />
      <Poly p={wallXQ(36, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-glass)" stroke="#ffffff26" />
      {/* executive floor balustrade */}
      <Poly p={wallYQ(G_Y0, 0, 36, UP_Z, UP_Z + 1.1)} fill="url(#fs-glass)" />
      <polyline points={pts(P(0, G_Y0, UP_Z + 1.1), P(36, G_Y0, UP_Z + 1.1))} stroke="#cdb27a" strokeOpacity=".6" strokeWidth="2" />
      <polyline points={pts(P(36, 0, UP_Z + 1.1), P(36, G_Y0, UP_Z + 1.1))} stroke="#cdb27a" strokeOpacity=".6" strokeWidth="2" />
      {/* investment floor front rail and planters */}
      <Poly p={wallYQ(G_Y1, 0, 36, 0, 1.1)} fill="url(#fs-glass)" />
      <polyline points={pts(P(0, G_Y1, 1.1), P(36, G_Y1, 1.1))} stroke="#cdb27a" strokeOpacity=".55" strokeWidth="2" />
      <Box x={0} y={G_Y1} w={36} d={0.6} z={0} h={0.4} top="#3a2e26" left="#241d18" right="#1d1814" />
      {[2.5, 9.5, 26.5, 33.5].map((x) => (
        <g key={x}>
          <Box x={x - 1.2} y={G_Y1 + 0.8} w={2.4} d={1.2} z={-0.8} h={1.2} top="#2a2521" left="#1f1b18" right="#191613" />
          <Plant x={x} y={G_Y1 + 1.4} z={0.4} s={1.05} />
        </g>
      ))}
      {/* entrance monument */}
      <Box x={14} y={25} w={8} d={1} z={-0.8} h={2.6} top="#2a2521" left="#141110" right="#0f0d0c" />
      <OnWallY x={14} y={26} z={1.8}>
        <rect x={10} y={6} width={30} height={30} fill="none" stroke="#cdb27a" strokeWidth="2" />
        <text x={25} y={26} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="12" fill="#e4cf9f">537</text>
        <text x={50} y={26} fontFamily="Fraunces, Georgia, serif" fontSize="13" letterSpacing="2.5" fill="#e4cf9f">THE 537 FIRM</text>
      </OnWallY>
      <Glow x={18} y={27} z={-0.8} r={3} />
      <StreetLamp x={12} y={30} />
      <StreetLamp x={24} y={30} />
      <StreetLamp x={39} y={9} />
      <Tree x={-2} y={24} s={1.1} />
      <Tree x={38.5} y={20} s={1.05} />
      <Tree x={38.5} y={4} s={1.15} />
      <Tree x={6} y={31} s={0.95} />
      <Tree x={30} y={31} s={1} />
    </g>
  );
});

export function StateGradients() {
  return (
    <defs>
      {(Object.keys(STATE_META) as AgentState["key"][]).map((k) => (
        <radialGradient key={k} id={`fs-state-${k}`}>
          <stop offset="0" stopColor={STATE_META[k].color} stopOpacity=".45" />
          <stop offset="1" stopColor={STATE_META[k].color} stopOpacity="0" />
        </radialGradient>
      ))}
    </defs>
  );
}

export { Shell, RoomDecor };
