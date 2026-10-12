// Office life: what idle people do between decisions. Purely cosmetic, and only for people
// whose real state is idle with the market open (mood "resting"). Each person keeps their own
// random schedule. Walks follow waypoints through open floor (up from the chair, around the
// side of the desk, along the aisle), and each walker is drawn behind or in front of the desk
// it is near, so nobody crosses furniture. Nothing runs unless the building is on screen and the
// tab is visible; anyone whose real state changes sits back down at once.

import { useEffect, useRef, useState } from "react";
import { type AgentId, P, ROOMS } from "./Building";
import { type Mood } from "./state";

export type LifeKind = "stretch" | "coffee" | "window" | "lounge" | "visit";
export type Layer = "behind" | "front";
export type Life = {
  kind: LifeKind;
  pos: [number, number]; // screen point (feet)
  dur: number; // seconds for the move to `pos`
  facing: 1 | -1;
  hidden: boolean;
  holding: boolean;
  walking: boolean;
  chatWith: AgentId | null;
  room: AgentId; // whose office layer draws the walker
  layer: Layer; // relative to that office's desk
};

type W = { x: number; y: number; z: number; layer: Layer; room: AgentId };
type Step = { to?: W; wait?: number; hidden?: boolean; holding?: boolean; chatWith?: AgentId | null; teleport?: boolean; stay?: number };

const SPEED = 50; // screen px per second
const rand = (a: number, b: number) => a + Math.random() * (b - a);
const pick = <T,>(xs: [T, number][]): T => {
  let r = Math.random() * xs.reduce((s, [, w]) => s + w, 0);
  for (const [x, w] of xs) if ((r -= w) <= 0) return x;
  return xs[xs.length - 1][0];
};
const screen = (w: W): [number, number] => P(w.x, w.y, w.z);

/** Waypoints around a desk, in world units. */
function around(id: AgentId) {
  const r = ROOMS[id];
  const { x, y, w, d } = r.desk;
  const z = r.z;
  const at = (px: number, py: number, layer: Layer, room: AgentId = id): W => ({ x: px, y: py, z, layer, room });
  return {
    seat: at(x + w / 2, y - 0.15, "behind"),
    side: at(x - 0.9, y + 0.3, "behind"),
    sideFront: at(x - 0.9, y + 0.3, "front"),
    front: at(x - 0.9, y + d + 1.1, "front"),
    right: at(x + w + 1.0, y + d + 1.3, "front"),
    guest: at(x + w + 0.9, y + d - 0.3, "front"),
    at,
  };
}

/** The coffee station sits in each office's open front-left corner, away from the desks. */
export const COFFEE_X = { tim: 0, alex: 12, max: 24 } as const;

