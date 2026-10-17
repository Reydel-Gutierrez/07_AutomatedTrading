import { type ReactNode, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Portrait } from "../space/Character";

export const AGENTS: Record<string, { name: string; title: string; color: string; initial: string; blurb: string }> = {
  cio: { name: "Graham", title: "Chief Investment Officer", color: "#dcc287", initial: "G", blurb: "Oversees the firm, allocates capital, and advises you." },
  tim: { name: "Tim", title: "Core Investment Manager", color: "#4f9cf9", initial: "T", blurb: "Diversified long-term ETFs, contributions, rebalancing." },
  alex: { name: "Alex", title: "Strategic Growth Manager", color: "#a78bfa", initial: "A", blurb: "Systematic stock selection with AI research." },
  max: { name: "Max", title: "Short-Term Trading Manager", color: "#f59e0b", initial: "M", blurb: "Swing trading with defined entries, exits, and stops." },
};

export function Card({ title, action, children, className = "" }: { title?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {(title || action) && (
        <header className="mb-4 flex items-center justify-between gap-3">
          {title && <h2 className="label">{title}</h2>}
          {action}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub, className = "" }: { label: string; value: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={className}>
      <div className="label">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-50">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-400">{sub}</div>}
    </div>
  );
}

const MODE_STYLE: Record<string, string> = {
  LIVE: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  SIM: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  PAUSED: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  INACTIVE: "bg-slate-500/10 text-slate-400 ring-slate-500/20",
};

export function Badge({ children, tone = "INACTIVE" }: { children: ReactNode; tone?: string }) {
  return <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${MODE_STYLE[tone] ?? MODE_STYLE.INACTIVE}`}>{children}</span>;
}

export function Avatar({ id, size = 48, working = false }: { id: string; size?: number; working?: boolean }) {
  const a = AGENTS[id] ?? AGENTS.cio;
  return (
    <div
      className={`grid shrink-0 place-items-end overflow-hidden rounded-2xl ${working ? "agent-working" : ""}`}
      style={{ width: size, height: size, background: `linear-gradient(160deg, ${a.color}, ${a.color}55)`, ["--ring" as string]: `${a.color}66` }}
      aria-label={a.name}
    >
      <Portrait id={id in AGENTS ? id : "cio"} size={size * 0.92} />
    </div>
  );
}

export function Modal({ open, title, children, onClose }: { open: boolean; title: string; children: ReactNode; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  // On the page body, so a panel with a backdrop blur or scrolling never clips the dialog.
  return createPortal(
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={onClose}>
      <div className="card w-full max-w-md" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <h3 className="mb-4 font-display text-lg text-slate-50">{title}</h3>
        {children}
      </div>
    </div>,
    document.body,
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      <div className="mt-1">{children}</div>
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-xl border border-dashed border-ink-700 p-6 text-center text-sm text-slate-500">{children}</div>;
}

export function ErrorLine({ error }: { error: string | null }) {
  return error ? <p className="mt-2 text-sm text-rose-400">{error}</p> : null;
}

/** Async action with pending/error state. */
export function useAction<A extends unknown[]>(fn: (...args: A) => Promise<unknown>, onDone?: () => void) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (...args: A) => {
    setPending(true);
    setError(null);
    try {
      await fn(...args);
      onDone?.();
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setPending(false);
    }
  };
  return { run, pending, error, setError };
}
