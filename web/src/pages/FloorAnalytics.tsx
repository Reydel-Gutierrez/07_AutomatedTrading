/**
 * One floor's boardroom, in depth: Core Investments (L4), Equity Trading (L3), the Forex Division (L2), Operations (L1),
 * and each Academy floor. Opened over the map by clicking that floor's boardroom.
 */
import { Link, useParams } from "react-router-dom";
import { useLive } from "../App";
import { type AgentCard, type Check, type WeekResult, fmtR } from "../academy";
import { Card, Empty, Stat } from "../components/ui";
import { useResource } from "../hooks";
import { money, pct, signedPct, tone } from "../format";
import { CheckList } from "./Academy";

type Daily = { date: string; nav: number; flow: number; return: number; spy: number | null };
type BookStats = { daily: Daily[]; days_tracked: number; max_drawdown: number; current_drawdown: number; best_day: Daily | null; worst_day: Daily | null; up_days: number; down_days: number };
type CoreManager = BookStats & { id: string; name: string; role: string; mode: string; book: string; implemented: boolean; orders: Record<string, number>; decisions: number };
type OpsDay = { day: string; events: number; warnings: number; critical: number; runs: number; orders: number; fills: number };
type FloorAgent = {
  id: string; name: string; color: string; style: string; version: number; since: string | null; equity: number; return_pct: number; max_drawdown: number; open_positions: number;
  this_floor: AgentCard["paper"]; weekly: WeekResult[]; readiness: AgentCard["readiness"];
  validation: { status: string | null; passed: boolean | null; unseen: { avg_r: number | null; trades: number } | null; failing: string[] } | null;
  sprint: AgentCard["sprint"]; auto_revise: boolean; trials: number;
};
type FloorData = {
  available: boolean; stage: string; floor: string; agents: FloorAgent[];
  engine: { online?: boolean; market_open?: boolean; data_ready?: boolean; demo?: { configured: boolean; seats: number } };
  sprint_history?: { day: string; tested: number; passed: number; improved: number; promoted: number }[];
  latest_report?: { day: string; passed: string[]; improved: string[] } | null;
};

const TITLES: Record<string, [string, string]> = {
  core: ["Core Investments", "L4 · Tim's and Alex's records, day by day, against the S&P 500"],
  trading: ["Equity Trading", "L3 · the swing desk's record, day by day, against the S&P 500"],
  forex: ["Forex Division", "L2 · live chairs and the Academy pipeline that fills them"],
  operations: ["Operations", "L1 · what the firm recorded over the last two weeks"],
  "academy-demo": ["Floor 3 · Demo Desk", "Real orders on an OANDA practice account (fake money)"],
  "academy-hall": ["Floor 2 · Paper Trading Hall", "$10,000 virtual accounts on live OANDA prices, week by week"],
  "academy-research": ["Floor 1 · Research", "No trading: nightly sprints on OANDA history, then the gates"],
};

export default function FloorAnalytics() {
  const { scope = "" } = useParams();
  const [title, sub] = TITLES[scope] ?? ["Analytics", ""];
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold text-slate-50">{title}</h1>
        <p className="text-sm text-slate-400">{sub}</p>
      </div>
      {scope === "core" ? <Core ids={["tim", "alex"]} /> : scope === "trading" ? <Core ids={["max"]} /> : scope === "operations" ? <Operations /> : scope === "forex" ? <Forex /> : scope.startsWith("academy-") ? <AcademyFloor scope={scope} /> : <Empty>Unknown floor.</Empty>}
    </div>
  );
}

function useScope<T>(scope: string) {
  const { bump } = useLive();
  return useResource<T>(`/api/analytics/${scope}`, { every: 60000, bump });
}

function Loading({ error }: { error: { message: string } | null }) {
  return error ? <div className="text-rose-400">Could not load: {error.message}</div> : <div className="text-slate-500">Loading…</div>;
}

