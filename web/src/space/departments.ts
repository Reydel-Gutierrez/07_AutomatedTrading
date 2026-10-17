// The Firm's map: every floor and department of the HQ Tower and the Academy, where it
// sits on the map, what it is for, and which folders (pages) hold its records. What each
// department is doing right now is computed from live records in the Firm page; nothing
// here invents activity.

export type FloorId = "exec" | "core" | "options" | "trading" | "forex" | "lobby" | "academy-demo" | "academy-hall" | "academy-research";

export type DeptId =
  | "cio" | "treasury"
  | "tim" | "alex" | "core-board"
  | "options-1" | "options-2" | "options-board"
  | "max" | "trading-2" | "trading-board"
  | "market-room" | "day-1" | "swing-1" | "forex-board"
  | "lobby" | "records" | "operations" | "ops-board"
  | "lab" | "studio" | "stock-research" | "stock-hall" | "research-board" | "research" | "alumni" | "demo-desk" | "demo-board" | "hall" | "hall-board";

/** Each floor's boardroom opens that floor's analytics. */
export type AnalyticsScope = "core" | "options" | "trading" | "forex" | "operations" | "academy-demo" | "academy-hall" | "academy-research";

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
  open?: string; // clicking the room also opens this page over the map
  trainees?: { stage: "RESEARCH" | "PAPER" | "DEMO" | "RETIRED"; market?: "forex" | "stocks" }; // Academy agents seated here
};

/** Does this Academy agent sit in this room? (market omitted: either class) */
export const seatsHere = (d: Dept, a: { stage: string; market?: string }) =>
  !!d.trainees && a.stage === d.trainees.stage && (!d.trainees.market || (a.market ?? "forex") === d.trainees.market);

export type Floor = { id: FloorId; building: "HQ Tower" | "The 537 Academy"; level: string; name: string; division: string; rect: Rect };

// -- geometry --------------------------------------------------------------------------------
export const WORLD = { w: 4360, h: 2040 };
export const GROUND = 1940;
export const SLAB = 12;
export const TOWER = { x0: 120, x1: 1620, top: 200 };
export const ACADEMY = { x0: 1760, x1: 4140, top: 1070 };
const TW = TOWER.x1 - TOWER.x0;
const AW = ACADEMY.x1 - ACADEMY.x0;
const BOARD_X = { hq: 1200, academy: 3720 }; // every floor's boardroom sits at the east end
const STOCK_X = 3080; // the Stock Swing Class's column in the Academy (Floors 1 and 2)
const BOARD_W = 420;
const FH = 290; // floor height (including its slab)

export const FLOORS: Floor[] = [
  { id: "exec", building: "HQ Tower", level: "L6", name: "Executive", division: "Executive level", rect: [TOWER.x0, 200, TW, FH] },
  { id: "core", building: "HQ Tower", level: "L5", name: "Core Investments", division: "Core Investments Division · Robinhood · long-term US stocks & ETFs", rect: [TOWER.x0, 490, TW, FH] },
  { id: "options", building: "HQ Tower", level: "L4", name: "Options Trading", division: "Options Trading · two live offices (real money) · design only, not open", rect: [TOWER.x0, 780, TW, FH] },
  { id: "trading", building: "HQ Tower", level: "L3", name: "Equity Trading", division: "Equity Trading · Robinhood · two live swing offices (real money)", rect: [TOWER.x0, 1070, TW, FH] },
  { id: "forex", building: "HQ Tower", level: "L2", name: "Forex Division", division: "Forex Division · OANDA · two live offices: one day trader, one swing trader", rect: [TOWER.x0, 1360, TW, FH] },
  { id: "lobby", building: "HQ Tower", level: "L1", name: "Lobby & Operations", division: "Ground floor · headquarters, records, operations", rect: [TOWER.x0, 1650, TW, FH] },
  { id: "academy-demo", building: "The 537 Academy", level: "A3", name: "Demo Desk", division: "Academy Floor 3 · real orders on an OANDA practice account", rect: [ACADEMY.x0, 1070, AW, FH] },
  { id: "academy-hall", building: "The 537 Academy", level: "A2", name: "Paper Trading", division: "Academy Floor 2 · paper trading: forex on live OANDA prices, stocks on new daily bars", rect: [ACADEMY.x0, 1360, AW, FH] },
  { id: "academy-research", building: "The 537 Academy", level: "A1", name: "Research", division: "Academy Floor 1 · nightly research sprints: forex on OANDA history, stocks on daily US history", rect: [ACADEMY.x0, 1650, AW, FH] },
];

