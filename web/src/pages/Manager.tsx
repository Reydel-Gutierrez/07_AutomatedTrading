import React, { useState } from "react";
import { useParams } from "react-router-dom";
import { useLive } from "../App";
import { api, type BookDetail, type ManagerDetail } from "../api";
import { PerformanceChart, WeightBars } from "../components/charts";
import { Feed } from "../components/Feed";
import { AGENTS, Avatar, Badge, Card, Empty, ErrorLine, Field, Modal, Stat, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { ago, money, pct, shares, signedPct, tone, until, when } from "../format";
import { isWorking } from "./HQ";

export default function ManagerPage() {
  const { id = "tim" } = useParams();
  const { bump, events } = useLive();
  const { data: m, reload, error } = useResource<ManagerDetail>(`/api/managers/${id}`, { every: 30000, bump });
  const [book, setBook] = useState<"LIVE" | "SIM" | null>(null);
  if (error) return <Empty>{error.message}</Empty>;
  if (!m) return <div className="text-slate-500">Opening the workspace…</div>;
  const a = AGENTS[m.id];
  const shown = book ?? m.active_book ?? "LIVE";
  const b = m.books[shown];
  const working = isWorking(m, events, m.id);
  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center gap-4">
        <Avatar id={m.id} size={64} working={working} />
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <h1 className="font-display text-3xl text-slate-50">{m.name}</h1>
            <Badge tone={m.mode}>{m.mode}</Badge>
            {m.status === "DEGRADED" && <Badge tone="PAUSED">NEEDS ATTENTION</Badge>}
          </div>
          <p className="text-sm text-slate-400">{m.role} · {a.blurb}</p>
        </div>
        {m.implemented && <ModeControl m={m} onDone={reload} />}
      </header>

      {!m.implemented ? (
        <Card>
          <p className="text-slate-300">{m.name}'s strategy activates in Phase {m.id === "alex" ? 3 : 4}. Capital you allocate to {m.name} now is held as cash in his sleeve.</p>
          <p className="mt-3 text-sm text-slate-400">Allocated today: {money(m.books.LIVE.cash)}</p>
        </Card>
      ) : (
        <>
          <section className="grid gap-6 lg:grid-cols-[1.2fr_1fr]">
            <Card title="Right now" action={<RunNow id={m.id} onDone={reload} />}>
              <p className="text-lg text-slate-100">{m.status === "DEGRADED" ? <span className="text-rose-300">{m.activity}</span> : describe(m.activity)}</p>
              {m.last_run && (
                <p className="mt-2 text-sm text-slate-400">
                  {m.last_run.summary.replace(`${m.name}: `, "")} <span className="text-slate-500">({ago(m.last_run.at)}, {m.last_run.book})</span>
                </p>
              )}
              <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
                <div><div className="label">Next decision</div><div className="mt-1 text-slate-200">{until(m.next_run)}</div><div className="text-xs text-slate-500">{when(m.next_run)}</div></div>
                <div><div className="label">Trading</div><div className="mt-1 text-slate-200">{m.active_book === "LIVE" ? "Real money" : m.active_book === "SIM" ? "Simulation" : "Stopped"}</div><div className="text-xs text-slate-500">since {when(m.mode_changed_at)} by {m.mode_changed_by}</div></div>
              </div>
            </Card>
            <Card
              title={m.mode === "LIVE" && shown === "SIM" ? "Portfolio · simulation history" : "Portfolio"}
              action={
                m.mode === "LIVE" ? (
                  // Real money is the only book that matters once live; the simulation stays one click away.
                  <button onClick={() => setBook(shown === "SIM" ? "LIVE" : "SIM")} className="text-xs text-slate-500 hover:text-slate-300">
                    {shown === "SIM" ? "← Back to real money" : "Simulation history"}
                  </button>
                ) : (
                  <div className="flex rounded-lg border border-ink-700 p-0.5 text-xs">
                    {(["LIVE", "SIM"] as const).map((k) => (
                      <button key={k} onClick={() => setBook(k)} className={`rounded-md px-2.5 py-1 ${shown === k ? "bg-ink-700 text-slate-50" : "text-slate-400"}`}>{k === "LIVE" ? "Live" : "Simulated"}</button>
                    ))}
                  </div>
                )
              }
            >
              <div className="grid grid-cols-2 gap-4">
                <Stat label="Value" value={money(b.nav)} sub={<span className={tone(b.performance.twr)}>{signedPct(b.performance.twr)} vs S&P {signedPct(b.performance.benchmark)}</span>} />
                <Stat label="Cash" value={money(b.cash)} sub={`${money(b.free_cash)} free`} />
                <Stat label="Invested" value={money(b.invested)} />
                <Stat label="Realized" value={<span className={tone(b.realized_pnl)}>{money(b.realized_pnl)}</span>} />
              </div>
            </Card>
          </section>

          <section className="grid gap-6 lg:grid-cols-2">
            <Card title={`Target vs actual (${shown === "LIVE" ? "live" : "simulated"})`}>
              <WeightBars rows={b.positions.map((p) => ({ symbol: p.symbol, weight: p.weight, target: p.target }))} color={a.color} />
            </Card>
            <Card title="Performance vs S&P 500">
              <PerformanceChart series={b.performance.series} color={a.color} />
            </Card>
          </section>

          <Card title="Positions"><Positions b={b} /></Card>

          <section className="grid gap-6 lg:grid-cols-2">
            <MandateCard m={m} onDone={reload} />
            <Card title="Decision log">
              <DecisionLog m={m} />
            </Card>
          </section>

          <Card title="Orders"><Orders m={m} /></Card>
          <Card title={`Activity · ${m.name}`}><Feed events={m.activity_feed} compact /></Card>
          {m.id === "alex" && <ResearchDesk />}
          <Card title={`Questions about ${m.name}`}>
            <p className="text-sm text-slate-400">
              Ask Graham in the <a href="/cio" className="text-gold-400 hover:underline">CIO Office</a>; he reads {m.name}'s decisions, orders{m.id === "alex" ? ", and research notes" : ""} before answering. Change {m.name}'s strategy through the mandate above; every change is versioned and logged.
            </p>
          </Card>
        </>
      )}
    </div>
  );
}