// -- L4 and L3 (both read the managers' records) -------------------------------------------------
function Core({ ids }: { ids: string[] }) {
  const { overview: o } = useLive();
  const { data, error } = useScope<{ managers: CoreManager[] }>("core");
  if (!data || !o) return <Loading error={error} />;
  return (
    <div className="space-y-6">
      {data.managers.filter((m) => ids.includes(m.id)).map((m) => {
        const card = o.managers.find((c) => c.id === m.id);
        const sim = m.mode === "SIM";
        const perf = card ? (sim ? card.performance.SIM : card.performance.LIVE) : null;
        const value = card ? (sim ? card.sim.nav : card.live.nav) : null;
        return (
          <Card key={m.id} title={<span>{m.name} · <span className="text-slate-400">{m.mode.toLowerCase()}{sim ? " (simulated money)" : ""}</span></span>} action={<Link className="text-xs text-gold-400 hover:underline" to={`/firm/${m.id}`}>Sit down with {m.name} →</Link>}>
            <p className="-mt-2 mb-4 text-xs text-slate-500">{m.role}</p>
            {!m.implemented ? <Empty>{m.name}'s strategy opens in a later phase.</Empty> : (
              <>
                <div className="grid grid-cols-2 gap-4 md:grid-cols-6">
                  <Stat label="Value" value={money(value)} />
                  <Stat label="Return" value={<span className={tone(perf?.twr)}>{signedPct(perf?.twr)}</span>} sub={<>S&P <span className={tone(perf?.benchmark)}>{signedPct(perf?.benchmark)}</span></>} />
                  <Stat label="Max drawdown" value={pct(m.max_drawdown, 2)} sub={`now ${pct(m.current_drawdown, 2)}`} />
                  <Stat label="Daily closes" value={m.days_tracked} sub={`${m.up_days} up · ${m.down_days} down`} />
                  <Stat label="Decisions" value={m.decisions} />
                  <Stat label="Orders" value={Object.values(m.orders).reduce((a, b) => a + b, 0)} sub={`${m.orders.FILLED ?? 0} filled`} />
                </div>
                <DailyTable daily={m.daily} best={m.best_day} worst={m.worst_day} />
              </>
            )}
          </Card>
        );
      })}
    </div>
  );
}