export const floorById = (id: FloorId) => FLOORS.find((f) => f.id === id)!;

const row = (floor: FloorId, x: number, w: number): Rect => {
  const f = floorById(floor).rect;
  return [x, f[1], w, f[3]];
};

const manager = (id: "tim" | "alex" | "max", name: string, title: string, x: number, floor: FloorId = "core", w = 360): Dept => ({
  id,
  name: `${name}'s office`,
  floor,
  rect: row(floor, x, w),
  purpose: title,
  people: [id],
  folders: [{ label: `${name}'s workspace`, path: `/managers/${id}`, note: "Portfolio, strategy, decisions, orders, and controls" }],
});

const chair = (id: DeptId, name: string, style: "day" | "swing", x: number): Dept => ({
  id,
  name,
  floor: "forex",
  rect: row("forex", x, 360),
  purpose:
    style === "day"
      ? "A live chair for an intraday forex agent: positions opened and closed within the trading day, flat by Friday's close. The agent trades its own OANDA sub-account."
      : "A live chair for a multi-day forex agent. Qualified swing agents may hold over weekends under stricter limits. The agent trades its own OANDA sub-account.",
  later: "An agent takes this chair only after the Academy's evidence gates, Graham's memo, and your authorization of the promotion and its capital.",
  folders: [{ label: "The 537 Academy", path: "/academy", note: "Where candidates for this chair are trained" }],
});

const boardroom = (id: DeptId, name: string, floor: FloorId, x: number, scope: AnalyticsScope, purpose: string): Dept => ({
  id,
  name,
  floor,
  rect: row(floor, x, BOARD_W),
  purpose: `${purpose} Click the room for the full analytics.`,
  open: `/analytics/${scope}`,
  folders: [{ label: `${name} analytics`, path: `/analytics/${scope}`, note: "This floor's numbers in depth" }],
});

