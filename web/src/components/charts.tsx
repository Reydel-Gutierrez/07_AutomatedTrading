import { useState } from "react";
import { type Series } from "../api";
import { money, pct } from "../format";

export function Donut({ slices, size = 180 }: { slices: { label: string; value: number; color: string }[]; size?: number }) {
  const total = slices.reduce((s, x) => s + Math.max(x.value, 0), 0);
  const r = size / 2 - 14;
  const c = 2 * Math.PI * r;
  let offset = 0;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Capital allocation">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="#1c2230" strokeWidth={22} />
      {total > 0 &&
        slices.map((s) => {
          const len = (Math.max(s.value, 0) / total) * c;
          const el = (
            <circle
              key={s.label}
              cx={size / 2}
              cy={size / 2}
              r={r}
              fill="none"
              stroke={s.color}
              strokeWidth={22}
              strokeDasharray={`${Math.max(len - 2, 0)} ${c}`}
              strokeDashoffset={-offset}
              transform={`rotate(-90 ${size / 2} ${size / 2})`}
            >
              <title>{`${s.label}: ${money(s.value)} (${pct(s.value / total)})`}</title>
            </circle>
          );
          offset += len;
          return el;
        })}
    </svg>
  );
}

/** Growth of $1 for the bucket (flow-adjusted) vs SPY over the same dates. */
export function PerformanceChart({ series, color = "#dcc287", height = 180 }: { series: Series; color?: string; height?: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const pts = series.filter((p) => p.nav > 0);
  if (pts.length < 2) {
    return <div className="grid h-[140px] place-items-center text-sm text-slate-500">Performance appears after two daily closes.</div>;
  }
  const growth: number[] = [1];
  for (let i = 1; i < pts.length; i++) growth.push(growth[i - 1] * ((pts[i].nav - pts[i].flow) / pts[i - 1].nav));
  const spy0 = pts[0].spy;
  const bench = pts.map((p) => (spy0 && p.spy ? p.spy / spy0 : null));
  const all = [...growth, ...bench.filter((x): x is number => x !== null)];
  const lo = Math.min(...all) * 0.995;
  const hi = Math.max(...all) * 1.005;
  const w = 600;
  const x = (i: number) => (i / (pts.length - 1)) * w;
  const y = (v: number) => height - ((v - lo) / (hi - lo)) * height;
  const path = (vals: (number | null)[]) =>
    vals.map((v, i) => (v === null ? "" : `${i === 0 || vals[i - 1] === null ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`)).join("");
  const h = hover ?? pts.length - 1;
  return (
    <div>
      <div className="mb-2 flex gap-4 text-xs text-slate-400">
        <span><span className="mr-1 inline-block h-2 w-2 rounded-full" style={{ background: color }} />Portfolio {pct(growth[h] - 1, 2)}</span>
        <span><span className="mr-1 inline-block h-2 w-2 rounded-full bg-slate-400" />S&amp;P 500 {bench[h] !== null ? pct((bench[h] as number) - 1, 2) : "—"}</span>
        <span className="ml-auto">{pts[h].date}</span>
      </div>
      <svg viewBox={`0 0 ${w} ${height}`} className="h-auto w-full" preserveAspectRatio="none" onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const rect = (e.currentTarget as SVGSVGElement).getBoundingClientRect();
          setHover(Math.round(((e.clientX - rect.left) / rect.width) * (pts.length - 1)));
        }}>
        <line x1={0} x2={w} y1={y(1)} y2={y(1)} stroke="#283044" strokeDasharray="4 4" />
        <path d={path(bench)} fill="none" stroke="#94a3b8" strokeWidth={1.5} opacity={0.7} vectorEffect="non-scaling-stroke" />
        <path d={path(growth)} fill="none" stroke={color} strokeWidth={2.2} vectorEffect="non-scaling-stroke" />
        <line x1={x(h)} x2={x(h)} y1={0} y2={height} stroke="#3a4560" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  );
}

export function WeightBars({ rows, color }: { rows: { symbol: string; weight: number; target: number }[]; color: string }) {
  const max = Math.max(0.01, ...rows.flatMap((r) => [r.weight, r.target]));
  return (
    <div className="space-y-3">
      {rows.map((r) => (
        <div key={r.symbol}>
          <div className="mb-1 flex justify-between text-xs">
            <span className="font-medium text-slate-200">{r.symbol}</span>
            <span className="tabular-nums text-slate-400">{pct(r.weight)} <span className="text-slate-600">/ target {pct(r.target, 0)}</span></span>
          </div>
          <div className="relative h-2 rounded-full bg-ink-800">
            <div className="h-2 rounded-full" style={{ width: `${(r.weight / max) * 100}%`, background: color }} />
            <div className="absolute top-[-3px] h-[14px] w-0.5 rounded bg-slate-200" style={{ left: `${(r.target / max) * 100}%` }} title={`target ${pct(r.target, 0)}`} />
          </div>
        </div>
      ))}
    </div>
  );
}
