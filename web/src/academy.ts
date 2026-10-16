// Types for /api/academy: both Academy classes, forex (the Forex engine's records) and stocks (the
// Stock Swing Class engine's), in one shape. `market` tells them apart; read-only through HQ.

export type Stats = { trades: number; win_rate: number | null; avg_r: number | null; r_stdev: number | null; profit_factor: number | null; net_usd: number };
export type Check = { name: string; value?: unknown; rule?: string; passed: boolean; detail?: string; advisory?: boolean };
export type Persona = { tagline: string; about: string; color: string; look?: Record<string, unknown> };

export type ValidationSummary = {
  running: { id: number; status: string; started_at: string } | null;
  id?: number;
  version?: number;
  kind?: string;
  status?: string;
  passed?: boolean;
  score?: number | null;
  finished_at?: string;
  checks?: Check[];
  unseen?: (Stats & { return_pct: number | null; max_drawdown: number; sharpe: number | null; years: number; trades_per_month: number | null }) | null;
  trial_adjusted_sharpe?: number | null;
  report?: ValidationReport & { market?: Market; dd_within_limit_share?: number | null; good_faith_violations?: number };
};

export type WindowMetrics = { trades: number; win_rate: number | null; avg_r: number | null; profit_factor: number | null; net_usd: number; return_pct: number | null; max_drawdown: number; sharpe: number | null; years: number; trades_per_month: number | null };

export type ValidationReport = {
  status: string;
  passed: boolean;
  score: number;
  checks: Check[];
  windows: Record<string, [number, number]>;
  metrics: Record<string, WindowMetrics>;
  baseline: { avg_r: number[]; p_value: number };
  stress: WindowMetrics;
  monte_carlo_dd95: number | null;
  trial_adjusted_sharpe: number | null;
  trials: number;
  by_instrument: Record<string, { trades: number; net_usd: number; avg_r: number }>;
  equity: [number, number][];
  seconds: number;
  refused?: Record<string, number>;
};

export type Stage = "RESEARCH" | "PAPER" | "DEMO" | "RETIRED";

export type SprintSummary = {
  day: string;
  outcome: string;
  verdict?: string;
  change?: string | null;
  tested?: number;
  trials?: number;
  current?: { score: number; avg_r: number; trades: number; folds: (number | null)[]; positive_folds: number };
  best?: { label: string; score: number; avg_r: number; trades: number; folds: (number | null)[]; positive_folds: number };
  validation?: { status: string; passed: boolean; score: number; checks: Check[]; unseen?: WindowMetrics; holdout?: WindowMetrics; trial_adjusted_sharpe?: number | null } | null;
  day1?: { passed?: boolean; runs?: number; horizon_days?: number; profitable_share?: number; median_return?: number; p10_return?: number; best_return?: number; worst_return?: number; worst_drawdown?: number; rule?: string };
  top_candidates?: { label: string; score: number; avg_r: number; trades: number; positive_folds: number }[];
  recommend_retire?: boolean;
  sprints_so_far?: number;
  agent?: string;
  gated?: "candidate" | "current";
};

export type SprintRow = { id: number; agent_id: string; day: string; started_at: string; finished_at: string | null; from_version: number; to_version: number | null; outcome: string | null; passed: number | null; summary: SprintSummary };

export type SprintReport = {
  day: string;
  generated_at?: number;
  agents: { agent_id: string; name: string; market?: Market; outcome: string; passed: boolean; from_version: number; to_version: number | null; verdict: string; tested: number; unseen?: WindowMetrics | null; day1?: SprintSummary["day1"]; recommend_retire?: boolean }[];
  passed: string[];
  improved: string[];
  floor2: string[];
  sprints?: SprintRow[];
};

export type Market = "forex" | "stocks";

export type AgentCard = {
  id: string;
  name: string;
  market?: Market;
  desk_comparison?: { since: string | null; max_return: number | null; vs_max: number | null } | null;
  stage: Stage;
  floor: string;
  stage_since: string | null;
  klass: number;
  sprint: { id: number; day: string; outcome: string; passed: boolean; from_version: number; to_version: number | null; finished_at: string; verdict?: string } | null;
  sprinting: { running: boolean; since: number; step: string } | null;
  style: "day" | "swing";
  strategy: string;
  persona: Persona;
  status: string;
  status_reason: string | null;
  paused: string | null;
  version: number;
  version_note: string | null;
  auto_revise: boolean;
  trials: number;
  equity: number;
  cash: number | null;
  return_pct: number;
  max_drawdown: number;
  open_positions: number;
  paper: Stats;
  paper_current_version: Stats;
  validation: ValidationSummary | null;
  activity: string | null;
  last_event: { id: number; ts: string; type: string; summary: string } | null;
  readiness: { next: string | null; automatic: boolean; eligible: boolean; label: string; checks: Check[]; days_on_floor?: number };
  weekly: WeekResult[];
};

/** One 7-day period on the agent's current floor, counted from the day it arrived. */
export type WeekResult = { week: number; start: number; end: number; complete: boolean; start_equity: number; end_equity: number; return_pct: number; trades: number; net_usd: number };
export type StrategyInfo = { title?: string; style?: string; description?: string; tunables?: { name: string }[] };

