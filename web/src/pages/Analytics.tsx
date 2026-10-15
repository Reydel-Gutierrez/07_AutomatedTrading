/**
 * Firm analytics: the Boardroom & Treasury's full numbers. Capital, contributions against value,
 * overall and daily returns against the S&P 500, drawdowns, managers, and the Academy.
 */
import { Link } from "react-router-dom";
import { useLive } from "../App";
import { Card, Empty, Stat } from "../components/ui";
import { useResource } from "../hooks";
import { todayReturn } from "../space/FirmMap";
import { day, money, pct, signedPct, tone, when } from "../format";

type Daily = { date: string; nav: number; flow: number; return: number; spy: number | null };
type Flow = { ts: string; kind: string; amount: number; contributed: number };
type Money = {
  contributed: number; deposits: number; withdrawals: number; dividends: number;
  flows: Flow[]; daily: Daily[];
  last_mark: { date: string; nav: number } | null; flows_since_last_mark: number; days_tracked: number;
  best_day: Daily | null; worst_day: Daily | null; up_days: number; down_days: number;
  max_drawdown: number; current_drawdown: number;
};
type FloorAgent = {
  id: string; name: string; color: string; version: number; style: string; equity: number; return_pct: number;
  trades: number; avg_r: number | null; validation: string | null; unseen_avg_r: number | null; unseen_trades: number | null;
  last_sprint: string | null; readiness: string;
};
type AcademyBlock = {
  available: boolean; engine_online?: boolean;
  floors?: Record<"RESEARCH" | "PAPER" | "DEMO" | "RETIRED", FloorAgent[]>;
  counts?: Record<string, number>; paper_equity?: number; paper_capital?: number;
  latest_report?: { day: string; passed: string[]; improved: string[]; floor2: string[] } | null;
  sprint_history?: { day: string; tested: number; passed: number; improved: number }[];
};

const KIND: Record<string, string> = { OPENING_BALANCE: "Opening balance", EXTERNAL_IN: "Deposit", EXTERNAL_OUT: "Withdrawal" };
const FLOOR_LABEL: [keyof NonNullable<AcademyBlock["floors"]>, string][] = [["DEMO", "Floor 3 · Demo Desk"], ["PAPER", "Floor 2 · Paper trading"], ["RESEARCH", "Floor 1 · Research"]];

