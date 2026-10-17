import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { type Department, type RiskAgent, useRisk } from "../components/risk";
import { Badge, Card, Empty, ErrorLine, Field, Stat, useAction } from "../components/ui";
import { money, pct, signedPct, tone, when } from "../format";

// Fields of a department the owner edits, with their display scale (100 = shown as a percent).
const EDIT: [keyof Department, string, number][] = [
  ["planned_capital", "Planned capital ($)", 1],
  ["expected_low", "Expected yearly return, low (%)", 100],
  ["expected_high", "Expected yearly return, high (%)", 100],
  ["drawdown_limit", "Drawdown limit (%)", 100],
  ["max_risk_per_trade", "Max risk per trade (%)", 100],
];

export default function RiskPage() {
  const { data, error, reload } = useRisk();
  if (!data) return error ? <div className="text-rose-400">Could not load: {error.message}</div> : <div className="text-slate-500">Loading risk policy…</div>;
  const agents = [...data.managers, ...data.academy];
  const blended = (k: "expected_low" | "expected_high") => data.departments.reduce((s, d) => s + d[k] * d.share, 0);
  const worst = data.departments.reduce((s, d) => s + d.drawdown_limit * d.share, 0);
  return (
    <div className="space-y-6">
      <section className="grid gap-4 md:grid-cols-4">
        <Card><Stat label="Planned capital" value={money(data.planned_total)} sub="Your plan; actual money moves on Capital" /></Card>
        <Card><Stat label="Expected yearly return (blended)" value={`${pct(blended("expected_low"), 1)} – ${pct(blended("expected_high"), 1)}`} sub="A scorecard, not a target" /></Card>
        <Card><Stat label="If every department hit its limit" value={<span className="text-rose-300">−{pct(worst, 1)}</span>} sub={`About ${money(worst * data.planned_total)} of the plan`} /></Card>
        <Card><Stat label="Last changed" value={<span className="text-base">{data.updated ? when(data.updated.updated_at) : "Defaults"}</span>} sub={data.updated ? `v${data.updated.version} by ${data.updated.updated_by}` : "Not edited yet"} /></Card>
      </section>

      <Card title="How this works">
        <ul className="list-disc space-y-1 pl-5 text-sm text-slate-400">
          <li>You set each department's limits here. Every agent's own risk must sit inside them; a change beyond them is refused, for you and for Graham.</li>
          <li>Change an agent's risk in its office (click its name below). Graham can read all of this and propose a change; nothing moves until you approve it.</li>
          <li>Expected returns and benchmarks only grade the departments. No agent trades toward a target, so none takes more risk to catch up after a bad stretch.</li>
        </ul>
      </Card>

      <section className="grid gap-6 lg:grid-cols-2">
        {data.departments.map((d) => (
          <DepartmentCard key={d.id} d={d} agents={agents.filter((a) => a.department === d.id)} onDone={reload} />
        ))}
      </section>
    </div>
  );
}

