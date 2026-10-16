// Hand-drawn SVG people for the Firm Space. Local origin (0,0) is the middle of the
// desk's top edge the character sits behind; the desk drawn afterwards hides the legs.
// <Body> goes behind the desktop, <Hands> in front of it.

export type Look = {
  skin: string;
  hair: string;
  hairStyle: "executive" | "neat" | "curly" | "buzz";
  top: string; // jacket / sweater
  inner: string; // shirt or turtleneck
  accent: string;
  glasses?: string;
  tie?: boolean;
  headset?: boolean;
  chair: string;
  tallChair?: boolean;
};

export const LOOKS: Record<string, Look> = {
  cio: { skin: "#e6c3a1", hair: "#c9cdd4", hairStyle: "executive", top: "#1d2740", inner: "#f1f3f6", accent: "#d6b874", glasses: "#cdb27a", tie: true, chair: "#3a2a22", tallChair: true },
  tim: { skin: "#f0c8a4", hair: "#5b3b24", hairStyle: "neat", top: "#2f6bd0", inner: "#eef2f7", accent: "#4f9cf9", chair: "#1f2633" },
  alex: { skin: "#b7825f", hair: "#22191a", hairStyle: "curly", top: "#5a46a6", inner: "#17171f", accent: "#a78bfa", glasses: "#d9d4f5", chair: "#1f2633" },
  max: { skin: "#d9a67c", hair: "#8a6a3c", hairStyle: "buzz", top: "#e8ecf2", inner: "#e8ecf2", accent: "#f59e0b", headset: true, chair: "#1f2633" },
};

export type Pose = "idle" | "typing" | "reading" | "alert" | "pleased" | "asleep";
export type Feeling = "calm" | "stressed" | "pleased";
export type Gaze = "viewer" | "monitor" | "wander";

export function Chair({ look }: { look: Look }) {
  return (
    <g>
      <rect x={-31} y={look.tallChair ? -118 : -96} width={62} height={look.tallChair ? 118 : 96} rx={12} fill={look.chair} />
      <rect x={-25} y={look.tallChair ? -110 : -89} width={50} height={8} rx={4} fill="#ffffff" opacity=".05" />
    </g>
  );
}

/** Floating z's over a sleeping head. */
export function Zzz({ x, y, size = 10 }: { x: number; y: number; size?: number }) {
  return (
    <g transform={`translate(${x} ${y})`} fontFamily="Inter, sans-serif" fontWeight="700" fill="#cbd5e1">
      {[0, 1, 2].map((i) => (
        <text key={i} className="fs-zzz" style={{ animationDelay: `${i * 1}s` }} fontSize={size + i * 3} x={i * size * 0.6} y={-i * size * 0.8}>z</text>
      ))}
    </g>
  );
}

function Hair({ look }: { look: Look }) {
  const c = look.hair;
  switch (look.hairStyle) {
    case "executive":
      return (
        <g fill={c}>
          <path d="M-15.5 -84 C-16 -99 -6 -103 2 -102 C11 -101 16.5 -96 15.6 -84 C14 -91 9 -94 1 -94.5 C-5 -94.6 -9 -92 -11 -89 Z" />
          <path d="M-15.8 -86 L-16.5 -77 L-14.2 -78 Z M15.8 -86 L16.4 -77 L14.2 -78 Z" />
        </g>
      );
    case "neat":
      return (
        <path
          fill={c}
          d="M-15.6 -82 C-17 -98 -7 -104 2 -103 C12 -102 17.5 -95 15.6 -82 C14.5 -88 13 -91 10 -92.5 C4 -91 -3 -94 -7 -96 C-10 -93 -13.5 -89 -15.6 -82 Z"
        />
      );
    case "curly":
      return (
        <g fill={c}>
          {[[-13, -90], [-7, -97], [0, -99], [7, -97], [13, -90], [-15, -82], [15, -82], [-10, -94], [10, -94]].map(([x, y], i) => (
            <circle key={i} cx={x} cy={y} r={6.2} />
          ))}
        </g>
      );
    case "buzz":
      return <path fill={c} opacity=".85" d="M-15 -84 C-15.5 -97 -7 -101 0 -101 C7 -101 15.5 -97 15 -84 C12 -92 6 -94.5 0 -94.5 C-6 -94.5 -12 -92 -15 -84 Z" />;
  }
}

