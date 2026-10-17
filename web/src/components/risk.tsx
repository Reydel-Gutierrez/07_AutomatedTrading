// The owner's risk policy: department limits (HQ Risk page) and each agent's own risk (its office).
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { useLive } from "../App";
import { useResource } from "../hooks";
import { pct } from "../format";
import { Card, ErrorLine, Field, useAction } from "./ui";

export type Department = {
  id: "core" | "trading" | "forex" | "options";
  name: string;
  floor: string;
  planned_capital: number;
  share: number;
  expected_low: number;
  expected_high: number;
  benchmark: string;
  drawdown_limit: number;
  max_risk_per_trade: number | null;
  note: string;
};
export type RiskAgent = {
  id: string;
  name: string;
  kind: "manager" | "forex" | "stocks";
  department: Department["id"];
  mode?: string;
  stage?: string;
  style?: string;
  color?: string;
  capital: number | null;
  return: number | null;
  drawdown: number | null;
  risk: Record<string, any>;
  problems?: string[];
  editable?: boolean;
  office: string;
};
export type RiskOverview = { departments: Department[]; planned_total: number; managers: RiskAgent[]; academy: RiskAgent[]; updated: { version: number; updated_at: string; updated_by: string } | null; forex_fields: Record<string, [number, number]> };

export function useRisk() {
  const { bump } = useLive();
  return useResource<RiskOverview>("/api/risk", { every: 60000, bump });
}

export const FOREX_RISK: [string, string, number, string][] = [
  ["risk_pct", "Risk per trade (%)", 100, "Share of the account lost if a trade hits its stop. Sizes every trade; the signals stay the same."],
  ["max_positions", "Max open trades", 1, "At most this many trades at once."],
  ["daily_loss", "Stop for the day after losing (%)", 100, "No new trades for the rest of the day after this loss."],
  ["drawdown_pause", "Pause at drawdown (%)", 100, "Stops trading at this decline from the account's peak, until you resume it."],
];

const num = (v: number, scale: number) => String(Math.round(v * scale * 1000) / 1000);

/** A forex agent's risk, in its office: what it carries, its department's limits, and the owner's controls. */
export function ForexRiskCard({ agentId, name, onDone }: { agentId: string; name: string; onDone?: () => void }) {
  const { data, reload } = useRisk();
  const a = data?.academy.find((x) => x.id === agentId);
  const dept = data?.departments.find((d) => d.id === "forex");
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const [sent, setSent] = useState(false);
  const save = useAction(
    () => {
      const changes = Object.fromEntries(FOREX_RISK.filter(([k, , scale]) => fields[k] !== undefined && fields[k] !== num(a!.risk[k], scale)).map(([k, , scale]) => [k, Number(fields[k]) / scale]));
      return api.post(`/api/academy/agents/${agentId}/risk`, { changes, note });
    },
    () => {
      setEditing(false);
      setSent(true);
      reload();
      onDone?.();
    },
  );
  if (!a || !dept) return null;
  const start = () => {
    setFields(Object.fromEntries(FOREX_RISK.map(([k, , scale]) => [k, num(a.risk[k], scale)])));
    setNote("");
    setSent(false);
    setEditing(true);
  };
  const ceiling: Record<string, number | null> = { risk_pct: dept.max_risk_per_trade, drawdown_pause: dept.drawdown_limit };
  return (
    <Card title={`${name}'s risk`} action={!editing && <button className="btn-ghost text-xs" onClick={start}>Adjust</button>}>
      {!editing ? (
        <div className="space-y-3">
          <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
            {FOREX_RISK.map(([k, label, scale]) => (
              <div key={k}>
                <dt className="text-xs text-slate-500">{label.replace(" (%)", "")}</dt>
                <dd className="tabular-nums text-slate-100">{scale === 100 ? pct(a.risk[k], 2) : a.risk[k]}{ceiling[k] != null && <span className="text-xs text-slate-500"> / limit {pct(ceiling[k], 2)}</span>}</dd>
              </div>
            ))}
          </dl>
          <p className="text-xs text-slate-500">
            Risk sizes each trade; {name}'s entries, stops and exits stay the same, so a change keeps {name}'s version and evidence. Twice the risk is roughly twice the gains and twice
            the drawdowns. The Forex Division's limits are on the <Link className="text-gold-400 hover:underline" to="/risk">Risk policy</Link> page.
          </p>
          {sent && <p className="text-xs text-emerald-400">Sent to the Forex engine; it applies within a minute.</p>}
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {FOREX_RISK.map(([k, label, , hint]) => (
              <Field key={k} label={label} hint={`${hint}${ceiling[k] != null ? ` Department limit ${pct(ceiling[k], 2)}.` : ""}`}>
                <input className="input" inputMode="decimal" value={fields[k] ?? ""} onChange={(e) => setFields({ ...fields, [k]: e.target.value })} />
              </Field>
            ))}
          </div>
          <Field label="Why this change"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Recorded in the Academy journal" /></Field>
          <ErrorLine error={save.error} />
          <div className="flex gap-2">
            <button className="btn-primary" disabled={save.pending || !note.trim()} onClick={() => save.run()}>Save risk</button>
            <button className="btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      )}
    </Card>
  );
}

/** A Tim/Alex/Max office: the manager's risk against the department's limits (edited through the mandate). */
export function ManagerRiskCard({ managerId }: { managerId: string }) {
  const { data } = useRisk();
  const a = data?.managers.find((x) => x.id === managerId);
  const dept = a && data?.departments.find((d) => d.id === a.department);
  if (!a || !dept) return null;
  const r = a.risk;
  const rows: [string, string][] = r.stays_invested
    ? [["Drawdown policy", "Stays invested through declines (your policy)"]]
    : [
        ...(r.risk_per_trade != null ? [["Risk per trade", `${pct(r.risk_per_trade, 2)} (${pct(r.position_pct, 0)} position × ${pct(r.stop_pct, 0)} stop)${dept.max_risk_per_trade != null ? ` · limit ${pct(dept.max_risk_per_trade, 2)}` : ""}`] as [string, string]] : []),
        ...(r.drawdown_stop != null ? [["Pauses buying at drawdown", `${pct(r.drawdown_stop, 0)} · limit ${pct(dept.drawdown_limit, 0)}`] as [string, string]] : []),
        ...(r.daily_loss != null ? [["Pauses after a day's loss", pct(r.daily_loss, 0)] as [string, string]] : []),
        ...(r.max_positions != null ? [["Positions", String(r.max_positions)] as [string, string]] : []),
      ];
  return (
    <Card title="Risk" action={<Link className="text-xs text-gold-400 hover:underline" to="/risk">{dept.name} limits →</Link>}>
      <dl className="space-y-1.5 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="flex justify-between gap-4"><dt className="text-slate-500">{k}</dt><dd className="text-right tabular-nums text-slate-200">{v}</dd></div>
        ))}
        <div className="flex justify-between gap-4"><dt className="text-slate-500">Drawdown now</dt><dd className="tabular-nums text-slate-200">{pct(a.drawdown, 1)}</dd></div>
      </dl>
      {a.problems?.length ? <p className="mt-2 text-xs text-rose-400">Outside the department's limits: {a.problems.join("; ")}</p> : null}
      <p className="mt-3 text-xs text-slate-500">Change these with Adjust on the strategy card; a change beyond {dept.name}'s limits is refused.</p>
    </Card>
  );
}
