"""The CIO conversation engine: context, tool loop, budget, verification, persistence."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from firm.ai.budget import Budget, BudgetExceeded
from firm.ai.providers import Provider, ProviderError, ToolCall
from firm.cio.memory import Memory
from firm.cio.prompt import INSTRUCTIONS, SUMMARY_INSTRUCTIONS
from firm.cio.proposals import Proposals
from firm.cio.tools import SPECS, CIOTools
from firm.clock import et, iso, is_regular_hours
from firm.core import Firm

AGENT = "cio"
MAX_STEPS = 8
RECENT_MESSAGES = 16
SUMMARIZE_AFTER = 40
ACTIVITY_CLAIM = re.compile(r"\b(tim|alex|max)\b[^.]{0,80}\b(bought|sold|buys|sells|rebalanc\w*|invested|placed|filled|traded|purchased)\b", re.I)


class CIO:
    def __init__(
        self,
        firm: Firm,
        *,
        provider: Provider,
        budget: Budget,
        health: Callable[[], dict[str, Any]],
        model: str = "claude-opus-5-5",
        summary_model: str = "claude-haiku-5-5",
        max_output_tokens: int = 16000,
        owner: str = "Reydel",
    ) -> None:
        self.firm = firm
        self.provider = provider
        self.budget = budget
        self.health = health
        self.model = model
        self.summary_model = summary_model
        self.max_output_tokens = max_output_tokens
        self.owner = owner
        self.proposals = Proposals(firm)
        self.memory = Memory(firm.db, firm.events, clock=firm.clock)

    # -- conversations -----------------------------------------------------------
    def create_conversation(self, title: str = "New conversation") -> dict[str, Any]:
        now = iso(self.firm.clock())
        with self.firm.db.tx() as conn:
            cid = int(conn.execute("INSERT INTO cio_conversations (title, created_at, updated_at) VALUES (?,?,?)", (title[:120], now, now)).lastrowid)
        return self.conversation(cid)

    def conversations(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.firm.db.query("SELECT id, title, created_at, updated_at FROM cio_conversations ORDER BY updated_at DESC LIMIT 100")]

    def conversation(self, cid: int) -> dict[str, Any]:
        row = self.firm.db.one("SELECT * FROM cio_conversations WHERE id = ?", (cid,))
        if row is None:
            raise KeyError(cid)
        msgs = [_msg(r) for r in self.firm.db.query("SELECT * FROM cio_messages WHERE conversation_id = ? ORDER BY id", (cid,))]
        return {**dict(row), "messages": msgs}

    def _save(self, cid: int, role: str, content: str, **extra: Any) -> dict[str, Any]:
        now = iso(self.firm.clock())
        with self.firm.db.tx() as conn:
            mid = conn.execute(
                "INSERT INTO cio_messages (conversation_id, role, content, citations_json, tools_json, proposals_json, flags_json, model, cost, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    cid, role, content,
                    json.dumps(extra.get("citations", [])), json.dumps(extra.get("tools", [])),
                    json.dumps(extra.get("proposals", [])), json.dumps(extra.get("flags", [])),
                    extra.get("model"), extra.get("cost"), now,
                ),
            ).lastrowid
            conn.execute("UPDATE cio_conversations SET updated_at = ? WHERE id = ?", (now, cid))
        return _msg(self.firm.db.one("SELECT * FROM cio_messages WHERE id = ?", (mid,)))

    # -- a turn ------------------------------------------------------------------
    def ask(self, cid: int, text: str) -> dict[str, Any]:
        text = text.strip()
        if not text:
            raise ValueError("empty message")
        conv = self.conversation(cid)
        if conv["title"] == "New conversation":
            with self.firm.db.tx() as conn:
                conn.execute("UPDATE cio_conversations SET title = ? WHERE id = ?", (text[:80], cid))
        self._save(cid, "user", text)
        if not self.provider.available():
            return self._save(cid, "notice", "The CIO's AI provider isn't configured on the Pi yet (no API key). Nothing was sent anywhere.", flags=["provider_not_configured"])
        tools = CIOTools(self.firm, self.proposals, self.memory, health=self.health, conversation_id=cid)
        instructions = self._instructions(tools, conv)
        history = [{"role": m["role"], "content": m["content"]} for m in self.conversation(cid)["messages"] if m["role"] in ("user", "assistant")][-RECENT_MESSAGES:]
        trace: list[dict[str, Any]] = []
        spent = 0.0
        state, results, last_text, model = None, None, "", self.model
        try:
            for _ in range(MAX_STEPS):
                est = (len(instructions) + sum(len(h["content"]) for h in history) + sum(len(r[1]) for r in (results or []))) // 3 + 500
                self.budget.authorize(AGENT, self.model, input_tokens_estimate=est, max_output_tokens=self.max_output_tokens)
                try:
                    step = self.provider.step(
                        model=self.model, instructions=instructions, messages=history if state is None else None,
                        tool_results=results, tools=SPECS, state=state, max_output_tokens=self.max_output_tokens,
                    )
                except ProviderError as exc:
                    self.budget.record(AGENT, provider=self.provider.name, model=self.model, purpose="conversation", input_tokens=0, output_tokens=0, ok=False, error=str(exc)[:300])
                    raise
                model = step.model or self.model
                spent += float(self.budget.record(
                    AGENT, provider=self.provider.name, model=model, purpose="conversation",
                    input_tokens=step.input_tokens, output_tokens=step.output_tokens,
                    cache_read_tokens=step.cache_read_tokens, cache_write_tokens=step.cache_write_tokens, ok=True,
                ))
                if not step.tool_calls:
                    last_text = step.text
                    break
                results = []
                for call in step.tool_calls:
                    out = tools.call(call.name, call.arguments)
                    results.append((call, out))
                    if call.name != "respond":
                        trace.append({"tool": call.name, "args": _brief(call)})
                if tools.final is not None:
                    break
                state = step.state
        except BudgetExceeded as exc:
            return self._save(cid, "notice", f"The CIO is paused for the rest of the month: {exc} You can raise the AI budget in System.", flags=["budget_exhausted"])
        except ProviderError as exc:
            if exc.account_problem:
                self.firm.events.emit("AI_PROVIDER_UNAVAILABLE", f"The CIO's AI provider rejected the request ({exc.account_problem}). Check the API key or credits.", actor="cio", severity="critical", payload={"problem": exc.account_problem})
            plain = {
                "billing": "The CIO's AI account is out of credits. Add credits with the provider (OpenAI: platform.openai.com → Billing); nothing else needs to change.",
                "auth": "The CIO's AI provider rejected the API key. Check the key in /etc/agentic-portfolio/env on the Pi.",
            }.get(exc.account_problem or "", f"The CIO couldn't reach its AI provider right now ({str(exc)[:160]}). Please try again shortly.")
            return self._save(cid, "notice", plain, flags=["provider_error"], tools=trace)

        flags: list[str] = []
        if tools.final is not None:
            message = tools.final["message"].strip()
            citations, rejected = tools.verify_citations(tools.final["citations"])
            if rejected:
                flags.append(f"removed {len(rejected)} citation(s) that do not exist in the firm's records")
        else:
            message, citations = (last_text or "I couldn't complete that answer. Please ask again.").strip(), []
            flags.append("answered without citing sources")
        cited = {c["type"] for c in citations}
        if ACTIVITY_CLAIM.search(message) and not cited & {"event", "order"}:
            flags.append("describes manager activity without citing an event or order; verify in Activity")
        reply = self._save(
            cid, "assistant", message,
            citations=citations, tools=trace, proposals=tools.created_proposals, flags=flags,
            model=model, cost=f"{spent:.5f}",
        )
        self._maybe_summarize(cid)
        return reply

    def _instructions(self, tools: CIOTools, conv: dict[str, Any]) -> str:
        now = self.firm.clock()
        base = INSTRUCTIONS.format(owner=self.owner, today=et(now).strftime("%A %B %d, %Y %I:%M %p"), market="open" if is_regular_hours(now) else "closed")
        snapshot = tools.t_get_firm_overview()
        memories = self.memory.active()
        parts = [base, "## Firm snapshot (from the ledger and broker, as of now)\n" + json.dumps(snapshot, default=str)]
        if memories:
            parts.append("## What you remember (memory ids are citable)\n" + "\n".join(f"- [memory {m['id']}] ({m['kind']}) {m['text']}" for m in reversed(memories)))
        if conv.get("summary"):
            parts.append("## Earlier in this conversation\n" + conv["summary"])
        pending = self.proposals.list("PENDING", limit=10)
        if pending:
            parts.append("## Proposals awaiting the owner\n" + "\n".join(f"- #{p['id']} {p['kind']}: {p['title']}" for p in pending))
        return "\n\n".join(parts)

    def _maybe_summarize(self, cid: int) -> None:
        conv = self.conversation(cid)
        msgs = [m for m in conv["messages"] if m["role"] in ("user", "assistant")]
        older = [m for m in msgs[:-RECENT_MESSAGES] if m["id"] > conv["summarized_through"]]
        if len(msgs) < SUMMARIZE_AFTER or len(older) < 10:
            return
        text = (conv.get("summary") or "") + "\n\n" + "\n".join(f"{m['role'].upper()}: {m['content']}" for m in older)
        try:
            self.budget.authorize(AGENT, self.summary_model, input_tokens_estimate=len(text) // 3, max_output_tokens=4000)
            step = self.provider.step(model=self.summary_model, instructions=SUMMARY_INSTRUCTIONS, messages=[{"role": "user", "content": text[-30000:]}], tool_results=None, tools=[], state=None, max_output_tokens=4000)
            self.budget.record(
                AGENT, provider=self.provider.name, model=step.model or self.summary_model, purpose="summary",
                input_tokens=step.input_tokens, output_tokens=step.output_tokens,
                cache_read_tokens=step.cache_read_tokens, cache_write_tokens=step.cache_write_tokens, ok=True,
            )
        except (BudgetExceeded, ProviderError):
            return
        if step.text:
            with self.firm.db.tx() as conn:
                conn.execute("UPDATE cio_conversations SET summary = ?, summarized_through = ? WHERE id = ?", (step.text.strip(), older[-1]["id"], cid))


def _brief(call: ToolCall) -> str:
    args = {k: v for k, v in call.arguments.items() if k not in ("rationale",)}
    text = json.dumps(args, default=str)
    return text if len(text) < 160 else text[:157] + "…"


def _msg(row: Any) -> dict[str, Any]:
    d = dict(row)
    for k in ("citations_json", "tools_json", "proposals_json", "flags_json"):
        d[k.removesuffix("_json")] = json.loads(d.pop(k) or "[]")
    return d
