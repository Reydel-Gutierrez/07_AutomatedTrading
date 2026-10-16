// The Forex boardroom's planner: what an agent's tested edge is worth per week at a given risk
// and capital, and what a weekly dollar goal would take. Informational only: agents never see a
// goal, and nothing here changes anyone's risk or capital.
//
// Inputs come from each agent's own records: the unseen-data backtest (average R after costs,
// R spread, trades per month, max drawdown at its risk per trade) and its paper weeks.

import type { AgentCard } from "./academy";

const WEEKS_PER_MONTH = 52 / 12;
/** Per-trade risk above this is past anything the firm would run (and the 5:1 leverage cap blocks much of it). */
export const RISK_CEILING = 0.02;
/** Drawdown above this is flagged: a 25% hole needs +33% to climb out. */
export const DD_CEILING = 0.25;
/** Fewer unseen trades than this is too little evidence to plan dollars on. */
export const MIN_EVIDENCE = 30;

export type Edge = {
  avgR: number; // expected R per trade, after costs (unseen backtest)
  sdR: number | null; // spread of R per trade
  tradesPerWeek: number;
  ddR: number | null; // the backtest's max drawdown, in R (risk units), so it can be rescaled to any risk
  trades: number; // how much evidence the edge rests on
};

export function edgeOf(a: AgentCard): Edge | null {
  const u = a.validation?.unseen;
  if (!u || u.avg_r === null || u.avg_r === undefined || !u.trades_per_month) return null;
  return {
    avgR: u.avg_r,
    sdR: u.r_stdev ?? null,
    tradesPerWeek: u.trades_per_month / WEEKS_PER_MONTH,
    ddR: a.risk_pct && u.max_drawdown ? u.max_drawdown / a.risk_pct : null,
    trades: u.trades,
  };
}

/** Expected weekly P&L and its typical ±1σ range for `riskUsd` dollars risked per trade. */
export function weekly(e: Edge, riskUsd: number): { mean: number; sd: number | null; lossOdds: number | null } {
  const mean = riskUsd * e.avgR * e.tradesPerWeek;
  const sd = e.sdR !== null ? riskUsd * e.sdR * Math.sqrt(e.tradesPerWeek) : null;
  return { mean, sd, lossOdds: sd ? normCdf(-mean / sd) : null };
}

/** The backtest's worst drawdown, rescaled to another risk per trade (same losing streak in R). */
export function drawdownAt(e: Edge, riskPct: number): number | null {
  return e.ddR === null ? null : 1 - Math.pow(1 - riskPct, e.ddR);
}

/** Dollars that must be risked per trade for `goal` per week; null when the edge is not positive. */
export function riskUsdFor(e: Edge, goal: number): number | null {
  const perUnit = e.avgR * e.tradesPerWeek;
  return perUnit > 0 ? goal / perUnit : null;
}

/** Several agents trading side by side, each risking the same dollars per trade. */
export function combined(edges: Edge[], goal: number): { riskUsd: number | null; perUnit: number } {
  edges = edges.filter((e) => e.trades >= MIN_EVIDENCE);
  const perUnit = edges.reduce((s, e) => s + Math.max(e.avgR, 0) * e.tradesPerWeek, 0);
  return { riskUsd: perUnit > 0 ? goal / perUnit : null, perUnit };
}

/** Average net P&L of completed paper weeks (the reality check). */
export function paperWeekly(a: AgentCard): { avg: number; weeks: number } | null {
  const done = (a.weekly ?? []).filter((w) => w.complete);
  return done.length ? { avg: done.reduce((s, w) => s + w.net_usd, 0) / done.length, weeks: done.length } : null;
}

function normCdf(z: number): number {
  // Abramowitz-Stegun 7.1.26
  const t = 1 / (1 + 0.3275911 * Math.abs(z) / Math.SQRT2);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-(z * z) / 2);
  return z >= 0 ? (1 + y) / 2 : (1 - y) / 2;
}
