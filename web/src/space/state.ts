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
};

export type Space = {
  as_of: string;
  market: { regular_hours_open?: boolean; trading_day?: boolean };
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
  headline: string;
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

const MANAGER_TYPES = new Set(["MANAGER_RUN", "ORDER_FILLED", "ORDER_SUBMITTED", "ORDER_BLOCKED", "ORDER_DEFERRED", "ORDER_REJECTED", "ORDER_UNKNOWN", "ORDER_RECOVERED", "ORDER_NOT_FOUND", "MANAGER_DEGRADED", "MANAGER_WAITING", "MANAGER_MODE", "MANDATE_CHANGED", "CAPITAL_ALLOCATED", "CAPITAL_DEALLOCATED", "SLEEVE_PAUSED", "TURNOVER_LIMIT"]);
export const CIO_TYPES = new Set(["PROPOSAL_CREATED", "PROPOSAL_APPLIED", "PROPOSAL_REJECTED", "PROPOSAL_FAILED", "CIO_MEMORY", "AI_PROVIDER_UNAVAILABLE"]);
const ERROR_TYPES = new Set(["MANAGER_DEGRADED", "ORDER_REJECTED", "ORDER_UNKNOWN", "ORDER_NOT_FOUND"]);
const DONE_TYPES = new Set(["MANAGER_RUN", "ORDER_FILLED", "ORDER_BLOCKED", "ORDER_RECOVERED"]);
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

export function managerState(a: SpaceAgent, live: FirmEvent[], now: number): AgentState {
  const ev = merge(a.recent, live, (e) => e.manager_id === a.id && MANAGER_TYPES.has(e.type));
  const base = { symbols: [], book: a.book, at: null, detail: null } as Pick<AgentState, "symbols" | "book" | "at" | "detail">;

  if (!a.implemented) {
    return { ...base, key: "inactive", headline: "Not yet operational", detail: `${a.name}'s engine is scheduled for Phase ${PHASE[a.id] ?? "—"}. No research or trading happens here yet.`, source: "Manager registry" };
  }
  if (a.mode === "INACTIVE") {
    return { ...base, key: "inactive", headline: "Inactive", detail: `${a.name} is switched off and manages no book right now.`, source: "Manager registry" };
  }
  if (a.status === "DEGRADED") {
    const e = ev.find((x) => x.type === "MANAGER_DEGRADED");
    return { ...base, key: "error", headline: "Needs attention", detail: a.activity ?? e?.summary ?? null, at: e?.ts ?? null, source: e ? `Event #${e.id}` : "Manager status" };
  }
  const lastRun = ev.find((e) => e.type === "MANAGER_RUN");
  const err = ev.find((e) => ERROR_TYPES.has(e.type) && age(e.ts, now) < ERROR_FOR);
  if (err && (!lastRun || err.id > lastRun.id)) {
    return { ...base, key: "error", headline: err.type.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()), detail: err.summary, symbols: err.symbol ? [{ symbol: err.symbol }] : [], at: err.ts, source: `Event #${err.id}` };
  }
  if (a.mode === "PAUSED") {
    return { ...base, key: "paused", headline: "Paused by the owner", detail: lastRun ? `Last decision: ${strip(lastRun.summary, a.name)}` : null, at: lastRun?.ts ?? null, source: "Manager registry" };
  }
  if (a.running) {
    const thisRun = lastRun && a.run_started && Date.parse(lastRun.ts) >= Date.parse(a.run_started) - 2000 ? lastRun : undefined;
    if (thisRun && intents(thisRun).length) {
      return { ...base, key: "preparing", headline: "Preparing orders", detail: strip(thisRun.summary, a.name), symbols: intents(thisRun), at: thisRun.ts, source: `Event #${thisRun.id}` };
    }
    return { ...base, key: "analyzing", headline: "Analyzing the portfolio", detail: "Checking prices, drift, and cash against the mandate. The decision appears here the moment it is recorded.", at: a.run_started, source: "Scheduler (run in progress)" };
  }
  if (a.open_orders.length) {
    const o = a.open_orders;
    return {
      ...base,
      key: "executing",
      headline: `Executing ${o.length} order${o.length > 1 ? "s" : ""}`,
      detail: o.map((x) => `${orderLine(x)} · ${x.status.toLowerCase()}`).join(" · "),
      symbols: o.map((x) => ({ symbol: x.symbol, side: x.side })),
      book: o[0].book,
      at: o[0].submitted_at ?? o[0].created_at,
      source: `Order${o.length > 1 ? "s" : ""} ${o.map((x) => `#${x.id}`).join(", ")}`,
    };
  }
  const latest = ev[0];
  if (latest && DONE_TYPES.has(latest.type) && age(latest.ts, now) < COMPLETED_FOR) {
    const runSyms = lastRun ? intents(lastRun) : [];
    const fills = ev.filter((e) => e.type === "ORDER_FILLED" && (!lastRun || e.id > lastRun.id) && e.symbol);
    const syms = latest.type === "ORDER_FILLED" ? fills.map((e) => ({ symbol: e.symbol!, side: sideOf(e) })) : latest.type === "MANAGER_RUN" || !latest.symbol ? runSyms : [{ symbol: latest.symbol, side: sideOf(latest) }];
    const headline =
      latest.type === "ORDER_FILLED" ? `${fills.length > 1 ? `${fills.length} orders` : "Order"} filled${latest.payload?.book === "SIM" ? " in simulation" : ""}`
      : latest.type === "ORDER_BLOCKED" ? "Order blocked by a safeguard"
      : latest.payload?.activity === "holding" ? "Review complete · holding"
      : "Decision recorded";
    return { ...base, key: "completed", headline, detail: strip(latest.summary, a.name), symbols: syms, at: latest.ts, book: (latest.payload?.book as string) ?? a.book, source: `Event #${latest.id}` };
  }
  if (latest?.type === "ORDER_DEFERRED" && age(latest.ts, now) < COMPLETED_FOR) {
    return { ...base, key: "waiting", headline: "Order deferred; will retry", detail: latest.summary, symbols: latest.symbol ? [{ symbol: latest.symbol, side: sideOf(latest) }] : [], at: latest.ts, source: `Event #${latest.id}` };
  }
  if (latest?.type === "MANAGER_WAITING" && age(latest.ts, now) < ERROR_FOR) {
    return { ...base, key: "waiting", headline: "Waiting for market data", detail: latest.summary, at: latest.ts, source: `Event #${latest.id}` };
  }
  return {
    ...base,
    key: "idle",
    headline: a.next_run ? "Idle until the next decision" : "Idle",
    detail: lastRun ? `Last decision: ${strip(lastRun.summary, a.name)}` : "No decisions recorded yet.",
    at: lastRun?.ts ?? null,
    source: lastRun ? `Event #${lastRun.id}` : "Event log",
  };
}

