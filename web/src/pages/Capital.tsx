import { useState } from "react";
import { useLive } from "../App";
import { api, type Capital } from "../api";
import { Badge } from "../components/ui";
import { Feed } from "../components/Feed";
import { AGENTS, Card, Empty, ErrorLine, Field, Stat, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { money, pct, when } from "../format";

export default function CapitalPage() {
  const { bump, reloadOverview, overview } = useLive();
  const { data: c, reload } = useResource<Capital>("/api/capital", { every: 30000, bump });
  if (!c) return <div className="text-slate-500">Loading capital…</div>;
  const done = () => {
    reload();
    reloadOverview();
  };
  return (
    <div className="space-y-6">
      <section className="grid gap-6 md:grid-cols-3">
        <Card><Stat label="Firm value" value={money(overview?.firm.nav)} /></Card>
        <Card><Stat label="Unallocated cash" value={money(c.unallocated)} sub="Available to assign to a manager" /></Card>
        <Card><Stat label="Awaiting your review" value={money(c.suspense_cash)} sub={Object.keys(c.foreign_orders).length ? `${Object.keys(c.foreign_orders).length} outside order(s)` : "Nothing unexplained"} /></Card>
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.3fr_1fr]">
        <Card title="Allocate capital">
          <Allocator c={c} onDone={done} />
        </Card>
        <Card title="New deposits">
          <SplitEditor c={c} onDone={done} />
        </Card>
      </section>

      <Card title="Graham's pool" action={<span className="text-xs text-slate-500">Money you give the CIO to distribute by performance, within your caps</span>}>
        <Pool c={c} onDone={done} />
      </Card>

      {(c.suspense_cash !== 0 || Object.keys(c.suspense_positions).length > 0 || Object.keys(c.foreign_orders).length > 0) && (
        <Card title="Needs your decision"><Review c={c} onDone={done} /></Card>
      )}

      <section className="grid gap-6 lg:grid-cols-2">
        <Card title="Adding or withdrawing money">
          <div className="space-y-2 text-sm text-slate-400">
            <p>The firm never moves money in or out of Robinhood. Deposit or withdraw in the Robinhood app; HQ detects the change on its next check.</p>
            <p>Deposits are split by the rule above (anything not assigned stays unallocated). A withdrawal is taken from unallocated cash. If that isn't enough, buying pauses and you pick which manager funds the rest.</p>
            <p>To fund a withdrawal from a manager, first move their cash back to unallocated below. If their money is invested, lower their allocation and they raise cash on their next run.</p>
          </div>
        </Card>
        <Card title="Capital history"><Feed events={c.flows.slice(0, 20)} compact /></Card>
      </section>
    </div>
  );
}

function Allocator({ c, onDone }: { c: Capital; onDone: () => void }) {
  const [manager, setManager] = useState("tim");
  const [mode, setMode] = useState<"amount" | "percent">("amount");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const body = () => ({ manager, note, ...(mode === "amount" ? { amount: value } : { percent: Number(value) }) });
  const give = useAction(() => api.post("/api/capital/allocate", body()), () => { setValue(""); onDone(); });
  const take = useAction(() => api.post("/api/capital/deallocate", body()), () => { setValue(""); onDone(); });
  return (
    <div className="space-y-4">
      <table className="tbl">
        <thead><tr><th>Manager</th><th>Mode</th><th className="text-right">Cash</th><th className="text-right">Free</th></tr></thead>
        <tbody>
          {c.managers.map((m) => (
            <tr key={m.id}>
              <td><span className="inline-flex items-center gap-2"><span className="h-2 w-2 rounded-full" style={{ background: AGENTS[m.id]?.color }} />{m.name}</span></td>
              <td className="text-slate-400">{m.mode}</td>
              <td className="text-right">{money(m.cash)}</td>
              <td className="text-right">{money(m.free_cash)}</td>
            </tr>
          ))}
          <tr>
            <td><span className="inline-flex items-center gap-2"><span className="h-2 w-2 rounded-full" style={{ background: AGENTS.cio.color }} />Graham's pool</span></td>
            <td className="text-slate-400">CIO</td>
            <td className="text-right">{money(Number(c.pool.cash))}</td>
            <td className="text-right">{money(Number(c.pool.free))}</td>
          </tr>
        </tbody>
      </table>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Manager">
          <select className="input" value={manager} onChange={(e) => setManager(e.target.value)}>
            {c.managers.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
            <option value="cio">Graham's pool (CIO distributes)</option>
          </select>
        </Field>
        <Field label={mode === "amount" ? "Amount ($)" : "Percent of firm"}>
          <div className="flex gap-1">
            <input className="input" inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder={mode === "amount" ? "100.00" : "10"} />
            <button className="btn-ghost px-2" onClick={() => setMode(mode === "amount" ? "percent" : "amount")} title="Switch $ / %">{mode === "amount" ? "$" : "%"}</button>
          </div>
        </Field>
        <Field label="Note"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Optional" /></Field>
      </div>
      <div className="flex flex-wrap gap-2">
        <button className="btn-primary" disabled={!value || give.pending} onClick={() => give.run()}>Allocate to {manager === "cio" ? "Graham's pool" : AGENTS[manager]?.name}</button>
        <button className="btn-ghost" disabled={!value || take.pending} onClick={() => take.run()}>Return to unallocated</button>
      </div>
      <ErrorLine error={give.error ?? take.error} />
    </div>
  );
}

function SplitEditor({ c, onDone }: { c: Capital; onDone: () => void }) {
  const rows = [...c.managers.map((m) => ({ id: m.id, name: m.name })), { id: "cio", name: "Graham's pool" }];
  const [split, setSplit] = useState<Record<string, string>>(() => Object.fromEntries(rows.map((m) => [m.id, String(Math.round((c.deposit_split[m.id] ?? 0) * 100))])));
  const total = Object.values(split).reduce((s, v) => s + (Number(v) || 0), 0);
  const save = useAction(() => api.put("/api/settings/deposit_split", { split: Object.fromEntries(Object.entries(split).filter(([, v]) => Number(v) > 0).map(([k, v]) => [k, Number(v) / 100])) }), onDone);
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-400">When a deposit arrives, split it automatically. The rest stays unallocated ({pct(Math.max(0, 100 - total) / 100, 0)}).</p>
      {rows.map((m) => (
        <div key={m.id} className="flex items-center gap-3">
          <span className="w-28 text-slate-200">{m.name}</span>
          <input className="input" type="number" min="0" max="100" value={split[m.id]} onChange={(e) => setSplit({ ...split, [m.id]: e.target.value })} />
          <span className="text-slate-500">%</span>
        </div>
      ))}
      <button className="btn-ghost" disabled={save.pending || total > 100} onClick={() => save.run()}>Save split</button>
      <ErrorLine error={save.error ?? (total > 100 ? "The split cannot exceed 100%." : null)} />
    </div>
  );
}

