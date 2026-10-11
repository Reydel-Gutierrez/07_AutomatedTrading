import { useState } from "react";
import { useLive } from "../App";
import { api, type SystemView } from "../api";
import { Card, ErrorLine, Field, Modal, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { ago, money, when } from "../format";

export default function SystemPage() {
  const { bump, reloadOverview } = useLive();
  const { data: s, reload } = useResource<SystemView>("/api/system", { every: 20000, bump });
  if (!s) return <div className="text-slate-500">Loading system…</div>;
  const done = () => {
    reload();
    reloadOverview();
  };
  const ks = s.controls.kill_switch;
  return (
    <div className="space-y-6">
      <section className="grid gap-6 lg:grid-cols-2">
        <Card title="Emergency stop">
          <p className={`text-lg ${ks.engaged ? "text-rose-300" : "text-slate-200"}`}>{ks.engaged ? `Engaged: ${ks.reason}` : "Released. Orders may be sent within every limit."}</p>
          {ks.engaged && <p className="mt-1 text-xs text-slate-500">by {ks.by} {ks.at && `at ${when(ks.at)}`} ({ks.source})</p>}
          <KillSwitch engaged={ks.engaged} forced={ks.source === "env"} onDone={done} />
        </Card>
        <Card title="Safeguards">
          <ul className="space-y-2 text-sm">
            <li>Robinhood order placement: <b className={s.controls.live_writes_enabled ? "text-emerald-300" : "text-slate-300"}>{s.controls.live_writes_enabled ? "enabled" : "disabled"}</b> <span className="text-slate-500">(set on the Pi)</span></li>
            <li>Books vs broker: <b className={s.controls.reconcile_hold.active ? "text-amber-300" : "text-emerald-300"}>{s.controls.reconcile_hold.active ? `buys paused: ${s.controls.reconcile_hold.reasons.join("; ")}` : "match"}</b></li>
            <li>Drawdown brake: <b className={s.controls.drawdown_brake.engaged ? "text-amber-300" : "text-emerald-300"}>{s.controls.drawdown_brake.engaged ? "engaged" : "off"}</b> <span className="text-slate-500">(at −{Math.round((s.firm_limits.drawdown_brake_pct ?? 0.2) * 100)}% from peak)</span></li>
            <li>Trading window: {s.firm_limits.window_start_et}–{s.firm_limits.window_end_et} ET, regular hours, cash only</li>
          </ul>
          {s.controls.drawdown_brake.engaged && <ResumeBrake onDone={done} />}
        </Card>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <Card title="Background work">
          <table className="tbl">
            <thead><tr><th>Job</th><th>Last run</th><th>Status</th><th className="text-right">Runs</th></tr></thead>
            <tbody>
              {Object.entries(s.jobs).map(([name, j]) => (
                <tr key={name}>
                  <td className="text-slate-200">{name}</td>
                  <td className="text-slate-400">{ago(j.last_started)}</td>
                  <td className={j.last_ok === false ? "text-rose-300" : "text-slate-300"} title={j.last_error ?? ""}>{j.running ? "running" : j.last_ok === false ? "failed" : j.last_ok ? "ok" : "—"}</td>
                  <td className="text-right">{j.runs}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-xs text-slate-500">Service started {when(s.health.started_at)} · heartbeat {ago(s.health.heartbeat)} · build {String(s.health.deployed?.sha ?? "").slice(0, 12)}</p>
        </Card>
        <Card title="Backups">
          {s.backup ? (
            <div className="space-y-1 text-sm">
              <p className={s.backup.ok ? "text-emerald-300" : "text-rose-300"}>{s.backup.ok ? "Last backup stored off-Pi and restore-verified" : `Last backup failed: ${s.backup.error}`}</p>
              <p className="text-slate-400">{s.backup.file} · {when(s.backup.at)}</p>
            </div>
          ) : (
            <p className="text-sm text-slate-400">No backup has run yet. It runs nightly at 02:30 ET.</p>
          )}
        </Card>
      </section>

      <section className="grid gap-6 lg:grid-cols-2">
        <Card title="AI operating budget"><Budget s={s} onDone={done} /></Card>
        <Card title="Recent reconciliations">
          <ul className="space-y-2 text-sm">
            {s.reconciliations.slice(0, 8).map((r) => (
              <li key={r.id} className="flex justify-between gap-3">
                <span className="text-slate-400">{when(r.ts)}</span>
                <span className={r.reconciled ? "text-emerald-300" : "text-amber-300"}>{r.reconciled ? "match" : (r.diff.reasons ?? []).join("; ") || "hold"}</span>
                <span className="tabular-nums text-slate-300">{money(Number(r.nav))}</span>
              </li>
            ))}
          </ul>
        </Card>
      </section>
    </div>
  );
}

function KillSwitch({ engaged, forced, onDone }: { engaged: boolean; forced: boolean; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const act = useAction(() => api.post("/api/kill-switch", { action: engaged ? "release" : "engage", note }), () => { setOpen(false); setNote(""); onDone(); });
  return (
    <>
      <button className={`${engaged ? "btn-ghost" : "btn-danger"} mt-4`} disabled={engaged && forced} onClick={() => setOpen(true)}>
        {engaged ? (forced ? "Forced on in the Pi env file" : "Release kill switch") : "Engage kill switch"}
      </button>
      <Modal open={open} title={engaged ? "Release the kill switch?" : "Stop all trading now?"} onClose={() => setOpen(false)}>
        <Field label={engaged ? "What was checked (required)" : "Reason"}><input className="input" value={note} onChange={(e) => setNote(e.target.value)} autoFocus /></Field>
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
          <button className={engaged ? "btn-primary" : "btn-danger"} disabled={act.pending || (engaged && !note.trim())} onClick={() => act.run()}>{engaged ? "Release" : "Engage"}</button>
        </div>
        <ErrorLine error={act.error} />
      </Modal>
    </>
  );
}

function ResumeBrake({ onDone }: { onDone: () => void }) {
  const [note, setNote] = useState("");
  const act = useAction(() => api.post("/api/brake/resume", { note }), onDone);
  return (
    <div className="mt-4 flex gap-2">
      <input className="input" placeholder="Why it's safe to resume" value={note} onChange={(e) => setNote(e.target.value)} />
      <button className="btn-primary" disabled={!note.trim()} onClick={() => act.run()}>Resume buying</button>
    </div>
  );
}

function Budget({ s, onDone }: { s: SystemView; onDone: () => void }) {
  const [monthly, setMonthly] = useState(String(s.ai_budget.monthly_usd));
  const save = useAction(() => api.put("/api/settings/ai_budget", { monthly_usd: Number(monthly) }), onDone);
  return (
    <div className="space-y-3">
      <Field label="Monthly cap (USD)" hint="Split across agents so one can't starve the others. Enforced from Phase 2, when the CIO starts using AI.">
        <div className="flex gap-2">
          <input className="input" type="number" min="0" step="1" value={monthly} onChange={(e) => setMonthly(e.target.value)} />
          <button className="btn-ghost" disabled={save.pending} onClick={() => save.run()}>Save</button>
        </div>
      </Field>
      <div className="flex flex-wrap gap-2 text-xs text-slate-400">
        {Object.entries(s.ai_budget.per_agent).map(([k, v]) => <span key={k} className="rounded bg-ink-800 px-2 py-1">{k.toUpperCase()} {Math.round(v * 100)}% · {money(v * Number(monthly || 0))}</span>)}
      </div>
      <ErrorLine error={save.error} />
    </div>
  );
}
