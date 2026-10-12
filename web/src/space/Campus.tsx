// The Forex Trading Floor and the Academy Annex, drawn with the HQ Tower's projection and
// parts. Neither engine has live records yet, so every desk is empty and every screen is
// dark; the only moving facts are the clocks and the forex market schedule.

import { memo, type ReactNode } from "react";
import { Box, Glow, OnWallX, OnWallY, P, Plant, Poly, SceneDefs, Sign, Sky, StreetLamp, Tree, floorQ, wallXQ, wallYQ, K } from "./Building";
import { type Floor, type Zone, SESSIONS, fxMarketOpen, localTime, sessionOpen } from "./floors";

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
function EmptyDesk({ x, y, w, d, screens = 1, tone = "#3d4452", plate }: { x: number; y: number; w: number; d: number; screens?: number; tone?: string; plate?: string }) {
  const cx = x + w / 2;
  const mw = Math.min(1.5, (w - 0.6) / screens - 0.1);
  return (
    <g className="fs-off-soft">
      <Box x={cx - 0.55} y={y - 1.15} w={1.1} d={0.25} z={0} h={2.1} top="#2b2f3a" left="#1f232c" right="#181b22" />
      <Box x={cx - 0.55} y={y - 1.0} w={1.1} d={0.95} z={0} h={0.95} top="#323744" left="#242833" right="#1c1f28" />
      <Box x={x} y={y} w={w} d={d} z={0} h={1.45} top={tone} left="#2a2f3a" right="#222631" />
      {Array.from({ length: screens }, (_, i) => {
        const mx = cx - (screens * (mw + 0.1)) / 2 + i * (mw + 0.1);
        return (
          <g key={i}>
            <Poly p={[P(mx + mw / 2 - 0.08, y + 0.25, 1.45), P(mx + mw / 2 + 0.08, y + 0.25, 1.45), P(mx + mw / 2 + 0.08, y + 0.25, 1.75), P(mx + mw / 2 - 0.08, y + 0.25, 1.75)]} fill="#2b3242" />
            <Poly p={wallYQ(y + 0.25, mx, mx + mw, 1.75, 2.75)} fill="#0b0e14" stroke="#2b3242" sw={1.2} />
          </g>
        );
      })}
      {plate && (
        <OnWallY x={cx - 1} y={y + d} z={1.05}>
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
      <Hands now={now} city={city} />
      <text x={K} y={K * 2.35} textAnchor="middle" fontSize="9" fontWeight="700" letterSpacing="1.4" fill="#e5e7eb" fontFamily="Inter, sans-serif">{city.toUpperCase()}</text>
      <text x={K} y={K * 2.95} textAnchor="middle" fontSize="8" letterSpacing="1.2" fill={open ? "#34d399" : "#94a3b8"} fontFamily="Inter, sans-serif">{open ? "SESSION OPEN" : "CLOSED"}</text>
    </OnWallY>
  );
}

function Hands({ now, city }: { now: number; city: string }) {
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
          <Poly p={floorQ(z.rect[0], z.rect[1], z.rect[2], z.rect[3], 0.02)} fill="transparent" stroke="transparent" sw={2} />
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

// -- Forex Trading Floor -----------------------------------------------------------------

const ForexRoom = memo(function ForexRoom() {
  const day = [6.5, 12.5].flatMap((y) => [2.5, 7.4, 12.3].map((x) => ({ x, y })));
  const swing = [6.5, 12.5].flatMap((y) => [20.6, 27.6].map((x) => ({ x, y })));
  return (
    <g>
      <Grounds />
      <Poly p={floorQ(0, 0, W, D, 0)} fill="url(#fs-stone)" />
      <Poly p={floorQ(1, 4, 17, 19, 0)} fill="#1c2635" op={0.92} />
      <Poly p={floorQ(19, 4, 35, 19, 0)} fill="#2a2236" op={0.92} />
      <Walls />
      {/* division sign */}
      <OnWallY x={1.2} y={0} z={7.1}>
        <rect width={11.6 * K} height={1.5 * K} rx={2} fill="#14110f" />
        <text x={14} y={21} fontFamily="Fraunces, Georgia, serif" fontSize="17" letterSpacing="3" fill="#e4cf9f">FOREX DIVISION</text>
        <rect y={1.5 * K - 3} width={11.6 * K} height={3} fill="#cdb27a" opacity=".5" />
      </OnWallY>
      {/* rates board: dark until a live feed exists */}
      <OnWallY x={14.2} y={0} z={6.9}>
        <rect width={12.6 * K} height={4.1 * K} rx={3} fill="#07090d" stroke="#2b3242" strokeWidth="4" />
        <text x={14} y={22} fontSize="11" fontWeight="700" letterSpacing="2.4" fill="#f59e0b" opacity=".85" fontFamily="Inter, sans-serif">LIVE RATES · OANDA</text>
        <text x={14} y={46} fontSize="10" fill="#94a3b8" fontFamily="Inter, sans-serif">Not connected yet</text>
        {["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD"].map((s, i) => (
          <g key={s} fontFamily="Inter, sans-serif">
            <text x={14 + (i % 2) * 120} y={64 + Math.floor(i / 2) * 14} fontSize="9" fill="#475569" letterSpacing="1">{s}</text>
            <text x={80 + (i % 2) * 120} y={64 + Math.floor(i / 2) * 14} fontSize="9" fill="#334155">— —</text>
          </g>
        ))}
      </OnWallY>
      {/* swing desk week planner (decorative) */}
      <OnWallY x={28.4} y={0} z={6.4}>
        <rect width={6.6 * K} height={3.2 * K} rx={2} fill="#efe9dc" opacity=".9" />
        {["MON", "TUE", "WED", "THU", "FRI", "WKND"].map((d, i) => (
          <g key={d}>
            <rect x={6 + i * 21} y={8} width={19} height={46} fill={d === "WKND" ? "#e6d3b0" : "#fff"} stroke="#c9bfa9" />
            <text x={15.5 + i * 21} y={18} textAnchor="middle" fontSize="6" fontWeight="700" fill="#57534e" fontFamily="Inter, sans-serif">{d}</text>
          </g>
        ))}
      </OnWallY>
      <Glow x={9} y={11} z={0} r={6} />
      <Glow x={27} y={11} z={0} r={6} />
      {day.map((p) => (
        <EmptyDesk key={`d${p.x}-${p.y}`} x={p.x} y={p.y} w={3.4} d={1.5} screens={2} plate="OPEN CHAIR" />
      ))}
      <Poly p={wallXQ(18, 3.5, 20, 0, 3.2)} fill="url(#fs-glass)" stroke="#ffffff1f" />
      {swing.map((p) => (
        <EmptyDesk key={`s${p.x}-${p.y}`} x={p.x} y={p.y} w={4.6} d={1.6} screens={1} tone="#4a3b5e" plate="OPEN CHAIR" />
      ))}
      <Plant x={1.2} y={20.6} z={0} s={1.1} />
      <Plant x={17.6} y={1.2} z={0} s={1} />
      <Plant x={35} y={20.5} z={0} s={1.1} />
    </g>
  );
});

export function ForexFloor({ floor, now, active, onPick, labels }: { floor: Floor; now: number; active: string | null; onPick: (z: Zone) => void; labels: boolean }) {
  const fxOpen = fxMarketOpen(now);
  return (
    <Scene label="Forex Trading Floor">
      <ForexRoom />
      {/* the clocks and market status are the only live facts on this floor */}
      {SESSIONS.map((s, i) => (
        <WallClock key={s.city} x={1.6 + i * 4} z={5.4} city={s.city} now={now} open={sessionOpen(now, s)} />
      ))}
      <OnWallY x={14.2} y={0} z={6.9}>
        <text x={12.6 * K - 14} y={46} textAnchor="end" fontSize="9.5" fontWeight="700" letterSpacing="1.4" fill={fxOpen ? "#34d399" : "#94a3b8"} fontFamily="Inter, sans-serif">
          {fxOpen ? "FX MARKET OPEN" : "FX MARKET CLOSED"}
        </text>
      </OnWallY>
      <FrontGlass name="Forex Trading Floor" />
      <Zones floor={floor} active={active} onPick={onPick} />
      {labels && (
        <g>
          <Sign at={[150, 300]} to={P(0, 8, 4)} title="Forex Trading Floor" sub="Forex Division · OANDA · not open yet" />
          <Sign at={P(9, 19.6, 0)} title="Day Desk" sub="Intraday agents · 0 live chairs" />
          <Sign at={P(27.5, 19.6, 0)} title="Swing Desk" sub="Multi-day agents · 0 live chairs" />
          <Sign at={P(7.5, 0, H + 2.4)} title="Session clocks" sub="Tokyo · London · New York" tone="muted" />
          <Sign at={P(21, 0, H + 2.4)} title="Rates board" sub="Live prices once OANDA connects" tone="muted" />
        </g>
      )}
    </Scene>
  );
}

// -- The 537 Academy ---------------------------------------------------------------------

const AcademyRoom = memo(function AcademyRoom() {
  const hall = [12.2, 16.8].flatMap((y) => [1.6, 7.3, 13, 18.7, 24.4, 30.1].map((x) => ({ x, y })));
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
        <text x={10} y={36} fontSize="9" fill="#64748b" fontFamily="Inter, sans-serif">Empty: no runs yet</text>
        <path d="M10 52 h120" stroke="#cbd5e1" strokeWidth="1.5" />
      </OnWallY>
      {[0.8, 2.5].map((x) => (
        <g key={x}>
          <Box x={x} y={0.5} w={1.4} d={1.3} z={0} h={4.2} top="#1f2430" left="#151922" right="#10131a" />
          <OnWallY x={x} y={1.8} z={4}>
            {Array.from({ length: 7 }, (_, i) => (
              <rect key={i} x={4} y={6 + i * 10} width={20} height={4} rx={1} fill="#1e293b" />
            ))}
          </OnWallY>
        </g>
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
      {hall.map((p) => (
        <EmptyDesk key={`${p.x}-${p.y}`} x={p.x} y={p.y} w={3.4} d={1.4} tone="#5a4232" />
      ))}
      <Plant x={35.2} y={21} z={0} s={1.1} />
    </g>
  );
});

export function AcademyAnnex({ floor, active, onPick, labels }: { floor: Floor; active: string | null; onPick: (z: Zone) => void; labels: boolean }) {
  return (
    <Scene label="The 537 Academy">
      <AcademyRoom />
      <FrontGlass name="The 537 Academy" />
      <Zones floor={floor} active={active} onPick={onPick} />
      {labels && (
        <g>
          <Sign at={[150, 300]} to={P(0, 8, 4)} title="The 537 Academy" sub="Academy Annex · not open yet" />
          <Sign at={P(6.5, 0, H + 2.4)} title="Strategy Lab" sub="Backtests & validation · 0 runs" />
          <Sign at={P(17.5, 0, H + 2.4)} title="Research Lead's office" sub="AI research assistant · unoccupied" tone="muted" />
          <Sign at={P(29, 0, H + 2.4)} title="Alumni wall" sub="Graduated & retired agents · none yet" tone="muted" />
          <Sign at={P(20, 14.4, 3.4)} title="Trading Hall" sub="Paper trading · $10,000 virtual each · 0 agents enrolled" />
        </g>
      )}
    </Scene>
  );
}
