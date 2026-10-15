// The Firm's map: every floor and department of the HQ Tower and the Academy, where it
// sits on the map, what it is for, and which folders (pages) hold its records. What each
// department is doing right now is computed from live records in the Firm page; nothing
// here invents activity.

export type FloorId = "exec" | "core" | "forex" | "lobby" | "academy-demo" | "academy-hall" | "academy-research";

export type DeptId =
  | "cio" | "treasury"
  | "tim" | "alex" | "max"
  | "market-room" | "day-1" | "day-2" | "swing-1" | "swing-2"
  | "lobby" | "records" | "operations"
  | "lab" | "studio" | "research" | "alumni" | "hall" | "demo-desk";

/** A page that holds a department's records, opened as a folder over the map. */
export type Folder = { label: string; path: string; note: string };

export type Rect = [number, number, number, number]; // x, y, w, h in map units

export type Dept = {
  id: DeptId;
  name: string;
  floor: FloorId;
  rect: Rect;
  purpose: string;
  folders: Folder[];
  people?: string[]; // HQ people who work here (agent ids)
  later?: string;
  decorative?: boolean;
};

export type Floor = { id: FloorId; building: "HQ Tower" | "The 537 Academy"; level: string; name: string; division: string; rect: Rect };

// -- geometry --------------------------------------------------------------------------------
export const WORLD = { w: 2600, h: 1460 };
export const GROUND = 1360;
export const SLAB = 12;
export const TOWER = { x0: 120, x1: 1200, top: 200 };
export const ACADEMY = { x0: 1340, x1: 2440, top: 490 };
const FH = 290; // floor height (including its slab)

export const FLOORS: Floor[] = [
  { id: "exec", building: "HQ Tower", level: "L4", name: "Executive", division: "Executive level", rect: [TOWER.x0, 200, 1080, FH] },
  { id: "core", building: "HQ Tower", level: "L3", name: "Core Investments", division: "Core Investments Division · Robinhood · US stocks & ETFs", rect: [TOWER.x0, 490, 1080, FH] },
  { id: "forex", building: "HQ Tower", level: "L2", name: "Forex Division", division: "Forex Division · OANDA · four live chairs", rect: [TOWER.x0, 780, 1080, FH] },
  { id: "lobby", building: "HQ Tower", level: "L1", name: "Lobby & Operations", division: "Ground floor · headquarters, records, operations", rect: [TOWER.x0, 1070, 1080, FH] },
  { id: "academy-demo", building: "The 537 Academy", level: "A3", name: "Demo Desk", division: "Academy Floor 3 · real orders on an OANDA practice account", rect: [ACADEMY.x0, 490, 1100, FH] },
  { id: "academy-hall", building: "The 537 Academy", level: "A2", name: "Paper Trading", division: "Academy Floor 2 · paper trading on live OANDA prices", rect: [ACADEMY.x0, 780, 1100, FH] },
  { id: "academy-research", building: "The 537 Academy", level: "A1", name: "Research", division: "Academy Floor 1 · nightly research sprints on OANDA history", rect: [ACADEMY.x0, 1070, 1100, FH] },
];

export const floorById = (id: FloorId) => FLOORS.find((f) => f.id === id)!;

const row = (floor: FloorId, x: number, w: number): Rect => {
  const f = floorById(floor).rect;
  return [x, f[1], w, f[3]];
};

const manager = (id: "tim" | "alex" | "max", name: string, title: string, x: number): Dept => ({
  id,
  name: `${name}'s office`,
  floor: "core",
  rect: row("core", x, 360),
  purpose: title,
  people: [id],
  folders: [{ label: `${name}'s workspace`, path: `/managers/${id}`, note: "Portfolio, strategy, decisions, orders, and controls" }],
});

const chair = (id: DeptId, name: string, style: "day" | "swing", x: number): Dept => ({
  id,
  name,
  floor: "forex",
  rect: row("forex", x, 180),
  purpose:
    style === "day"
      ? "A live chair for an intraday forex agent: positions opened and closed within the trading day, flat by Friday's close. The agent trades its own OANDA sub-account."
      : "A live chair for a multi-day forex agent. Qualified swing agents may hold over weekends under stricter limits. The agent trades its own OANDA sub-account.",
  later: "An agent takes this chair only after the Academy's evidence gates, Graham's memo, and your authorization of the promotion and its capital.",
  folders: [{ label: "The 537 Academy", path: "/academy", note: "Where candidates for this chair are trained" }],
});