function DailyTable({ daily, best, worst }: { daily: Daily[]; best: Daily | null; worst: Daily | null }) {
  if (!daily.length) return <p className="mt-4 text-sm text-slate-500">Daily returns appear after two closes.</p>;
  return (
    <div className="mt-4">
      <div className="mb-2 flex gap-6 text-xs text-slate-400">
        {best && <span>Best {best.date}: <span className="text-emerald-400">{signedPct(best.return)}</span></span>}
        {worst && <span>Worst {worst.date}: <span className="text-rose-400">{signedPct(worst.return)}</span></span>}
      </div>
      <div className="max-h-56 overflow-y-auto">
        <table className="w-full text-sm">
          <thead className="sticky top-0 bg-ink-900 text-left text-xs text-slate-500"><tr><th className="pb-1">Day</th><th className="text-right">Value</th><th className="text-right">Return</th><th className="text-right">S&P</th><th className="text-right">Flow</th></tr></thead>
          <tbody>
            {[...daily].reverse().map((d) => (
              <tr key={d.date} className="border-t border-ink-800">
                <td className="py-1">{d.date}</td>
                <td className="text-right tabular-nums">{money(d.nav)}</td>
                <td className={`text-right tabular-nums ${tone(d.return)}`}>{signedPct(d.return)}</td>
                <td className={`text-right tabular-nums ${tone(d.spy)}`}>{signedPct(d.spy)}</td>
                <td className="text-right tabular-nums text-slate-400">{d.flow ? money(d.flow) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// -- L1 -------------------------------------------------------------------------------------------
function Operations() {
  const { data, error } = useScope<{ days: OpsDay[]; top_types: { type: string; n: number }[]; totals: { events: number; orders: number } }>("operations");
  if (!data) return <Loading error={error} />;
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card><Stat label="Events, all time" value={data.totals.events.toLocaleString()} /></Card>
        <Card><Stat label="Orders, all time" value={data.totals.orders.toLocaleString()} /></Card>
        <Card><Stat label="Events, 14 days" value={data.days.reduce((a, d) => a + d.events, 0)} /></Card>
        <Card><Stat label="Warnings, 14 days" value={data.days.reduce((a, d) => a + d.warnings + d.critical, 0)} /></Card>
      </div>
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card title="Day by day">
          {data.days.length ? (
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-slate-500"><tr><th className="pb-2">Day (UTC)</th><th className="text-right">Decisions</th><th className="text-right">Orders</th><th className="text-right">Filled</th><th className="text-right">Warnings</th><th className="text-right">Events</th></tr></thead>
              <tbody>
                {data.days.map((d) => (
                  <tr key={d.day} className="border-t border-ink-800">
                    <td className="py-1.5">{d.day}</td>
                    <td className="text-right tabular-nums">{d.runs}</td>
                    <td className="text-right tabular-nums">{d.orders}</td>
                    <td className="text-right tabular-nums">{d.fills}</td>
                    <td className={`text-right tabular-nums ${d.warnings + d.critical ? "text-amber-300" : ""}`}>{d.warnings + d.critical}</td>
                    <td className="text-right tabular-nums text-slate-400">{d.events}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : <Empty>Nothing recorded in the last two weeks.</Empty>}
        </Card>
        <Card title="Most recorded, 14 days">
          <ul className="space-y-1.5 text-sm">
            {data.top_types.map((t) => <li key={t.type} className="flex justify-between"><span className="text-slate-300">{t.type.toLowerCase().replace(/_/g, " ")}</span><span className="tabular-nums text-slate-400">{t.n}</span></li>)}
          </ul>
          <Link to="/activity" className="mt-3 inline-block text-xs text-gold-400 hover:underline">The full event log →</Link>
        </Card>
      </div>
    </div>
  );
}

// -- L2 -------------------------------------------------------------------------------------------
type Pipeline = { available: boolean; engine_online?: boolean; counts?: Record<string, number>; floors?: Record<string, { id: string; name: string; color: string; version: number; style: string; return_pct: number; trades: number; validation: string | null; unseen_avg_r: number | null; readiness: string }[]> };

function Forex() {
  const { data, error } = useScope<{ academy: Pipeline; chairs: number; live_chairs_filled: number }>("forex");
  if (!data) return <Loading error={error} />;
  const ac = data.academy;
  const chairs = [["Day Chair 1", "Intraday, flat by Friday"], ["Day Chair 2", "Intraday, flat by Friday"], ["Swing Chair 1", "Multi-day, weekend rules"], ["Swing Chair 2", "Multi-day, weekend rules"]];
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Card><Stat label="Live chairs filled" value={`${data.live_chairs_filled} of ${data.chairs}`} /></Card>
        <Card><Stat label="Live forex trading" value={<span className="text-amber-300">Not authorized</span>} sub="Only you can authorize it" /></Card>
        <Card><Stat label="Academy engine" value={ac.engine_online ? "Online" : "Offline"} /></Card>
        <Card><Stat label="Candidates past research" value={(ac.counts?.PAPER ?? 0) + (ac.counts?.DEMO ?? 0)} sub={`${ac.counts?.RESEARCH ?? 0} still in research`} /></Card>
      </div>
      <Card title="The chairs">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {chairs.map(([n, s]) => (
            <div key={n} className="rounded-xl border border-dashed border-ink-600 p-3">
              <div className="text-sm font-medium text-slate-100">{n}</div>
              <div className="text-xs text-slate-500">{s}</div>
              <div className="mt-2 text-xs text-slate-400">Open</div>
            </div>
          ))}
        </div>
        <p className="mt-3 text-xs text-slate-500">A chair is filled only after an agent clears the Demo Desk, Graham writes a memo, and you authorize the agent and its capital.</p>
      </Card>
      <Card title="Academy pipeline" action={<Link className="text-xs text-gold-400 hover:underline" to="/academy">Open the Academy →</Link>}>
        {!ac.available || !ac.floors ? <Empty>The Forex engine has not created the Academy yet.</Empty> : (
          <div className="space-y-4">
            {(["DEMO", "PAPER", "RESEARCH"] as const).map((st) => (
              <div key={st}>
                <div className="label mb-1">{{ DEMO: "Floor 3 · Demo Desk", PAPER: "Floor 2 · Paper trading", RESEARCH: "Floor 1 · Research" }[st]} ({ac.floors![st]?.length ?? 0})</div>
                {(ac.floors![st] ?? []).length ? (
                  <table className="w-full text-sm">
                    <tbody>
                      {ac.floors![st].map((a) => (
                        <tr key={a.id} className="border-t border-ink-800">
                          <td className="py-1.5"><span className="mr-2 inline-block h-2 w-2 rounded-full" style={{ background: a.color }} /><Link to={`/firm/${a.id}`} className="text-gold-400 hover:underline">{a.name}</Link> <span className="text-xs text-slate-500">v{a.version} · {a.style}</span></td>
                          <td className="text-right text-xs">{st === "RESEARCH" ? <span className={tone(a.unseen_avg_r)}>{fmtR(a.unseen_avg_r, 3)} unseen</span> : <span className={tone(a.return_pct)}>{signedPct(a.return_pct)}</span>}</td>
                          <td className="text-right text-xs text-slate-400">{a.readiness}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : <div className="text-xs text-slate-500">Nobody on this floor.</div>}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}

// -- the Academy floors ----------------------------------------------------------------------------
function AcademyFloor({ scope }: { scope: string }) {
  const { data, error } = useScope<FloorData>(scope);
  if (!data) return <Loading error={error} />;
  if (!data.available) return <Empty>The Forex engine has not created the Academy yet.</Empty>;
  const trading = data.stage !== "RESEARCH";
  return (
    <div className="space-y-6">
      {data.stage === "DEMO" && (
        <Card>
          <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
            <Stat label="Seats taken" value={`${data.agents.length} of 2`} />
            <Stat label="OANDA practice account" value={data.engine.demo?.configured ? "Connected" : "Not set up yet"} sub={data.engine.demo?.configured ? `${data.engine.demo.seats} seat accounts` : "Run oanda_credentials.py set-practice on the Pi when you're ready"} />
            <Stat label="Real money" value="None" sub="The practice account uses fake money" />
          </div>
        </Card>
      )}
      {data.stage === "RESEARCH" && <ResearchSummary data={data} />}
      {data.agents.length === 0 ? (
        <Card><Empty>{data.stage === "PAPER" ? "No agent has passed research yet. Agents arrive here automatically when they pass every gate." : data.stage === "DEMO" ? "No agent has a Demo Desk seat yet. You give one to a Floor 2 agent after evaluating its weekly results." : "No agents in research."}</Empty></Card>
      ) : data.agents.map((a) => (
        <Card key={a.id} title={<span><span className="mr-2 inline-block h-2.5 w-2.5 rounded-full" style={{ background: a.color }} />{a.name} <span className="text-xs font-normal text-slate-500">v{a.version} · {a.style} trader</span></span>} action={<Link className="text-xs text-gold-400 hover:underline" to={`/firm/${a.id}`}>Sit down with {a.name} →</Link>}>
          {trading ? <TradingAgent a={a} /> : <ResearchAgent a={a} />}
        </Card>
      ))}
    </div>
  );
}

function TradingAgent({ a }: { a: FloorAgent }) {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
      <div>
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Stat label="Equity" value={money(a.equity)} sub="started at $10,000" />
          <Stat label="Return" value={<span className={tone(a.return_pct)}>{signedPct(a.return_pct)}</span>} />
          <Stat label="Max drawdown" value={pct(a.max_drawdown, 1)} />
          <Stat label="Trades here" value={a.this_floor.trades} sub={`avg ${fmtR(a.this_floor.avg_r)} · win ${pct(a.this_floor.win_rate, 0)}`} />
        </div>
        <h3 className="label mb-2 mt-5">Week by week</h3>
        {a.weekly.length ? (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th className="pb-1">Week</th><th>From</th><th className="text-right">Start</th><th className="text-right">End</th><th className="text-right">Return</th><th className="text-right">Trades</th><th className="text-right">Net</th></tr></thead>
            <tbody>
              {a.weekly.map((w) => (
                <tr key={w.week} className="border-t border-ink-800">
                  <td className="py-1.5">{w.week}{w.complete ? "" : <span className="text-xs text-slate-500"> so far</span>}</td>
                  <td className="text-slate-400">{new Date(w.start * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</td>
                  <td className="text-right tabular-nums">{money(w.start_equity)}</td>
                  <td className="text-right tabular-nums">{money(w.end_equity)}</td>
                  <td className={`text-right tabular-nums ${tone(w.return_pct)}`}>{signedPct(w.return_pct)}</td>
                  <td className="text-right tabular-nums">{w.trades}</td>
                  <td className={`text-right tabular-nums ${tone(w.net_usd)}`}>{money(w.net_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="text-sm text-slate-500">Week 1 starts the day the agent arrives.</p>}
      </div>
      <div>
        <h3 className="label mb-2">{a.readiness.next ? `Next: ${a.readiness.next}` : "Next"}</h3>
        <CheckList checks={a.readiness.checks as Check[]} />
        <p className="mt-3 text-xs text-slate-500">You evaluate and decide. Graham may propose a Demo Desk seat after 2 profitable weeks; it moves only when you approve.</p>
      </div>
    </div>
  );
}

function ResearchAgent({ a }: { a: FloorAgent }) {
  const v = a.validation;
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
      <Stat label="Validation" value={<span className={v?.passed ? "text-emerald-300" : "text-slate-200"}>{v?.status ? v.status.toLowerCase() : "pending"}</span>} sub={v?.failing.length ? `failing: ${v.failing.join(", ")}` : undefined} />
      <Stat label="Unseen data" value={<span className={tone(v?.unseen?.avg_r)}>{fmtR(v?.unseen?.avg_r, 3)}</span>} sub={v?.unseen ? `${v.unseen.trades} trades` : undefined} />
      <Stat label="Last sprint" value={<span className="text-base">{a.sprint ? a.sprint.outcome.replace(/_/g, " ").toLowerCase() : "none yet"}</span>} sub={a.sprint?.day} />
      <Stat label="Variants tried" value={a.trials} sub={a.auto_revise ? "nightly sprints on" : "nightly sprints off"} />
      <Stat label="Next" value={<span className="text-base">{a.readiness.eligible ? "moves up tonight" : "in research"}</span>} sub={a.readiness.label} />
    </div>
  );
}

function ResearchSummary({ data }: { data: FloorData }) {
  const hist = data.sprint_history ?? [];
  const r = data.latest_report;
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card title="Last night">
        {r ? (
          <div className="space-y-2 text-sm">
            <p className="text-slate-300">Sprint {r.day}</p>
            <p>Passed every gate: <span className="text-emerald-300">{r.passed.join(", ") || "nobody"}</span></p>
            <p>Improved: <span className="text-sky-300">{r.improved.join(", ") || "nobody"}</span></p>
            <Link to={`/academy/reports/${r.day}`} className="inline-block text-xs text-gold-400 hover:underline">The full report →</Link>
          </div>
        ) : <Empty>No sprint report yet. The next sprint runs at 1:00 AM New York.</Empty>}
      </Card>
      <Card title="Nightly sprints">
        {hist.length ? (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th className="pb-1">Night</th><th className="text-right">Tested</th><th className="text-right">Improved</th><th className="text-right">Passed</th></tr></thead>
            <tbody>
              {hist.map((h) => (
                <tr key={h.day} className="border-t border-ink-800">
                  <td className="py-1"><Link to={`/academy/reports/${h.day}`} className="text-gold-400 hover:underline">{h.day}</Link></td>
                  <td className="text-right tabular-nums">{h.tested}</td>
                  <td className="text-right tabular-nums text-sky-300">{h.improved}</td>
                  <td className={`text-right tabular-nums ${h.passed ? "text-emerald-300" : "text-slate-500"}`}>{h.passed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty>No sprints yet.</Empty>}
      </Card>
    </div>
  );
}
