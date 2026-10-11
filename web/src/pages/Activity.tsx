import { useEffect, useState } from "react";
import { useLive } from "../App";
import { api, type FirmEvent } from "../api";
import { Feed } from "../components/Feed";
import { Card } from "../components/ui";

const FILTERS: [string, string][] = [["", "Everything"], ["tim", "Tim"], ["alex", "Alex"], ["max", "Max"]];

export default function Activity() {
  const { bump } = useLive();
  const [manager, setManager] = useState("");
  const [events, setEvents] = useState<FirmEvent[]>([]);
  const [more, setMore] = useState(true);
  useEffect(() => {
    api.get<{ events: FirmEvent[] }>(`/api/events?limit=100${manager ? `&manager=${manager}` : ""}`).then((r) => {
      setEvents(r.events);
      setMore(r.events.length === 100);
    });
  }, [manager, bump]);
  const loadMore = () => {
    const last = events[events.length - 1];
    if (!last) return;
    api.get<{ events: FirmEvent[] }>(`/api/events?limit=100&before_id=${last.id}${manager ? `&manager=${manager}` : ""}`).then((r) => {
      setEvents([...events, ...r.events]);
      setMore(r.events.length === 100);
    });
  };
  return (
    <Card
      title="Activity and audit trail"
      action={
        <div className="flex gap-1 text-xs">
          {FILTERS.map(([id, label]) => (
            <button key={id} onClick={() => setManager(id)} className={`rounded-md px-2.5 py-1 ${manager === id ? "bg-ink-700 text-slate-50" : "text-slate-400"}`}>{label}</button>
          ))}
        </div>
      }
    >
      <Feed events={events} />
      {more && <button className="btn-ghost mt-4" onClick={loadMore}>Older</button>}
    </Card>
  );
}
