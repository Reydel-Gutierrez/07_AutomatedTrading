// Turns the real firm state (/api/space + the live event stream) into what each
// character is doing. Nothing here invents work: every non-idle state points at a
// scheduler job, an order in the ledger, an event row, or the CIO's live turn.

import type { FirmEvent } from "../api";

export type SpaceOrder = {
  id: number;
  book: string;
  symbol: string;
  side: string;
  notional: string | null;
  quantity: string | null;
  status: string;
  created_at: string;
  submitted_at: string | null;
};

export type SpaceAgent = {
  id: string;
  name: string;
  role: string;
  mode: "INACTIVE" | "SIM" | "LIVE" | "PAUSED";
  book: "LIVE" | "SIM" | null;
  status: string;
  activity: string | null;
  implemented: boolean;
  running: boolean;
  run_started: string | null;
  last_run: { at: string; summary: string; activity: string | null; book: string | null } | null;
  next_run: string | null;
  open_orders: SpaceOrder[];
  recent: FirmEvent[];
  research?: { running: boolean; symbol: string | null; since: string | null } | null;
  mood?: { book: string; marks: number; twr: number | null; excess: number | null; drawdown: number; day_change: number | null };
};

export type AcademySpaceAgent = {
  id: string;
  name: string;
  style: "day" | "swing";
  persona: { tagline: string; color: string; look?: Record<string, unknown> };
  status: string;
  paused: string | null;
  equity: number;
  return_pct: number;
  positions: number;
  activity: string | null;
  last_event: { ts: string; type: string; summary: string } | null;
  validation: string | null;
  version: number;
  stage: "RESEARCH" | "PAPER" | "DEMO" | "RETIRED";
  sprinting?: string | null;
  last_sprint?: { day: string; outcome: string } | null;
  stage_since?: string | null;
  floor_trades?: number;
};

export type EquityClassAgent = {
  id: string;
  name: string;
  persona: { tagline: string; color: string; look?: Record<string, unknown> };
  stage: "RESEARCH" | "PAPER" | "RETIRED";
  status: string;
  version: number;
  equity: number;
  return_pct: number;
  sprinting: string | null;
  last_sprint: { day: string; outcome: string } | null;
  activity: string | null;
};

export type EquityClassSpace = { available: boolean; online?: boolean; data_ready?: boolean; agents?: EquityClassAgent[] };

export type AcademySpace = {
  available: boolean;
  engine?: { online: boolean; heartbeat?: number; market_open?: boolean; data_ready?: boolean; job?: { name: string; since: number } | null; last_error?: string | null; demo?: { configured: boolean; problem: string | null; seats: number } };
  agents?: AcademySpaceAgent[];
  validations_running?: { agent_id: string; status: string }[];
  prices?: { instrument: string; bid: number; ask: number; time: number; tradeable: number }[];
  latest_report?: { day: string; passed: string[]; improved: string[]; floor2: string[] } | null;
};

export type TreasuryBrief = {
  contributed: number;
  last_mark: { date: string; nav: number } | null;
  flows_since_last_mark: number;
  days_tracked: number;
  max_drawdown: number;
  current_drawdown: number;
  last_day: { date: string; nav: number; flow: number; return: number; spy: number | null } | null;
  spark: number[];
};

export type Space = {
  academy?: AcademySpace;
  equity_class?: EquityClassSpace;
  treasury?: TreasuryBrief;
  today?: { events: number; runs: number; warnings: number; critical: number; cio: number; orders: number; fills: number };
  as_of: string;
  market: { regular_hours_open?: boolean; trading_day?: boolean; next_open?: string | null };
  benchmarks: { symbol: string; label: string; closes: { date: string; close: number }[] }[];
  broker_connected: boolean | null;
  controls: { kill_switch: boolean; reconcile_hold: boolean; drawdown_brake: boolean; live_writes_enabled: boolean };
  cio: {
    configured: boolean;
    provider_ready: boolean;
    working: { since: string; conversation_id: number; steps: number; tool: string | null } | null;
    pending_proposals: { id: number; kind: string; title: string; created_at: string; expires_at: string }[];
    recent: FirmEvent[];
  };
  agents: SpaceAgent[];
};