export function cioState(c: Space["cio"], live: FirmEvent[], now: number): AgentState {
  const base = { symbols: [], book: null, at: null, detail: null } as Pick<AgentState, "symbols" | "book" | "at" | "detail">;
  if (!c.configured) return { ...base, key: "inactive", headline: "Office not configured", detail: "The CIO service is not running on this host.", source: "Service" };
  if (c.working) {
    const tool = c.working.tool;
    return {
      ...base,
      key: "reviewing",
      headline: tool === "create_proposal" ? "Drafting a proposal" : "Answering your question",
      detail: tool ? `${TOOL_LABEL[tool] ?? `Using ${tool}`} · step ${c.working.steps}` : "Reading the firm snapshot and your conversation.",
      at: c.working.since,
      source: `Conversation #${c.working.conversation_id} (live)`,
    };
  }
  const ev = merge(c.recent, live, (e) => CIO_TYPES.has(e.type));
  const down = ev.find((e) => e.type === "AI_PROVIDER_UNAVAILABLE" && age(e.ts, now) < 60 * MIN);
  if (down) return { ...base, key: "error", headline: "AI provider unavailable", detail: down.summary, at: down.ts, source: `Event #${down.id}` };
  if (!c.provider_ready) return { ...base, key: "waiting", headline: "Waiting for an AI provider key", detail: "Conversations resume once the provider is configured on the Pi.", source: "Service" };
  const filed = ev.find((e) => e.type === "PROPOSAL_CREATED" && age(e.ts, now) < COMPLETED_FOR);
  if (filed) return { ...base, key: "completed", headline: "Proposal filed", detail: filed.summary, at: filed.ts, source: `Event #${filed.id}` };
  const n = c.pending_proposals.length;
  if (n) {
    return { ...base, key: "idle", headline: `${n} proposal${n > 1 ? "s" : ""} awaiting your decision`, detail: c.pending_proposals.map((p) => `#${p.id} ${p.title}`).join(" · "), at: c.pending_proposals[0].created_at, source: "Proposals" };
  }
  return { ...base, key: "idle", headline: "Available", detail: "Ask a question in the CIO Office; the CIO works only when you do.", source: "CIO office" };
}
