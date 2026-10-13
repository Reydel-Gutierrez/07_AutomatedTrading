// The Forex Division floor of the HQ Tower and the Academy Annex, drawn with the HQ
// Tower's projection and parts. Neither engine has live records yet, so every desk is
// empty and every screen is dark; the only moving facts are the clocks and the forex
// market schedule.

import { memo, type ReactNode } from "react";
import { Box, Foreground, G_Y0, G_Y1, Glow, OnWallX, OnWallY, P, Plant, Poly, SceneDefs, Sign, Sky, StreetLamp, Tree, UP_Z, floorQ, wallXQ, wallYQ, K } from "./Building";
import { type Floor, type Zone, SESSIONS, fxMarketOpen, localTime, sessionOpen } from "./floors";
import { Body, Chair, Hands, type Look, type Pose } from "./Character";
import { type AcademySpace, type AcademySpaceAgent } from "./state";

const W = 36; // floor width (x)
const D = 22; // floor depth (y)
const H = 7.5; // wall height

// -- shared parts --------------------------------------------------------------------

function Grounds() {
  return (
    <g>
      <Poly p={floorQ(-5, -2, 46, 40, -0.8)} fill="#14201a" />
      <Poly p={floorQ(-3, 23, 39, 30, -0.8)} fill="#2a2724" />
      <Poly p={floorQ(14, 23, 22, 40, -0.8)} fill="#2a2724" />
      <Box x={-1} y={-1} w={W + 2} d={D + 2} z={-0.8} h={0.8} top="#2c2621" left="#211c18" right="#1a1613" />
    </g>
  );
}

function Walls({ windows = true }: { windows?: boolean }) {
  return (
    <g>
      <Poly p={wallYQ(0, 0, W, 0, H)} fill="url(#fs-wall)" />
      <Poly p={wallYQ(0, 0, W, 0, H)} fill="url(#fs-slat-y)" />
      <Poly p={wallYQ(0, 0, W, 0, H)} fill="url(#fs-wash)" />
      <Poly p={wallXQ(0, 0, D, 0, H)} fill="url(#fs-wall2)" />
      <Poly p={wallXQ(0, 0, D, 0, H)} fill="url(#fs-slat-x)" />
      <Poly p={wallXQ(0, 0, D, 0, H)} fill="url(#fs-wash)" />
      <Poly p={[P(0, 0, H), P(W, 0, H), P(W, -0.6, H), P(0, -0.6, H)]} fill="#4a3b30" />
      <Poly p={[P(0, 0, H), P(0, D, H), P(-0.6, D, H), P(-0.6, 0, H)]} fill="#4a3b30" />
      {windows && (
        <OnWallX x={0} y={D - 1} z={5.6}>
          {[0, 1, 2, 3, 4].map((i) => (
            <rect key={i} x={i * 76} y={0} width={46} height={66} fill="url(#fs-night)" stroke="#3a2e26" strokeWidth="3" />
          ))}
        </OnWallX>
      )}
    </g>
  );
}

