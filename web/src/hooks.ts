import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type FirmEvent } from "./api";

/** Fetch a resource, refetch on an interval and whenever `bump` changes. */
export function useResource<T>(path: string | null, { every = 30000, bump = 0 }: { every?: number; bump?: number } = {}) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    if (!path) return;
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    reload();
    if (!every) return;
    const t = window.setInterval(reload, every);
    return () => window.clearInterval(t);
  }, [reload, every, bump]);
  return { data, error, loading, reload };
}

/** Live firm events over SSE. Calls onEvent for each new event; reconnects automatically. */
export function useEventStream(onEvent: (e: FirmEvent) => void) {
  const cb = useRef(onEvent);
  cb.current = onEvent;
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let source: EventSource | null = null;
    let closed = false;
    let lastId = 0;
    let retry: number | undefined;
    const open = () => {
      source = new EventSource(`/api/stream${lastId ? `?after=${lastId}` : ""}`);
      source.addEventListener("hello", () => setConnected(true));
      source.addEventListener("firm", (msg) => {
        const e = JSON.parse((msg as MessageEvent).data) as FirmEvent;
        lastId = Math.max(lastId, e.id);
        cb.current(e);
      });
      source.onerror = () => {
        setConnected(false);
        source?.close();
        if (!closed) retry = window.setTimeout(open, 5000);
      };
    };
    open();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      source?.close();
    };
  }, []);
  return connected;
}

/** False while the browser tab is hidden, so pages can stop polling. */
export function usePageVisible() {
  const [visible, setVisible] = useState(() => document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}