export type StateKey = "inactive" | "idle" | "waiting" | "paused" | "analyzing" | "reviewing" | "preparing" | "executing" | "completed" | "error";

export type AgentState = {
  key: StateKey;
  headline: string; // exactly what they are doing or waiting for
  sub: string | null; // one short supporting line for the indicator
  detail: string | null;
  symbols: { symbol: string; side?: string }[];
  at: string | null; // when the underlying fact happened
  source: string; // where the fact comes from, shown on hover
  book: string | null;
};

export const STATE_META: Record<StateKey, { label: string; color: string; working: boolean }> = {
  inactive: { label: "Not operational", color: "#64748b", working: false },
  idle: { label: "Idle", color: "#94a3b8", working: false },
  waiting: { label: "Waiting", color: "#a3a3a3", working: false },
  paused: { label: "Paused", color: "#f59e0b", working: false },
  analyzing: { label: "Analyzing", color: "#38bdf8", working: true },
  reviewing: { label: "Reviewing", color: "#dcc287", working: true },
  preparing: { label: "Preparing orders", color: "#a78bfa", working: true },
  executing: { label: "Executing", color: "#f59e0b", working: true },
  completed: { label: "Completed", color: "#34d399", working: false },
  error: { label: "Error", color: "#f43f5e", working: false },
};

const MANAGER_TYPES = new Set(["MANAGER_RUN", "ORDER_FILLED", "ORDER_SUBMITTED", "ORDER_BLOCKED", "ORDER_DEFERRED", "ORDER_REJECTED", "ORDER_UNKNOWN", "ORDER_RECOVERED", "ORDER_NOT_FOUND", "MANAGER_DEGRADED", "MANAGER_WAITING", "MANAGER_MODE", "MANDATE_CHANGED", "CAPITAL_ALLOCATED", "CAPITAL_DEALLOCATED", "SLEEVE_PAUSED", "TURNOVER_LIMIT", "RESEARCH_NOTE", "RESEARCH_FAILED", "RESEARCH_PAUSED"]);
export const CIO_TYPES = new Set(["CIO_ALLOCATION", "PROPOSAL_CREATED", "PROPOSAL_APPLIED", "PROPOSAL_REJECTED", "PROPOSAL_FAILED", "CIO_MEMORY", "AI_PROVIDER_UNAVAILABLE"]);
const ERROR_TYPES = new Set(["MANAGER_DEGRADED", "ORDER_REJECTED", "ORDER_UNKNOWN", "ORDER_NOT_FOUND"]);
const sideOf = (e: FirmEvent) => (/\bSELL\b/.test(e.summary) ? "sell" : /\bBUY\b/.test(e.summary) ? "buy" : undefined);
const MIN = 60_000;
const COMPLETED_FOR = 20 * MIN;
const ERROR_FOR = 30 * MIN;

export const PHASE: Record<string, number> = { alex: 3, max: 4 };

const TOOL_LABEL: Record<string, string> = {
  get_firm_overview: "Reviewing the firm overview",
  get_manager: "Reviewing a manager's workspace",
  get_activity: "Reading the activity log",
  get_performance: "Checking performance records",
  get_capital: "Reviewing the capital ledger",
  get_risk_controls: "Checking risk controls",
  get_quotes: "Looking up market quotes",
  get_price_history: "Studying price history",
  search_memory: "Searching its memory",
  list_proposals: "Reviewing open proposals",
  create_proposal: "Drafting a proposal for you",
  remember: "Noting something to remember",
  respond: "Writing the answer",
};

const age = (iso: string, now: number) => now - Date.parse(iso);
const strip = (s: string, name: string) => s.replace(new RegExp(`^${name}: `), "");

/** Server events plus live-stream events, newest first, de-duplicated. */
export function merge(server: FirmEvent[], live: FirmEvent[], keep: (e: FirmEvent) => boolean): FirmEvent[] {
  const byId = new Map<number, FirmEvent>();
  for (const e of [...server, ...live]) if (keep(e)) byId.set(e.id, e);
  return [...byId.values()].sort((a, b) => b.id - a.id);
}