function FrontGlass({ name }: { name: string }) {
  return (
    <g pointerEvents="none">
      <Poly p={wallXQ(W, 0, D, 0, H - 0.6)} fill="url(#fs-glass)" stroke="#ffffff26" />
      <Poly p={wallYQ(D, 0, W, 0, 1.1)} fill="url(#fs-glass)" />
      <polyline points={[P(0, D, 1.1), P(W, D, 1.1)].map((p) => p.join(",")).join(" ")} stroke="#cdb27a" strokeOpacity=".55" strokeWidth="2" fill="none" />
      <Box x={0} y={D} w={W} d={0.6} z={0} h={0.4} top="#3a2e26" left="#241d18" right="#1d1814" />
      {[2.5, 9.5, 26.5, 33.5].map((x) => (
        <g key={x}>
          <Box x={x - 1.2} y={D + 0.8} w={2.4} d={1.2} z={-0.8} h={1.2} top="#2a2521" left="#1f1b18" right="#191613" />
          <Plant x={x} y={D + 1.4} z={0.4} s={1.05} />
        </g>
      ))}
      <Box x={14} y={25} w={8} d={1} z={-0.8} h={2.6} top="#2a2521" left="#141110" right="#0f0d0c" />
      <OnWallY x={14} y={26} z={1.8}>
        <rect x={10} y={6} width={30} height={30} fill="none" stroke="#cdb27a" strokeWidth="2" />
        <text x={25} y={26} textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="12" fill="#e4cf9f">537</text>
        <text x={50} y={26} fontFamily="Fraunces, Georgia, serif" fontSize="12" letterSpacing="2" fill="#e4cf9f">{name.toUpperCase()}</text>
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
}

/** An empty desk: chair pushed in behind it, dark screens, and an "open" plate on the front. */
function EmptyDesk({ x, y, z = 0, w, d, screens = 1, tone = "#3d4452", plate }: { x: number; y: number; z?: number; w: number; d: number; screens?: number; tone?: string; plate?: string }) {
  const cx = x + w / 2;
  const mw = Math.min(1.5, (w - 0.6) / screens - 0.1);
  return (
    <g className="fs-off-soft">
      <Box x={cx - 0.55} y={y - 1.15} w={1.1} d={0.25} z={z} h={2.1} top="#2b2f3a" left="#1f232c" right="#181b22" />
      <Box x={cx - 0.55} y={y - 1.0} w={1.1} d={0.95} z={z} h={0.95} top="#323744" left="#242833" right="#1c1f28" />
      <Box x={x} y={y} w={w} d={d} z={z} h={1.45} top={tone} left="#2a2f3a" right="#222631" />
      {Array.from({ length: screens }, (_, i) => {
        const mx = cx - (screens * (mw + 0.1)) / 2 + i * (mw + 0.1);
        return (
          <g key={i}>
            <Poly p={[P(mx + mw / 2 - 0.08, y + 0.25, z + 1.45), P(mx + mw / 2 + 0.08, y + 0.25, z + 1.45), P(mx + mw / 2 + 0.08, y + 0.25, z + 1.75), P(mx + mw / 2 - 0.08, y + 0.25, z + 1.75)]} fill="#2b3242" />
            <Poly p={wallYQ(y + 0.25, mx, mx + mw, z + 1.75, z + 2.75)} fill="#0b0e14" stroke="#2b3242" sw={1.2} />
          </g>
        );
      })}
      {plate && (
        <OnWallY x={cx - 1} y={y + d} z={z + 1.05}>
          <rect width={2 * K} height={11} rx={2} fill="#14110f" stroke="#cdb27a" strokeOpacity=".5" />
          <text x={K} y={8.2} textAnchor="middle" fontSize="6.5" fontWeight="700" letterSpacing="1" fill="#cdb27a" opacity=".85" fontFamily="Inter, sans-serif">
            {plate}
          </text>
        </OnWallY>
      )}
    </g>
  );
}

function WallClock({ x, z, city, now, open }: { x: number; z: number; city: string; now: number; open: boolean }) {
  return (
    <OnWallY x={x} y={0} z={z}>
      <circle cx={K} cy={K} r={K * 0.85} fill="#0f1218" stroke={open ? "#34d399" : "#4a3b30"} strokeWidth="3" />
      {Array.from({ length: 12 }, (_, i) => (
        <line key={i} x1={K} y1={K * 0.28} x2={K} y2={K * 0.4} stroke="#cbd5e1" strokeWidth="1.2" transform={`rotate(${i * 30} ${K} ${K})`} />
      ))}
      <ClockHands now={now} city={city} />
      <text x={K} y={K * 2.35} textAnchor="middle" fontSize="9" fontWeight="700" letterSpacing="1.4" fill="#e5e7eb" fontFamily="Inter, sans-serif">{city.toUpperCase()}</text>
      <text x={K} y={K * 2.95} textAnchor="middle" fontSize="8" letterSpacing="1.2" fill={open ? "#34d399" : "#94a3b8"} fontFamily="Inter, sans-serif">{open ? "SESSION OPEN" : "CLOSED"}</text>
    </OnWallY>
  );
}

function ClockHands({ now, city }: { now: number; city: string }) {
  const tz = SESSIONS.find((s) => s.city === city)!.tz;
  const { hour, minute } = localTime(now, tz);
  return (
    <g stroke="#e5e7eb" strokeLinecap="round">
      <line x1={K} y1={K} x2={K} y2={K * 0.52} strokeWidth="2.2" transform={`rotate(${(hour % 12) * 30 + minute / 2} ${K} ${K})`} />
      <line x1={K} y1={K} x2={K} y2={K * 0.32} strokeWidth="1.4" transform={`rotate(${minute * 6} ${K} ${K})`} />
    </g>
  );
}

function Zones({ floor, active, onPick }: { floor: Floor; active: string | null; onPick: (z: Zone) => void }) {
  return (
    <g>
      {floor.zones.map((z) => (
        <g
          key={z.id}
          role="button"
          tabIndex={0}
          aria-label={`${z.name}. ${z.today} Open details.`}
          onClick={() => onPick(z)}
          onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onPick(z))}
          className={`fs-zone outline-none ${active === z.id ? "fs-zone-on" : ""}`}
        >
          <Poly p={floorQ(z.rect[0], z.rect[1], z.rect[2], z.rect[3], (z.z ?? 0) + 0.02)} fill="transparent" stroke="transparent" sw={2} />
        </g>
      ))}
    </g>
  );
}

