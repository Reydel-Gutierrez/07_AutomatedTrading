// The 537 Academy, floor by floor, both classes: forex (OANDA history, paper on live prices, the
// OANDA Demo Desk) and stocks (the Stock Swing Class: daily US history, paper beside Max's desk).
// Every number is read from the two engines' records; nothing here can place a live broker order.

import { ForexRiskCard } from "../components/risk";
import { type ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { type AcademyOverview, type AgentCard, type AgentDetail, type Check, FLOOR_ORDER, type FxPrice, type SprintReport, type Stats, MARKET, epochIso, fmtPx, fmtR, marketOf, pair, pips } from "../academy";
import { Card, Empty, ErrorLine, Modal, useAction } from "../components/ui";
import { usePageVisible, useResource } from "../hooks";
import { ago, money, pct, signedPct, tone, when } from "../format";

export default function Academy() {
  const { id, day } = useParams();
  if (id === "reports") return <ReportsPage day={day} />;
  return id ? <AgentPage id={id} /> : <AcademyHome />;
}

// -- overview --------------------------------------------------------------------------------

function AcademyHome() {
  const visible = usePageVisible();
  const { data, error } = useResource<AcademyOverview>("/api/academy", { every: visible ? 15000 : 0 });
  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!data) return <div className="p-6 text-slate-500">Opening the Academy…</div>;
  if (!data.available) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <Card title="The 537 Academy">
          <p className="text-sm text-slate-300">{data.reason}</p>
          <p className="mt-2 text-xs text-slate-500">The Forex engine starts with the firm service on the Pi and creates the Academy on its first run.</p>
        </Card>
      </div>
    );
  }
  const order = data.leaderboard ?? data.agents.map((a) => a.id);
  const agents = [...data.agents].sort((a, b) => order.indexOf(a.id) - order.indexOf(b.id));
  return (
    <div className="mx-auto max-w-7xl space-y-5 p-4 sm:p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl text-slate-50">The 537 Academy</h1>
          <p className="mt-1 max-w-2xl text-sm text-slate-400">
            Two classes climb the floors on evidence. Forex agents: research on OANDA history, paper trading on live prices, then real
            orders on an OANDA practice account; a live chair needs Graham's memo and your authorization. Stock agents (the Stock Swing
            Class): research on daily US stock history, then paper trading beside Max's desk; a live swing seat is your decision.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1.5">
          <EngineBadge label="Forex engine" online={!!data.engine?.online} />
          <EngineBadge label="Stock class engine" online={!!data.stock_engine?.online} />
        </div>
      </header>
      <EngineBar engine={data.engine} stocks={data.stock_engine} />
      <ReportCard report={data.latest_report ?? null} />
      {FLOOR_ORDER.map((f) => {
        const here = agents.filter((a) => a.stage === f.stage);
        return (
          <section key={f.stage}>
            <header className="mb-2">
              <h2 className="text-sm font-semibold uppercase tracking-[0.18em] text-gold-300">{f.title} <span className="text-slate-500">· {here.length}</span></h2>
              <p className="text-xs text-slate-500">{f.blurb}</p>
            </header>
            {here.length ? (
              <div className="grid gap-4 md:grid-cols-2">{here.map((a) => <AgentTile key={a.id} a={a} rank={agents.indexOf(a) + 1} />)}</div>
            ) : (
              <p className="rounded-xl border border-dashed border-ink-700 p-4 text-sm text-slate-500">
                {f.stage === "DEMO" ? (data.engine?.demo?.configured ? "Both seats are open." : "Both seats are open. The Demo Desk also needs the OANDA practice account set up on the Pi.") : f.stage === "PAPER" ? "No agent has passed research yet." : "No agents in research."}
              </p>
            )}
          </section>
        );
      })}
      {agents.some((a) => a.stage === "RETIRED") && (
        <section>
          <h2 className="mb-2 text-sm font-semibold uppercase tracking-[0.18em] text-slate-400">Alumni (retired)</h2>
          <div className="grid gap-4 md:grid-cols-2">{agents.filter((a) => a.stage === "RETIRED").map((a) => <AgentTile key={a.id} a={a} rank={0} />)}</div>
        </section>
      )}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_360px]">
        <Card title="Academy journal">
          {data.events.length ? (
            <ul className="space-y-2.5">
              {data.events.map((e) => (
                <li key={e.id} className="flex gap-2.5 text-sm">
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: agents.find((a) => a.id === e.agent_id)?.persona.color ?? "#64748b" }} />
                  <div className="min-w-0">
                    <p className="text-slate-200">{e.summary}</p>
                    <p className="text-[11px] text-slate-500" title={when(e.ts)}>{e.type.toLowerCase().replace(/_/g, " ")} · {ago(e.ts)}</p>
                  </div>
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-slate-500">Nothing recorded yet.</p>}
        </Card>
        <Rates prices={data.prices} />
      </div>
    </div>
  );
}

function ReportCard({ report }: { report: SprintReport | null }) {
  if (!report) {
    return (
      <Card title="Research sprint report">
        <p className="text-sm text-slate-400">The nightly sprints run at 1:00 AM (forex) and 1:30 AM (stocks) New York. Their report appears here, with a short message to you at 8:00 AM.</p>
      </Card>
    );
  }
  return (
    <Card title={`Research sprint report · ${report.day}`} action={<Link to={`/academy/reports/${report.day}`} className="text-xs text-gold-400 hover:underline">Full report →</Link>}>
      <div className="grid gap-3 text-sm sm:grid-cols-3">
        <div><div className="label">Passed → Floor 2</div><div className="mt-1 text-slate-100">{report.passed.join(", ") || "None yet"}</div></div>
        <div><div className="label">Improved (new version)</div><div className="mt-1 text-slate-100">{report.improved.join(", ") || "None"}</div></div>
        <div><div className="label">Tested</div><div className="mt-1 text-slate-100">{report.agents.map((a) => a.name).join(", ")}</div></div>
      </div>
    </Card>
  );
}

function ReportsPage({ day }: { day?: string }) {
  const list = useResource<{ reports: SprintReport[] }>("/api/academy/reports", { every: 0 });
  const chosen = day ?? list.data?.reports[0]?.day;
  const rep = useResource<SprintReport>(chosen ? `/api/academy/reports/${chosen}` : null, { every: 0 });
  const r = rep.data;
  return (
    <div className="mx-auto max-w-6xl space-y-5 p-4 sm:p-6">
      <Link to="/academy" className="text-xs text-slate-500 hover:text-slate-300">← The 537 Academy</Link>
      <header className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="font-display text-2xl text-slate-50">Research sprint reports</h1>
        <div className="flex flex-wrap gap-1 text-xs">
          {(list.data?.reports ?? []).map((x) => (
            <Link key={x.day} to={`/academy/reports/${x.day}`} className={`rounded-md px-2.5 py-1 ${x.day === chosen ? "bg-ink-700 text-slate-50" : "text-slate-400 hover:text-slate-200"}`}>{x.day}</Link>
          ))}
        </div>
      </header>
      {!chosen && <Empty>No sprint has run yet. The first run tonight (forex 1:00 AM, stocks 1:30 AM New York).</Empty>}
      {r && (
        <>
          <p className="text-sm text-slate-400">
            Each Floor 1 agent was polished on its own history (forex on OANDA, stocks on daily US bars; candidates scored on everything before the locked final year), then its best
            version went through the gates: the Examiner's validation and the day-1 test (fresh $10,000 accounts started on random dates).
          </p>
          {(r.sprints ?? []).map((sp) => {
            const sm = sp.summary;
            const v = sm.validation;
            const d1 = sm.day1;
            return (
              <Card key={sp.id} title={`${sm.agent ?? sp.agent_id} · v${sp.from_version}${sp.to_version ? ` → v${sp.to_version}` : ""} · ${(sp.outcome ?? "").replace(/_/g, " ").toLowerCase()}`}
                action={<Link to={`/academy/${sp.agent_id}`} className="text-xs text-gold-400 hover:underline">Agent →</Link>}>
                <p className={`text-sm ${sp.passed ? "text-emerald-300" : "text-slate-200"}`}>{sm.verdict}</p>
                <div className="mt-3 grid gap-3 text-xs sm:grid-cols-3">
                  <div className="rounded-lg bg-ink-850 p-2.5">
                    <div className="label">Research window</div>
                    <p className="mt-1 text-slate-300">Current: {fmtR(sm.current?.avg_r)} over {sm.current?.trades ?? 0} trades, {sm.current?.positive_folds ?? 0}/4 periods positive</p>
                    {sm.best && <p className="mt-1 text-slate-300">Best candidate ({sm.best.label}): {fmtR(sm.best.avg_r)} over {sm.best.trades}, {sm.best.positive_folds}/4 positive</p>}
                    <p className="mt-1 text-slate-500">{sm.tested ?? 0} new variants tried tonight · {sm.sprints_so_far ?? 1} sprint(s) so far</p>
                  </div>
                  <div className="rounded-lg bg-ink-850 p-2.5">
                    <div className="label">Validation ({sm.gated === "candidate" ? "candidate" : "current version"})</div>
                    {v ? (
                      <>
                        <p className="mt-1 text-slate-300">Unseen data: {fmtR(v.unseen?.avg_r)} over {v.unseen?.trades ?? 0} trades, {signedPct(v.unseen?.return_pct)}</p>
                        <p className="mt-1 text-slate-300">Locked final year: {fmtR(v.holdout?.avg_r)}, {signedPct(v.holdout?.return_pct)}</p>
                        <p className="mt-1 text-slate-500">Failing: {v.checks.filter((c) => !c.passed).map((c) => c.name).join(", ") || "nothing"}</p>
                      </>
                    ) : <p className="mt-1 text-slate-500">Not run</p>}
                  </div>
                  <div className="rounded-lg bg-ink-850 p-2.5">
                    <div className="label">Day-1 test (random start dates)</div>
                    {d1?.runs ? (
                      <>
                        <p className="mt-1 text-slate-300">{pct(d1.profitable_share, 0)} of {d1.runs} starts profitable after {d1.horizon_days} days</p>
                        <p className="mt-1 text-slate-300">Median {signedPct(d1.median_return)} · worst {signedPct(d1.worst_return)} · best {signedPct(d1.best_return)}</p>
                        <p className="mt-1 text-slate-500">{d1.passed ? "Passed" : "Not passed"}: {d1.rule}</p>
                      </>
                    ) : <p className="mt-1 text-slate-500">Not run</p>}
                  </div>
                </div>
                {sm.recommend_retire && <p className="mt-3 text-xs text-amber-300">Retirement suggested: {sm.sprints_so_far} sprints without passing. You decide from the agent's page.</p>}
              </Card>
            );
          })}
        </>
      )}
    </div>
  );
}

function EngineBadge({ label, online: on }: { label: string; online: boolean }) {
  return (
    <span className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs ring-1 ring-inset ${on ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30" : "bg-amber-500/10 text-amber-300 ring-amber-500/30"}`}>
      <span className={`h-2 w-2 rounded-full ${on ? "bg-emerald-400" : "bg-amber-400"}`} />
      {label} {on ? "online" : "offline"}
    </span>
  );
}

function EngineBar({ engine, stocks }: { engine?: AcademyOverview["engine"]; stocks?: AcademyOverview["stock_engine"] }) {
  if (!engine) return null;
  const data = engine.data ?? {};
  const sd = stocks?.data ?? {};
  const items: [string, ReactNode][] = [
    ["FX market", engine.market_open ? "Open" : "Closed"],
    ["Forex history", data.ready ? "Loaded" : data.progress ? `Loading ${data.progress}` : "Not loaded"],
    ["Stock history", sd.ready ? `${sd.sessions} sessions to ${sd.last_date}` : sd.reason ?? "Not loaded"],
    ["Background work", engine.job ? `${engine.job.name.replace(":", " · ")} (${ago(epochIso(engine.job.since))})` : engine.queue?.length ? `${engine.queue.length} queued` : "Idle"],
    ["Demo Desk", engine.demo?.configured ? `${engine.demo.seats} practice seat${engine.demo.seats === 1 ? "" : "s"} ready` : "Practice account not set up"],
    ["Heartbeat", engine.heartbeat ? ago(epochIso(engine.heartbeat)) : "—"],
  ];
  return (
    <section className="grid grid-cols-2 gap-3 rounded-xl border border-ink-800 bg-ink-900/60 p-3 text-xs sm:grid-cols-6">
      {items.map(([k, v]) => (
        <div key={k} className="min-w-0">
          <div className="text-slate-500">{k}</div>
          <div className="mt-0.5 truncate text-slate-200">{v}</div>
        </div>
      ))}
      {engine.last_error && <p className="col-span-full text-amber-300/90">Last forex engine issue: {engine.last_error}</p>}
      {stocks?.last_error && <p className="col-span-full text-amber-300/90">Last stock class engine issue: {stocks.last_error}</p>}
    </section>
  );
}

function ValidationBadge({ v }: { v: AgentCard["validation"] }) {
  if (v?.running) return <span className="rounded bg-sky-500/10 px-1.5 py-0.5 text-[10px] text-sky-300">Validating…</span>;
  if (!v?.status) return <span className="rounded bg-slate-500/10 px-1.5 py-0.5 text-[10px] text-slate-400">Not validated yet</span>;
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${v.passed ? "bg-emerald-500/10 text-emerald-300" : "bg-amber-500/10 text-amber-300"}`}>{v.passed ? "Validated" : "Not passing yet"}</span>;
}

function AgentTile({ a, rank }: { a: AgentCard; rank: number }) {
  const v = a.validation;
  return (
    <Link to={`/academy/${a.id}`} className="card block transition hover:border-gold-500/40">
      <div className="flex items-start gap-3">
        <div className="grid h-12 w-12 shrink-0 place-items-center rounded-2xl font-display text-xl text-ink-950" style={{ background: a.persona.color }}>{a.name[0]}</div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-display text-lg text-slate-50">{a.name}</span>
            <span className="text-xs text-slate-500">#{rank}</span>
            <ValidationBadge v={v} />
            {a.paused && <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300">Paused</span>}
          </div>
          <div className="text-xs text-slate-400">{a.persona.tagline} · {MARKET[marketOf(a)].label} · {MARKET[marketOf(a)].trader(a.style)} · v{a.version}{a.klass > 1 ? ` · class ${a.klass}` : ""}</div>
        </div>
        {a.stage === "PAPER" || a.stage === "DEMO" ? (
          <div className="text-right">
            <div className="font-semibold tabular-nums text-slate-50">{money(a.equity)}</div>
            <div className={`text-xs tabular-nums ${tone(a.return_pct)}`}>{signedPct(a.return_pct)}</div>
          </div>
        ) : (
          <div className="text-right text-xs text-slate-400">{a.sprinting ? <span className="text-sky-300">Sprinting: {a.sprinting.step}</span> : a.sprint ? `Last sprint ${a.sprint.day}` : "First sprint tonight"}</div>
        )}
      </div>
      {a.stage === "PAPER" || a.stage === "DEMO" ? (
        <dl className="mt-4 grid grid-cols-4 gap-2 text-xs">
          <Mini label={a.stage === "DEMO" ? "Demo trades" : "Paper trades"} value={a.paper_current_version.trades} />
          <Mini label="Win rate" value={pct(a.paper_current_version.win_rate, 0)} />
          <Mini label="Avg R" value={fmtR(a.paper_current_version.avg_r)} />
          <Mini label="Max DD" value={pct(a.max_drawdown, 1)} />
        </dl>
      ) : (
        <p className="mt-3 line-clamp-3 text-xs text-slate-300">{a.sprint?.verdict ?? `Waiting for its first research sprint (${MARKET[marketOf(a)].sprintAt} NY).`}</p>
      )}
      <div className="mt-3 flex items-center justify-between gap-2 text-xs">
        <span className="text-slate-400">
          {v?.unseen ? <>Backtest (unseen data): {fmtR(v.unseen.avg_r)} avg over {v.unseen.trades} trades</> : "Backtest evidence pending"}
        </span>
        <span className={a.readiness.eligible ? "text-emerald-300" : "text-slate-500"}>{a.readiness.label}</span>
      </div>
      {a.last_event && <p className="mt-2 truncate text-[11px] text-slate-500">{a.last_event.summary} · {ago(a.last_event.ts)}</p>}
    </Link>
  );
}

function Mini({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-lg bg-ink-850 px-2 py-1.5">
      <dt className="text-[10px] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="mt-0.5 font-semibold tabular-nums text-slate-100">{value}</dd>
    </div>
  );
}

function Rates({ prices }: { prices: FxPrice[] }) {
  return (
    <Card title="Live rates · OANDA">
      {prices.length ? (
        <table className="w-full text-xs">
          <thead className="text-slate-500">
            <tr><th className="pb-1 text-left font-normal">Pair</th><th className="pb-1 text-right font-normal">Bid</th><th className="pb-1 text-right font-normal">Ask</th><th className="pb-1 text-right font-normal">Spread</th></tr>
          </thead>
          <tbody>
            {prices.map((p) => (
              <tr key={p.instrument} className="border-t border-ink-800">
                <td className="py-1 font-semibold text-slate-200">{pair(p.instrument)}</td>
                <td className="py-1 text-right tabular-nums text-slate-300">{fmtPx(p.instrument, p.bid)}</td>
                <td className="py-1 text-right tabular-nums text-slate-300">{fmtPx(p.instrument, p.ask)}</td>
                <td className="py-1 text-right tabular-nums text-slate-400">{((p.ask - p.bid) * pips(p.instrument)).toFixed(1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : <p className="text-sm text-slate-500">Prices appear while the forex market is open.</p>}
      {prices[0]?.updated_at && <p className="mt-2 text-[11px] text-slate-500">Updated {ago(prices[0].updated_at)}</p>}
    </Card>
  );
}

// -- one agent ---------------------------------------------------------------------------------

function AgentPage({ id }: { id: string }) {
  const visible = usePageVisible();
  const [bump, setBump] = useState(0);
  const { data: a, error } = useResource<AgentDetail>(`/api/academy/agents/${id}`, { every: visible ? 20000 : 0, bump });
  if (error) return <div className="p-6"><Empty>{error.message}</Empty></div>;
  if (!a) return <div className="p-6 text-slate-500">Loading…</div>;
  const v = a.validation;
  const report = v?.report;
  const mk = marketOf(a);
  return (
    <div className="mx-auto max-w-7xl space-y-5 p-4 sm:p-6">
      <Link to="/academy" className="text-xs text-slate-500 hover:text-slate-300">← The 537 Academy</Link>
      <header className="flex flex-wrap items-start gap-4">
        <div className="grid h-16 w-16 place-items-center rounded-2xl font-display text-3xl text-ink-950" style={{ background: a.persona.color }}>{a.name[0]}</div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-display text-2xl text-slate-50">{a.name}</h1>
            <span className="rounded bg-gold-500/10 px-1.5 py-0.5 text-[10px] text-gold-200">{a.floor}</span>
            <ValidationBadge v={v} />
            {a.paused && <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-300">Paused: {a.paused}</span>}
          </div>
          <p className="text-sm text-slate-400">{a.persona.tagline} · {MARKET[mk].label} · {MARKET[mk].trader(a.style)} · strategy v{a.version}{a.version_note ? ` (${a.version_note})` : ""}</p>
          <p className="mt-2 max-w-3xl text-sm text-slate-300">{a.persona.about}</p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Link to={`/firm/${a.id}`} className="btn-primary text-xs">Sit down with {a.name} →</Link>
          <AcademyControls a={a} onDone={() => setBump((b) => b + 1)} />
        </div>
      </header>

      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Mini label="Virtual equity" value={money(a.equity)} />
        <Mini label="Return" value={<span className={tone(a.return_pct)}>{signedPct(a.return_pct)}</span>} />
        <Mini label="Max drawdown" value={pct(a.max_drawdown, 1)} />
        <Mini label="Paper trades" value={a.paper.trades} />
        <Mini label="Avg R / trade" value={fmtR(a.paper.avg_r)} />
        <Mini label="Open positions" value={a.open_positions} />
      </section>

      {mk === "forex" && a.stage !== "RETIRED" && <ForexRiskCard agentId={a.id} name={a.name} onDone={() => setBump((b) => b + 1)} />}

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Paper equity ($10,000 start)"><EquityLine points={a.equity_series} color={a.persona.color} empty={MARKET[mk].marks} /></Card>
        <Card title={`Backtest equity on ${MARKET[mk].history}`}>
          {report?.equity?.length ? <EquityLine points={report.equity} color="#94a3b8" windows={report.windows} /> : <p className="text-sm text-slate-500">{v?.running ? `Validation running: ${v.running.status.replace("RUNNING: ", "")}` : "Appears after the first validation."}</p>}
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_380px]">
        <ValidationCard report={report} summary={v} />
        <Card title={a.readiness.next ? `Next: ${a.readiness.next}` : "Retired"}>
          <CheckList checks={a.readiness.checks} />
          <p className="mt-3 text-xs text-slate-500">
            {a.readiness.automatic
              ? "Floor 1 → 2 is automatic when every gate passes. After that, every move is your decision."
              : mk === "stocks" ? "You decide: a live swing seat on L3, or more tests. Nothing moves automatically." : "You decide every move above Floor 2. Live capital always needs Graham's memo and your explicit authorization."}
          </p>
        </Card>
      </div>

      <Card title={`Open positions (${a.positions.length})`}>
        {a.positions.length ? (
          <Table head={[MARKET[mk].instrument, "Side", MARKET[mk].units, "Entry", "Mark", "Stop", "Target", "Risk", "Opened", "Why"]}>
            {a.positions.map((p) => (
              <tr key={p.id} className="border-t border-ink-800">
                <td className="py-1.5 font-semibold text-slate-200">{pair(p.instrument)}</td>
                <td className={p.side > 0 ? "text-emerald-300" : "text-rose-300"}>{p.side > 0 ? "Long" : "Short"}</td>
                <td className="tabular-nums">{p.units.toLocaleString()}</td>
                <td className="tabular-nums">{fmtPx(p.instrument, p.entry)}</td>
                <td className="tabular-nums">{fmtPx(p.instrument, p.mark)}</td>
                <td className="tabular-nums">{fmtPx(p.instrument, p.stop)}</td>
                <td className="tabular-nums">{fmtPx(p.instrument, p.target)}</td>
                <td className="tabular-nums">{money(p.risk_usd)}</td>
                <td className="text-slate-400">{ago(epochIso(p.opened_at))}</td>
                <td className="max-w-[260px] truncate text-slate-400" title={p.tag}>{p.tag}</td>
              </tr>
            ))}
          </Table>
        ) : <p className="text-sm text-slate-500">Flat.</p>}
      </Card>

      <Card title="Recent paper trades">
        {a.trades.length ? (
          <Table head={["Closed", MARKET[mk].instrument, "Side", MARKET[mk].units, "Entry", "Exit", "P&L", "R", "Exit", "v"]}>
            {a.trades.map((t) => (
              <tr key={t.id} className="border-t border-ink-800">
                <td className="py-1.5 text-slate-400">{when(epochIso(t.closed_at))}</td>
                <td className="font-semibold text-slate-200">{pair(t.instrument)}</td>
                <td className={t.side > 0 ? "text-emerald-300" : "text-rose-300"}>{t.side > 0 ? "Long" : "Short"}</td>
                <td className="tabular-nums">{t.units.toLocaleString()}</td>
                <td className="tabular-nums">{fmtPx(t.instrument, t.entry)}</td>
                <td className="tabular-nums">{fmtPx(t.instrument, t.exit)}</td>
                <td className={`tabular-nums ${tone(t.pnl_usd + t.financing_usd)}`}>{money(t.pnl_usd + t.financing_usd)}</td>
                <td className={`tabular-nums ${tone(t.r)}`}>{fmtR(t.r)}</td>
                <td className="text-slate-400">{t.exit_reason}</td>
                <td className="text-slate-500">{t.version}</td>
              </tr>
            ))}
          </Table>
        ) : <p className="text-sm text-slate-500">No closed trades yet. {a.style === "day" ? "Day trades open and close within the session." : "Swing trades can take days to set up and resolve."}</p>}
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="What the paper record says">
          <Diagnostics d={a.diagnostics} market={mk} />
        </Card>
        <Card title={`Strategy v${a.version}`}>
          <Params version={a.versions[0]} />
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Research sprints">
          {a.sprints.length ? (
            <ul className="space-y-3 text-sm">
              {a.sprints.map((sp) => (
                <li key={sp.id}>
                  <p className="text-slate-200">
                    <Link to={`/academy/reports/${sp.day}`} className="text-gold-300 hover:underline">{sp.day}</Link> · {(sp.outcome ?? "running").replace(/_/g, " ").toLowerCase()}
                    {sp.to_version ? ` → v${sp.to_version}` : ""}
                  </p>
                  <p className="text-xs text-slate-500">{sp.summary.verdict ?? sp.summary.change ?? ""}</p>
                </li>
              ))}
            </ul>
          ) : <p className="text-sm text-slate-500">{a.stage === "RESEARCH" ? `The first sprint runs tonight at ${MARKET[mk].sprintAt} New York.` : "Agents above Floor 1 are not modified; a strategy that needs changes goes back to research."}</p>}
        </Card>
        <Card title="Versions and revisions"><Versions a={a} /></Card>
        <Card title="Journal">
          <ul className="space-y-2 text-sm">
            {a.events.map((e) => (
              <li key={e.id}>
                <p className="text-slate-200">{e.summary.replace(`${a.name}: `, "")}</p>
                <p className="text-[11px] text-slate-500">{e.type.toLowerCase().replace(/_/g, " ")} · {ago(e.ts)}</p>
              </li>
            ))}
          </ul>
        </Card>
      </div>
    </div>
  );
}

export function AcademyControls({ a, onDone }: { a: AgentDetail; onDone: () => void }) {
  const [confirm, setConfirm] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const stocks = marketOf(a) === "stocks";
  const act = useAction(
    (action: string) => api.post(`/api/academy/agents/${a.id}/${action}`, { note: "" }).then(() => setSent(action)),
    () => {
      onDone();
      // the engine reads its inbox within a minute; look again so the result shows up here
      [10_000, 30_000, 75_000].forEach((ms) => setTimeout(onDone, ms));
    },
  );
  const failing = a.readiness.checks.filter((c) => !c.passed && !c.advisory).map((c) => c.name);
  const LABELS: Record<string, [string, string]> = {
    pause: ["Pause", stocks ? "Skip this agent in sprints and paper updates until you resume it." : "Stop new paper entries. Open paper positions keep their stops and targets."],
    resume: ["Resume", stocks ? "Include this agent again." : "Allow new paper entries again (the drawdown peak resets to today's equity)."],
    validate: ["Re-validate", "Run the full validation on the latest history. Takes a few minutes on the Pi."],
    sprint: ["Sprint now", "Run tonight's research sprint now (a few minutes on the Pi). It adopts a better version, and promotes to Floor 2 only if every gate passes."],
    auto_on: ["Nightly sprints on", "Include this agent in the nightly research sprints."],
    auto_off: ["Nightly sprints off", "Skip this agent in the nightly sprints; it keeps its current version."],
    to_paper: ["Move to paper trading", `Move to Floor 2 now, on your decision, ${failing.length ? `before these gates are met: ${failing.join("; ")}` : "ahead of tonight's sprint"}. ${stocks ? "It starts a fresh $10,000 paper record: each day it decides at the close and fills at the next session's open on real daily bars, compared with Max's live desk." : "It starts a fresh $10,000 paper account trading live OANDA prices."} Nightly sprints stop changing it; you can send it back to research any time.`],
    to_demo: ["Give a Demo Desk seat", "Move to Floor 3: real orders on its own OANDA practice sub-account (fake money). Its paper positions close first."],
    to_research: ["Send back to research", stocks ? "Return to Floor 1 for more tests; its paper record is cleared and nightly sprints resume." : "Return to Floor 1. Any open paper positions close at the market; nightly sprints resume."],
    retire: ["Retire", "Retire this agent to the Alumni wall. Its records are kept; it stops trading and researching."],
  };
  const paused = !!a.paused || a.status === "PAUSED";
  const buttons = stocks
    ? a.stage === "RESEARCH" ? ["sprint", "to_paper", paused ? "resume" : "pause", "retire"] : a.stage === "PAPER" ? [paused ? "resume" : "pause", "to_research", "retire"] : []
    : a.stage === "RESEARCH"
    ? ["sprint", "to_paper", a.auto_revise ? "auto_off" : "auto_on", "validate", paused ? "resume" : "pause", "retire"]
    : a.stage === "PAPER"
      ? [a.paused ? "resume" : "pause", "to_demo", "to_research", "retire"]
      : a.stage === "DEMO"
        ? [a.paused ? "resume" : "pause", "to_research", "retire"]
        : [];
  const last = a.sprinting?.running ? `Sprint running${a.sprinting.step ? `: ${a.sprinting.step}` : ""}…` : a.sprint ? `Last sprint ${ago(a.sprint.finished_at)}: ${a.sprint.verdict ?? a.sprint.outcome.toLowerCase().replace(/_/g, " ")}` : null;
  return (
    <div className="space-y-2">
    <div className="flex flex-wrap gap-2">
      {buttons.map((b) => (
        <button key={b} className="btn-ghost text-xs" onClick={() => setConfirm(b)}>{LABELS[b][0]}</button>
      ))}
    </div>
    {sent && <p className="text-xs text-emerald-400">{LABELS[sent]?.[0] ?? sent}: sent. The engine picks it up within a minute{sent === "sprint" ? " and the result shows below." : "."}</p>}
    {a.stage === "RESEARCH" && last && <p className="max-w-xl text-xs text-slate-400">{last}</p>}
      <Modal open={!!confirm} title={confirm ? `${LABELS[confirm][0]} ${a.name}?` : ""} onClose={() => setConfirm(null)}>
        {confirm && (
          <div className="space-y-3 text-sm">
            <p className="text-slate-300">{LABELS[confirm][1]}</p>
            <p className="text-xs text-slate-500">{confirm === "to_demo" ? "Practice account only: fake money, real OANDA orders. Your live account is never touched." : stocks ? "Paper only: this never places an order." : "This never touches your live OANDA account."}</p>
            <ErrorLine error={act.error} />
            <div className="flex justify-end gap-2">
              <button className="btn-ghost" onClick={() => setConfirm(null)}>Cancel</button>
              <button className="btn-primary" disabled={act.pending} onClick={() => act.run(confirm).then((ok) => ok && setConfirm(null))}>{LABELS[confirm][0]}</button>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

export function CheckList({ checks }: { checks: Check[] }) {
  return (
    <ul className="space-y-2 text-sm">
      {checks.map((c) => (
        <li key={c.name} className="flex gap-2">
          <span className={`mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full text-[10px] ${c.passed ? "bg-emerald-500/20 text-emerald-300" : "bg-slate-500/15 text-slate-400"}`}>{c.passed ? "✓" : "·"}</span>
          <div className="min-w-0">
            <p className={c.advisory ? "text-slate-400" : "text-slate-200"}>{c.name}{c.advisory && <span className="ml-1.5 rounded bg-slate-500/10 px-1 text-[10px] text-slate-500">context, not a gate</span>}</p>
            <p className="text-xs text-slate-500">{c.detail ?? c.rule}{c.value !== undefined && c.value !== null ? ` · ${String(c.value)}` : ""}</p>
          </div>
        </li>
      ))}
    </ul>
  );
}

export function ValidationCard({ report, summary }: { report?: NonNullable<AgentDetail["validation"]>["report"]; summary: AgentDetail["validation"] }) {
  if (!report) {
    return <Card title="Validation (the Examiner)"><p className="text-sm text-slate-500">{summary?.running ? `Running: ${summary.running.status.replace("RUNNING: ", "")}` : "Pending: runs once market history is loaded."}</p></Card>;
  }
  const mk = report.market === "stocks" ? "stocks" : "forex";
  const W: [string, string][] = [["in_sample", "In-sample"], ["out_of_sample", "Out-of-sample"], ["holdout", "Locked final year"]];
  const yr = (k: string) => {
    const w = report.windows[k];
    return w ? `${new Date(w[0] * 1000).toLocaleDateString(undefined, { month: "short", year: "2-digit" })}–${new Date(w[1] * 1000).toLocaleDateString(undefined, { month: "short", year: "2-digit" })}` : "";
  };
  return (
    <Card title={`Validation (the Examiner) · ${report.passed ? "passed" : "failed"}`} action={summary?.finished_at ? <span className="text-xs text-slate-500">{ago(summary.finished_at)} · {report.seconds}s</span> : null}>
      <CheckList checks={report.checks} />
      <Table head={["Window", "Trades", "Win", "Avg R", "PF", "Return", "Max DD", "Sharpe"]} className="mt-4">
        {W.map(([k, label]) => {
          const m = report.metrics[k];
          return m ? (
            <tr key={k} className="border-t border-ink-800">
              <td className="py-1.5 text-slate-200">{label} <span className="text-slate-500">{yr(k)}</span></td>
              <td className="tabular-nums">{m.trades}</td>
              <td className="tabular-nums">{pct(m.win_rate, 0)}</td>
              <td className={`tabular-nums ${tone(m.avg_r)}`}>{fmtR(m.avg_r)}</td>
              <td className="tabular-nums">{m.profit_factor === null ? "—" : m.profit_factor.toFixed(2)}</td>
              <td className={`tabular-nums ${tone(m.return_pct)}`}>{signedPct(m.return_pct)}</td>
              <td className="tabular-nums">{pct(m.max_drawdown, 1)}</td>
              <td className="tabular-nums">{m.sharpe === null ? "—" : m.sharpe.toFixed(2)}</td>
            </tr>
          ) : null;
        })}
      </Table>
      <p className="mt-3 text-xs text-slate-500">
        {MARKET[mk].baseline} (same entries, stops, and exits): p = {report.baseline.p_value.toFixed(2)} · Cost stress ({MARKET[mk].stress}): {money(report.stress.net_usd)} · {mk === "stocks" ? `1-year windows within a 10% drawdown: ${pct(report.dd_within_limit_share, 0)}` : `95th pct resampled drawdown: ${pct(report.monte_carlo_dd95, 1)}`} · Trial-adjusted Sharpe: {report.trial_adjusted_sharpe === null ? "—" : report.trial_adjusted_sharpe.toFixed(2)} ({report.trials} variants tried)
      </p>
      {Object.keys(report.by_instrument).length > 0 && (
        <p className="mt-2 text-xs text-slate-500">
          Unseen-data results by {MARKET[mk].instrument.toLowerCase()}: {Object.entries(report.by_instrument).map(([k, s]) => `${pair(k)} ${fmtR(s.avg_r)} (${s.trades})`).join(" · ")}
        </p>
      )}
    </Card>
  );
}

export function Diagnostics({ d, market }: { d: AgentDetail["diagnostics"]; market: "forex" | "stocks" }) {
  const blocks = ([["by_instrument", `By ${MARKET[market].instrument.toLowerCase()}`], ["by_session", "By session opened"], ["by_exit", "By exit"], ["by_side", "By direction"]] as [keyof AgentDetail["diagnostics"], string][])
    .filter(([k]) => Object.keys(d[k] ?? {}).length);
  if (!Object.keys(d.by_instrument).length) return <p className="text-sm text-slate-500">Breakdowns appear after the first closed paper trades. The weekly review uses them.</p>;
  return (
    <div className="space-y-4">
      {blocks.map(([k, label]) => (
        <div key={k}>
          <h3 className="label mb-1">{label}</h3>
          <Table head={["", "Trades", "Win", "Avg R", "Net"]}>
            {Object.entries(d[k]).map(([name, s]: [string, Stats]) => (
              <tr key={name} className="border-t border-ink-800">
                <td className="py-1 text-slate-200">{k === "by_instrument" ? pair(name) : name}</td>
                <td className="tabular-nums">{s.trades}</td>
                <td className="tabular-nums">{pct(s.win_rate, 0)}</td>
                <td className={`tabular-nums ${tone(s.avg_r)}`}>{fmtR(s.avg_r)}</td>
                <td className={`tabular-nums ${tone(s.net_usd)}`}>{money(s.net_usd)}</td>
              </tr>
            ))}
          </Table>
        </div>
      ))}
    </div>
  );
}

export function Params({ version }: { version?: AgentDetail["versions"][number] }) {
  if (!version) return null;
  const env = version.envelope as Record<string, number | string | boolean>;
  const fmt = (v: unknown) => (Array.isArray(v) ? v.map((x) => (Array.isArray(x) ? x.join("–") : pair(String(x)))).join(", ") : String(v));
  return (
    <div className="space-y-3 text-xs">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
        {Object.entries(version.params).map(([k, v]) => (
          <div key={k} className="flex justify-between gap-2 border-b border-ink-800 py-0.5"><dt className="text-slate-500">{k.replace(/_/g, " ")}</dt><dd className="truncate text-right text-slate-200" title={fmt(v)}>{fmt(v)}</dd></div>
        ))}
      </dl>
      <p className="text-slate-400">
        Risk envelope: {pct(Number(env.risk_pct), 2)} of equity per trade · up to {String(env.max_positions)} positions · leverage ≤ {String(env.max_leverage)}:1 · margin ≤ {pct(Number(env.max_margin_frac), 0)} · daily loss stop {pct(Number(env.daily_loss), 0)} · pause at {pct(Number(env.drawdown_pause), 0)} drawdown
        {env.style === "swing" ? (env.weekend_hold ? ` · weekend holds ≤ ${String(env.weekend_max_leverage)}:1 and ≤ ${pct(Number(env.weekend_gap_loss), 1)} gap risk` : " · flat for weekends") : " · flat before the 17:00 NY rollover"}
      </p>
    </div>
  );
}

export function Table({ head, children, className = "" }: { head: string[]; children: ReactNode; className?: string }) {
  return (
    <div className={`overflow-x-auto ${className}`}>
      <table className="w-full min-w-[520px] text-xs text-slate-300">
        <thead className="text-slate-500"><tr>{head.map((h, i) => <th key={i} className="pb-1 text-left font-normal">{h}</th>)}</tr></thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function EquityLine({ points, color, empty = "No data yet.", windows }: { points: [number, number][]; color: string; empty?: string; windows?: Record<string, [number, number]> }) {
  const [hover, setHover] = useState<number | null>(null);
  if (points.length < 2) return <div className="grid h-[150px] place-items-center text-sm text-slate-500">{empty}</div>;
  const w = 600, h = 170;
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const vals = points.map((p) => p[1]);
  const lo = Math.min(...vals, 10_000) * 0.995, hi = Math.max(...vals, 10_000) * 1.005;
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * w;
  const y = (v: number) => h - ((v - lo) / (hi - lo || 1)) * h;
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const i = hover ?? points.length - 1;
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-slate-400">
        <span className="tabular-nums">{money(points[i][1])} <span className={tone(points[i][1] - 10_000)}>({signedPct(points[i][1] / 10_000 - 1)})</span></span>
        <span>{new Date(points[i][0] * 1000).toLocaleString()}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} className="h-auto w-full" preserveAspectRatio="none" onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const r = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          const t = t0 + ((e.clientX - r.left) / r.width) * (t1 - t0);
          let best = 0;
          for (let k = 0; k < points.length; k++) if (Math.abs(points[k][0] - t) < Math.abs(points[best][0] - t)) best = k;
          setHover(best);
        }}>
        {windows && (["out_of_sample", "holdout"] as const).map((k) => windows[k] && (
          <rect key={k} x={x(windows[k][0])} y={0} width={Math.max(x(windows[k][1]) - x(windows[k][0]), 0)} height={h} fill={k === "holdout" ? "#cdb27a" : "#38bdf8"} opacity={0.06} />
        ))}
        <line x1={0} x2={w} y1={y(10_000)} y2={y(10_000)} stroke="#283044" strokeDasharray="4 4" />
        <path d={d} fill="none" stroke={color} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        <line x1={x(points[i][0])} x2={x(points[i][0])} y1={0} y2={h} stroke="#3a4560" vectorEffect="non-scaling-stroke" />
      </svg>
      {windows && <p className="mt-1 text-[11px] text-slate-500">Shaded: blue = out-of-sample, gold = locked final year.</p>}
    </div>
  );
}

export function Versions({ a }: { a: AgentDetail }) {
  return (
    <>
    <ul className="space-y-3 text-sm">
      {a.revisions.map((r) => (
        <li key={`r${r.id}`}>
          <p className="text-slate-200">Weekly review: <span className={r.outcome === "ADOPTED" ? "text-emerald-300" : "text-slate-300"}>{r.outcome.toLowerCase()}</span>{r.chosen ? ` · ${r.chosen}` : ""}</p>
          <p className="text-xs text-slate-500">{r.reason} · {r.report.trials ?? 0} candidates · {ago(r.ts)}</p>
        </li>
      ))}
      {a.versions.map((ver) => (
        <li key={`v${ver.version}`}>
          <p className="text-slate-200">v{ver.version} · {ver.note}</p>
          <p className="text-xs text-slate-500">{ver.validation_status.toLowerCase()} · by {ver.created_by} · {ago(ver.created_at)}</p>
        </li>
      ))}
    </ul>
    <p className="mt-3 text-xs text-slate-500">
      Auto-revision is {a.auto_revise ? "on" : "off"}. Weekly reviews test one-step parameter changes (and dropping a losing pair) on older data; a change is adopted only if it passes the full validation. {a.trials} strategy variants tried so far.
    </p>
    </>
  );
}
