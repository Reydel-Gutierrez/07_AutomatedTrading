// The Stock Swing Class: equity Academy agents competing for a live swing seat on the Equity
// Trading floor. Floor 1 research (nightly sprints on daily US stock history), Floor 2 a paper
// record on new daily bars compared with Max's desk. Every number is read from the class
// engine's records; nothing here can place an order.

import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Card, Empty, ErrorLine, Stat, useAction } from "../components/ui";
import { usePageVisible, useResource } from "../hooks";
import { ago, money, pct, signedPct, tone } from "../format";

type Check = { name: string; value: unknown; rule: string; passed: boolean };
type Paper = {
  since: string; through: string; sessions: number; equity: number; return: number; trades: number; win_rate: number | null;
  profit_factor: number | null; avg_r: number | null; max_desk_return: number | null; vs_max: number | null;
  weeks: { week: string; return: number }[]; profitable_weeks: number; qualified: boolean;
  positions: { symbol: string; shares: number; entry: number; stop: number; opened: string }[];
};
type Agent = {
  id: string; name: string; persona: { tagline: string; about: string; color: string }; strategy: string; stage: string; floor: string;
  stage_since: string; status: string; version: number; trials: number; symbols: string[]; params: Record<string, unknown>;
  validation: { status: string; version: number; at: string; checks: Check[] | null; unseen: Record<string, number | null>; day1: Record<string, number | boolean | null> } | null;
  last_sprint: { day: string; outcome: string; verdict: string | null } | null; sprinting: string | null; activity: string; paper: Paper | null;
};
type Overview = {
  available: boolean; reason?: string; agents: Agent[];
  engine?: { online: boolean; job: { name: string; since: number } | null; queue: string[]; last_error: string | null; data_ready: boolean; data: { ready?: boolean; reason?: string; first_date?: string; last_date?: string; sessions?: number } };
  events?: { id: number; ts: string; agent_id: string | null; type: string; summary: string }[];
  latest_report?: { day: string; passed: string[]; improved: string[] } | null;
  qualify?: { sessions: number; trades: number };
};
type Detail = Agent & {
  sprints: { day: string; outcome: string; from: number; to: number | null; verdict: string | null }[];
  paper_trades: { symbol: string; entry: number; exit: number; opened: string; closed: string; net_usd: number; r: number; exit_reason: string }[];
  backtest: { metrics?: Record<string, Record<string, number | null>>; windows?: Record<string, [string, string]>; by_symbol?: Record<string, { trades: number; net_usd: number; avg_r: number }> };
};

const STRATEGY: Record<string, string> = {
  pullback_atr_v1: "Pullback, range-sized stops", breakout_trail_v1: "Breakout, trailing stop", rotation_v1: "Sector rotation", pullback_highbeta_v1: "High-beta pullback",
};

export default function StockSwing() {
  const { id } = useParams();
  return id ? <AgentPage id={id} /> : <Home />;
}

function Home() {
  const visible = usePageVisible();
  const { data, error, reload } = useResource<Overview>("/api/equity-academy", { every: visible ? 20000 : 0 });
  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!data) return <div className="p-6 text-slate-500">Opening the Stock Swing Class…</div>;
  if (!data.available) {
    return <div className="mx-auto max-w-3xl p-6"><Card title="The 537 Academy · Stock Swing Class"><p className="text-sm text-slate-300">{data.reason}</p></Card></div>;
  }
  const e = data.engine;
  return (
    <div className="mx-auto max-w-7xl space-y-6 p-4 md:p-6">
      <Card title="The 537 Academy · Stock Swing Class" action={<span className={`text-xs ${e?.online ? "text-emerald-400" : "text-amber-400"}`}>{e?.online ? "engine online" : "engine offline"}</span>}>
        <p className="text-sm text-slate-300">
          The Academy's stock class: four agents developing swing strategies for a live seat on the Equity Trading floor's swing desk. Floor 1: every night at 1:30 AM New York each one tries
          variants of its strategy on years of daily US stock history and must pass every gate. Floor 2: a $10,000 paper record on new
          daily bars, compared with Max's desk over the same days. After about four good weeks you decide: a live seat, or more tests.
        </p>
        <p className="mt-2 text-xs text-slate-500">
          {e?.data?.ready ? `Market data: ${e.data.sessions} sessions, ${e.data.first_date} to ${e.data.last_date}.` : (e?.data?.reason ?? "Waiting for daily bars (the firm fetches them at 5:15 PM New York).")}
          {e?.job ? ` Working on: ${e.job.name}.` : ""}{e?.last_error ? ` Last error: ${e.last_error}` : ""}
        </p>
        {data.latest_report && <p className="mt-1 text-xs text-slate-400">Last sprint night {data.latest_report.day}: passed {data.latest_report.passed.join(", ") || "none"}; improved {data.latest_report.improved.join(", ") || "none"}.</p>}
      </Card>
      <div className="grid gap-4 md:grid-cols-2">
        {data.agents.map((a) => <AgentCard key={a.id} a={a} qualify={data.qualify} onDone={reload} />)}
      </div>
      {!!data.events?.length && (
        <Card title="Recent class events">
          <ul className="space-y-1 text-sm">
            {data.events.slice(0, 12).map((ev) => <li key={ev.id} className="text-slate-300"><span className="text-slate-500">{ago(ev.ts)} · </span>{ev.summary}</li>)}
          </ul>
        </Card>
      )}
    </div>
  );
}