function Scene({ children, label }: { children: ReactNode; label: string }) {
  return (
    <g aria-label={label}>
      <SceneDefs />
      <Sky />
      {children}
    </g>
  );
}

// -- HQ Tower: Forex Division floor ----------------------------------------------------
// Same tower geometry as the Executive / Core Investments view: a market room on the upper
// level and four glass-walled offices below, one per live chair. Chairs are few on purpose:
// only agents promoted from the Academy, with the owner's authorization, sit here.

export const FX_CHAIRS = [
  { id: "day-1", name: "Day Chair 1", style: "day", x0: 0, x1: 9 },
  { id: "day-2", name: "Day Chair 2", style: "day", x0: 9, x1: 18 },
  { id: "swing-1", name: "Swing Chair 1", style: "swing", x0: 18, x1: 27 },
  { id: "swing-2", name: "Swing Chair 2", style: "swing", x0: 27, x1: 36 },
] as const;

const UP_H = 7;

const ForexTower = memo(function ForexTower() {
  return (
    <g>
      {/* grounds and plinth (as the HQ view) */}
      <Poly p={floorQ(-5, -2, 46, 40, -0.8)} fill="#14201a" />
      <Poly p={floorQ(-3, 23, 39, 30, -0.8)} fill="#2a2724" />
      <Poly p={floorQ(14, 23, 22, 40, -0.8)} fill="#2a2724" />
      <Poly p={floorQ(-2, -2, 38, 24, -0.8)} fill="#1b1714" />
      <Box x={-1} y={-1} w={38} d={24} z={-0.8} h={0.8} top="#2c2621" left="#211c18" right="#1a1613" />

      {/* base under the market room; its front face is the chairs' back wall */}
      <Poly p={wallXQ(36, 0, G_Y0, 0, UP_Z)} fill="#1c1916" />
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-wall)" />
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-slat-y)" />
      <Poly p={wallYQ(G_Y0, 0, 36, 0, UP_Z)} fill="url(#fs-wash)" />
      {[4.5, 13.5, 22.5, 31.5].map((x) => (
        <g key={x}>
          <Glow x={x} y={G_Y0 + 0.2} z={4.6} r={1.5} color="url(#fs-bulb)" />
          <Poly p={wallYQ(G_Y0 + 0.05, x - 0.2, x + 0.2, 5.6, 6.2)} fill="#ffd48a" />
        </g>
      ))}

      {/* market room (upper level) */}
      <Box x={0} y={0} w={36} d={G_Y0} z={UP_Z - 0.6} h={0.6} top="#3a2e26" left="#2a221d" right="#211b17" />
      <Poly p={floorQ(0, 0, 36, G_Y0, UP_Z)} fill="url(#fs-stone)" />
      <Poly p={floorQ(1, 2.5, 35, 9, UP_Z)} fill="#1c2635" op={0.85} />
      <Poly p={wallXQ(0, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-wall2)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-wall)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-slat-y)" />
      <Poly p={wallYQ(0, 0, 36, UP_Z, UP_Z + UP_H)} fill="url(#fs-wash)" />
      <Poly p={wallXQ(0, 0, G_Y0, UP_Z, UP_Z + UP_H)} fill="url(#fs-wash)" />
      <Poly p={[P(0, 0, UP_Z + UP_H), P(36, 0, UP_Z + UP_H), P(36, -0.6, UP_Z + UP_H), P(0, -0.6, UP_Z + UP_H)]} fill="#4a3b30" />
      <Poly p={[P(0, 0, UP_Z + UP_H), P(0, G_Y0, UP_Z + UP_H), P(-0.6, G_Y0, UP_Z + UP_H), P(-0.6, 0, UP_Z + UP_H)]} fill="#4a3b30" />
      <OnWallX x={0} y={G_Y0 - 0.6} z={UP_Z + 5.4}>
        {[0, 1, 2].map((i) => (
          <rect key={i} x={i * 56} y={0} width={36} height={70} fill="url(#fs-night)" stroke="#3a2e26" strokeWidth="3" />
        ))}
      </OnWallX>
      <OnWallY x={1.2} y={0} z={UP_Z + 6.6}>
        <rect width={11.6 * K} height={1.5 * K} rx={2} fill="#14110f" />
        <text x={14} y={21} fontFamily="Fraunces, Georgia, serif" fontSize="17" letterSpacing="3" fill="#e4cf9f">FOREX DIVISION</text>
        <rect y={1.5 * K - 3} width={11.6 * K} height={3} fill="#cdb27a" opacity=".5" />
      </OnWallY>
      <OnWallY x={14.2} y={0} z={UP_Z + 6.4}>
        <rect width={12.6 * K} height={4.1 * K} rx={3} fill="#07090d" stroke="#2b3242" strokeWidth="4" />
        <text x={14} y={22} fontSize="11" fontWeight="700" letterSpacing="2.4" fill="#f59e0b" opacity=".85" fontFamily="Inter, sans-serif">LIVE RATES · OANDA</text>
      </OnWallY>
      <OnWallY x={28.4} y={0} z={UP_Z + 6}>
        <rect width={6.6 * K} height={3.2 * K} rx={2} fill="#efe9dc" opacity=".9" />
        {["MON", "TUE", "WED", "THU", "FRI", "WKND"].map((d, i) => (
          <g key={d}>
            <rect x={6 + i * 21} y={8} width={19} height={46} fill={d === "WKND" ? "#e6d3b0" : "#fff"} stroke="#c9bfa9" />
            <text x={15.5 + i * 21} y={18} textAnchor="middle" fontSize="6" fontWeight="700" fill="#57534e" fontFamily="Inter, sans-serif">{d}</text>
          </g>
        ))}
      </OnWallY>
      <Glow x={18} y={5} z={UP_Z} r={7} />
      <EmptyDesk x={6} y={5.6} z={UP_Z} w={9} d={1.6} screens={4} />
      <EmptyDesk x={21} y={5.6} z={UP_Z} w={9} d={1.6} screens={4} tone="#4a3b5e" />
      <Plant x={1.2} y={9} z={UP_Z} s={1.1} />
      <Plant x={34.6} y={8.4} z={UP_Z} s={1.2} />

      {/* chairs floor (ground level) */}
      <Poly p={floorQ(0, G_Y0, 36, G_Y1, 0)} fill="url(#fs-plank)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-wall2)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-slat-x)" />
      <Poly p={wallXQ(0, G_Y0, G_Y1, 0, UP_Z - 0.6)} fill="url(#fs-wash)" />
      <Poly p={[P(0, G_Y0, UP_Z - 0.6), P(0, G_Y1, UP_Z - 0.6), P(-0.6, G_Y1, UP_Z - 0.6), P(-0.6, G_Y0, UP_Z - 0.6)]} fill="#4a3b30" />
      {FX_CHAIRS.map((c, i) => (
        <g key={c.id}>
          <Glow x={(c.x0 + c.x1) / 2} y={15.5} z={0} r={3.4} />
          <Poly p={floorQ(c.x0 + 0.8, G_Y0 + 2.6, c.x1 - 0.8, G_Y1 - 1.6, 0)} fill={c.style === "day" ? "#1c2635" : "#2a2236"} op={0.75} />
          <OnWallY x={c.x0 + 1.4} y={G_Y0} z={6.4}>
            <rect width={6 * K} height={18} fill="#07090d" />
            <text x={3 * K} y={13} textAnchor="middle" fontSize="8.5" fill={c.style === "day" ? "#f59e0b" : "#c4b5fd"} opacity=".5" letterSpacing="2" fontFamily="Inter, sans-serif">
              {c.style === "day" ? "INTRADAY · FLAT BY FRIDAY" : "MULTI-DAY · WEEKEND RULES"}
            </text>
          </OnWallY>
          <EmptyDesk x={c.x0 + 1.7} y={14.4} w={5.6} d={1.9} screens={c.style === "day" ? 2 : 1} tone={c.style === "day" ? "#3d4452" : "#4a3b5e"} plate="OPEN CHAIR" />
          <Plant x={c.x1 - 0.9} y={G_Y0 + 1} z={0} s={1} />
          {i > 0 && <Poly p={wallXQ(c.x0, G_Y0, G_Y1, 0, UP_Z - 1.2)} fill="url(#fs-glass)" stroke="#ffffff1f" />}
        </g>
      ))}
    </g>
  );
});