export const DEPTS: Dept[] = [
  // L4 Executive
  { id: "cio", name: "Office of the CIO", floor: "exec", rect: row("exec", 120, 400), purpose: "Graham, the Chief Investment Officer: oversees the firm, allocates his capital pool, and answers your questions with the records behind every answer.", people: ["cio"], folders: [{ label: "CIO Office", path: "/cio", note: "Conversations, proposals, memory" }] },
  { id: "treasury", name: "Boardroom & Treasury", floor: "exec", rect: row("exec", 520, 680), purpose: "The firm's numbers in one room: capital, what you have contributed against what it is worth, overall and daily returns against the S&P 500, drawdowns, Graham's proposals waiting for you, and the Academy's floors and latest sprint. Click the room for the full analytics.", folders: [{ label: "Firm analytics", path: "/analytics", note: "Returns, contributions, daily marks, managers, and the Academy" }, { label: "CIO Office", path: "/cio", note: "Review and decide on proposals" }, { label: "Capital", path: "/capital", note: "Allocate, recall, fund the pool, caps" }] },
  // L3 Core Investments
  manager("tim", "Tim", "Core Investment Manager: a diversified long-term ETF portfolio, new cash invested toward targets, rebalanced on drift.", 120),
  manager("alex", "Alex", "Strategic Growth Manager: systematic momentum stock selection, each candidate checked by an AI research desk.", 480),
  manager("max", "Max", "Short-Term Trading Manager: trend-pullback swing trades with defined entries, exits, and stops.", 840),
  // L2 Forex
  { id: "market-room", name: "Market room", floor: "forex", rect: row("forex", 120, 360), purpose: "Live OANDA rates, the Tokyo, London, and New York sessions, and the desk heads' workstations for the day and swing desks.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Rates, agents, and validation evidence" }] },
  chair("day-1", "Day Chair 1", "day", 480),
  chair("day-2", "Day Chair 2", "day", 660),
  chair("swing-1", "Swing Chair 1", "swing", 840),
  chair("swing-2", "Swing Chair 2", "swing", 1020),
  // L1 Lobby & Operations
  { id: "lobby", name: "Lobby", floor: "lobby", rect: row("lobby", 120, 440), purpose: "The front door: the firm at a glance, its capital, its managers, and today's activity.", folders: [{ label: "Headquarters", path: "/", note: "The firm dashboard" }] },
  { id: "records", name: "Records room", floor: "lobby", rect: row("lobby", 560, 300), purpose: "Every event the firm records: decisions, orders, fills, capital moves, safeguards, and sign-ins.", folders: [{ label: "Activity", path: "/activity", note: "The full event log" }] },
  { id: "operations", name: "Operations", floor: "lobby", rect: row("lobby", 860, 340), purpose: "The machinery: broker connection, scheduled jobs, safeguards, reconciliations, backups, and the AI budget.", folders: [{ label: "System", path: "/system", note: "Health, jobs, limits, backups" }] },
  // Academy
  // Academy A3: Floor 3 (OANDA Demo Desk), plus the Research Lead and the Alumni wall
  { id: "demo-desk", name: "Demo Desk", floor: "academy-demo", rect: row("academy-demo", 1340, 500), purpose: "Floor 3: two seats trading real orders on an OANDA practice account (fake money), each in its own sub-account. After 4+ good weeks here, Graham writes a memo and you decide on a live chair.", later: "You give a Floor 2 agent a seat from its Academy page.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Floors, agents, and evidence" }] },
  { id: "research", name: "Research Lead", floor: "academy-demo", rect: row("academy-demo", 1840, 200), purpose: "The AI research assistant that helps draft strategies and cites research. It helps write strategies but never grades or promotes them.", later: "You name the Research Lead when this role opens.", folders: [] },
  { id: "alumni", name: "Alumni wall", floor: "academy-demo", rect: row("academy-demo", 2040, 400), purpose: "Portraits of agents that graduated to a live chair, and of retired agents, whose records are kept.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Agents and their records" }] },
  // Academy A2: Floor 2
  { id: "hall", name: "Paper Trading Hall", floor: "academy-hall", rect: row("academy-hall", 1340, 1100), purpose: "Floor 2: agents that passed research, each trading a fresh $10,000 virtual account on live OANDA prices for at least 4 weeks. No broker orders are placed. You choose who gets a Demo Desk seat.", later: "Agents arrive here automatically when they pass every research gate.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Floors, journal, and rates" }] },
  // Academy A1: Floor 1
  { id: "lab", name: "Strategy Lab", floor: "academy-research", rect: row("academy-research", 1340, 200), purpose: "Where the nightly research sprints run: candidate strategies scored on OANDA history, then the gates (validation with a locked final year, random-direction baselines, cost stress, and the day-1 test).", folders: [{ label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }] },
  { id: "studio", name: "Research floor", floor: "academy-research", rect: row("academy-research", 1540, 900), purpose: "Floor 1: agents in research. They do not trade; each night at 1:00 AM New York a sprint polishes their strategy on OANDA history, and an agent moves up automatically when it passes every gate.", later: "New agents start here.", folders: [{ label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }, { label: "The 537 Academy", path: "/academy", note: "Agents and evidence" }] },
];