function Checks({ checks }: { checks: Check[] | null | undefined }) {
  if (!checks?.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-1.5">
      {checks.map((c) => (
        <span key={c.name} title={`${c.rule} · value ${String(c.value)}`} className={`rounded px-1.5 py-0.5 text-[11px] ${c.passed ? "bg-emerald-500/10 text-emerald-300" : "bg-rose-500/10 text-rose-300"}`}>
          {c.passed ? "✓" : "✗"} {c.name}
        </span>
      ))}
    </div>
  );
}

function AgentCard({ a, qualify, onDone }: { a: Agent; qualify?: { sessions: number; trades: number }; onDone: () => void }) {
  const u = a.validation?.unseen ?? {};
  return (
    <Card title={<Link to={`/equity-academy/${a.id}`} className="hover:underline"><span style={{ color: a.persona.color }}>●</span> {a.name} <span className="text-slate-400">· {a.persona.tagline}</span></Link>}
      action={<span className="text-xs text-slate-400">{a.floor}{a.status !== "ACTIVE" ? ` · ${a.status.toLowerCase()}` : ""}</span>}>
      <p className="-mt-2 text-xs text-slate-500">{STRATEGY[a.strategy] ?? a.strategy} · v{a.version} · {a.symbols.length} names · {a.trials} variants tried</p>
      {a.sprinting && <p className="mt-2 text-xs text-amber-300">Sprinting: {a.sprinting}</p>}
      {a.paper ? <PaperStats p={a.paper} qualify={qualify} /> : (
        <div className="mt-3 grid grid-cols-3 gap-3">
          <Stat label="Unseen return" value={<span className={tone(u.return_pct)}>{signedPct(u.return_pct)}</span>} sub={`${u.trades ?? 0} trades`} />
          <Stat label="Unseen avg R" value={u.avg_r == null ? "—" : u.avg_r.toFixed(2)} sub={`Sharpe ${u.sharpe == null ? "—" : u.sharpe.toFixed(2)}`} />
          <Stat label="Day-1 test" value={a.validation?.day1?.profitable_share == null ? "—" : pct(Number(a.validation.day1.profitable_share), 0)} sub="of starts profitable" />
        </div>
      )}
      <Checks checks={a.validation?.checks} />
      {a.last_sprint?.verdict && <p className="mt-2 text-xs text-slate-400">{a.last_sprint.day}: {a.last_sprint.verdict}</p>}
      <Controls a={a} onDone={onDone} />
    </Card>
  );
}

function PaperStats({ p, qualify }: { p: Paper; qualify?: { sessions: number; trades: number } }) {
  return (
    <div className="mt-3 space-y-2">
      <div className="grid grid-cols-3 gap-3">
        <Stat label="Paper return" value={<span className={tone(p.return)}>{signedPct(p.return)}</span>} sub={`${money(p.equity)} since ${p.since}`} />
        <Stat label="vs Max's desk" value={<span className={tone(p.vs_max)}>{p.vs_max == null ? "—" : signedPct(p.vs_max)}</span>} sub={`Max ${signedPct(p.max_desk_return)}`} />
        <Stat label="Trades" value={p.trades} sub={`${p.profitable_weeks} of ${p.weeks.length} weeks up`} />
      </div>
      <p className="text-xs text-slate-500">
        {p.qualified ? "Enough evidence for your decision: a live seat or more tests." : `Evidence so far: ${p.sessions} of ${qualify?.sessions ?? 20} sessions, ${p.trades} of ${qualify?.trades ?? 10} trades.`}
      </p>
    </div>
  );
}