function Review({ c, onDone }: { c: Capital; onDone: () => void }) {
  const [manager, setManager] = useState("tim");
  const [note, setNote] = useState("");
  const resolve = useAction((as_kind: string) => api.post("/api/suspense/resolve", { as_kind, manager: as_kind === "manager" ? manager : null, note }), onDone);
  const ack = useAction((id: string) => api.post(`/api/foreign-orders/${id}/acknowledge`, { note: note || "acknowledged" }), onDone);
  return (
    <div className="space-y-5">
      {c.suspense_cash !== 0 && (
        <div>
          <p className="text-sm text-slate-300">{money(Math.abs(c.suspense_cash))} {c.suspense_cash > 0 ? "arrived" : "left the account"} that the firm couldn't explain on its own. Buying is paused until you say what it was.</p>
          <div className="mt-3 flex flex-wrap items-end gap-2">
            <Field label="Note"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} /></Field>
            {c.suspense_cash > 0 && <button className="btn-ghost" onClick={() => resolve.run("deposit")}>It was a deposit</button>}
            {c.suspense_cash > 0 && <button className="btn-ghost" onClick={() => resolve.run("dividend")}>It was a dividend</button>}
            <select className="input w-auto" value={manager} onChange={(e) => setManager(e.target.value)}>{c.managers.map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}</select>
            <button className="btn-ghost" onClick={() => resolve.run("manager")}>Assign to manager</button>
          </div>
          <ErrorLine error={resolve.error} />
        </div>
      )}
      {Object.keys(c.suspense_positions).length > 0 && <Empty>Positions held at the broker but not owned by any manager: {Object.entries(c.suspense_positions).map(([s, q]) => `${s} ${q}`).join(", ")}. Ask for an adoption tool before trading them.</Empty>}
      {Object.entries(c.foreign_orders).map(([id, o]) => (
        <div key={id} className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-ink-850 p-3 text-sm">
          <span>Outside order: <b className="text-slate-100">{o.side} {o.quantity} {o.symbol}</b> via {o.placed_agent ?? "unknown"} on {when(o.created_at)}</span>
          <button className="btn-ghost text-xs" onClick={() => ack.run(id)}>Acknowledge</button>
        </div>
      ))}
      <ErrorLine error={ack.error} />
    </div>
  );
}

function Pool({ c, onDone }: { c: Capital; onDone: () => void }) {
  const p = c.pool;
  const [caps, setCaps] = useState<Record<string, string>>(() => Object.fromEntries(p.managers.map((m) => [m.id, String(Math.round((p.policy.caps[m.id] ?? 0) * 100))])));
  const save = useAction(() => api.put("/api/settings/cio_pool", { caps: Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, Number(v) / 100])) }), onDone);
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_1.4fr]">
      <div className="space-y-3 text-sm text-slate-400">
        <Stat label="In the pool" value={money(Number(p.cash))} sub={`${p.moves_today} of ${p.policy.max_moves_per_day} moves used today`} />
        <p>Fund the pool above (or with the deposit split). Graham reviews it when you add money and weekly, and moves it to the managers trading real money that are doing best.</p>
        <p>He can pull back only what he gave, never your own allocations, and never past your caps: a cap is the most of the whole firm one manager may hold after his moves. Every move appears in Capital history and on your phone.</p>
      </div>
      <div>
        <table className="tbl">
          <thead><tr><th>Manager</th><th className="text-right">Share now</th><th className="text-right">Your cap</th><th className="text-right">From Graham</th></tr></thead>
          <tbody>
            {p.managers.map((m) => (
              <tr key={m.id}>
                <td>
                  <span className="inline-flex items-center gap-2">
                    {m.name}
                    {!m.eligible && <Badge tone="INACTIVE">{m.mode === "LIVE" ? "LIVE" : `${m.mode}: not eligible`}</Badge>}
                  </span>
                </td>
                <td className="text-right">{pct(m.share, 1)}</td>
                <td className="text-right">
                  <span className="inline-flex items-center gap-1">
                    <input className="input w-16 py-1 text-right" type="number" min="0" max="100" value={caps[m.id]} onChange={(e) => setCaps({ ...caps, [m.id]: e.target.value })} />%
                  </span>
                </td>
                <td className="text-right">{money(Number(m.granted))}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <button className="btn-ghost mt-3" disabled={save.pending} onClick={() => save.run()}>Save caps</button>
        <ErrorLine error={save.error} />
      </div>
    </div>
  );
}
