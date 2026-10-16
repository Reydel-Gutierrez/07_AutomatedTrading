// The Forex boardroom's planner card. Informational only: it turns each agent's tested edge into
// expected dollars per week, and shows what a weekly goal would take in capital and risk, with the
// drawdown that comes with it. Agents never see the goal; nothing here changes risk or capital.

import { useState } from "react";
import { Link } from "react-router-dom";
import type { AcademyOverview, AgentCard } from "../academy";
import { Card, Empty } from "../components/ui";
import { useResource } from "../hooks";
import { money, pct } from "../format";
import { DD_CEILING, MIN_EVIDENCE, RISK_CEILING, combined, drawdownAt, edgeOf, paperWeekly, riskUsdFor, weekly } from "../planner";

const STAGE: Record<string, string> = { RESEARCH: "research", PAPER: "paper", DEMO: "demo" };

function stored(key: string, fallback: number): number {
  try {
    const v = Number(localStorage.getItem(key));
    return Number.isFinite(v) && v > 0 ? v : fallback;
  } catch {
    return fallback;
  }
}
function remember(key: string, v: number) {
  try {
    localStorage.setItem(key, String(v));
  } catch {
    /* the planner works without storage */
  }
}

export default function ForexPlanner() {
  const { data, error } = useResource<AcademyOverview>("/api/academy", { every: 60000 });
  const [goal, setGoal] = useState(() => stored("planner.goal", 200));
  const [capital, setCapital] = useState(() => stored("planner.capital", 10_000));
  if (error) return <Card title="Planner"><Empty>{error.message}</Empty></Card>;
  if (!data) return <Card title="Planner"><p className="text-sm text-slate-500">Loading…</p></Card>;
  const agents = data.agents.filter((a) => a.market !== "stocks" && a.stage !== "RETIRED");
  const rows = agents.map((a) => ({ a, e: edgeOf(a) }));
  const proven = rows.filter((r) => r.e && r.e.trades >= MIN_EVIDENCE && (r.a.stage === "PAPER" || r.a.stage === "DEMO"));
  const team = combined(proven.map((r) => r.e!), goal);
  const num = (set: (n: number) => void, key: string) => (ev: React.ChangeEvent<HTMLInputElement>) => {
    const n = Number(ev.target.value);
    if (Number.isFinite(n) && n > 0) {
      set(n);
      remember(key, n);
    }
  };

  return (
    <Card title="Planner · what the edge is worth" action={<span className="text-xs text-slate-500">informational · agents never see a goal</span>}>
      <div className="flex flex-wrap items-end gap-4">
        <label className="text-xs text-slate-400">Weekly goal
          <div className="mt-1 flex items-center gap-1"><span className="text-slate-500">$</span>
            <input type="number" min={1} step={50} defaultValue={goal} onChange={num(setGoal, "planner.goal")} className="w-28 rounded-lg border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm text-slate-100" aria-label="Weekly goal in dollars" />
          </div>
        </label>
        <label className="text-xs text-slate-400">Capital per agent
          <div className="mt-1 flex items-center gap-1"><span className="text-slate-500">$</span>
            <input type="number" min={1} step={1000} defaultValue={capital} onChange={num(setCapital, "planner.capital")} className="w-32 rounded-lg border border-ink-700 bg-ink-900 px-2 py-1.5 text-sm text-slate-100" aria-label="Capital per agent in dollars" />
          </div>
        </label>
        <p className="max-w-xl text-xs text-slate-500">
          Expected dollars = dollars risked per trade × average R after costs × trades per week, from each agent's unseen-data backtest.
          Drawdown rescales the backtest's worst losing stretch to the new risk. Paper weeks are the reality check.
        </p>
      </div>

      <h3 className="label mb-2 mt-5">At each agent's current risk, with {money(capital)}</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr><th className="pb-1">Agent</th><th>Edge</th><th className="text-right">Risk / trade</th><th className="text-right">Expected / week</th><th className="text-right">Typical week</th><th className="text-right">Losing weeks</th><th className="text-right">Paper / week</th><th className="text-right">Worst drawdown</th></tr>
          </thead>
          <tbody>
            {rows.map(({ a, e }) => {
              const r = a.risk_pct ?? 0;
              const w = e ? weekly(e, capital * r) : null;
              const pw = paperWeekly(a);
              return (
                <tr key={a.id} className="border-t border-ink-800">
                  <td className="py-1.5"><Name a={a} /></td>
                  <td className="text-xs text-slate-400">{e ? `${e.avgR >= 0 ? "+" : ""}${e.avgR.toFixed(3)}R × ${e.tradesPerWeek.toFixed(1)}/wk (${e.trades} trades)` : "not validated yet"}</td>
                  <td className="text-right tabular-nums">{pct(r, 2)} · {money(capital * r)}</td>
                  <td className={`text-right tabular-nums ${w ? (w.mean >= 0 ? "text-emerald-300" : "text-rose-300") : ""}`}>{w ? money(w.mean) : "—"}</td>
                  <td className="text-right text-xs tabular-nums text-slate-400">{w?.sd ? `${money(w.mean - w.sd)} to ${money(w.mean + w.sd)}` : "—"}</td>
                  <td className="text-right tabular-nums">{w?.lossOdds !== null && w?.lossOdds !== undefined ? pct(w.lossOdds, 0) : "—"}</td>
                  <td className="text-right text-xs tabular-nums text-slate-400">{pw ? `${money(pw.avg)} (${pw.weeks} wk)` : a.stage === "RESEARCH" ? "not trading" : "first week running"}</td>
                  <td className="text-right tabular-nums">{e ? pct(drawdownAt(e, r), 1) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <h3 className="label mb-2 mt-6">To make {money(goal)} a week</h3>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead className="text-left text-xs text-slate-500">
            <tr><th className="pb-1">Agent</th><th className="text-right">Risked per trade</th><th className="text-right">Capital at its current risk</th><th className="text-right">Risk needed on {money(capital)}</th><th className="text-right">Drawdown at that risk</th><th>Verdict</th></tr>
          </thead>
          <tbody>
            {rows.map(({ a, e }) => {
              const need = e && e.trades >= MIN_EVIDENCE ? riskUsdFor(e, goal) : null;
              const r = a.risk_pct ?? 0;
              const riskOnCap = need !== null ? need / capital : null;
              const dd = e && riskOnCap !== null ? drawdownAt(e, Math.min(riskOnCap, 0.99)) : null;
              return (
                <tr key={a.id} className="border-t border-ink-800">
                  <td className="py-1.5"><Name a={a} /></td>
                  <td className="text-right tabular-nums">{need !== null ? money(need) : "—"}</td>
                  <td className="text-right tabular-nums">{need !== null && r ? money(need / r) : "—"}</td>
                  <td className="text-right tabular-nums">{riskOnCap !== null ? pct(riskOnCap, 2) : "—"}</td>
                  <td className={`text-right tabular-nums ${dd !== null && dd > DD_CEILING ? "text-rose-300" : ""}`}>{dd !== null ? pct(dd, 1) : "—"}</td>
                  <td className="text-xs text-slate-400">{verdict(a, e, riskOnCap, dd)}</td>
                </tr>
              );
            })}
            <tr className="border-t-2 border-ink-700">
              <td className="py-2 font-medium text-slate-200">Together ({proven.length ? proven.map((r) => r.a.name).join(", ") : "nobody past research yet"})</td>
              <td className="text-right tabular-nums">{team.riskUsd !== null ? `${money(team.riskUsd)} each` : "—"}</td>
              <td className="text-right tabular-nums">{team.riskUsd !== null ? money(proven.reduce((s, x) => s + team.riskUsd! / (x.a.risk_pct || 1), 0)) : "—"}</td>
              <td className="text-right tabular-nums">{team.riskUsd !== null ? pct(team.riskUsd / capital, 2) : "—"}</td>
              <td />
              <td className="text-xs text-slate-400">{proven.length ? "Only agents past research count; research backtests are unproven." : "The floor has no proven edge to plan with yet."}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="mt-3 text-xs text-slate-500">
        A goal is a planning number, not an order: raising risk to chase it scales the losing weeks and the drawdown by the same factor.
        Real money moves only through the Demo Desk, Graham's memo, and your authorization. <Link to="/academy" className="text-gold-400 hover:underline">The Academy →</Link>
      </p>
    </Card>
  );
}

function Name({ a }: { a: AgentCard }) {
  return (
    <span>
      <span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: a.persona.color }} />
      <Link to={`/firm/${a.id}`} className="text-gold-400 hover:underline">{a.name}</Link>
      <span className="text-xs text-slate-500"> {STAGE[a.stage] ?? a.stage.toLowerCase()}</span>
    </span>
  );
}

function verdict(a: AgentCard, e: ReturnType<typeof edgeOf>, risk: number | null, dd: number | null): string {
  if (!e) return "No validated edge to plan with.";
  if (e.avgR <= 0) return "No positive edge: no amount of capital reaches the goal.";
  if (e.trades < MIN_EVIDENCE) return `Only ${e.trades} unseen trade${e.trades === 1 ? "" : "s"}: too little evidence to plan with.`;
  const unproven = a.stage === "RESEARCH" ? " Unproven: still in research." : "";
  if (risk !== null && risk > RISK_CEILING) return `Needs ${pct(risk, 1)} a trade: past the ${pct(RISK_CEILING, 0)} ceiling. Needs more capital, not more risk.${unproven}`;
  if (dd !== null && dd > DD_CEILING) return `Drawdown past ${pct(DD_CEILING, 0)}: too deep.${unproven}`;
  return `Within limits at this capital.${unproven}`;
}