export function Body({ look, pose = "idle", working = false, feeling = "calm", gaze = "viewer", chair = true }: { look: Look; pose?: Pose; working?: boolean; feeling?: Feeling; gaze?: Gaze; chair?: boolean }) {
  const shut = pose === "asleep";
  const stressed = feeling === "stressed";
  return (
    <g>
      {chair && <Chair look={look} />}
      <g className="fs-breathe">
        {/* torso */}
        <path d="M-30 -48 Q-30 -58 -19 -61 L19 -61 Q30 -58 30 -48 L34 0 L-34 0 Z" fill={look.top} />
        {look.hairStyle === "curly" ? (
          <rect x={-8} y={-68} width={16} height={12} rx={4} fill={look.inner} />
        ) : (
          <path d="M-9 -61 L0 -40 L9 -61 Z" fill={look.inner} />
        )}
        {look.tie && <path d="M-2.6 -58 L2.6 -58 L3.8 -43 L0 -36 L-3.8 -43 Z" fill={look.accent} />}
        {look.tie && <path d="M-9 -61 L-3 -44 M9 -61 L3 -44" stroke="#0b1020" strokeOpacity=".5" strokeWidth="1.2" fill="none" />}
        {look.tie && <path d="M15 -46 l7 0 l-1 3 l-5 0 Z" fill={look.accent} />}
        {look.hairStyle === "neat" && <path d="M-9 -61 L-4 -53 L0 -60 L4 -53 L9 -61" fill={look.inner} stroke="#cfd6e2" strokeWidth=".6" />}
        {look.hairStyle === "buzz" && (
          <g stroke={look.accent} strokeWidth="3" strokeLinecap="round">
            <path d="M-15 -59 L-12 0 M15 -59 L12 0" />
          </g>
        )}
        {/* neck + head */}
        <rect x={-6} y={-70} width={12} height={11} rx={3} fill={look.skin} />
        <g transform={shut ? "rotate(10 0 -70)" : undefined}>
        <g className={gaze === "wander" && !shut ? "fs-headturn" : undefined}>
        <circle cx={-15.2} cy={-81} r={3.4} fill={look.skin} />
        <circle cx={15.2} cy={-81} r={3.4} fill={look.skin} />
        <ellipse cx={0} cy={-83} rx={15} ry={17.5} fill={look.skin} />
        <ellipse cx={0} cy={-74} rx={11} ry={6} fill="#000" opacity=".04" />
        <Hair look={look} />
        {/* face */}
        <g transform={gaze === "monitor" && !shut ? "translate(2.2 0.9)" : undefined}>
        <g className={gaze === "wander" && !shut ? "fs-glance" : undefined}>
        <g className={shut ? "" : "fs-blink"}>
          {shut ? (
            <path d="M-8 -82 h4 M4 -82 h4" stroke="#2a1d18" strokeWidth="1.4" strokeLinecap="round" />
          ) : (
            <>
              <circle cx={-5.6} cy={-82} r={1.7} fill="#1e1612" />
              <circle cx={5.6} cy={-82} r={1.7} fill="#1e1612" />
            </>
          )}
        </g>
        </g>
        </g>
        {stressed && (
          <g>
            <ellipse cx={-8.5} cy={-76.5} rx={3.4} ry={2} fill="#f43f5e" opacity=".55" className="fs-flush" />
            <ellipse cx={8.5} cy={-76.5} rx={3.4} ry={2} fill="#f43f5e" opacity=".55" className="fs-flush" />
            <path className="fs-sweat" d="M14 -94 q-2.6 4.2 0 5.8 q2.6 -1.6 0 -5.8 Z" fill="#93c5fd" />
          </g>
        )}
        {feeling === "pleased" && !stressed && (
          <g opacity=".35">
            <ellipse cx={-8.5} cy={-76.5} rx={3} ry={1.7} fill="#fb7185" />
            <ellipse cx={8.5} cy={-76.5} rx={3} ry={1.7} fill="#fb7185" />
          </g>
        )}
        <path d={stressed ? "M-9 -86.5 l5 -3 M9 -86.5 l-5 -3" : "M-8 -87.5 l4.5 -1 M8 -87.5 l-4.5 -1"} stroke={look.hair === "#c9cdd4" ? "#8d939c" : look.hair} strokeWidth="1.3" strokeLinecap="round" />
        {look.glasses && (
          <g fill="none" stroke={look.glasses} strokeWidth="1.1">
            <rect x={-10.5} y={-85.5} width={9} height={7} rx={2.5} />
            <rect x={1.5} y={-85.5} width={9} height={7} rx={2.5} />
            <path d="M-1.5 -82.5 h3" />
          </g>
        )}
        <path
          d={stressed ? "M-4.5 -72.5 q2.2 -1.6 4.5 0 q2.2 1.6 4.5 0" : pose === "alert" ? "M-4 -72.5 Q0 -74.5 4 -72.5" : pose === "pleased" || feeling === "pleased" ? "M-5 -74 Q0 -69.5 5 -74" : working ? "M-3.5 -73 h7" : shut ? "M-2.5 -73 q2.5 1.5 5 0" : "M-4.5 -73.5 Q0 -71 4.5 -73.5"}
          stroke="#7a3f33"
          strokeWidth="1.4"
          fill="none"
          strokeLinecap="round"
        />
        {look.headset && (
          <g fill="none" stroke="#2a2f3a" strokeWidth="2.2">
            <path d="M-16 -84 C-16 -104 16 -104 16 -84" />
            <path d="M-16 -80 Q-14 -70 -6 -71" strokeWidth="1.4" />
            <circle cx={-6} cy={-71} r={1.6} fill={look.accent} stroke="none" />
            <rect x={-19} y={-87} width={5} height={9} rx={2} fill={look.accent} stroke="none" />
            <rect x={14} y={-87} width={5} height={9} rx={2} fill={look.accent} stroke="none" />
          </g>
        )}
        </g>
        </g>
      </g>
    </g>
  );
}