const BOARD = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", "USD_CAD", "EUR_GBP"];

export function ForexFloor({ floor, now, active, onPick, labels, academy }: { floor: Floor; now: number; active: string | null; onPick: (z: Zone) => void; labels: boolean; academy?: AcademySpace }) {
  const fxOpen = fxMarketOpen(now);
  const prices = (academy?.prices ?? []).filter((p) => BOARD.includes(p.instrument));
  const fresh = prices.length > 0 && now / 1000 - Math.max(...prices.map((p) => p.time)) < 900;
  return (
    <Scene label="HQ Tower, Forex Division floor">
      <ForexTower />
      {/* the clocks and market status are the only live facts on this floor */}
      {SESSIONS.map((s, i) => (
        <WallClock key={s.city} x={1.6 + i * 4} z={UP_Z + 4.8} city={s.city} now={now} open={sessionOpen(now, s)} />
      ))}
      <OnWallY x={14.2} y={0} z={UP_Z + 6.4}>
        <text x={12.6 * K - 14} y={46} textAnchor="end" fontSize="9.5" fontWeight="700" letterSpacing="1.4" fill={fxOpen ? "#34d399" : "#94a3b8"} fontFamily="Inter, sans-serif">
          {fxOpen ? "FX MARKET OPEN" : "FX MARKET CLOSED"}
        </text>
        {prices.length ? (
          prices.map((p, i) => {
            const d = p.instrument.endsWith("JPY") ? 3 : 5;
            return (
              <g key={p.instrument} fontFamily="Inter, sans-serif">
                <text x={14 + (i % 2) * 124} y={44 + Math.floor(i / 2) * 13} fontSize="8.5" fill={fresh ? "#cbd5e1" : "#64748b"} letterSpacing=".8">{p.instrument.replace("_", "/")}</text>
                <text x={70 + (i % 2) * 124} y={44 + Math.floor(i / 2) * 13} fontSize="8.5" fill={fresh ? "#34d399" : "#64748b"}>{((p.bid + p.ask) / 2).toFixed(d)}</text>
              </g>
            );
          })
        ) : (
          <text x={14} y={46} fontSize="10" fill="#94a3b8" fontFamily="Inter, sans-serif">{academy?.available ? "Waiting for prices" : "Forex engine not running"}</text>
        )}
        {prices.length > 0 && !fresh && <text x={14} y={80} fontSize="7.5" fill="#94a3b8" fontFamily="Inter, sans-serif">last prices before the market closed</text>}
      </OnWallY>
      <Foreground />
      <Zones floor={floor} active={active} onPick={onPick} />
      {labels && (
        <g>
          <Sign at={[165, 190]} to={P(0, 4, UP_Z + 3)} title="Market room" sub="Upper level · rates, sessions, desk heads" />
          <Sign at={[150, 575]} title="Forex Division" sub="HQ Tower · OANDA · 4 live chairs, all open" />
          {FX_CHAIRS.map((c) => (
            <Sign key={c.id} at={P(c.x0 + 6.5, G_Y0, UP_Z - 1.3)} title={c.name} sub={c.style === "day" ? "Intraday · open chair" : "Multi-day · open chair"} />
          ))}
        </g>
      )}
    </Scene>
  );
}

