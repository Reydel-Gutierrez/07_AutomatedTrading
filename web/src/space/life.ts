// Office life: what idle people do between decisions. Purely cosmetic, and only for people
// whose real state is idle with the market open (mood "resting"). Each person runs their own
// schedule with random timing, so nobody moves in lockstep. Nothing runs unless the building is
// on screen and the tab is visible; anyone whose real state changes sits back down at once.

import { useEffect, useRef, useState } from "react";
import { type AgentId, P, ROOMS } from "./Building";
import { type Mood } from "./state";

export type LifeKind = "stretch" | "coffee" | "window" | "lounge" | "visit";
export type Life = {
  kind: LifeKind;
  pos: [number, number]; // screen point (feet)
  dur: number; // seconds for the move to `pos`
  facing: 1 | -1;
  hidden: boolean; // between floors (Graham's visits)
  holding: boolean; // a coffee in hand
  walking: boolean;
  chatWith: AgentId | null;
};

type Step = { to?: [number, number]; wait: number; hidden?: boolean; holding?: boolean; walking?: boolean; chatWith?: AgentId | null; teleport?: boolean };

const SPEED = 55; // screen px per second
const rand = (a: number, b: number) => a + Math.random() * (b - a);
const pick = <T,>(xs: [T, number][]): T => {
  let r = Math.random() * xs.reduce((s, [, w]) => s + w, 0);
  for (const [x, w] of xs) if ((r -= w) <= 0) return x;
  return xs[xs.length - 1][0];
};

function front(id: AgentId): [number, number] {
  const r = ROOMS[id];
  return P(r.desk.x - 0.5, r.desk.y + r.desk.d + 0.8, r.z);
}
const at = (x: number, y: number, z = 0) => P(x, y, z);

/** Where each person can wander. World coordinates come from the building's floor plan. */
const SPOTS: Record<AgentId, Partial<Record<LifeKind, [number, number]>> & { door?: [number, number]; visit?: [number, number] }> = {
  tim: { stretch: at(1.6, 19.2), coffee: at(6.9, 11.4), visit: at(9.6, 17) },
  alex: { stretch: at(13.6, 19.4), coffee: at(18.9, 11.4), visit: at(21.6, 17) },
  max: { stretch: at(25.6, 19.4), coffee: at(30.9, 11.4), visit: at(33.6, 17) },
  cio: { coffee: at(21.2, 7.4, 8.6), window: at(7.5, 1.6, 8.6), lounge: at(30.5, 7.2, 8.6), door: at(13.2, 8.6, 8.6) },
};

/** Coffee stations drawn in each office (screen points), so the coffee runs have somewhere to go. */
export const COFFEE_STATIONS: { x: number; y: number; z: number }[] = [
  { x: 6.5, y: 10.05, z: 0 },
  { x: 18.5, y: 10.05, z: 0 },
  { x: 30.5, y: 10.05, z: 0 },
];

function walk(from: [number, number], to: [number, number], extra: Partial<Step> = {}): Step {
  const d = Math.hypot(to[0] - from[0], to[1] - from[1]);
  return { to, wait: Math.max(1.2, d / SPEED), walking: true, ...extra };
}

function plan(id: AgentId, kind: LifeKind, visit: AgentId | null): Step[] {
  const home = front(id);
  const s = SPOTS[id];
  if (kind === "visit" && visit) {
    const door = s.door!;
    const there = SPOTS[visit].visit!;
    const stay = rand(14, 26);
    return [
      walk(home, door),
      { wait: 0.7, hidden: true },
      { to: there, wait: 0.1, hidden: true, teleport: true },
      { wait: 0.7, hidden: false },
      { wait: stay, walking: false, chatWith: visit },
      { wait: 0.7, hidden: true, chatWith: null },
      { to: door, wait: 0.1, hidden: true, teleport: true },
      { wait: 0.7, hidden: false },
      walk(door, home),
    ];
  }
  const spot = s[kind] ?? home;
  const coffee = kind === "coffee";
  return [
    walk(home, spot),
    { wait: rand(coffee ? 6 : 4, coffee ? 14 : 9), walking: false, holding: coffee },
    walk(spot, home, { holding: coffee }),
  ];
}

