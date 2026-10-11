import { Link } from "react-router-dom";
import { type FirmEvent } from "../api";
import { ago, when } from "../format";
import { AGENTS } from "./ui";

const DOT: Record<string, string> = { critical: "bg-rose-500", warning: "bg-amber-400", info: "bg-slate-500" };

export function Feed({ events, compact = false }: { events: FirmEvent[]; compact?: boolean }) {
  if (!events.length) return <p className="text-sm text-slate-500">Nothing yet. Every decision, order, and owner action appears here as it happens.</p>;
  return (
    <ol className="space-y-3">
      {events.map((e) => {
        const agent = e.manager_id ? AGENTS[e.manager_id] : null;
        return (
          <li key={e.id} className="flex gap-3">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${agent ? "" : DOT[e.severity]}`} style={agent ? { background: agent.color } : undefined} />
            <div className="min-w-0 flex-1">
              <p className={`text-sm ${e.severity === "critical" ? "text-rose-300" : "text-slate-200"}`}>{e.summary}</p>
              <p className="mt-0.5 text-xs text-slate-500" title={when(e.ts)}>
                {ago(e.ts)} · {e.type.replace(/_/g, " ").toLowerCase()}
                {!compact && e.actor && ` · ${e.actor}`}
                {agent && !compact && (
                  <>
                    {" · "}
                    <Link className="hover:text-slate-300" to={`/managers/${e.manager_id}`}>{agent.name}</Link>
                  </>
                )}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