// -- The 537 Academy ---------------------------------------------------------------------

// Trading Hall seats: the founding class takes the first four; the rest wait for new agents.
const SEATS = [12.4, 17.2].flatMap((y) => [1.8, 10.4, 19, 27.6].map((x) => ({ x, y })));
const SEAT_W = 4.6;
const SEAT_D = 1.5;

const AcademyRoom = memo(function AcademyRoom() {
  return (
    <g>
      <Grounds />
      <Poly p={floorQ(0, 0, W, D, 0)} fill="url(#fs-plank)" />
      <Poly p={floorQ(0, 0, 13, 9, 0)} fill="#2a3340" op={0.85} />
      <Poly p={floorQ(13, 0, 22, 9, 0)} fill="#3b2c24" op={0.8} />
      <Poly p={floorQ(1, 10.8, 35, 21, 0)} fill="#33302a" op={0.55} />
      <Walls />
      {/* the Academy's name on the side wall */}
      <OnWallX x={0} y={D - 0.6} z={7.2}>
        <rect width={11 * K} height={1.6 * K} rx={2} fill="#14110f" />
        <text x={14} y={22} fontFamily="Fraunces, Georgia, serif" fontSize="17" letterSpacing="3" fill="#e4cf9f">THE 537 ACADEMY</text>
        <rect y={1.6 * K - 3} width={11 * K} height={3} fill="#cdb27a" opacity=".5" />
      </OnWallX>
      {/* Strategy Lab: whiteboard, racks, workbenches */}
      <OnWallY x={4.4} y={0} z={6.3}>
        <rect width={7.6 * K} height={3.4 * K} rx={2} fill="#eef0ea" opacity=".92" />
        <text x={10} y={18} fontSize="9" fontWeight="700" letterSpacing="1.4" fill="#334155" fontFamily="Inter, sans-serif">VALIDATION QUEUE</text>
        <path d="M10 24 h120" stroke="#cbd5e1" strokeWidth="1.5" />
      </OnWallY>
      {[0.8, 2.5].map((x) => (
        <Box key={x} x={x} y={0.5} w={1.4} d={1.3} z={0} h={4.2} top="#1f2430" left="#151922" right="#10131a" />
      ))}
      <Box x={1.2} y={5} w={4.6} d={1.4} z={0} h={1.4} top="#5a6474" left="#3e4552" right="#333945" />
      <Box x={7} y={5} w={4.6} d={1.4} z={0} h={1.4} top="#5a6474" left="#3e4552" right="#333945" />
      <Poly p={wallYQ(9, 0, 13, 0, 3.4)} fill="url(#fs-glass)" stroke="#ffffff1f" />
      <Poly p={wallXQ(13, 0, 9, 0, H - 0.3)} fill="url(#fs-glass)" stroke="#ffffff1f" />
      {/* Research Lead's office */}
      <OnWallY x={14.2} y={0} z={6.2}>
        <rect width={6.6 * K} height={4.6 * K} fill="#2a1f19" stroke="#4a3a2e" strokeWidth="3" />
        {[0, 1, 2].map((r) => (
          <g key={r}>
            <rect y={r * 30 + 28} width={6.6 * K} height={3} fill="#4a3a2e" />
            {Array.from({ length: 12 }, (_, i) => (
              <rect key={i} x={5 + i * 10.5} y={r * 30 + 8} width={7} height={20} fill={["#6b2d2d", "#2d4a6b", "#8a7448", "#33553f"][(i + r) % 4]} />
            ))}
          </g>
        ))}
      </OnWallY>
      <EmptyDesk x={15.4} y={4.6} w={4.2} d={1.6} tone="#6b4430" />
      <Poly p={wallYQ(9, 13, 22, 0, 3.4)} fill="url(#fs-glass)" stroke="#ffffff1f" />
      <Poly p={wallXQ(22, 0, 9, 0, H - 0.3)} fill="url(#fs-glass)" stroke="#ffffff1f" />
      {/* Alumni wall: empty frames */}
      <OnWallY x={23.2} y={0} z={6.6}>
        {Array.from({ length: 10 }, (_, i) => (
          <g key={i} transform={`translate(${(i % 5) * 50} ${Math.floor(i / 5) * 54})`}>
            <rect width={38} height={44} fill="#1a1512" stroke="#b8975a" strokeWidth="2.5" />
            <rect x={5} y={5} width={28} height={34} fill="#0f0d0c" />
          </g>
        ))}
      </OnWallY>
      <Box x={25.5} y={5.6} w={7} d={1} z={0} h={0.8} top="#6b4430" left="#4a2f22" right="#3b2519" />
      <Plant x={34.8} y={1.2} z={0} s={1.1} />
      <Plant x={23} y={8.4} z={0} s={0.9} />
      {/* Trading Hall */}
      <Glow x={18} y={15} z={0} r={9} />
      <Plant x={35.2} y={21} z={0} s={1.1} />
    </g>
  );
});