/** Each idle person's current cosmetic activity, or null when they're at their desk. */
export function useOfficeLife(moods: Record<AgentId, Mood> | null, enabled: boolean): Record<AgentId, Life | null> {
  const [life, setLife] = useState<Record<AgentId, Life | null>>({ cio: null, tim: null, alex: null, max: null });
  const timers = useRef<Record<string, number>>({});
  const busy = useRef<Record<string, boolean>>({});
  const hosting = useRef<Record<string, boolean>>({});
  const moodsRef = useRef(moods);
  moodsRef.current = moods;

  const resting = (id: AgentId) => !!moodsRef.current && moodsRef.current[id]?.kind === "resting";

  useEffect(() => {
    const ids: AgentId[] = ["cio", "tim", "alex", "max"];
    const clear = (id: AgentId) => {
      window.clearTimeout(timers.current[id]);
      timers.current[id] = 0;
      busy.current[id] = false;
      if (id === "cio") hosting.current = {};
      setLife((l) => (l[id] ? { ...l, [id]: null } : l));
    };
    if (!enabled || !moods) {
      ids.forEach(clear);
      return;
    }
    const schedule = (id: AgentId, first = false) => {
      window.clearTimeout(timers.current[id]);
      timers.current[id] = window.setTimeout(() => start(id), (first ? rand(6, 45) : rand(35, 140)) * 1000);
    };
    const run = (id: AgentId, kind: LifeKind, steps: Step[], i: number, pos: [number, number], holding: boolean) => {
      if (!resting(id)) return clear(id);
      if (i >= steps.length) {
        busy.current[id] = false;
        setLife((l) => ({ ...l, [id]: null }));
        return schedule(id);
      }
      const st = steps[i];
      if (st.chatWith) hosting.current[st.chatWith] = true;
      if (st.chatWith === null) hosting.current = {};
      const to = st.to ?? pos;
      const facing: 1 | -1 = to[0] < pos[0] ? -1 : 1;
      const h = st.holding ?? holding;
      setLife((l) => ({
        ...l,
        [id]: {
          kind,
          pos: to,
          dur: st.teleport ? 0 : st.to ? st.wait : 0,
          facing: st.to && to[0] !== pos[0] ? facing : (l[id]?.facing ?? 1),
          hidden: st.hidden ?? l[id]?.hidden ?? false,
          holding: h,
          walking: st.walking ?? false,
          chatWith: st.chatWith === undefined ? (l[id]?.chatWith ?? null) : st.chatWith,
        },
      }));
      timers.current[id] = window.setTimeout(() => run(id, kind, steps, i + 1, to, h), st.wait * 1000);
    };
    const start = (id: AgentId) => {
      if (!resting(id) || hosting.current[id]) return schedule(id);
      let kind: LifeKind;
      let visit: AgentId | null = null;
      if (id === "cio") {
        const hosts = (["tim", "alex", "max"] as AgentId[]).filter((m) => !busy.current[m] && moodsRef.current?.[m] && !["focused", "stressed"].includes(moodsRef.current[m].kind) && moodsRef.current[m].kind !== "asleep");
        kind = pick<LifeKind>([["coffee", 3], ["window", 2], ["lounge", 2], ["visit", hosts.length ? 3 : 0]]);
        if (kind === "visit") visit = hosts[Math.floor(Math.random() * hosts.length)];
      } else {
        kind = pick<LifeKind>([["stretch", 4], ["coffee", 5]]);
      }
      busy.current[id] = true;
      const home = front(id);
      setLife((l) => ({ ...l, [id]: { kind, pos: home, dur: 0, facing: 1, hidden: false, holding: false, walking: false, chatWith: null } }));
      // Let the first frame render at the desk before moving, so the walk animates.
      timers.current[id] = window.setTimeout(() => run(id, kind, plan(id, kind, visit), 0, home, false), 60);
    };
    for (const id of ids) {
      if (resting(id)) {
        if (!busy.current[id] && !timers.current[id]) schedule(id, true);
      } else if (busy.current[id] || timers.current[id]) {
        clear(id);
      }
    }
    return () => {
      // Effect re-runs on mood changes; timers for still-resting people survive via refs.
    };
  }, [enabled, moods]);

  useEffect(() => () => Object.values(timers.current).forEach((t) => window.clearTimeout(t)), []);
  return life;
}
