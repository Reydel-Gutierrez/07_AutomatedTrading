// The 537 Firm's identity: a quiet monogram and a letter-spaced wordmark.
// Technical identifiers (service names, paths, database) keep their original names.

export const BRAND = { name: "The 537 Firm", descriptor: "Private Investment Management", tagline: "Capital · Strategy · Growth" };

export function Monogram({ size = 30 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" className="shrink-0">
      <rect x="2" y="2" width="60" height="60" rx="13" fill="#0f131b" stroke="#cdb27a" strokeOpacity=".75" strokeWidth="2" />
      <text x="32" y="41" textAnchor="middle" fontFamily="Fraunces, Georgia, serif" fontSize="24" fontWeight="600" fill="#e4cf9f">
        537
      </text>
    </svg>
  );
}

export function Wordmark({ size = "sm" }: { size?: "sm" | "lg" }) {
  if (size === "lg") {
    return (
      <div className="text-center">
        <div className="text-[10px] font-medium uppercase tracking-[0.32em] text-slate-500">{BRAND.descriptor}</div>
        <div className="mt-2 font-display text-[2rem] font-medium tracking-[0.14em] text-slate-50">THE 537 FIRM</div>
        <div className="mx-auto my-3 h-px w-24 bg-gradient-to-r from-transparent via-gold-500/60 to-transparent" />
        <div className="text-sm tracking-wide text-slate-400">{BRAND.tagline}</div>
      </div>
    );
  }
  return (
    <span className="flex items-center gap-2.5">
      <Monogram size={30} />
      <span className="leading-none">
        <span className="block text-[13px] font-semibold tracking-[0.24em] text-slate-50">THE 537 FIRM</span>
        <span className="mt-1 block text-[9px] font-medium uppercase tracking-[0.26em] text-slate-500">Investment HQ</span>
      </span>
    </span>
  );
}
