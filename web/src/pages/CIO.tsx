import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useLive } from "../App";
import { api, type CioMessage, type Conversation, type Memory, type Proposal, type Usage } from "../api";
import { AGENTS, Avatar, Card, ErrorLine, Field, Modal, useAction } from "../components/ui";
import { useResource } from "../hooks";
import { ago, money, when } from "../format";

const STARTERS = [
  "Why is Tim holding so much cash?",
  "How have our strategies performed compared with the S&P 500?",
  "Should we increase Alex's allocation to 40%?",
  "What do you think about investing more aggressively this month?",
  "Which manager deserves more capital based on performance?",
  "What changes would you recommend to our investment strategies?",
];

/** Conversation state and actions, shared by the CIO Office page and Graham's desk in The Firm. */
export function useCioChat(onChange: () => void) {
  const convs = useResource<{ conversations: Conversation[]; provider_ready: boolean }>("/api/cio/conversations", { every: 0 });
  const [active, setActive] = useState<number | null>(null);
  const [conv, setConv] = useState<Conversation | null>(null);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (active === null && convs.data?.conversations.length) setActive(convs.data.conversations[0].id);
  }, [convs.data, active]);
  useEffect(() => {
    if (active !== null) api.get<Conversation>(`/api/cio/conversations/${active}`).then(setConv).catch((e) => setError(String(e.message)));
  }, [active]);
  useEffect(() => {
    // Statement body: some browsers return a Promise from scrollIntoView, which React would call as a cleanup.
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [conv?.messages?.length, pending]);

  const send = async (text: string) => {
    const content = text.trim();
    if (!content || pending) return;
    setError(null);
    let id = active;
    if (id === null) {
      const c = await api.post<Conversation>("/api/cio/conversations", { title: "New conversation" });
      id = c.id;
      setActive(c.id);
      setConv({ ...c, messages: [] });
    }
    setDraft("");
    setPending(content);
    try {
      await api.post<CioMessage>(`/api/cio/conversations/${id}/messages`, { content });
      setConv(await api.get<Conversation>(`/api/cio/conversations/${id}`));
      convs.reload();
      onChange();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDraft(content);
    } finally {
      setPending(null);
    }
  };

  const newConversation = async () => {
    const c = await api.post<Conversation>("/api/cio/conversations", { title: "New conversation" });
    setActive(c.id);
    setConv({ ...c, messages: [] });
    convs.reload();
  };
  return { convs, active, setActive, conv, draft, setDraft, pending, error, bottom, send, newConversation };
}

export type CioChat = ReturnType<typeof useCioChat>;

/** Messages, starters, and the composer. */
export function ChatThread({ chat, proposalsById, onDecided, greeting = "Good to see you. What's on your mind?", starters = STARTERS }: { chat: CioChat; proposalsById: Record<number, Proposal>; onDecided: () => void; greeting?: string; starters?: string[] }) {
  const { convs, conv, pending, draft, setDraft, send, error, bottom } = chat;
  return (
    <>
      <div className="flex-1 space-y-5 overflow-y-auto p-5">
        {convs.data && !convs.data.provider_ready && (
          <p className="rounded-lg bg-amber-500/10 p-3 text-sm text-amber-200">Graham's AI provider is not configured on the Pi (no API key). Conversations will show an error until it is.</p>
        )}
        {!conv?.messages?.length && !pending && (
          <div className="py-4">
            <p className="font-display text-2xl text-slate-100">{greeting}</p>
            <p className="mt-2 text-sm text-slate-400">I read the firm's books, the managers' decisions, and the market before I answer, and I show you the records I used. I can propose changes; only you can approve them.</p>
            <div className="mt-5 flex flex-wrap gap-2">
              {starters.map((q) => (
                <button key={q} onClick={() => send(q)} className="rounded-full border border-ink-600 px-3 py-1.5 text-sm text-slate-300 hover:border-gold-500 hover:text-slate-100">{q}</button>
              ))}
            </div>
          </div>
        )}
        {conv?.messages?.map((m) => <Message key={m.id} m={m} proposals={proposalsById} onDecided={onDecided} />)}
        {pending && (
          <>
            <Bubble mine text={pending} />
            <div className="flex items-center gap-3 text-sm text-slate-400">
              <Avatar id="cio" size={28} working />
              Reviewing the books…
            </div>
          </>
        )}
        <div ref={bottom} />
      </div>
      <form
        className="border-t border-ink-800 p-4"
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
      >
        <textarea
          className="input min-h-[56px] resize-y"
          value={draft}
          placeholder="Ask Graham about strategy, performance, allocations, or the market…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send(draft);
            }
          }}
        />
        <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
          <span>Enter to send · Shift+Enter for a new line</span>
          <button className="btn-primary" disabled={!draft.trim() || !!pending}>Send</button>
        </div>
        <ErrorLine error={error} />
      </form>
    </>
  );
}