function intents(e: FirmEvent | undefined): { symbol: string; side?: string }[] {
  const list = (e?.payload?.intents as { symbol: string; side: string }[] | undefined) ?? [];
  return list.map((i) => ({ symbol: i.symbol, side: i.side }));
}

function orderLine(o: SpaceOrder): string {
  const size = o.notional ? `$${Number(o.notional).toFixed(2)}` : o.quantity ? `${Number(o.quantity)} sh` : "";
  return `${o.side.toUpperCase()} ${size} ${o.symbol}`.replace(/\s+/g, " ");
}

export function etTime(iso: string | null | undefined, withDay = true): string | null {
  if (!iso) return null;
  const opts: Intl.DateTimeFormatOptions = { timeZone: "America/New_York", hour: "numeric", minute: "2-digit", ...(withDay ? { weekday: "short" } : {}) };
  return `${new Intl.DateTimeFormat("en-US", opts).format(new Date(iso))} ET`;
}

const REASON: Record<string, string> = {
  MARKET_CLOSED: "market closed",
  OUTSIDE_WINDOW: "outside the trading window",
  QUOTE_STALE: "quote too old",
  QUOTE_UNAVAILABLE: "no quote",
  SPREAD_WIDE: "spread too wide",
  BROKER_UNAVAILABLE: "broker offline",
};
const codes = (e: FirmEvent) => ((e.payload?.reasons as string[] | undefined) ?? []).map((r) => r.split(":")[0]);
const symList = (xs: { symbol: string; side?: string }[]) => [...new Set(xs.map((x) => x.symbol))].join(", ");
const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