function routeFor(id: AgentId, kind: LifeKind, visit: AgentId | null): Step[] {
  const a = around(id);
  if (id !== "cio") {
    const leave = [a.side, a.sideFront, a.front];
    const back = [a.front, a.sideFront, a.side, a.seat];
    if (kind === "coffee") {
      const cx = COFFEE_X[id as "tim" | "alex" | "max"];
      const station = a.at(cx + 1.7, 19.7, "front");
      return [...leave.map((to) => ({ to })), { to: station }, { stay: rand(7, 14), holding: true }, ...back.map((to) => ({ to, holding: true }))];
    }
    return [...leave.map((to) => ({ to })), { to: a.right }, { stay: rand(4, 9) }, ...back.map((to) => ({ to }))];
  }
  // Graham, upstairs.
  const window: W = a.at(7.6, 1.5, "behind");
  if (kind === "window") return [{ to: window }, { stay: rand(6, 12) }, { to: a.seat }];
  const exit = [a.side, a.sideFront, a.front, a.at(12.4, 8.7, "front"), a.at(14.2, 8.9, "front"), a.at(18.6, 9.0, "front")];
  const home = [...exit].reverse().concat(a.seat);
  if (kind === "coffee" || kind === "lounge") {
    const spot = kind === "coffee" ? a.at(22.2, 7.5, "front") : a.at(30.4, 7.6, "front");
    return [...exit.map((to) => ({ to })), { to: spot }, { stay: rand(8, 16), holding: kind === "coffee" }, ...home.map((to) => ({ to, holding: kind === "coffee" }))];
  }
  // A visit: out through the lounge door, down (fade), beside the manager's desk, and back.
  const door = exit[exit.length - 1];
  const guest = around(visit!).guest;
  return [
    ...exit.map((to) => ({ to })),
    { hidden: true, wait: 0.7 },
    { to: guest, teleport: true, hidden: true },
    { hidden: false, wait: 0.7 },
    { stay: rand(14, 26), chatWith: visit },
    { hidden: true, wait: 0.7, chatWith: null },
    { to: door, teleport: true, hidden: true },
    { hidden: false, wait: 0.7 },
    ...home.map((to) => ({ to })),
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
    const set = (id: AgentId, v: Life | null) => setLife((l) => ({ ...l, [id]: v }));
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
      // ?life=demo shortens the waits (for checking routes); behavior is otherwise identical.
      const demo = new URLSearchParams(window.location.search).get("life") === "demo";
      timers.current[id] = window.setTimeout(() => start(id), (demo ? rand(1, 4) : first ? rand(5, 40) : rand(40, 150)) * 1000);
    };
    const run = (id: AgentId, steps: Step[], i: number, cur: Life) => {
      if (!resting(id)) return clear(id);
      if (i >= steps.length) {
        busy.current[id] = false;
        set(id, null);
        return schedule(id);
      }
      const st = steps[i];
      if (st.chatWith) hosting.current[st.chatWith] = true;
      if (st.chatWith === null) hosting.current = {};
      const holding = st.holding ?? cur.holding;
      if (st.to && !st.teleport && (st.to.layer !== cur.layer || st.to.room !== cur.room)) {
        // Changing drawing layer remounts the walker: re-place it where it stands, then walk.
        const placed = { ...cur, layer: st.to.layer, room: st.to.room, dur: 0, walking: false };
        set(id, placed);
        timers.current[id] = window.setTimeout(() => run(id, steps, i, placed), 50);
        return;
      }
      const to = st.to ? screen(st.to) : cur.pos;
      const dist = Math.hypot(to[0] - cur.pos[0], to[1] - cur.pos[1]);
      const moving = !!st.to && !st.teleport && dist > 0.5;
      const dur = moving ? Math.max(0.6, dist / SPEED) : 0;
      const next: Life = {
        ...cur,
        pos: to,
        dur,
        facing: moving && Math.abs(to[0] - cur.pos[0]) > 1 ? (to[0] < cur.pos[0] ? -1 : 1) : cur.facing,
        hidden: st.hidden ?? cur.hidden,
        holding,
        walking: moving,
        chatWith: st.chatWith === undefined ? cur.chatWith : st.chatWith,
        room: st.to ? st.to.room : cur.room,
        layer: st.to ? st.to.layer : cur.layer,
      };
      set(id, next);
      const wait = st.stay ?? (moving ? dur : st.wait ?? 0.05);
      timers.current[id] = window.setTimeout(() => run(id, steps, i + 1, { ...next, walking: false }), wait * 1000);
    };
    const start = (id: AgentId) => {
      if (!resting(id) || hosting.current[id]) return schedule(id);
      let kind: LifeKind;
      let visit: AgentId | null = null;
      if (id === "cio") {
        const hosts = (["tim", "alex", "max"] as AgentId[]).filter((m) => !busy.current[m] && moodsRef.current?.[m] && !["focused", "stressed", "asleep"].includes(moodsRef.current[m].kind));
        kind = pick<LifeKind>([["coffee", 3], ["window", 2], ["lounge", 2], ["visit", hosts.length ? 3 : 0]]);
        if (kind === "visit") visit = hosts[Math.floor(Math.random() * hosts.length)];
      } else {
        kind = pick<LifeKind>([["stretch", 4], ["coffee", 5]]);
      }
      busy.current[id] = true;
      const seat = around(id).seat;
      const first: Life = { kind, pos: screen(seat), dur: 0, facing: -1, hidden: false, holding: false, walking: false, chatWith: null, room: id, layer: "behind" };
      set(id, first);
      timers.current[id] = window.setTimeout(() => run(id, routeFor(id, kind, visit), 0, first), 80);
    };
    for (const id of ids) {
      if (resting(id)) {
        if (!busy.current[id] && !timers.current[id]) schedule(id, true);
      } else if (busy.current[id] || timers.current[id]) {
        clear(id);
      }
    }
  }, [enabled, moods]);

  useEffect(() => () => Object.values(timers.current).forEach((t) => window.clearTimeout(t)), []);
  return life;
}