export default function CioOffice() {
  const { bump, reloadOverview } = useLive();
  const proposals = useResource<{ proposals: Proposal[] }>("/api/cio/proposals", { every: 30000, bump });
  const memory = useResource<{ memories: Memory[] }>("/api/cio/memory", { every: 0, bump });
  const usage = useResource<Usage>("/api/cio/usage", { every: 60000 });
  const refreshSide = () => {
    proposals.reload();
    memory.reload();
    usage.reload();
    reloadOverview();
  };
  const chat = useCioChat(refreshSide);
  const { convs, active, setActive, pending, newConversation } = chat;

  const byId = Object.fromEntries((proposals.data?.proposals ?? []).map((p) => [p.id, p]));
  const open = (proposals.data?.proposals ?? []).filter((p) => p.status === "PENDING");
  const cio = usage.data?.agents.cio;

  return (
    <div className="grid gap-6 xl:grid-cols-[230px_1fr_320px]">
      <aside className="space-y-3">
        <div className="flex items-center gap-3">
          <Avatar id="cio" size={44} working={!!pending} />
          <div>
            <div className="font-display text-lg text-slate-50">Graham</div>
            <div className="text-xs text-slate-400">{AGENTS.cio.title}</div>
          </div>
        </div>
        <button className="btn-primary w-full" onClick={newConversation}>New conversation</button>
        <nav className="space-y-1">
          {(convs.data?.conversations ?? []).map((c) => (
            <button key={c.id} onClick={() => setActive(c.id)} className={`block w-full truncate rounded-lg px-3 py-2 text-left text-sm ${c.id === active ? "bg-ink-800 text-slate-50" : "text-slate-400 hover:bg-ink-900"}`}>
              {c.title}
              <span className="block text-[11px] text-slate-600">{ago(c.updated_at)}</span>
            </button>
          ))}
        </nav>
      </aside>

      <section className="card flex min-h-[70vh] flex-col p-0">
        <ChatThread chat={chat} proposalsById={byId} onDecided={refreshSide} />
      </section>

      <aside className="space-y-6">
        <Card title={`Awaiting your decision${open.length ? ` (${open.length})` : ""}`}>
          {open.length ? <div className="space-y-3">{open.map((p) => <ProposalCard key={p.id} p={p} onDecided={refreshSide} />)}</div> : <p className="text-sm text-slate-500">No open proposals.</p>}
        </Card>
        <MemoryPanel memories={memory.data?.memories ?? []} onChange={memory.reload} />
        {cio && (
          <Card title="CIO budget">
            <div className="flex justify-between text-sm"><span className="text-slate-300">{money(cio.spent)} used</span><span className="text-slate-500">of {money(cio.cap)} · {usage.data?.month}</span></div>
            <div className="mt-2 h-2 rounded-full bg-ink-800"><div className="h-2 rounded-full bg-gold-500" style={{ width: `${Math.min(100, (cio.spent / Math.max(cio.cap, 0.0001)) * 100)}%` }} /></div>
            <p className="mt-2 text-xs text-slate-500">Set in <Link className="text-gold-400" to="/system">System</Link>.</p>
          </Card>
        )}
      </aside>
    </div>
  );
}

function Bubble({ text, mine = false }: { text: string; mine?: boolean }) {
  return (
    <div className={`flex ${mine ? "justify-end" : ""}`}>
      <div className={`max-w-[85%] whitespace-pre-wrap rounded-2xl px-4 py-3 text-sm leading-relaxed ${mine ? "bg-gold-500/15 text-slate-100" : "bg-ink-850 text-slate-200"}`}>{text}</div>
    </div>
  );
}

function Message({ m, proposals, onDecided }: { m: CioMessage; proposals: Record<number, Proposal>; onDecided: () => void }) {
  if (m.role === "user") return <Bubble mine text={m.content} />;
  if (m.role === "notice") return <p className="rounded-lg bg-amber-500/10 p-3 text-sm text-amber-200">{m.content}</p>;
  return (
    <div className="flex gap-3">
      <Avatar id="cio" size={32} />
      <div className="min-w-0 flex-1 space-y-2">
        <div className="whitespace-pre-wrap rounded-2xl bg-ink-850 px-4 py-3 text-sm leading-relaxed text-slate-200">{m.content}</div>
        {m.flags.map((f) => <p key={f} className="text-xs text-amber-300">⚠ {f}</p>)}
        {m.citations.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {m.citations.map((c) => (
              <Link key={`${c.type}-${c.id}`} to={c.type === "proposal" || c.type === "memory" ? "/cio" : "/activity"} title={c.label} className="max-w-[260px] truncate rounded-md border border-ink-700 px-2 py-0.5 text-[11px] text-slate-400 hover:text-slate-200">
                {c.type} #{c.id}{c.label ? ` · ${c.label}` : ""}
              </Link>
            ))}
          </div>
        )}
        {m.tools.length > 0 && <p className="text-[11px] text-slate-600">Checked: {Array.from(new Set(m.tools.map((t) => t.tool.replace(/^get_/, "").replace(/_/g, " ")))).join(", ")} · {when(m.created_at)}</p>}
        {m.proposals.map((id) => proposals[id] && <ProposalCard key={id} p={proposals[id]} onDecided={onDecided} />)}
      </div>
    </div>
  );
}

