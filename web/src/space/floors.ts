// The building directory: every floor and named space in The 537 Firm, what it is for, and
// what it shows today. Forex and the Academy have no live records yet (the OANDA adapter is
// read-only and no Academy agents exist), so their spaces say exactly that instead of
// inventing activity. When those engines ship, their status comes from /api/space.

export type FloorId = "hq" | "forex" | "academy";

export type Zone = {
  id: string;
  name: string;
  purpose: string;
  today: string; // the factual state right now
  later?: string; // what will appear here once it opens
  rect: [number, number, number, number]; // world x0, y0, x1, y1 on the floor (for click targets)
  z?: number; // floor height of the zone (upper level of the tower), default 0
};

export type Floor = {
  id: FloorId;
  building: "HQ Tower" | "The 537 Academy";
  name: string; // the floor's name within its building
  short: string;
  division: string;
  open: boolean;
  blurb: string;
  zones: Zone[];
};

const UPPER = 8.6; // the tower's upper level (Building.UP_Z)

const chair = (id: string, name: string, style: "day" | "swing", x0: number): Zone => ({
  id,
  name,
  purpose:
    style === "day"
      ? "A live chair for an intraday forex agent: positions opened and closed within the trading day, flat by Friday's close. The agent trades its own OANDA sub-account."
      : "A live chair for a multi-day forex agent. Qualified swing agents may hold over weekends under stricter limits. The agent trades its own OANDA sub-account.",
  today: "Open. No agent has been promoted to live forex trading.",
  later: "An agent takes this chair only after the Academy's evidence gates, Graham's memo, and your authorization of the promotion and its capital.",
  rect: [x0 + 0.4, 10.4, x0 + 8.6, 21.6],
});

export const FLOORS: Floor[] = [
  {
    id: "hq",
    building: "HQ Tower",
    name: "Executive & Core Investments",
    short: "Core",
    division: "Executive level · Core Investments Division",
    open: true,
    blurb: "Graham's office and lounge upstairs; Tim, Alex, and Max on the investment floor.",
    zones: [],
  },
  {
    id: "forex",
    building: "HQ Tower",
    name: "Forex Division",
    short: "Forex",
    division: "Forex Division · OANDA",
    open: false,
    blurb: "Four live chairs for forex agents promoted from the Academy, each trading its own OANDA sub-account. All chairs are open.",
    zones: [
      {
        id: "market-room",
        name: "Market room",
        purpose: "Live OANDA rates, the Tokyo, London, and New York sessions, and the desk heads' workstations for the day and swing desks.",
        today: "Session clocks follow the market schedule. The read-only OANDA connection is verified from the Pi, but live rates are not wired into the Firm yet.",
        rect: [0.5, 0.5, 35.5, 9.5],
        z: UPPER,
      },
      chair("day-1", "Day Chair 1", "day", 0),
      chair("day-2", "Day Chair 2", "day", 9),
      chair("swing-1", "Swing Chair 1", "swing", 18),
      chair("swing-2", "Swing Chair 2", "swing", 27),
    ],
  },
  {
    id: "academy",
    building: "The 537 Academy",
    name: "Academy Annex",
    short: "Academy",
    division: "Academy Annex · strategy development",
    open: false,
    blurb: "Where agents are created, tested, paper traded with $10,000 of virtual capital each, improved, and promoted on evidence.",
    zones: [
      {
        id: "lab",
        name: "Strategy Lab",
        purpose: "Backtests and validation: walk-forward tests, a locked hold-out year, cost modeling, Monte Carlo, and a random-entry baseline.",
        today: "No validation runs. The Lab opens with the Academy core.",
        later: "Each running validation lights a rig with its progress.",
        rect: [0.5, 0.5, 12.5, 8.5],
      },
      {
        id: "research",
        name: "Research Lead's office",
        purpose: "The AI research assistant that helps draft strategies and cites research. It helps write strategies but never grades or promotes them.",
        today: "Unoccupied. You name the Research Lead when this role opens.",
        rect: [13.5, 0.5, 21.5, 8.5],
      },
      {
        id: "alumni",
        name: "Alumni wall",
        purpose: "Portraits of agents that graduated to a live chair, and of retired agents, whose records are kept.",
        today: "Empty. No agent has graduated or retired.",
        rect: [22.5, 0.5, 35.5, 8.5],
      },
      {
        id: "hall",
        name: "Trading Hall",
        purpose: "Paper trading: every Academy agent trades its own isolated $10,000 virtual account at live prices.",
        today: "No agents enrolled.",
        later: "Desks fill as agents enroll, grouped by department and by day or swing style.",
        rect: [0.5, 10, 35.5, 21.5],
      },
    ],
  },
];

export const floorById = (id: FloorId) => FLOORS.find((f) => f.id === id)!;

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
