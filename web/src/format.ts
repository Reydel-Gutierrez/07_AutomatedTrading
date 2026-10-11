const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const et = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const etDay = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" });

export const money = (v: number | null | undefined) => (v === null || v === undefined ? "—" : usd.format(v));
export const pct = (v: number | null | undefined, digits = 1) => (v === null || v === undefined ? "—" : `${(v * 100).toFixed(digits)}%`);
export const signedPct = (v: number | null | undefined, digits = 2) => (v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`);
export const shares = (v: number) => (Math.abs(v) >= 100 ? v.toFixed(2) : v.toFixed(4).replace(/\.?0+$/, ""));
export const when = (iso: string | null | undefined) => (iso ? `${et.format(new Date(iso))} ET` : "—");
export const day = (iso: string | null | undefined) => (iso ? etDay.format(new Date(iso)) : "—");

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function until(iso: string | null | undefined): string {
  if (!iso) return "not scheduled";
  const s = (new Date(iso).getTime() - Date.now()) / 1000;
  if (s <= 0) return "due now";
  if (s < 3600) return `in ${Math.ceil(s / 60)}m`;
  if (s < 86400) return `in ${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return day(iso);
}

export const tone = (v: number | null | undefined) => (v === null || v === undefined ? "text-slate-400" : v >= 0 ? "text-emerald-400" : "text-rose-400");