export const DEPTS: Dept[] = [
  // L6 Executive
  { id: "cio", name: "Office of the CIO", floor: "exec", rect: row("exec", 120, 400), purpose: "Graham, the Chief Investment Officer: oversees the firm, allocates his capital pool, proposes changes for your approval, and answers your questions with the records behind every answer. Sit down with him to talk.", people: ["cio"], folders: [{ label: "Talk to Graham", path: "/firm/cio", note: "Conversations, proposals, memory, his pool" }] },
  { id: "treasury", name: "Boardroom & Treasury", floor: "exec", rect: row("exec", 520, 1100), open: "/analytics", purpose: "The firm's numbers in one room: capital, what you have contributed against what it is worth, overall and daily returns against the S&P 500, drawdowns, Graham's proposals waiting for you, and the Academy's floors and latest sprint. Click the room for the full analytics.", folders: [{ label: "Firm analytics", path: "/analytics", note: "Returns, contributions, daily marks, managers, and the Academy" }, { label: "Graham's proposals", path: "/firm/cio", note: "Review and decide in his office" }, { label: "Capital", path: "/capital", note: "Allocate, recall, fund the pool, caps" }] },
  // L5 Core Investments: long-term money
  manager("tim", "Tim", "Core Investment Manager: a diversified long-term ETF portfolio, new cash invested toward targets, rebalanced on drift.", 120, "core", 540),
  manager("alex", "Alex", "Strategic Growth Manager: systematic momentum stock selection, each candidate checked by an AI research desk.", 660, "core", 540),
  boardroom("core-board", "Core Boardroom", "core", BOARD_X.hq, "core", "Core Investments at a glance: Tim's and Alex's value, return against the S&P 500 over the same days, and mode."),
  // L4 Options Trading: designed, not open (no agents, no options code, no broker options access yet).
  // Two offices: at the capital planned for options (about $5,000) more seats would split it too thin.
  { id: "options-1", name: "Options office 1", floor: "options", rect: row("options", 120, 540), purpose: "A live office for an option-trading agent: real money only. Agents would train in the Academy first and take the office only on your approval.", later: "Design only. Nothing is built: no option agents, no options data or pricing, and the Robinhood account has no options trading set up. It opens when you decide to build it.", folders: [{ label: "Risk policy", path: "/risk", note: "The Options department's limits" }] },
  { id: "options-2", name: "Options office 2", floor: "options", rect: row("options", 660, 540), purpose: "A second live options office, for a strategy unlike the first one's so the two do not lose together.", later: "Design only, like office 1. Worth filling only once the department has the capital for two agents.", folders: [{ label: "Risk policy", path: "/risk", note: "The Options department's limits" }] },
  boardroom("options-board", "Options Boardroom", "options", BOARD_X.hq, "options", "Options Trading at a glance, once the floor opens: seats filled, each agent's value and return against the S&P 500, and the risk it carries."),
  // L3 Equity Trading: short-term money
  manager("max", "Max", "Live swing office 1: trend-pullback swing trades with per-trade stops, a cooldown after a stop, and at most two names per correlated group. Real money only.", 120, "trading", 540),
  { id: "trading-2", name: "Swing office 2", floor: "trading", rect: row("trading", 660, 540), purpose: "The second live swing office, for a Stock Swing Class graduate you approve. Real money only.", later: "Open. A graduate takes it only on your decision, after about four good weeks of paper trading.", folders: [{ label: "The 537 Academy", path: "/academy", note: "The Stock Swing Class, floor by floor" }, { label: "Risk policy", path: "/risk", note: "Equity Trading's limits" }] },
  boardroom("trading-board", "Trading Boardroom", "trading", BOARD_X.hq, "trading", "Equity Trading at a glance: the swing desk's value, return against the S&P 500 over the same days, and mode."),
  // L2 Forex
  { id: "market-room", name: "Market room", floor: "forex", rect: row("forex", 120, 360), purpose: "Live OANDA rates, the Tokyo, London, and New York sessions, and the desk heads' workstations for the day and swing desks.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Rates, agents, and validation evidence" }] },
  // Two live offices: at the capital planned for forex (about $10,000) more chairs would split it too thin.
  chair("day-1", "Day trader's office", "day", 480),
  chair("swing-1", "Swing trader's office", "swing", 840),
  boardroom("forex-board", "Forex Boardroom", "forex", BOARD_X.hq, "forex", "The Forex Division: live chairs filled, the FX market, and the Academy pipeline of candidates for a chair."),
  // L1 Lobby & Operations
  { id: "lobby", name: "Lobby", floor: "lobby", rect: row("lobby", 120, 440), purpose: "The front door: the firm at a glance, its capital, its managers, and today's activity.", folders: [{ label: "Headquarters", path: "/", note: "The firm dashboard" }] },
  { id: "records", name: "Records room", floor: "lobby", rect: row("lobby", 560, 300), purpose: "Every event the firm records: decisions, orders, fills, capital moves, safeguards, and sign-ins.", folders: [{ label: "Activity", path: "/activity", note: "The full event log" }] },
  { id: "operations", name: "Operations", floor: "lobby", rect: row("lobby", 860, 340), purpose: "The machinery: broker connection, scheduled jobs, safeguards, reconciliations, backups, and the AI budget.", folders: [{ label: "System", path: "/system", note: "Health, jobs, limits, backups" }] },
  boardroom("ops-board", "Operations Boardroom", "lobby", BOARD_X.hq, "operations", "Today at the firm: decisions, orders, fills, warnings, and safeguards, from the event log."),
  // Academy A3: Floor 3 (OANDA Demo Desk), the Research Lead, the Alumni wall
  { id: "demo-desk", name: "Demo Desk", floor: "academy-demo", trainees: { stage: "DEMO", market: "forex" }, rect: row("academy-demo", ACADEMY.x0, 700), purpose: "Floor 3: two seats trading real orders on an OANDA practice account (fake money), each in its own sub-account. After 4+ good weeks here, Graham writes a memo and you decide on a live chair.", later: "Opens once the OANDA practice account is set up. You give a Floor 2 agent a seat from its office, or approve Graham's proposal.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Floors, agents, and evidence" }] },
  { id: "research", name: "Research Lead", floor: "academy-demo", rect: row("academy-demo", ACADEMY.x0 + 700, 220), purpose: "The AI research assistant that helps draft strategies and cites research. It helps write strategies but never grades or promotes them.", later: "You name the Research Lead when this role opens.", folders: [] },
  { id: "alumni", name: "Alumni wall", floor: "academy-demo", trainees: { stage: "RETIRED" }, rect: row("academy-demo", ACADEMY.x0 + 920, BOARD_X.academy - ACADEMY.x0 - 920), purpose: "Portraits of agents that graduated to a live chair, and of retired agents, whose records are kept.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Agents and their records" }] },
  boardroom("demo-board", "Demo Desk Boardroom", "academy-demo", BOARD_X.academy, "academy-demo", "Floor 3 at a glance: seats taken, the practice account, and who on Floor 2 is next in line."),
  // Academy A2: Floor 2
  { id: "hall", name: "Forex · Paper trading hall", floor: "academy-hall", trainees: { stage: "PAPER", market: "forex" }, rect: row("academy-hall", ACADEMY.x0, STOCK_X - ACADEMY.x0), purpose: "Floor 2: agents that passed research, each trading a fresh $10,000 virtual account on live OANDA prices. You evaluate their weekly results; after 4 profitable weeks you can give a Demo Desk seat, and Graham may propose one after 2. No broker orders are placed.", later: "Agents arrive here automatically when they pass every research gate.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Floors, journal, and rates" }] },
  { id: "stock-hall", name: "Stocks · Paper trading", floor: "academy-hall", trainees: { stage: "PAPER", market: "stocks" }, rect: row("academy-hall", STOCK_X, BOARD_X.academy - STOCK_X), purpose: "Floor 2 for stocks: Stock Swing Class agents that passed research, each with a $10,000 paper record rebuilt from new daily bars (decide at the close, fill at the next open) and compared with Max's live desk over the same days. After about four good weeks you decide: a live swing seat, or more tests.", later: "Agents arrive here automatically when they pass every research gate.", folders: [{ label: "The 537 Academy", path: "/academy", note: "Both classes, floor by floor" }, { label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }] },
  boardroom("hall-board", "Paper Trading Boardroom", "academy-hall", BOARD_X.academy, "academy-hall", "Floor 2 week by week: each agent's weeks on paper, return, trades, and readiness for the Demo Desk."),
  // Academy A1: Floor 1
  { id: "lab", name: "Strategy Lab", floor: "academy-research", rect: row("academy-research", ACADEMY.x0, 220), purpose: "Where the nightly research sprints run: candidate strategies scored on OANDA history, then the gates (validation with a locked final year, random-direction baselines, cost stress, and the day-1 test).", folders: [{ label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }] },
  { id: "studio", name: "Forex · Research floor", floor: "academy-research", trainees: { stage: "RESEARCH", market: "forex" }, rect: row("academy-research", ACADEMY.x0 + 220, STOCK_X - ACADEMY.x0 - 220), purpose: "Floor 1: agents in research. They do not trade; each night at 1:00 AM New York a sprint polishes their strategy on OANDA history, and an agent moves up automatically when it passes every gate.", later: "New agents start here.", folders: [{ label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }, { label: "The 537 Academy", path: "/academy", note: "Agents and evidence" }] },
  { id: "stock-research", name: "Stocks · Research floor", floor: "academy-research", trainees: { stage: "RESEARCH", market: "stocks" }, rect: row("academy-research", STOCK_X, BOARD_X.academy - STOCK_X), purpose: "Floor 1 for stocks: Juniper, Rook, Meridian, and Talon, each on its own list of US names. Every night at 1:30 AM New York a sprint polishes each strategy on years of daily history; an agent moves to paper trading only when it passes every gate.", folders: [{ label: "Sprint reports", path: "/academy/reports", note: "Every night's results" }, { label: "The 537 Academy", path: "/academy", note: "Both classes, floor by floor" }] },
  boardroom("research-board", "Research Boardroom", "academy-research", BOARD_X.academy, "academy-research", "Floor 1 at a glance: the next sprint, last night's results, and each agent's validation."),
];

export const deptById = (id: DeptId) => DEPTS.find((d) => d.id === id)!;
export const deptOf = (person: string) => DEPTS.find((d) => d.people?.includes(person));

/** Every page the map can open as a folder, for titles. */
export const FOLDER_TITLES: [RegExp, string][] = [
  [/^\/$/, "Headquarters"],
  [/^\/capital$/, "Capital"],
  [/^\/risk$/, "Risk policy"],
  [/^\/activity$/, "Activity"],
  [/^\/system$/, "System"],
  [/^\/analytics$/, "Firm analytics"],
  [/^\/analytics\/core$/, "Core Investments analytics"],
  [/^\/analytics\/options$/, "Options Trading analytics"],
  [/^\/analytics\/trading$/, "Equity Trading analytics"],
  [/^\/analytics\/forex$/, "Forex Division analytics"],
  [/^\/analytics\/operations$/, "Operations analytics"],
  [/^\/analytics\/academy-demo$/, "Floor 3 · Demo Desk analytics"],
  [/^\/analytics\/academy-hall$/, "Floor 2 · Paper trading analytics"],
  [/^\/analytics\/academy-research$/, "Floor 1 · Research analytics"],
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