export function managerState(a: SpaceAgent, live: FirmEvent[], now: number): AgentState {
  const ev = merge(a.recent, live, (e) => e.manager_id === a.id && MANAGER_TYPES.has(e.type));
  const base = { symbols: [], book: a.book, at: null, detail: null, sub: null } as Pick<AgentState, "symbols" | "book" | "at" | "detail" | "sub">;
  const next = etTime(a.next_run);

  if (!a.implemented) {
    return { ...base, key: "inactive", headline: `Not operational (Phase ${PHASE[a.id] ?? "—"})`, sub: "No research or trading yet", detail: `${a.name}'s engine is scheduled for Phase ${PHASE[a.id] ?? "—"}. Capital allocated to ${a.name} is held as cash until then.`, source: "Manager registry" };
  }
  if (a.mode === "INACTIVE") {
    return { ...base, key: "inactive", headline: "Switched off", sub: "Manages no book right now", detail: `${a.name} is inactive. Open Controls at his desk to switch him on.`, source: "Manager registry" };
  }
  if (a.status === "DEGRADED") {
    const e = ev.find((x) => x.type === "MANAGER_DEGRADED");
    return { ...base, key: "error", headline: "Stopped by an error", sub: (a.activity ?? "").slice(0, 80) || null, detail: a.activity ?? e?.summary ?? null, at: e?.ts ?? null, source: e ? `Event #${e.id}` : "Manager status" };
  }
  const lastRun = ev.find((e) => e.type === "MANAGER_RUN");
  const err = ev.find((e) => ERROR_TYPES.has(e.type) && age(e.ts, now) < ERROR_FOR);
  if (err && (!lastRun || err.id > lastRun.id)) {
    const sym = err.symbol ?? "an";
    const headline =
      err.type === "ORDER_REJECTED" ? `${sym} order rejected`
      : err.type === "ORDER_UNKNOWN" ? `Confirming an unacknowledged ${sym} order`
      : err.type === "ORDER_NOT_FOUND" ? `Broker can't find the ${sym} order`
      : "Run failed";
    return { ...base, key: "error", headline, sub: err.summary.slice(0, 80), detail: err.summary, symbols: err.symbol ? [{ symbol: err.symbol, side: sideOf(err) }] : [], at: err.ts, source: `Event #${err.id}` };
  }
  if (a.mode === "PAUSED") {
    return { ...base, key: "paused", headline: "Paused by you", sub: "No runs while paused", detail: lastRun ? `Last decision: ${strip(lastRun.summary, a.name)}` : null, at: lastRun?.ts ?? null, source: "Manager registry" };
  }
  if (a.running) {
    const thisRun = lastRun && a.run_started && Date.parse(lastRun.ts) >= Date.parse(a.run_started) - 2000 ? lastRun : undefined;
    if (thisRun && intents(thisRun).length) {
      const xs = intents(thisRun);
      return { ...base, key: "preparing", headline: `Sending ${plural(xs.length, "order")} to the desk`, sub: symList(xs), detail: strip(thisRun.summary, a.name), symbols: xs, at: thisRun.ts, source: `Event #${thisRun.id}` };
    }
    return { ...base, key: "analyzing", headline: "Analyzing prices, drift and cash", sub: `Run started ${etTime(a.run_started, false) ?? "just now"}`, detail: "Checking the book against the mandate. The decision appears here the moment it is recorded.", at: a.run_started, source: "Scheduler (run in progress)" };
  }
  const note = ev.find((e) => e.type === "RESEARCH_NOTE");
  if (a.research?.running) {
    const since = a.research.since;
    const done = ev.filter((e) => e.type === "RESEARCH_NOTE" && since && Date.parse(e.ts) >= Date.parse(since) - 600_000).length;
    return {
      ...base,
      key: "analyzing",
      headline: a.research.symbol ? `Researching ${a.research.symbol}` : "Researching screen candidates",
      sub: `Due diligence with web search${done ? ` · ${done} done this session` : ""}`,
      detail: "Reading recent earnings, guidance, and news on the screen's top-ranked names. Each verdict is recorded with its sources.",
      symbols: a.research.symbol ? [{ symbol: a.research.symbol }] : [],
      at: since,
      source: "Research desk (in progress)",
    };
  }
  if (a.open_orders.length) {
    const o = a.open_orders;
    return {
      ...base,
      key: "executing",
      headline: `Waiting on Robinhood to fill ${plural(o.length, "order")}`,
      sub: o.map(orderLine).join(" · "),
      detail: o.map((x) => `${orderLine(x)} · ${x.status.toLowerCase()}`).join(" · "),
      symbols: o.map((x) => ({ symbol: x.symbol, side: x.side })),
      book: o[0].book,
      at: o[0].submitted_at ?? o[0].created_at,
      source: `Order${o.length > 1 ? "s" : ""} ${o.map((x) => `#${x.id}`).join(", ")}`,
    };
  }
  const latest = ev[0];
  if (latest?.type === "MANAGER_WAITING" && age(latest.ts, now) < ERROR_FOR) {
    return { ...base, key: "waiting", headline: "Waiting for market data", sub: next ? `Next run ${next}` : null, detail: latest.summary, at: latest.ts, source: `Event #${latest.id}` };
  }
  if (note && age(note.ts, now) < COMPLETED_FOR && (!lastRun || note.id > lastRun.id)) {
    const verdict = note.payload?.verdict === "approve" ? "approved" : "rejected";
    return { ...base, key: "completed", headline: `Researched ${note.symbol}: ${verdict} (${note.payload?.conviction ?? "?"}/5)`, sub: next ? `Next decision ${next}` : null, detail: note.summary, symbols: note.symbol ? [{ symbol: note.symbol }] : [], at: note.ts, source: `Event #${note.id}` };
  }
  if (lastRun) {
    const after = ev.filter((e) => e.id > lastRun.id);
    const fills = after.filter((e) => e.type === "ORDER_FILLED" && e.symbol);
    const deferred = after.filter((e) => e.type === "ORDER_DEFERRED");
    const blocked = after.filter((e) => e.type === "ORDER_BLOCKED");
    const activity = lastRun.payload?.activity as string | undefined;
    const summary = strip(lastRun.summary, a.name);
    const runBook = (lastRun.payload?.book as string | undefined) ?? a.book;
    const recent = age((after[0] ?? lastRun).ts, now) < COMPLETED_FOR;
    if (fills.length && recent) {
      const xs = fills.map((e) => ({ symbol: e.symbol!, side: sideOf(e) }));
      return { ...base, key: "completed", headline: `Filled ${symList(xs)}${runBook === "SIM" ? " in simulation" : ""}`, sub: next ? `Next decision ${next}` : null, detail: fills.map((e) => e.summary).join(" · "), symbols: xs, book: runBook, at: fills[0].ts, source: `Event #${fills[0].id}` };
    }
    if (deferred.length && !fills.length) {
      // The plan was recorded but the desk deferred it; the next run decides again with fresh prices.
      const amount = activity === "investing" ? summary.match(/\$[\d,]+(?:\.\d+)?/)?.[0] : undefined;
      const why = [...new Set(deferred.flatMap(codes))].map((c) => REASON[c] ?? c.toLowerCase().replace(/_/g, " ")).join(", ");
      const headline = amount ? `Waiting to invest ${amount}` : activity === "rebalancing" ? "Waiting to rebalance" : `Waiting to place ${plural(deferred.length, "deferred order")}`;
      return {
        ...base,
        key: "waiting",
        headline,
        sub: [why.replace(/^\w/, (ch) => ch.toUpperCase()), next && `next run ${next}`].filter(Boolean).join(" · ") || null,
        detail: `Planned: ${summary} The desk deferred ${plural(deferred.length, "order")} (${why}); the next run re-decides with fresh prices.`,
        symbols: intents(lastRun),
        book: runBook,
        at: deferred[0].ts,
        source: `Event #${deferred[0].id}`,
      };
    }
    if (blocked.length && recent) {
      return { ...base, key: "error", headline: "Order blocked by a safeguard", sub: codes(blocked[0]).join(", ") || blocked[0].summary.slice(0, 60), detail: blocked.map((e) => e.summary).join(" · "), symbols: blocked.filter((e) => e.symbol).map((e) => ({ symbol: e.symbol!, side: sideOf(e) })), at: blocked[0].ts, source: `Event #${blocked[0].id}` };
    }
    if (recent && latest?.id === lastRun.id) {
      const verbs: Record<string, string> = { holding: "Decided to hold", investing: "Decided to invest", rebalancing: "Decided to rebalance", entering: "Entered pullback trades", exiting: "Exited trades", watching: "Stops checked", "waiting for research": "Waiting for research approvals" };
      const verb = verbs[activity ?? ""] ?? "Decision recorded";
      return { ...base, key: "completed", headline: verb, sub: summary.slice(0, 80), detail: summary, symbols: intents(lastRun), book: runBook, at: lastRun.ts, source: `Event #${lastRun.id}` };
    }
  }
  return {
    ...base,
    key: "idle",
    headline: next ? `Next decision ${next}` : "No decision scheduled",
    sub: lastRun ? `Last: ${strip(lastRun.summary, a.name).slice(0, 70)}` : "No decisions recorded yet",
    detail: lastRun ? `Last decision: ${strip(lastRun.summary, a.name)}` : "No decisions recorded yet.",
    at: lastRun?.ts ?? null,
    source: lastRun ? `Event #${lastRun.id}` : "Event log",
  };
}

