import { Link } from "react-router-dom";
import { useLive } from "../App";
import { type FirmEvent, type ManagerCard, type Overview, type Proposal } from "../api";
import { useResource } from "../hooks";
import { Donut, PerformanceChart } from "../components/charts";
import { Feed } from "../components/Feed";
import { AGENTS, Avatar, Badge, Card, Empty, Stat } from "../components/ui";
import { ago, money, pct, shares, signedPct, tone, until } from "../format";

const ALLOC_COLORS: Record<string, string> = { tim: AGENTS.tim.color, alex: AGENTS.alex.color, max: AGENTS.max.color, unallocated: "#475569" };
const WORKING = new Set(["investing", "rebalancing"]);

export function isWorking(m: { activity: string | null }, events: FirmEvent[], id: string): boolean {
  // Driven only by real backend events: an order or run for this agent in the last 3 minutes.
  const recent = events.find((e) => e.manager_id === id);
  const fresh = recent ? Date.now() - new Date(recent.ts).getTime() < 180000 : false;
  return fresh && (WORKING.has(m.activity ?? "") || recent!.type.startsWith("ORDER_"));
}

export default function HQ() {
  const { overview: o, events } = useLive();
  if (!o) return <div className="text-slate-500">Loading the firm…</div>;
  if (!o.books_open) {
    return <Empty>The firm's books are not open yet. The migration opens them from a fresh Robinhood snapshot.</Empty>;
  }
  const perf = o.firm.performance;
  return (
    <div className="space-y-6">
      <section className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <Card>
          <div className="flex flex-wrap items-end justify-between gap-6">
            <div>
              <div className="label">Firm value</div>
              <div className="mt-1 font-display text-5xl text-slate-50 tabular-nums">{money(o.firm.nav)}</div>
              <div className="mt-2 text-sm text-slate-400">
                Since {perf.since ?? "inception"}: <span className={tone(perf.twr)}>{signedPct(perf.twr)}</span> vs S&amp;P 500{" "}
                <span className={tone(perf.benchmark)}>{signedPct(perf.benchmark)}</span>
              </div>
            </div>
            <div className="flex gap-8">
              <Stat label="Invested" value={money(o.firm.invested)} />
              <Stat label="Unallocated" value={money(o.firm.unallocated)} sub={<Link to="/capital" className="text-gold-400 hover:underline">Allocate →</Link>} />
              {o.firm.suspense !== 0 && <Stat label="To review" value={money(o.firm.suspense)} sub={<Link to="/capital" className="text-amber-300">Resolve →</Link>} />}
            </div>
          </div>
          <div className="mt-6">
            <PerformanceChart series={perf.series} />
          </div>
          {o.price_source !== "live" && <p className="mt-2 text-xs text-slate-500">Prices: {o.price_source} (market data unavailable right now).</p>}
        </Card>
        <Card title="Capital allocation" action={<Link to="/capital" className="text-xs text-gold-400 hover:underline">Manage</Link>}>
          <div className="flex items-center gap-6">
            <Donut slices={o.allocation.map((a) => ({ label: a.name, value: a.nav, color: ALLOC_COLORS[a.id] ?? "#64748b" }))} />
            <ul className="flex-1 space-y-2 text-sm">
              {o.allocation.map((a) => (
                <li key={a.id} className="flex items-center gap-2">
                  <span className="h-2.5 w-2.5 rounded-sm" style={{ background: ALLOC_COLORS[a.id] }} />
                  <span className="flex-1 text-slate-300">{a.name}</span>
                  <span className="tabular-nums text-slate-400">{pct(a.pct, 0)}</span>
                  <span className="w-24 text-right tabular-nums text-slate-200">{money(a.nav)}</span>
                </li>
              ))}
            </ul>
          </div>
        </Card>
      </section>

      <section>
        <h2 className="label mb-3">Your investment team</h2>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <CioCard />
          {o.managers.map((m) => (
            <ManagerTile key={m.id} m={m} working={isWorking(m, events, m.id)} />
          ))}
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-[1.4fr_1fr]">
        <Card title="Current investments">
          <Holdings o={o} />
        </Card>
        <Card title="Live activity" action={<Link to="/activity" className="text-xs text-gold-400 hover:underline">All activity</Link>}>
          <Feed events={events.slice(0, 12)} compact />
        </Card>
      </section>
    </div>
  );
}