function DepartmentCard({ d, agents, onDone }: { d: Department; agents: RiskAgent[]; onDone: () => void }) {
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [benchmark, setBenchmark] = useState(d.benchmark);
  const [note, setNote] = useState("");
  const save = useAction(
    () =>
      api.put("/api/risk/policy", {
        note,
        departments: { [d.id]: { ...Object.fromEntries(EDIT.map(([k, , s]) => [k, fields[k] === "" && k === "max_risk_per_trade" ? null : Number(fields[k]) / s])), benchmark } },
      }),
    () => {
      setEditing(false);
      onDone();
    },
  );
  const start = () => {
    setFields(Object.fromEntries(EDIT.map(([k, , s]) => [k, d[k] == null ? "" : String(Math.round(Number(d[k]) * s * 1000) / 1000)])));
    setBenchmark(d.benchmark);
    setNote("");
    setEditing(true);
  };
  const live = agents.filter((a) => a.kind === "manager");
  const training = agents.filter((a) => a.kind !== "manager");
  return (
    <Card title={`${d.floor} · ${d.name}`} action={!editing && <button className="btn-ghost text-xs" onClick={start}>Adjust limits</button>}>
      {!editing ? (
        <div className="space-y-4">
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
            <Item k="Planned capital" v={`${money(d.planned_capital)} · ${pct(d.share, 0)}`} />
            <Item k="Expected yearly" v={`${pct(d.expected_low, 0)} – ${pct(d.expected_high, 0)}`} />
            <Item k="Benchmark" v={d.benchmark} />
            <Item k="Drawdown limit" v={`−${pct(d.drawdown_limit, 0)} (${money(d.drawdown_limit * d.planned_capital)})`} />
            <Item k="Max risk per trade" v={d.max_risk_per_trade == null ? "—" : `${pct(d.max_risk_per_trade, 2)} (${money(d.max_risk_per_trade * d.planned_capital)})`} />
          </dl>
          <p className="text-xs text-slate-500">{d.note}</p>
          <AgentTable title={live.length ? "Managers" : "Live offices"} agents={live} empty={d.id === "options" ? "Not open: no option agents yet." : "Nobody live yet."} />
          {training.length > 0 && <AgentTable title="In the Academy (candidates)" agents={training} />}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {EDIT.map(([k, label]) => (
              <Field key={k} label={label} hint={k === "max_risk_per_trade" ? "Leave empty for no per-trade limit" : undefined}>
                <input className="input" inputMode="decimal" value={fields[k] ?? ""} onChange={(e) => setFields({ ...fields, [k]: e.target.value })} />
              </Field>
            ))}
            <Field label="Benchmark"><input className="input" value={benchmark} onChange={(e) => setBenchmark(e.target.value)} /></Field>
          </div>
          <Field label="Why this change"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Recorded in the activity log" /></Field>
          <ErrorLine error={save.error} />
          <div className="flex gap-2">
            <button className="btn-primary" disabled={save.pending || !note.trim()} onClick={() => save.run()}>Save limits</button>
            <button className="btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      )}
    </Card>
  );
}

function Item({ k, v }: { k: string; v: string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{k}</dt>
      <dd className="text-slate-100">{v}</dd>
    </div>
  );
}

function riskLine(a: RiskAgent): string {
  const r = a.risk;
  if (a.kind === "forex") return `${pct(r.risk_pct, 2)}/trade · ${r.max_positions} open · day stop ${pct(r.daily_loss, 1)} · pause at −${pct(r.drawdown_pause, 0)}`;
  if (a.kind === "stocks") return `${pct(r.position_pct, 0)} per name · up to ${r.max_positions ?? "—"} · stop ${r.stop_atr ?? "—"}× ATR`;
  if (r.stays_invested) return "Stays invested through declines";
  return [r.risk_per_trade != null && `${pct(r.risk_per_trade, 2)}/trade`, r.drawdown_stop != null && `pause at −${pct(r.drawdown_stop, 0)}`, r.daily_loss != null && `day stop ${pct(r.daily_loss, 0)}`].filter(Boolean).join(" · ");
}

const STAGE: Record<string, string> = { RESEARCH: "Floor 1", PAPER: "Floor 2 · paper", DEMO: "Floor 3 · demo" };

function AgentTable({ title, agents, empty }: { title: string; agents: RiskAgent[]; empty?: string }) {
  return (
    <div>
      <div className="label mb-1">{title}</div>
      {!agents.length ? (
        <Empty>{empty}</Empty>
      ) : (
        <div className="divide-y divide-ink-800 rounded-xl border border-ink-800">
          {agents.map((a) => (
            <Link key={a.id} to={a.office} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-3 py-2 text-sm hover:bg-ink-800/50">
              <span className="flex items-center gap-2">
                {a.color && <span className="h-2 w-2 rounded-full" style={{ background: a.color }} />}
                <span className="text-slate-100">{a.name}</span>
                {a.mode ? <Badge tone={a.mode}>{a.mode}</Badge> : <span className="text-xs text-slate-500">{STAGE[a.stage ?? ""] ?? a.stage}</span>}
                {a.problems?.length ? <span className="text-xs text-rose-400">over limit</span> : null}
              </span>
              <span className="text-xs text-slate-400">{riskLine(a)}</span>
              <span className="w-full text-xs text-slate-500 sm:w-auto">
                {a.capital != null && `${money(a.capital)} · `}
                <span className={tone(a.return)}>{signedPct(a.return)}</span> · drawdown {pct(a.drawdown, 1)}
                {a.kind === "stocks" && " · fixed while training"}
              </span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