export default function Analytics() {
  const { overview: o, bump } = useLive();
  const { data, error } = useResource<{ money: Money; academy: AcademyBlock }>("/api/analytics", { every: 60000, bump });
  if (error) return <div className="text-rose-400">Could not load analytics: {error.message}</div>;
  if (!data || !o) return <div className="text-slate-500">Loading analytics…</div>;
  const m = data.money;
  const ac = data.academy;
  const nav = o.firm.nav;
  const gain = nav - m.contributed;
  const gainPct = m.contributed ? gain / m.contributed : null;
  const today = todayReturn(nav, m.last_mark, m.flows_since_last_mark, m.daily[m.daily.length - 1] ?? null);
  const perf = o.firm.performance;
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-50">Firm analytics</h1>
        <p className="text-sm text-slate-400">Live value from current prices; daily figures from the firm's end-of-day marks (as of {when(o.as_of)}).</p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card><Stat label="Current capital" value={money(nav)} sub={`${money(o.firm.unallocated)} unallocated`} /></Card>
        <Card><Stat label="Contributed" value={money(m.contributed)} sub={`${money(m.deposits)} in · ${money(m.withdrawals)} out`} /></Card>
        <Card><Stat label="Gain on contributions" value={<span className={tone(gain)}>{`${gain >= 0 ? "+" : "-"}${money(Math.abs(gain))}`}</span>} sub={<span className={tone(gainPct)}>{signedPct(gainPct)}</span>} /></Card>
        <Card><Stat label="Today" value={<span className={tone(today)}>{signedPct(today)}</span>} sub={m.last_mark ? `vs close of ${m.last_mark.date}` : "No close recorded yet"} /></Card>
        <Card><Stat label="Overall return (time-weighted)" value={<span className={tone(perf.twr)}>{signedPct(perf.twr)}</span>} sub={perf.since ? `since ${perf.since}` : "—"} /></Card>
        <Card><Stat label="S&P 500, same days" value={<span className={tone(perf.benchmark)}>{signedPct(perf.benchmark)}</span>} sub={<>Excess <span className={tone(perf.excess)}>{signedPct(perf.excess)}</span></>} /></Card>
        <Card><Stat label="Max drawdown" value={pct(m.max_drawdown, 2)} sub={`Now ${pct(m.current_drawdown, 2)} below peak`} /></Card>
        <Card><Stat label="Daily closes tracked" value={m.days_tracked} sub={`${m.up_days} up · ${m.down_days} down · ${money(m.dividends)} dividends`} /></Card>
      </div>

      <Card title="Value against what you put in">
        <Growth daily={m.daily} flows={m.flows} />
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Managers">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th className="pb-2">Manager</th><th>Mode</th><th className="text-right">Value</th><th className="text-right">Share</th><th className="text-right">Return</th><th className="text-right">S&P</th></tr></thead>
            <tbody>
              {o.managers.map((mg) => {
                const p = mg.mode === "SIM" ? mg.performance.SIM : mg.performance.LIVE;
                const value = mg.mode === "SIM" ? mg.sim.nav : mg.live.nav;
                const share = o.allocation.find((a) => a.id === mg.id)?.pct;
                return (
                  <tr key={mg.id} className="border-t border-slate-800">
                    <td className="py-2"><Link className="text-gold-400 hover:underline" to={`/managers/${mg.id}`}>{mg.name}</Link></td>
                    <td className="text-xs text-slate-400">{mg.mode}</td>
                    <td className="text-right tabular-nums">{money(value)}</td>
                    <td className="text-right tabular-nums text-slate-400">{mg.mode === "SIM" ? "sim" : pct(share, 0)}</td>
                    <td className={`text-right tabular-nums ${tone(p.twr)}`}>{signedPct(p.twr)}</td>
                    <td className={`text-right tabular-nums ${tone(p.benchmark)}`}>{signedPct(p.benchmark)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>

        <Card title="Daily returns">
          {m.daily.length === 0 ? <Empty>No daily closes yet.</Empty> : (
            <>
              <div className="mb-3 flex gap-6 text-xs text-slate-400">
                {m.best_day && <span>Best {m.best_day.date}: <span className="text-emerald-400">{signedPct(m.best_day.return)}</span></span>}
                {m.worst_day && <span>Worst {m.worst_day.date}: <span className="text-rose-400">{signedPct(m.worst_day.return)}</span></span>}
              </div>
              <div className="max-h-72 overflow-y-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-slate-900 text-left text-xs text-slate-500"><tr><th className="pb-2">Day</th><th className="text-right">Value</th><th className="text-right">Firm</th><th className="text-right">S&P</th><th className="text-right">Flow</th></tr></thead>
                  <tbody>
                    {[...m.daily].reverse().map((d) => (
                      <tr key={d.date} className="border-t border-slate-800">
                        <td className="py-1.5">{d.date}</td>
                        <td className="text-right tabular-nums">{money(d.nav)}</td>
                        <td className={`text-right tabular-nums ${tone(d.return)}`}>{signedPct(d.return)}</td>
                        <td className={`text-right tabular-nums ${tone(d.spy)}`}>{signedPct(d.spy)}</td>
                        <td className="text-right tabular-nums text-slate-400">{d.flow ? money(d.flow) : ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </Card>
      </div>

      <Card title="Contributions">
        {m.flows.length === 0 ? <Empty>No deposits yet.</Empty> : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th className="pb-2">When</th><th>Kind</th><th className="text-right">Amount</th><th className="text-right">Total contributed</th></tr></thead>
            <tbody>
              {[...m.flows].reverse().map((f, i) => (
                <tr key={i} className="border-t border-slate-800">
                  <td className="py-1.5">{day(f.ts)}</td>
                  <td className="text-slate-300">{KIND[f.kind] ?? f.kind}</td>
                  <td className={`text-right tabular-nums ${tone(f.amount)}`}>{money(f.amount)}</td>
                  <td className="text-right tabular-nums">{money(f.contributed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      <AcademySection ac={ac} />
    </div>
  );
}

/** Firm value (line) against cumulative contributions (step), from the daily marks. */
function Growth({ daily, flows }: { daily: Daily[]; flows: Flow[] }) {
  if (daily.length < 2) return <Empty>The chart fills in after a couple of daily closes.</Empty>;
  const W = 800, H = 200, P = 8;
  const contributedAt = (date: string) => {
    let c = 0;
    for (const f of flows) if (f.ts.slice(0, 10) <= date) c = f.contributed;
    return c;
  };
  const navs = daily.map((d) => d.nav);
  const contrib = daily.map((d) => contributedAt(d.date));
  const lo = Math.min(...navs, ...contrib.filter((c) => c > 0));
  const hi = Math.max(...navs, ...contrib);
  const X = (i: number) => P + (i / (daily.length - 1)) * (W - 2 * P);
  const Y = (v: number) => H - P - ((v - lo) / (hi - lo || 1)) * (H - 2 * P);
  const line = (vals: number[]) => vals.map((v, i) => `${X(i)},${Y(v)}`).join(" ");
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-52 w-full" preserveAspectRatio="none">
        <polyline points={line(contrib)} fill="none" stroke="#64748b" strokeWidth="1.5" strokeDasharray="5 4" vectorEffect="non-scaling-stroke" />
        <polyline points={line(navs)} fill="none" stroke="#34d399" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="mt-2 flex justify-between text-xs text-slate-500">
        <span>{daily[0].date}</span>
        <span><span className="text-emerald-400">━</span> Firm value · <span className="text-slate-400">┅</span> Contributed</span>
        <span>{daily[daily.length - 1].date}</span>
      </div>
    </div>
  );
}

function AcademySection({ ac }: { ac: AcademyBlock }) {
  if (!ac.available || !ac.floors) {
    return <Card title="The 537 Academy"><Empty>The Forex engine has not created the Academy yet.</Empty></Card>;
  }
  const paperN = (ac.counts?.PAPER ?? 0) + (ac.counts?.DEMO ?? 0);
  const paperRet = ac.paper_capital ? (ac.paper_equity ?? 0) / ac.paper_capital - 1 : null;
  return (
    <Card title="The 537 Academy" action={<Link className="text-xs text-gold-400 hover:underline" to="/academy">Open the Academy →</Link>}>
      <div className="mb-4 grid grid-cols-2 gap-4 md:grid-cols-4">
        <Stat label="Engine" value={ac.engine_online ? "Online" : "Offline"} />
        <Stat label="Agents trading paper" value={paperN} sub={paperN ? `${money(ac.paper_equity)} of ${money(ac.paper_capital)} virtual` : "None yet"} />
        <Stat label="Paper return, combined" value={<span className={tone(paperRet)}>{signedPct(paperRet)}</span>} sub="Virtual money, live OANDA prices" />
        <Stat label="Latest sprint" value={ac.latest_report?.day ?? "—"} sub={ac.latest_report ? `Passed: ${ac.latest_report.passed.join(", ") || "none"}` : undefined} />
      </div>
      <div className="space-y-4">
        {FLOOR_LABEL.map(([stage, label]) => (
          <div key={stage}>
            <div className="label mb-1">{label} ({ac.floors![stage]?.length ?? 0})</div>
            {(ac.floors![stage] ?? []).length === 0 ? <div className="text-xs text-slate-500">Nobody on this floor.</div> : (
              <table className="w-full text-sm">
                <tbody>
                  {ac.floors![stage].map((a) => (
                    <tr key={a.id} className="border-t border-slate-800">
                      <td className="py-1.5"><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: a.color }} /><Link className="text-gold-400 hover:underline" to={`/academy/${a.id}`}>{a.name}</Link> <span className="text-xs text-slate-500">v{a.version} · {a.style}</span></td>
                      {stage === "RESEARCH" ? (
                        <>
                          <td className="text-xs text-slate-400">Validation: {a.validation ?? "—"}</td>
                          <td className={`text-right text-xs tabular-nums ${tone(a.unseen_avg_r)}`}>{a.unseen_avg_r !== null ? `${a.unseen_avg_r >= 0 ? "+" : ""}${a.unseen_avg_r.toFixed(3)}R unseen (${a.unseen_trades})` : "—"}</td>
                          <td className="text-right text-xs text-slate-400">{a.last_sprint ?? "no sprint yet"}</td>
                        </>
                      ) : (
                        <>
                          <td className="text-right tabular-nums">{money(a.equity)}</td>
                          <td className={`text-right tabular-nums ${tone(a.return_pct)}`}>{signedPct(a.return_pct)}</td>
                          <td className="text-right text-xs text-slate-400">{a.trades} trades · {a.readiness}</td>
                        </>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ))}
      </div>
      {(ac.sprint_history?.length ?? 0) > 0 && (
        <div className="mt-5">
          <div className="label mb-1">Nightly sprints</div>
          <div className="flex flex-wrap gap-2">
            {ac.sprint_history!.map((s) => (
              <Link key={s.day} to={`/academy/reports/${s.day}`} className="rounded border border-slate-800 px-2 py-1 text-xs hover:border-slate-600">
                <span className="text-slate-400">{s.day.slice(5)}</span> <span className={s.passed ? "text-emerald-400" : "text-slate-500"}>{s.passed}/{s.tested} passed</span>{s.improved ? <span className="text-sky-300"> · {s.improved} improved</span> : null}
              </Link>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}