export function Hands({ look, pose = "idle" }: { look: Look; pose?: Pose }) {
  const sleeve = look.hairStyle === "buzz" ? look.inner : look.top;
  if (pose === "reading") {
    return (
      <g className="fs-breathe">
        <path d="M-30 -40 Q-30 -22 -16 -22 M30 -40 Q30 -22 16 -22" stroke={sleeve} strokeWidth="9" strokeLinecap="round" fill="none" />
        <g className="fs-read">
          <rect x={-17} y={-36} width={34} height={25} rx={1.5} fill="#f4f1e8" />
          <path d="M-12 -30 h22 M-12 -26 h18 M-12 -22 h20 M-12 -18 h14" stroke="#9aa3b2" strokeWidth="1.2" />
        </g>
        <circle cx={-16} cy={-22} r={4.3} fill={look.skin} />
        <circle cx={16} cy={-22} r={4.3} fill={look.skin} />
      </g>
    );
  }
  const typing = pose === "typing";
  return (
    <g>
      <g className={typing ? "fs-type-a" : ""}>
        <path d="M-30 -40 Q-31 -14 -13 -6" stroke={sleeve} strokeWidth="9" strokeLinecap="round" fill="none" />
        <circle cx={-12} cy={-5.5} r={4.4} fill={look.skin} />
      </g>
      <g className={typing ? "fs-type-b" : ""}>
        <path d="M30 -40 Q31 -14 13 -6" stroke={sleeve} strokeWidth="9" strokeLinecap="round" fill="none" />
        <circle cx={12} cy={-5.5} r={4.4} fill={look.skin} />
      </g>
    </g>
  );
}

/** Head-and-shoulders portrait for avatars elsewhere in HQ. */
export function Portrait({ id, size = 48 }: { id: string; size?: number }) {
  const look = LOOKS[id] ?? LOOKS.cio;
  return (
    <svg width={size} height={size} viewBox="-30 -112 60 60" aria-hidden="true">
      <g transform="translate(0 6)">
        <Body look={{ ...look, chair: "transparent" }} />
      </g>
    </svg>
  );
}