export function cioState(c: Space["cio"], live: FirmEvent[], now: number): AgentState {
  const base = { symbols: [], book: null, at: null, detail: null, sub: null } as Pick<AgentState, "symbols" | "book" | "at" | "detail" | "sub">;
  if (!c.configured) return { ...base, key: "inactive", headline: "Office not configured", detail: "The CIO service is not running on this host.", source: "Service" };
  if (c.working) {
    const tool = c.working.tool;
    return {
      ...base,
      key: "reviewing",
      headline: tool ? (TOOL_LABEL[tool] ?? `Using ${tool}`) : "Reading your question",
      sub: `Answering you · step ${c.working.steps}`,
      detail: tool ? `${TOOL_LABEL[tool] ?? `Using ${tool}`} · step ${c.working.steps}` : "Reading the firm snapshot and your conversation.",
      at: c.working.since,
      source: `Conversation #${c.working.conversation_id} (live)`,
    };
  }
  const ev = merge(c.recent, live, (e) => CIO_TYPES.has(e.type));
  const down = ev.find((e) => e.type === "AI_PROVIDER_UNAVAILABLE" && age(e.ts, now) < 60 * MIN);
  if (down) return { ...base, key: "error", headline: "AI provider unavailable", sub: "Check the API key or credits", detail: down.summary, at: down.ts, source: `Event #${down.id}` };
  if (!c.provider_ready) return { ...base, key: "waiting", headline: "Waiting for an AI provider key", sub: "Configured on the Pi", detail: "Conversations resume once the provider is configured on the Pi.", source: "Service" };
  const n = c.pending_proposals.length;
  const moved = ev.find((e) => e.type === "CIO_ALLOCATION" && age(e.ts, now) < COMPLETED_FOR);
  if (moved) {
    const what = moved.summary.replace(/^Graham /, "").split(": ")[0];
    return { ...base, key: "completed", headline: what.charAt(0).toUpperCase() + what.slice(1), sub: moved.summary.split(": ").slice(1).join(": ").slice(0, 80) || null, detail: moved.summary, at: moved.ts, source: `Event #${moved.id}` };
  }
  const filed = ev.find((e) => e.type === "PROPOSAL_CREATED" && age(e.ts, now) < COMPLETED_FOR);
  if (filed) return { ...base, key: "completed", headline: `Filed proposal #${filed.payload?.proposal_id ?? ""}`.trim(), sub: n ? "Waiting on your decision" : null, detail: filed.summary, at: filed.ts, source: `Event #${filed.id}` };
  if (n) {
    return { ...base, key: "waiting", headline: `Waiting on your decision: ${plural(n, "proposal")}`, sub: `#${c.pending_proposals[0].id} ${c.pending_proposals[0].title}`.slice(0, 70), detail: c.pending_proposals.map((p) => `#${p.id} ${p.title}`).join(" · "), at: c.pending_proposals[0].created_at, source: "Proposals" };
  }
  return { ...base, key: "idle", headline: "Ready for your questions", sub: "No proposals pending", detail: "Ask me anything about the firm. I work only when you ask, and I show the records behind every answer.", source: "CIO office" };
}

