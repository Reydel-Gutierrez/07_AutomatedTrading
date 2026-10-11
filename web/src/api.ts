// Typed client for the Investment HQ API. Writes carry the CSRF token.

export type Perf = { twr: number | null; benchmark: number | null; excess: number | null; since: string | null; marks: number };
export type Series = { date: string; nav: number; flow: number; spy: number | null }[];
export type PerfFull = Perf & { series: Series };

export type FirmEvent = {
  id: number;
  ts: string;
  actor: string;
  type: string;
  severity: "info" | "warning" | "critical";
  manager_id: string | null;
  symbol: string | null;
  summary: string;
  payload: Record<string, any>;
};

export type ManagerCard = {
  id: string;
  name: string;
  role: string;
  mode: "INACTIVE" | "SIM" | "LIVE" | "PAUSED";
  status: string;
  activity: string | null;
  last_run: { at: string; summary: string; activity: string; book: string } | null;
  next_run: string | null;
  live: { nav: number; cash: number; invested: number };
  sim: { nav: number; cash: number; invested: number };
  performance: { LIVE: Perf; SIM: Perf };
};

export type Controls = {
  kill_switch: { engaged: boolean; reason: string | null; by: string | null; at: string | null; source: string };
  reconcile_hold: { active: boolean; reasons: string[]; since: string | null };
  drawdown_brake: { engaged: boolean; [k: string]: any };
  live_writes_enabled: boolean;
};

export type Overview = {
  as_of: string;
  price_source: string;
  books_open: boolean;
  firm: { nav: number; unallocated: number; suspense: number; invested: number; performance: PerfFull };
  allocation: { id: string; name: string; nav: number; pct: number }[];
  managers: ManagerCard[];
  holdings: { manager: string; symbol: string; quantity: number; price: number | null; value: number; cost: number; pnl: number }[];
  controls: Controls;
  broker: { connected: boolean; last_snapshot: Record<string, any> | null };
};

export type Position = { symbol: string; quantity: number; price: number | null; value: number; cost: number; pnl: number; weight: number; target: number };
export type BookDetail = { nav: number; cash: number; free_cash: number; invested: number; positions: Position[]; realized_pnl: number; performance: PerfFull };
export type Order = Record<string, any> & { id: number; book: string; symbol: string; side: string; status: string; created_at: string };

export type ManagerDetail = {
  id: string;
  name: string;
  role: string;
  mode: ManagerCard["mode"];
  active_book: "LIVE" | "SIM" | null;
  status: string;
  activity: string | null;
  mode_changed_at: string;
  mode_changed_by: string;
  mandate: { version: number; params: Record<string, any>; history: { version: number; created_at: string; created_by: string; note: string; params: Record<string, any> }[] };
  books: { LIVE: BookDetail; SIM: BookDetail };
  price_source: string;
  orders: Order[];
  runs: FirmEvent[];
  activity_feed: FirmEvent[];
  last_run: ManagerCard["last_run"];
  next_run: string | null;
  implemented: boolean;
};

export type Capital = {
  unallocated: number;
  suspense_cash: number;
  suspense_positions: Record<string, number>;
  deposit_split: Record<string, number>;
  foreign_orders: Record<string, { symbol: string; side: string; state: string; placed_agent: string; created_at: string; quantity: string }>;
  managers: { id: string; name: string; mode: string; cash: number; free_cash: number }[];
  flows: FirmEvent[];
};

export type SystemView = {
  health: Record<string, any>;
  controls: Controls;
  jobs: Record<string, { last_started: string | null; last_finished: string | null; last_ok: boolean | null; last_error: string | null; runs: number; failures: number; running: boolean }>;
  backup: Record<string, any> | null;
  firm_limits: Record<string, any>;
  ai_budget: { monthly_usd: number; per_agent: Record<string, number> };
  reconciliations: { id: number; ts: string; nav: string; cash: string; reconciled: number; diff: Record<string, any> }[];
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

function csrf(): string {
  const m = document.cookie.match(/(?:^|; )firm_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(method === "GET" ? {} : { "X-CSRF-Token": csrf() }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  get: <T,>(path: string) => request<T>("GET", path),
  post: <T = { ok: boolean },>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T = { ok: boolean },>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
};