/** Server racks in the Strategy Lab: lit and blinking only while a validation or review is running. */
function Rigs({ busy }: { busy: boolean }) {
  return (
    <g>
      {[0.8, 2.5].map((x) => (
        <OnWallY key={x} x={x} y={1.8} z={4}>
          {Array.from({ length: 7 }, (_, i) => (
            <g key={i}>
              <rect x={4} y={6 + i * 10} width={20} height={4} rx={1} fill="#1e293b" />
              <circle cx={7} cy={8 + i * 10} r={1.4} fill={busy ? (i % 3 ? "#34d399" : "#38bdf8") : "#334155"} className={busy ? "fs-pulse" : undefined} style={busy ? { animationDelay: `${i * 0.17}s` } : undefined} />
            </g>
          ))}
        </OnWallY>
      ))}
    </g>
  );
}

const POSE: Record<string, Pose> = { trading: "typing", analyzing: "typing", watching: "reading", error: "alert" };

/** One Academy agent at its desk. Its posture follows the engine's recorded activity. */
function TraderDesk({ x, y, a, onPick }: { x: number; y: number; a: AcademySpaceAgent; onPick: (id: string) => void }) {
  const look = { chair: "#1f2633", skin: "#d9a67c", hair: "#2b2523", hairStyle: "neat", top: "#334155", inner: "#e2e8f0", accent: a.persona.color, ...(a.persona.look ?? {}) } as Look;
  const paused = !!a.paused || a.status === "PAUSED";
  const pose: Pose = paused ? "idle" : POSE[a.activity ?? ""] ?? "idle";
  const feeling = a.return_pct <= -0.05 ? "stressed" : a.return_pct >= 0.02 ? "pleased" : "calm";
  const s = 0.62;
  const [ax, ay] = P(x + SEAT_W / 2, y, 1.45);
  const busy = pose === "typing";
  return (
    <g role="button" tabIndex={0} aria-label={`${a.name}: open Academy record`} className="fs-station cursor-pointer outline-none" onClick={() => onPick(a.id)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onPick(a.id))}>
      <Poly p={floorQ(x - 1, y - 2.4, x + SEAT_W + 1, y + SEAT_D + 1.2, 0.03)} fill="transparent" />
      {busy && <Glow x={x + SEAT_W / 2} y={y + SEAT_D / 2} z={0} r={2.4} color="url(#fs-warm)" className="fs-pulse" />}
      <g className={paused ? "fs-off" : undefined}>
        <g transform={`translate(${ax} ${ay}) scale(${s})`}>
          <Chair look={look} />
        </g>
        <g transform={`translate(${ax} ${ay}) scale(${s})`}>
          <Body look={look} pose={pose} working={busy} feeling={feeling} gaze={busy ? "monitor" : "wander"} chair={false} />
        </g>
        <Box x={x} y={y} w={SEAT_W} d={SEAT_D} z={0} h={1.45} top="#4a3b30" left="#33281f" right="#2a2019" />
        <g transform={`translate(${ax} ${ay}) scale(${s})`}>
          <Hands look={look} pose={pose} />
        </g>
        {[x + SEAT_W - 1.75, x + 0.25].map((mx, i) => (
          <g key={i}>
            <Poly p={wallYQ(y + 0.3, mx, mx + 1.5, 1.8, 2.85)} fill="#0b0e14" stroke="#2b3242" sw={1.2} />
            <Poly p={wallYQ(y + 0.31, mx + 0.1, mx + 1.4, 1.9, 2.75)} fill={busy ? a.persona.color : "#13243c"} op={busy ? 0.45 : 0.85} className={busy ? "fs-pulse" : undefined} />
          </g>
        ))}
        <OnWallY x={x + SEAT_W / 2 - 1.2} y={y + SEAT_D} z={1.05}>
          <rect width={2.4 * K} height={11} rx={2} fill="#14110f" stroke={a.persona.color} strokeOpacity=".7" />
          <text x={1.2 * K} y={8.2} textAnchor="middle" fontSize="6.5" fontWeight="700" letterSpacing="1.2" fill={a.persona.color} fontFamily="Inter, sans-serif">{a.name.toUpperCase()}</text>
        </OnWallY>
      </g>
    </g>
  );
}