// -- moods ------------------------------------------------------------------------------------
// How someone looks is read from the same records as what they're doing: performance vs the
// S&P 500, drawdown, today's move, safeguards, errors. Resting and sleeping are what idle
// people do when nothing is happening; they never stand in for activity.

export type MoodKind = "stressed" | "pleased" | "focused" | "calm" | "resting" | "asleep";
export type Mood = { kind: MoodKind; reason: string | null };
export const MOOD_LABEL: Record<MoodKind, string> = {
  stressed: "Under pressure",
  pleased: "In good spirits",
  focused: "Focused",
  calm: "Calm",
  resting: "Stretching his legs",
  asleep: "Resting",
};

const fmt = (x: number) => `${(Math.abs(x) * 100).toFixed(1)}%`;

export function moodOf(state: AgentState, a: SpaceAgent | null, marketOpen: boolean, now: number): Mood {
  if (state.key === "error") return { kind: "stressed", reason: state.headline };
  const m = a?.mood;
  const paused = a?.recent.find((e) => e.type === "SLEEVE_PAUSED" && age(e.ts, now) < 24 * 60 * MIN);
  if (paused) return { kind: "stressed", reason: "His safeguard paused new buys" };
  if (m) {
    if (m.day_change !== null && m.day_change <= -0.015) return { kind: "stressed", reason: `Down ${fmt(m.day_change)} today` };
    if (m.drawdown >= 0.05) return { kind: "stressed", reason: `${fmt(m.drawdown)} below his peak` };
    if (m.marks >= 5 && m.excess !== null && m.excess <= -0.03) return { kind: "stressed", reason: `Trailing the S&P 500 by ${fmt(m.excess)}` };
  }
  if (STATE_META[state.key].working) return { kind: "focused", reason: null };
  if (m) {
    if (m.day_change !== null && m.day_change >= 0.01) return { kind: "pleased", reason: `Up ${fmt(m.day_change)} today` };
    if (m.marks >= 5 && m.excess !== null && m.excess >= 0.02) return { kind: "pleased", reason: `Ahead of the S&P 500 by ${fmt(m.excess)}` };
  }
  if (state.key === "completed") return { kind: "pleased", reason: state.headline };
  if (state.key === "inactive") return { kind: "calm", reason: null }; // switched off: stays put
  if (["idle", "waiting", "paused"].includes(state.key)) {
    return marketOpen ? { kind: "resting", reason: "Nothing to do until his next decision" } : { kind: "asleep", reason: "Market closed" };
  }
  return { kind: "calm", reason: null };
}