export const deptById = (id: DeptId) => DEPTS.find((d) => d.id === id)!;
export const deptOf = (person: string) => DEPTS.find((d) => d.people?.includes(person));

/** Every page the map can open as a folder, for titles. */
export const FOLDER_TITLES: [RegExp, string][] = [
  [/^\/$/, "Headquarters"],
  [/^\/cio$/, "CIO Office"],
  [/^\/capital$/, "Capital"],
  [/^\/activity$/, "Activity"],
  [/^\/system$/, "System"],
  [/^\/analytics$/, "Firm analytics"],
  [/^\/academy$/, "The 537 Academy"],
  [/^\/academy\/reports/, "Sprint reports"],
  [/^\/academy\/(\w+)$/, "Academy agent"],
  [/^\/managers\/(\w+)$/, "Manager workspace"],
];

export function folderTitle(path: string): string {
  const m = path.match(/^\/managers\/(\w+)$/);
  if (m) return `${m[1].charAt(0).toUpperCase()}${m[1].slice(1)}'s workspace`;
  if (path.startsWith("/academy/reports")) return "Sprint reports";
  const a = path.match(/^\/academy\/(\w+)$/);
  if (a) return `${a[1].charAt(0).toUpperCase()}${a[1].slice(1)} · Academy`;
  return FOLDER_TITLES.find(([re]) => re.test(path))?.[1] ?? path;
}

// -- forex market schedule ----------------------------------------------------------------------

/** Trading sessions by local schedule (not holiday-aware). */
export const SESSIONS = [
  { city: "Tokyo", tz: "Asia/Tokyo", open: 9, close: 18 },
  { city: "London", tz: "Europe/London", open: 8, close: 17 },
  { city: "New York", tz: "America/New_York", open: 8, close: 17 },
] as const;

function local(now: number, tz: string) {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: tz, weekday: "short", hour: "numeric", minute: "numeric", hourCycle: "h23" }).formatToParts(now);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return { day: get("weekday"), hour: Number(get("hour")) % 24, minute: Number(get("minute")) };
}

export function sessionOpen(now: number, s: (typeof SESSIONS)[number]): boolean {
  const { day, hour } = local(now, s.tz);
  return !["Sat", "Sun"].includes(day) && hour >= s.open && hour < s.close && fxMarketOpen(now);
}

/** Spot FX trades from Sunday 17:00 to Friday 17:00 New York time. */
export function fxMarketOpen(now: number): boolean {
  const { day, hour } = local(now, "America/New_York");
  if (day === "Sat") return false;
  if (day === "Sun") return hour >= 17;
  if (day === "Fri") return hour < 17;
  return true;
}

export function localTime(now: number, tz: string): { hour: number; minute: number } {
  const { hour, minute } = local(now, tz);
  return { hour, minute };
}