export type EngineStatus = {
  online: boolean;
  demo?: { configured: boolean; problem: string | null; seats: number };
  heartbeat?: number;
  started_at?: number;
  market_open?: boolean;
  data_ready?: boolean;
  job?: { name: string; since: number } | null;
  queue?: string[];
  last_error?: string | null;
  data?: { ready?: boolean; progress?: string; rows?: number };
};

export type FxPrice = { instrument: string; bid: number; ask: number; time: number; tradeable: number; updated_at?: string };
export type AcademyEvent = { id: number; ts: string; agent_id: string | null; type: string; summary: string };

export type AcademyOverview = {
  available: boolean;
  reason?: string;
  engine?: EngineStatus;
  agents: AgentCard[];
  leaderboard?: string[];
  prices: FxPrice[];
  events: AcademyEvent[];
  latest_report?: SprintReport | null;
  sprint_day?: string | null;
  stock_engine?: (EngineStatus & { data?: { ready?: boolean; reason?: string; first_date?: string; last_date?: string; sessions?: number } }) | null;
};

export type PaperPosition = { id: string; instrument: string; side: number; units: number; entry: number; stop: number; target: number | null; opened_at: number; risk_usd: number; tag: string; version: number; financing_usd: number; mark: number | null };
export type PaperTrade = { id: string; version: number; instrument: string; side: number; units: number; entry: number; exit: number; opened_at: number; closed_at: number; pnl_usd: number; financing_usd: number; r: number; exit_reason: string; tag: string };
export type Version = { version: number; note: string; created_by: string; created_at: string; parent: number | null; validation_status: string; params: Record<string, unknown>; envelope: Record<string, unknown> };
export type Revision = { id: number; ts: string; from_version: number; to_version: number | null; outcome: string; chosen: string | null; reason: string; report: { current?: Record<string, number>; candidates?: ({ label: string } & Record<string, number>)[]; trials?: number } };

export type AgentDetail = AgentCard & {
  equity_series: [number, number][];
  positions: PaperPosition[];
  trades: PaperTrade[];
  versions: Version[];
  revisions: Revision[];
  events: { id: number; ts: string; type: string; summary: string }[];
  validation: ValidationSummary | null;
  decision: { at: number; bar: number; trigger: string; actions: number; note: Record<string, unknown> } | null;
  diagnostics: Record<"by_instrument" | "by_exit" | "by_session" | "by_side", Record<string, Stats>>;
  sprints: SprintRow[];
  transitions: { id: number; from_stage: string | null; to_stage: string; by: string; reason: string; ts: string }[];
  strategy_info?: StrategyInfo | null;
};

/** A forex pair as EUR/USD; a stock symbol as itself. */
export const pair = (i: string) => i.replace("_", "/");
export const pips = (i: string) => (i.endsWith("JPY") ? 100 : 10000);
export const fmtR = (r: number | null | undefined, d = 2) => (r === null || r === undefined ? "—" : `${r >= 0 ? "+" : ""}${r.toFixed(d)}R`);
export const fmtPx = (i: string, v: number | null | undefined) => (v === null || v === undefined ? "—" : v.toFixed(!i.includes("_") ? 2 : i.endsWith("JPY") ? 3 : 5));

/** Wording that differs between the two classes; everything else is shared. */
export const MARKET = {
  forex: { label: "Forex", instrument: "Pair", units: "Units", sprintAt: "1:00 AM", history: "OANDA history", trader: (style: string) => (style === "day" ? "Day trader" : "Swing trader"), marks: "The curve starts with the first 15-minute equity mark.", baseline: "Random-direction baselines", stress: "+0.5 pip" },
  stocks: { label: "Stocks", instrument: "Name", units: "Shares", sprintAt: "1:30 AM", history: "daily US stock history", trader: () => "Swing trader", marks: "The curve starts with the first daily close on paper.", baseline: "Random-name baselines", stress: "+10 bps" },
} as const;
export const marketOf = (a: { market?: Market } | null | undefined): Market => (a?.market === "stocks" ? "stocks" : "forex");
export const epochIso = (t: number) => new Date(t * 1000).toISOString();

export const FLOOR_ORDER: { stage: Stage; title: string; blurb: string }[] = [
  { stage: "DEMO", title: "Floor 3 · OANDA Demo Desk", blurb: "Two seats trading an OANDA practice account with real orders and fake money. 4+ weeks here, then Graham's memo and your decision on a live chair." },
  { stage: "PAPER", title: "Floor 2 · Paper trading", blurb: "Passed research. Each trades a fresh $10,000 virtual account for at least 4 weeks: forex on live OANDA prices, stocks on new daily bars beside Max's desk. Forex agents go on to the Demo Desk; stock agents to a live swing seat, by your decision." },
  { stage: "RESEARCH", title: "Floor 1 · Research", blurb: "No trading. A research sprint every night (forex 1:00 AM, stocks 1:30 AM New York) polishes each strategy on years of history; an agent moves up automatically when it passes every gate." },
];