function describe(activity: string | null): string {
  const map: Record<string, string> = {
    investing: "Investing new cash toward his targets.",
    rebalancing: "Rebalancing: selling overweight funds, then buying underweight ones.",
    holding: "Holding. The portfolio is within its bands.",
    "waiting for research": "Waiting for research approvals on today's top-ranked names before buying.",
    watching: "Watching his positions' stops; entries are decided near the close.",
    entering: "Entering pullback trades, each with a stop.",
    exiting: "Exiting trades: target, stop, time, or trend.",
    "waiting for market data": "Waiting for market data.",
    idle: "Idle: not enough capital to trade.",
  };
  return map[activity ?? ""] ?? (activity ? activity : "Waiting for the first run.");
}

function RunNow({ id, onDone }: { id: string; onDone: () => void }) {
  const act = useAction(() => api.post(`/api/managers/${id}/run`), onDone);
  return (
    <button className="btn-ghost text-xs" disabled={act.pending} onClick={() => act.run()} title="Request a run now. Orders still only go out inside market hours.">
      {act.pending ? "Requested…" : "Run now"}
    </button>
  );
}

function ModeControl({ m, onDone }: { m: ManagerDetail; onDone: () => void }) {
  const [target, setTarget] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [typed, setTyped] = useState("");
  const act = useAction(() => api.post(`/api/managers/${m.id}/mode`, { mode: target, note, confirm: target === "LIVE" }), () => {
    setTarget(null);
    setNote("");
    setTyped("");
    onDone();
  });
  return (
    <>
      <div className="flex gap-2">
        {(["SIM", "LIVE", "PAUSED"] as const).filter((x) => x !== m.mode).map((x) => (
          <button key={x} className={x === "LIVE" ? "btn-primary" : "btn-ghost"} onClick={() => setTarget(x)}>
            {x === "SIM" ? "Simulate" : x === "LIVE" ? "Go live" : "Pause"}
          </button>
        ))}
      </div>
      <Modal open={!!target} title={target === "LIVE" ? `Let ${m.name} trade real money?` : `Switch ${m.name} to ${target}?`} onClose={() => setTarget(null)}>
        {target === "LIVE" && (
          <p className="mb-4 text-sm text-slate-300">
            {m.name} will place real orders with his {money(m.books.LIVE.free_cash)} of live cash inside his mandate, during market hours, through the Execution Desk's checks. Orders also require broker writes to be enabled on the Pi.
          </p>
        )}
        <Field label="Reason"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why the change" /></Field>
        {target === "LIVE" && (
          <div className="mt-3"><Field label='Type "LIVE" to confirm'><input className="input" value={typed} onChange={(e) => setTyped(e.target.value)} /></Field></div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-ghost" onClick={() => setTarget(null)}>Cancel</button>
          <button className={target === "LIVE" ? "btn-primary" : "btn-ghost"} disabled={act.pending || !note.trim() || (target === "LIVE" && typed !== "LIVE")} onClick={() => act.run()}>Confirm</button>
        </div>
        <ErrorLine error={act.error} />
      </Modal>
    </>
  );
}

function Positions({ b }: { b: BookDetail }) {
  const held = b.positions.filter((p) => p.quantity > 0);
  if (!held.length) return <Empty>No positions in this book yet.</Empty>;
  return (
    <table className="tbl">
      <thead><tr><th>Symbol</th><th className="text-right">Shares</th><th className="text-right">Price</th><th className="text-right">Value</th><th className="text-right">Cost</th><th className="text-right">Gain</th><th className="text-right">Weight</th></tr></thead>
      <tbody>
        {held.map((p) => (
          <tr key={p.symbol}>
            <td className="font-medium text-slate-100">{p.symbol}</td>
            <td className="text-right">{shares(p.quantity)}</td>
            <td className="text-right">{money(p.price)}</td>
            <td className="text-right">{money(p.value)}</td>
            <td className="text-right text-slate-400">{money(p.cost)}</td>
            <td className={`text-right ${tone(p.pnl)}`}>{money(p.pnl)}</td>
            <td className="text-right">{pct(p.weight)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MandateCard({ m, onDone }: { m: ManagerDetail; onDone: () => void }) {
  if (m.id === "alex") return <AlexMandateCard m={m} onDone={onDone} />;
  if (m.id === "max") return <AlexMandateCard m={m} onDone={onDone} fieldsList={MAX_FIELDS} summary={<MaxSummary p={m.mandate.params} />} />;
  return <TimMandateCard m={m} onDone={onDone} />;
}

const ALEX_FIELDS: [string, string, number][] = [
  ["positions", "Positions held", 1],
  ["max_position_pct", "Max per position (%)", 100],
  ["cash_buffer_pct", "Cash buffer (%)", 100],
  ["min_trade_usd", "Minimum trade ($)", 1],
  ["research_daily_limit", "Research notes per day", 1],
];

const MAX_FIELDS: [string, string, number][] = [
  ["max_positions", "Max positions", 1],
  ["position_pct", "Per position (%)", 100],
  ["stop_pct", "Stop below entry (%)", 100],
  ["max_hold_days", "Max sessions held", 1],
  ["rsi_entry_below", "Enter when RSI(2) below", 1],
  ["min_trade_usd", "Minimum trade ($)", 1],
];

function MaxSummary({ p }: { p: Record<string, any> }) {
  const universe: string[] = p.universe ?? [];
  return (
    <div className="space-y-3 text-sm text-slate-400">
      <p>
        Trend pullbacks in {universe.length} liquid ETFs and mega caps: buys when the price is above its {p.trend_sma_days}-day average and RSI({p.rsi_days}) is below {p.rsi_entry_below},
        up to {p.max_positions} positions of {pct(p.position_pct, 0)} each.
      </p>
      <p>
        Every trade has a stop {pct(p.stop_pct, 0)} below entry. Exits on a close above the {p.exit_sma_days}-day average, after {p.max_hold_days} sessions, or when the trend breaks.
        Checks stops at {(p.run_times_et ?? [])[0]} ET and decides entries at {p.entry_time_et} ET. New buys pause at −{pct(p.sleeve_drawdown_pause, 0)} from the sleeve's peak or −{pct(p.daily_loss_pause, 0)} in a day.
      </p>
      <details className="text-xs text-slate-500">
        <summary className="cursor-pointer">Universe ({universe.length})</summary>
        <p className="mt-2 leading-relaxed">{universe.join(" · ")}</p>
      </details>
    </div>
  );
}

function AlexMandateCard({ m, onDone, fieldsList = ALEX_FIELDS, summary }: { m: ManagerDetail; onDone: () => void; fieldsList?: [string, string, number][]; summary?: React.ReactNode }) {
  const ALEX_FIELDS_ = fieldsList;
  const p = m.mandate.params;
  const timed = "run_time_et" in p;
  const [editing, setEditing] = useState(false);
  const [fields, setFields] = useState<Record<string, string>>(() => ({ ...Object.fromEntries(ALEX_FIELDS_.map(([k, , scale]) => [k, String(Math.round(Number(p[k]) * scale * 100) / 100)])), ...(timed ? { run_time_et: String(p.run_time_et) } : {}) }));
  const [note, setNote] = useState("");
  const save = useAction(
    () =>
      api.post(`/api/managers/${m.id}/mandate`, {
        note,
        params: { ...p, ...Object.fromEntries(ALEX_FIELDS_.map(([k, , scale]) => [k, Number(fields[k]) / scale])), ...(timed ? { run_time_et: fields.run_time_et } : {}) },
      }),
    () => {
      setEditing(false);
      setNote("");
      onDone();
    },
  );
  const universe: string[] = p.universe ?? [];
  return (
    <Card title={`Strategy · mandate v${m.mandate.version}`} action={!editing && <button className="btn-ghost text-xs" onClick={() => setEditing(true)}>Adjust</button>}>
      {!editing && summary ? (
        <div className="space-y-3">
          {summary}
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer">History</summary>
            <ul className="mt-2 space-y-1">{m.mandate.history.map((h) => <li key={h.version}>v{h.version} · {when(h.created_at)} · {h.created_by}: {h.note}</li>)}</ul>
          </details>
        </div>
      ) : !editing ? (
        <div className="space-y-3 text-sm text-slate-400">
          <p>
            Screens {universe.length} large US stocks by 12-1 month momentum ÷ volatility, keeping names above their {p.trend_sma_days}-day average. Buys the top {p.positions} names
            with a fresh research approval (≤ {p.research_max_age_days} days), equal weight, at most {pct(p.max_position_pct, 0)} each, keeping {pct(p.cash_buffer_pct, 0)} cash.
          </p>
          <p>
            Rebalances on the first run of each month; sells right away when a trend breaks or research turns to reject. Never trades less than {money(p.min_trade_usd)}. Decides daily at {p.run_time_et} ET.
            New buys pause if the sleeve falls {pct(p.sleeve_drawdown_pause, 0)} below its peak.
          </p>
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer">Universe ({universe.length})</summary>
            <p className="mt-2 leading-relaxed">{universe.join(" · ")}</p>
          </details>
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer">History</summary>
            <ul className="mt-2 space-y-1">{m.mandate.history.map((h) => <li key={h.version}>v{h.version} · {when(h.created_at)} · {h.created_by}: {h.note}</li>)}</ul>
          </details>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            {ALEX_FIELDS_.map(([k, label]) => (
              <Field key={k} label={label}>
                <input className="input" type="number" value={fields[k]} onChange={(e) => setFields({ ...fields, [k]: e.target.value })} />
              </Field>
            ))}
            {timed && (
              <Field label="Daily run (ET)">
                <input className="input" value={fields.run_time_et} onChange={(e) => setFields({ ...fields, run_time_et: e.target.value })} />
              </Field>
            )}
          </div>
          <Field label="Why the change?">
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Required" />
          </Field>
          <div className="flex gap-2">
            <button className="btn-primary" disabled={!note || save.pending} onClick={() => save.run()}>Save as v{m.mandate.version + 1}</button>
            <button className="btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
          </div>
          <ErrorLine error={save.error} />
        </div>
      )}
    </Card>
  );
}

function TimMandateCard({ m, onDone }: { m: ManagerDetail; onDone: () => void }) {
  const p = m.mandate.params;
  const [editing, setEditing] = useState(false);
  const [targets, setTargets] = useState<Record<string, string>>(() => Object.fromEntries(Object.entries(p.targets ?? {}).map(([k, v]) => [k, String(Math.round(Number(v) * 1000) / 10)])));
  const [newSym, setNewSym] = useState("");
  const [fields, setFields] = useState<Record<string, string>>({
    cash_buffer_pct: String(p.cash_buffer_pct * 100),
    abs_band: String(p.abs_band * 100),
    rel_band: String(p.rel_band * 100),
    min_trade_usd: String(p.min_trade_usd),
    run_time_et: String(p.run_time_et),
  });
  const [note, setNote] = useState("");
  const total = Object.values(targets).reduce((s, v) => s + (Number(v) || 0), 0);
  const save = useAction(
    () =>
      api.post(`/api/managers/${m.id}/mandate`, {
        note,
        params: {
          ...p,
          targets: Object.fromEntries(Object.entries(targets).filter(([, v]) => Number(v) > 0).map(([k, v]) => [k.toUpperCase(), Number(v) / 100])),
          cash_buffer_pct: Number(fields.cash_buffer_pct) / 100,
          abs_band: Number(fields.abs_band) / 100,
          rel_band: Number(fields.rel_band) / 100,
          min_trade_usd: Number(fields.min_trade_usd),
          run_time_et: fields.run_time_et,
        },
      }),
    () => {
      setEditing(false);
      setNote("");
      onDone();
    },
  );
  return (
    <Card title={`Strategy · mandate v${m.mandate.version}`} action={!editing && <button className="btn-ghost text-xs" onClick={() => setEditing(true)}>Adjust</button>}>
      {!editing ? (
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap gap-2">
            {Object.entries(p.targets ?? {}).map(([k, v]) => (
              <span key={k} className="rounded-lg bg-ink-800 px-2.5 py-1"><span className="font-medium text-slate-100">{k}</span> <span className="text-slate-400">{pct(Number(v), 0)}</span></span>
            ))}
          </div>
          <p className="text-slate-400">
            Keeps {pct(p.cash_buffer_pct, 0)} cash. Rebalances when a fund drifts more than {pct(p.abs_band, 0)} (or {pct(p.rel_band, 0)} of its target), plus a quarterly check. Never trades less than {money(p.min_trade_usd)}. Decides daily at {p.run_time_et} ET.
          </p>
          <details className="text-xs text-slate-500">
            <summary className="cursor-pointer">History</summary>
            <ul className="mt-2 space-y-1">{m.mandate.history.map((h) => <li key={h.version}>v{h.version} · {when(h.created_at)} · {h.created_by}: {h.note}</li>)}</ul>
          </details>
        </div>
      ) : (
        <div className="space-y-3">
          <div className="label">Target weights (%) · total {total.toFixed(1)}%</div>
          {Object.entries(targets).map(([k, v]) => (
            <div key={k} className="flex items-center gap-2">
              <span className="w-16 font-medium text-slate-200">{k}</span>
              <input className="input" type="number" step="0.5" min="0" value={v} onChange={(e) => setTargets({ ...targets, [k]: e.target.value })} />
            </div>
          ))}
          <div className="flex gap-2">
            <input className="input" placeholder="Add ETF symbol" value={newSym} onChange={(e) => setNewSym(e.target.value.toUpperCase())} />
            <button className="btn-ghost" disabled={!/^[A-Z.]{1,6}$/.test(newSym)} onClick={() => { setTargets({ ...targets, [newSym]: "0" }); setNewSym(""); }}>Add</button>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Cash buffer %"><input className="input" value={fields.cash_buffer_pct} onChange={(e) => setFields({ ...fields, cash_buffer_pct: e.target.value })} /></Field>
            <Field label="Abs. band (pp)"><input className="input" value={fields.abs_band} onChange={(e) => setFields({ ...fields, abs_band: e.target.value })} /></Field>
            <Field label="Rel. band %"><input className="input" value={fields.rel_band} onChange={(e) => setFields({ ...fields, rel_band: e.target.value })} /></Field>
            <Field label="Min trade $"><input className="input" value={fields.min_trade_usd} onChange={(e) => setFields({ ...fields, min_trade_usd: e.target.value })} /></Field>
            <Field label="Daily run (ET)"><input className="input" value={fields.run_time_et} onChange={(e) => setFields({ ...fields, run_time_et: e.target.value })} /></Field>
          </div>
          <Field label="Why this change"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="Recorded in the mandate history" /></Field>
          <div className="flex justify-end gap-2">
            <button className="btn-ghost" onClick={() => setEditing(false)}>Cancel</button>
            <button className="btn-primary" disabled={save.pending || !note.trim() || Math.abs(total - 100) > 0.01} onClick={() => save.run()}>Save mandate</button>
          </div>
          <ErrorLine error={save.error ?? (Math.abs(total - 100) > 0.01 ? "Weights must total 100%." : null)} />
        </div>
      )}
    </Card>
  );
}

function DecisionLog({ m }: { m: ManagerDetail }) {
  if (!m.runs.length) return <Empty>No decisions yet.</Empty>;
  return (
    <ol className="max-h-[420px] space-y-3 overflow-y-auto pr-2">
      {m.runs.map((r) => (
        <li key={r.id} className="rounded-xl bg-ink-850 p-3">
          <div className="flex justify-between text-xs text-slate-500"><span>{when(r.ts)} · {r.payload.book} · {r.payload.trigger}</span><Badge tone={r.payload.book}>{r.payload.activity}</Badge></div>
          <p className="mt-1 text-sm text-slate-200">{r.summary.replace(`${m.name}: `, "")}</p>
          {r.payload.detail?.drift && (
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              {Object.entries(r.payload.detail.drift as Record<string, number>).map(([s, d]) => (
                <span key={s} className="rounded bg-ink-800 px-1.5 py-0.5 text-slate-400">{s} {signedPct(d, 1)}</span>
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}

function Orders({ m }: { m: ManagerDetail }) {
  if (!m.orders.length) return <Empty>No orders yet.</Empty>;
  return (
    <div className="overflow-x-auto">
      <table className="tbl min-w-[720px]">
        <thead><tr><th>When</th><th>Book</th><th>Side</th><th>Symbol</th><th className="text-right">Size</th><th className="text-right">Filled</th><th className="text-right">Avg</th><th>Status</th><th>Why</th></tr></thead>
        <tbody>
          {m.orders.map((o) => (
            <tr key={o.id}>
              <td className="whitespace-nowrap text-slate-400">{when(o.created_at)}</td>
              <td><Badge tone={o.book}>{o.book}</Badge></td>
              <td className={o.side === "buy" ? "text-emerald-300" : "text-rose-300"}>{o.side.toUpperCase()}</td>
              <td className="font-medium text-slate-100">{o.symbol}</td>
              <td className="text-right">{o.notional ? money(Number(o.notional)) : `${o.quantity} sh`}</td>
              <td className="text-right">{Number(o.filled_quantity) ? shares(Number(o.filled_quantity)) : "—"}</td>
              <td className="text-right">{o.average_price ? money(Number(o.average_price)) : "—"}</td>
              <td title={o.block_reasons ?? ""}>{o.status}</td>
              <td className="max-w-[260px] truncate text-slate-400" title={o.reason ?? ""}>{o.reason}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type Note = { id: number; symbol: string; verdict: "approve" | "reject"; conviction: number; thesis: string; risks: string[]; sources: { title: string; url: string }[]; rank: number | null; searches: number; cost: string; created_at: string };

/** Alex's due-diligence notes: what the research desk read and decided, with its sources. */
function ResearchDesk() {
  const { bump } = useLive();
  const { data } = useResource<{ notes: Note[] }>("/api/managers/alex/research", { every: 60000, bump });
  const notes = data?.notes ?? [];
  return (
    <Card title="Research desk" action={<span className="text-xs text-slate-500">AI due diligence with web search · research can veto, never add</span>}>
      {!notes.length ? (
        <Empty>No research yet. Once Alex is switched on, the desk researches the screen's top candidates each trading morning from 8:30 ET.</Empty>
      ) : (
        <ul className="divide-y divide-ink-800">
          {notes.map((n) => (
            <li key={n.id} className="py-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold text-slate-100">{n.symbol}</span>
                <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${n.verdict === "approve" ? "bg-emerald-500/15 text-emerald-300" : "bg-rose-500/15 text-rose-300"}`}>{n.verdict === "approve" ? "Approved" : "Rejected"}</span>
                <span className="text-xs text-slate-500">conviction {n.conviction}/5{n.rank ? ` · ranked #${n.rank}` : " · held name"} · {ago(n.created_at)} · {n.searches} searches · ${Number(n.cost).toFixed(2)}</span>
              </div>
              <p className="mt-1 text-sm text-slate-300">{n.thesis}</p>
              {n.risks.length > 0 && <p className="mt-1 text-xs text-slate-500">Risks: {n.risks.join(" · ")}</p>}
              {n.sources.length > 0 && (
                <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                  {n.sources.map((src) => (
                    <a key={src.url} href={src.url} target="_blank" rel="noreferrer noopener" className="truncate text-gold-400/90 hover:underline" title={src.url}>{src.title}</a>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