function describe(p: Proposal): string[] {
  const x = p.payload;
  if (p.kind === "reallocate") return x.moves.map((mv: any) => `Move ${money(Number(mv.amount))} from ${mv.from === "firm" ? "unallocated" : AGENTS[mv.from]?.name ?? mv.from} to ${mv.to === "firm" ? "unallocated" : AGENTS[mv.to]?.name ?? mv.to}`);
  if (p.kind === "mandate_change") return Object.entries(x.diff as Record<string, { from: any; to: any }>).map(([k, v]) => `${AGENTS[x.manager_id]?.name}: ${k} ${JSON.stringify(v.from)} → ${JSON.stringify(v.to)}`);
  if (p.kind === "mode_change") return [`${AGENTS[x.manager_id]?.name}: ${x.from} → ${x.mode}`];
  return [`Deposit split → ${Object.entries(x.split as Record<string, number>).map(([k, v]) => `${AGENTS[k]?.name ?? k} ${Math.round(v * 100)}%`).join(", ") || "all unallocated"}`];
}

export function ProposalCard({ p, onDecided }: { p: Proposal; onDecided: () => void }) {
  const [deciding, setDeciding] = useState<"approve" | "reject" | null>(null);
  const [note, setNote] = useState("");
  const act = useAction(() => api.post(`/api/cio/proposals/${p.id}/${deciding}`, { confirm: deciding === "approve", note }), () => {
    setDeciding(null);
    onDecided();
  });
  const tone = { PENDING: "border-gold-500/40", APPLIED: "border-emerald-500/40", REJECTED: "border-ink-700", EXPIRED: "border-ink-700", FAILED: "border-rose-500/40" }[p.status];
  return (
    <div className={`rounded-xl border ${tone} bg-ink-900 p-3`}>
      <div className="flex items-start justify-between gap-2">
        <div className="text-sm font-medium text-slate-100">#{p.id} {p.title}</div>
        <span className="text-[11px] text-slate-500">{p.status}</span>
      </div>
      <ul className="mt-1 list-disc pl-4 text-xs text-slate-300">{describe(p).map((d) => <li key={d}>{d}</li>)}</ul>
      <p className="mt-2 text-xs text-slate-400">{p.rationale}</p>
      {p.status === "PENDING" ? (
        <div className="mt-3 flex gap-2">
          <button className="btn-primary px-2.5 py-1 text-xs" onClick={() => setDeciding("approve")}>Approve</button>
          <button className="btn-ghost px-2.5 py-1 text-xs" onClick={() => setDeciding("reject")}>Decline</button>
        </div>
      ) : (
        <p className="mt-2 text-[11px] text-slate-500">{p.decided_by && `${p.status.toLowerCase()} by ${p.decided_by} ${ago(p.decided_at)}`}{p.result?.error && ` · ${p.result.error}`}</p>
      )}
      <Modal open={!!deciding} title={deciding === "approve" ? `Approve #${p.id}?` : `Decline #${p.id}?`} onClose={() => setDeciding(null)}>
        {deciding === "approve" && <ul className="mb-3 list-disc pl-4 text-sm text-slate-200">{describe(p).map((d) => <li key={d}>{d}</li>)}</ul>}
        <Field label="Note (optional)"><input className="input" value={note} onChange={(e) => setNote(e.target.value)} /></Field>
        <div className="mt-4 flex justify-end gap-2">
          <button className="btn-ghost" onClick={() => setDeciding(null)}>Cancel</button>
          <button className={deciding === "approve" ? "btn-primary" : "btn-ghost"} disabled={act.pending} onClick={() => act.run()}>{deciding === "approve" ? "Approve and apply" : "Decline"}</button>
        </div>
        <ErrorLine error={act.error} />
      </Modal>
    </div>
  );
}

export function MemoryPanel({ memories, onChange }: { memories: Memory[]; onChange: () => void }) {
  const [text, setText] = useState("");
  const add = useAction(() => api.post("/api/cio/memory", { kind: "preference", text }), () => {
    setText("");
    onChange();
  });
  const remove = useAction((id: number) => api.put(`/api/cio/memory/${id}`, { active: false }), onChange);
  const live = memories.filter((m) => m.active);
  return (
    <Card title="What Graham remembers">
      {live.length ? (
        <ul className="max-h-72 space-y-2 overflow-y-auto pr-1 text-sm">
          {live.map((m) => (
            <li key={m.id} className="group flex gap-2">
              <span className="mt-0.5 shrink-0 rounded bg-ink-800 px-1.5 text-[10px] uppercase text-slate-500">{m.kind}</span>
              <span className="flex-1 text-slate-300">{m.text}</span>
              <button className="text-xs text-slate-600 opacity-0 hover:text-rose-300 group-hover:opacity-100" onClick={() => remove.run(m.id)} title="Forget">✕</button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-slate-500">Nothing yet. Tell Graham your goals and preferences; they will be noted here, and you can edit or remove any of them.</p>
      )}
      <div className="mt-3 flex gap-2">
        <input className="input" placeholder="Add a note for Graham" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="btn-ghost" disabled={!text.trim()} onClick={() => add.run()}>Add</button>
      </div>
    </Card>
  );
}