function Controls({ a, onDone }: { a: Agent; onDone: () => void }) {
  const [msg, setMsg] = useState<string | null>(null);
  const act = useAction((action: string) => api.post(`/api/equity-academy/agents/${a.id}/${action}`, { note: "" }).then(() => setMsg(`${action.replace("_", " ")} queued`)), onDone);
  const buttons: [string, string][] = [
    ...(a.stage === "RESEARCH" && a.status === "ACTIVE" ? [["sprint", "Sprint now"]] as [string, string][] : []),
    ...(a.status === "ACTIVE" ? [["pause", "Pause"]] as [string, string][] : a.status === "PAUSED" ? [["resume", "Resume"]] as [string, string][] : []),
    ...(a.stage === "PAPER" ? [["to_research", "Back to research"]] as [string, string][] : []),
  ];
  if (!buttons.length) return null;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {buttons.map(([action, label]) => <button key={action} className="btn-ghost text-xs" disabled={act.pending} onClick={() => act.run(action)}>{label}</button>)}
      {msg && <span className="text-xs text-slate-400">{msg}</span>}
      <ErrorLine error={act.error} />
    </div>
  );
}

function AgentPage({ id }: { id: string }) {
  const { data, error, reload } = useResource<Detail>(`/api/equity-academy/agents/${id}`, { every: 30000 });
  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!data) return <div className="p-6 text-slate-500">Loading…</div>;
  const m = data.backtest.metrics ?? {};
  return (
    <div className="mx-auto max-w-6xl space-y-6 p-4 md:p-6">
      <Link to="/equity-academy" className="text-xs text-gold-400 hover:underline">← Stock Swing Class</Link>
      <AgentCard a={data} onDone={reload} />
      <Card title="About">
        <p className="text-sm text-slate-300">{data.persona.about}</p>
        <p className="mt-2 text-xs text-slate-500">Names: {data.symbols.join(", ")}</p>
        <p className="mt-1 text-xs text-slate-500">Settings: {Object.entries(data.params).map(([k, v]) => `${k} ${String(v)}`).join(" · ")}</p>
      </Card>
      {!!Object.keys(m).length && (
        <Card title={`Latest validation (v${data.validation?.version})`}>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500"><tr><th>Window</th><th>Dates</th><th className="text-right">Trades</th><th className="text-right">Return</th><th className="text-right">Avg R</th><th className="text-right">Max DD</th><th className="text-right">Sharpe</th></tr></thead>
            <tbody>
              {["in_sample", "out_of_sample", "holdout", "unseen"].filter((k) => m[k]).map((k) => (
                <tr key={k} className="border-t border-ink-800">
                  <td className="py-1">{k.replace("_", " ")}</td>
                  <td className="text-xs text-slate-400">{data.backtest.windows?.[k]?.join(" → ")}</td>
                  <td className="text-right tabular-nums">{m[k].trades}</td>
                  <td className={`text-right tabular-nums ${tone(m[k].return_pct)}`}>{signedPct(m[k].return_pct)}</td>
                  <td className="text-right tabular-nums">{m[k].avg_r == null ? "—" : Number(m[k].avg_r).toFixed(2)}</td>
                  <td className="text-right tabular-nums">{pct(m[k].max_drawdown, 1)}</td>
                  <td className="text-right tabular-nums">{m[k].sharpe == null ? "—" : Number(m[k].sharpe).toFixed(2)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
      {!!data.paper_trades.length && (
        <Card title="Paper trades">
          <div className="max-h-80 overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-ink-900 text-left text-xs text-slate-500"><tr><th>Name</th><th>Opened</th><th>Closed</th><th className="text-right">Net</th><th className="text-right">R</th><th>Why</th></tr></thead>
              <tbody>
                {data.paper_trades.map((t, i) => (
                  <tr key={i} className="border-t border-ink-800">
                    <td className="py-1">{t.symbol}</td><td className="text-xs">{t.opened}</td><td className="text-xs">{t.closed}</td>
                    <td className={`text-right tabular-nums ${tone(t.net_usd)}`}>{money(t.net_usd)}</td>
                    <td className="text-right tabular-nums">{t.r.toFixed(2)}</td><td className="text-xs text-slate-400">{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      <Card title="Sprint history">
        {data.sprints.length ? (
          <ul className="space-y-1 text-sm">{data.sprints.map((s, i) => <li key={i} className="text-slate-300"><span className="text-slate-500">{s.day} · {s.outcome.toLowerCase().replace("_", " ")} · </span>{s.verdict}</li>)}</ul>
        ) : <Empty>No sprints yet. The first runs as soon as there is enough history.</Empty>}
      </Card>
    </div>
  );
}