function CioCard() {
  const { bump } = useLive();
  const open = useResource<{ proposals: Proposal[] }>("/api/cio/proposals?status=PENDING", { every: 60000, bump }).data?.proposals.length ?? 0;
  return (
    <Link to="/cio" className="card flex flex-col border-gold-500/30 bg-gradient-to-br from-ink-900 to-ink-850 transition hover:border-gold-500/60">
      <div className="flex items-center gap-3">
        <Avatar id="cio" />
        <div>
          <div className="font-display text-lg text-slate-50">CIO</div>
          <div className="text-xs text-slate-400">{AGENTS.cio.title}</div>
        </div>
      </div>
      <p className="mt-4 flex-1 text-sm text-slate-400">{AGENTS.cio.blurb}</p>
      <div className="mt-4 flex items-center justify-between rounded-lg bg-ink-800/80 px-3 py-2 text-xs">
        <span className="text-gold-400">Enter the executive office →</span>
        {open > 0 && <span className="rounded-full bg-gold-500 px-2 py-0.5 font-semibold text-ink-950">{open} to decide</span>}
      </div>
    </Link>
  );
}

function ManagerTile({ m, working }: { m: ManagerCard; working: boolean }) {
  const a = AGENTS[m.id];
  const active = m.mode === "LIVE" || m.mode === "SIM";
  const book = m.mode === "SIM" ? m.sim : m.live;
  const perf = m.mode === "SIM" ? m.performance.SIM : m.performance.LIVE;
  return (
    <Link to={`/managers/${m.id}`} className="card flex flex-col transition hover:border-ink-600 hover:bg-ink-850">
      <div className="flex items-center gap-3">
        <Avatar id={m.id} working={working} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-display text-lg text-slate-50">{m.name}</span>
            <Badge tone={m.mode}>{m.mode}</Badge>
          </div>
          <div className="truncate text-xs text-slate-400">{m.role}</div>
        </div>
      </div>
      <div className="mt-4 flex items-baseline justify-between">
        <span className="text-2xl font-semibold tabular-nums text-slate-50">{money(m.live.nav)}</span>
        {active && <span className={`text-sm tabular-nums ${tone(perf.twr)}`}>{signedPct(perf.twr)}</span>}
      </div>
      <p className="mt-3 flex-1 text-sm text-slate-300">
        {!active
          ? m.mode === "INACTIVE" && m.id !== "tim"
            ? `${a.blurb} Activates in Phase ${m.id === "alex" ? 3 : 4}.`
            : `${m.name} is ${m.mode.toLowerCase()}.`
          : m.status === "DEGRADED"
            ? <span className="text-rose-300">Needs attention: {m.activity}</span>
            : m.last_run?.summary.replace(`${m.name}: `, "") ?? "Waiting for the first run."}
      </p>
      <div className="mt-3 flex justify-between text-xs text-slate-500">
        <span>{m.last_run ? `Last ran ${ago(m.last_run.at)}` : "Not run yet"}</span>
        {active && <span>Next {until(m.next_run)}</span>}
      </div>
      {m.mode === "SIM" && <div className="mt-2 text-xs text-sky-300">Simulated book {money(book.nav)} · live cash {money(m.live.cash)} waiting</div>}
    </Link>
  );
}

function Holdings({ o }: { o: Overview }) {
  if (!o.holdings.length) return <Empty>No live positions yet. Tim's first purchases appear here after he goes live.</Empty>;
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Manager</th>
          <th>Symbol</th>
          <th className="text-right">Shares</th>
          <th className="text-right">Value</th>
          <th className="text-right">Gain</th>
        </tr>
      </thead>
      <tbody>
        {o.holdings.map((h) => (
          <tr key={`${h.manager}-${h.symbol}`}>
            <td>
              <span className="inline-flex items-center gap-2">
                <span className="h-2 w-2 rounded-full" style={{ background: AGENTS[h.manager]?.color }} />
                {AGENTS[h.manager]?.name ?? h.manager}
              </span>
            </td>
            <td className="font-medium text-slate-100">{h.symbol}</td>
            <td className="text-right">{shares(h.quantity)}</td>
            <td className="text-right">{money(h.value)}</td>
            <td className={`text-right ${tone(h.pnl)}`}>{money(h.pnl)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