export function AcademyAnnex({ floor, active, onPick, labels, academy, onAgent }: { floor: Floor; active: string | null; onPick: (z: Zone) => void; labels: boolean; academy?: AcademySpace; onAgent: (id: string) => void }) {
  const agents = academy?.agents ?? [];
  const running = academy?.validations_running ?? [];
  const job = academy?.engine?.job?.name ?? null;
  const busyLab = running.length > 0 || !!job?.startsWith("review") || !!job?.startsWith("validate");
  const queueLines = [
    ...running.map((r) => `${agents.find((a) => a.id === r.agent_id)?.name ?? r.agent_id}: ${r.status.replace("RUNNING: ", "").slice(0, 26)}`),
    ...(job === "backfill" ? ["Loading OANDA market history"] : []),
  ];
  const seats = SEATS.map((p, i) => ({ ...p, a: agents[i] as AcademySpaceAgent | undefined }));
  const pnl = (a: AcademySpaceAgent) => `${a.return_pct >= 0 ? "+" : ""}${(a.return_pct * 100).toFixed(2)}%`;
  const doing = (a: AcademySpaceAgent) => (a.paused || a.status === "PAUSED" ? "paused" : a.positions ? `${a.positions} open position${a.positions > 1 ? "s" : ""}` : a.activity === "watching" ? "watching the market" : academy?.engine?.data_ready ? "waiting for a setup" : "waiting for market history");
  return (
    <Scene label="The 537 Academy">
      <AcademyRoom />
      <Rigs busy={busyLab} />
      <OnWallY x={4.4} y={0} z={6.3}>
        {(queueLines.length ? queueLines : ["Queue empty"]).slice(0, 3).map((line, i) => (
          <text key={i} x={10} y={38 + i * 12} fontSize="8" fill={queueLines.length ? "#0f766e" : "#64748b"} fontFamily="Inter, sans-serif">{line}</text>
        ))}
      </OnWallY>
      {[...seats].sort((p, q) => p.x + p.y - (q.x + q.y)).map((p) =>
        p.a ? <TraderDesk key={p.a.id} x={p.x} y={p.y} a={p.a} onPick={onAgent} /> : <EmptyDesk key={`${p.x}-${p.y}`} x={p.x} y={p.y} w={SEAT_W} d={SEAT_D} tone="#4a3b30" plate="OPEN SEAT" />,
      )}
      <FrontGlass name="The 537 Academy" />
      <Zones floor={floor} active={active} onPick={onPick} />
      {labels && (
        <g>
          <Sign at={[150, 300]} to={P(0, 8, 4)} title="The 537 Academy" sub={academy?.available ? `Academy Annex · ${agents.length} agents in training` : "Academy Annex · engine not running"} />
          <Sign at={P(6.5, 0, H + 2.4)} title="Strategy Lab" sub={running.length ? `Validating ${running.length} agent${running.length > 1 ? "s" : ""}` : "Backtests & validation · idle"} />
          <Sign at={P(17.5, 0, H + 2.4)} title="Research Lead's office" sub="AI research assistant · unoccupied" tone="muted" />
          <Sign at={P(29, 0, H + 2.4)} title="Alumni wall" sub="Graduated & retired agents · none yet" tone="muted" />
        </g>
      )}
      {seats.filter((p) => p.a).map((p) => (
        <Sign key={`s-${p.a!.id}`} at={P(p.x + SEAT_W / 2, p.y, 5.2)} title={`${p.a!.name} · ${p.a!.style}`} sub={`${pnl(p.a!)} · ${doing(p.a!)}`} tone={p.a!.paused ? "muted" : "gold"} />
      ))}
    </Scene>
  );
}
