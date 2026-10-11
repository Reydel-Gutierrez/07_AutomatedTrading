"""Investment HQ API. Admin-only; localhost-bound; Cloudflare Access in front for remote use."""

from __future__ import annotations

import asyncio
import json
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from firm.api import views
from firm.api.auth import CSRF_COOKIE, SESSION_COOKIE, SESSION_TTL, Sessions
from firm.core import Firm
from firm.ledger import LedgerError
from firm.managers.registry import MODES
from firm.managers.tim import validate_mandate
from firm.money import D, cents
from firm.paths import project_root

WEB_DIST = project_root() / "web" / "dist"
STATEFUL = {"POST", "PUT", "PATCH", "DELETE"}


class Host(Protocol):
    firm: Firm

    def health(self) -> dict[str, Any]: ...
    def jobs(self) -> dict[str, Any]: ...


class LoginBody(BaseModel):
    username: str
    password: str


class CapitalBody(BaseModel):
    manager: str
    amount: str | None = None
    percent: float | None = Field(default=None, ge=0, le=100)
    note: str = ""


class ModeBody(BaseModel):
    mode: str
    note: str = ""
    confirm: bool = False


class MandateBody(BaseModel):
    params: dict[str, Any]
    note: str


class SwitchBody(BaseModel):
    action: str  # engage | release
    note: str = ""


class SplitBody(BaseModel):
    split: dict[str, float]


class BudgetBody(BaseModel):
    monthly_usd: float = Field(ge=0, le=500)
    per_agent: dict[str, float] | None = None


class SuspenseBody(BaseModel):
    as_kind: str
    manager: str | None = None
    note: str = ""


class NoteBody(BaseModel):
    note: str


class ChatBody(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class TitleBody(BaseModel):
    title: str = "New conversation"


class DecisionBody(BaseModel):
    confirm: bool = False
    note: str = ""


class MemoryBody(BaseModel):
    kind: str = "fact"
    text: str | None = None
    active: bool | None = None


def create_app(host: Host) -> FastAPI:
    firm = host.firm
    sessions = Sessions(firm.db, firm.settings)
    app = FastAPI(title="Investment HQ", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def csrf_and_headers(request: Request, call_next):
        if request.method in STATEFUL and request.url.path.startswith("/api/") and request.url.path != "/api/login":
            cookie = request.cookies.get(CSRF_COOKIE)
            header = request.headers.get("x-csrf-token")
            if not cookie or not header or not secrets.compare_digest(cookie, header):
                return JSONResponse({"detail": "csrf"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def user(request: Request) -> str:
        name = sessions.user(request.cookies.get(SESSION_COOKIE))
        if not name:
            raise HTTPException(401, "login required")
        return name

    def secure(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    # -- public --------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        h = host.health()
        return {k: h.get(k) for k in ("agent", "heartbeat", "broker_connected", "books_open", "kill_switch", "reconcile_hold", "live_writes_enabled", "managers", "deployed", "market", "open_live_orders")}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
        if not sessions.has_admin():
            raise HTTPException(409, "no admin account yet: run scripts/firm_admin.py set-password on the Pi")
        token = sessions.login(body.username, body.password, request.client.host if request.client else "?")
        if not token:
            raise HTTPException(401, "invalid credentials")
        csrf = secrets.token_urlsafe(24)
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="strict", secure=secure(request))
        response.set_cookie(CSRF_COOKIE, csrf, max_age=SESSION_TTL, httponly=False, samesite="strict", secure=secure(request))
        firm.events.emit("OWNER_LOGIN", f"{body.username} signed in to HQ.", actor=body.username)
        return {"user": body.username, "csrf": csrf}

    @app.post("/api/logout")
    def logout(response: Response) -> dict[str, Any]:
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(CSRF_COOKIE)
        return {"ok": True}

    @app.get("/api/session")
    def session(request: Request) -> dict[str, Any]:
        name = sessions.user(request.cookies.get(SESSION_COOKIE))
        return {"user": name, "has_admin": sessions.has_admin()}

    # -- reads -------------------------------------------------------------
    @app.get("/api/firm")
    def firm_overview(_: str = Depends(user)) -> dict[str, Any]:
        return views.overview(firm, host.health())

    @app.get("/api/managers/{manager_id}")
    def manager(manager_id: str, _: str = Depends(user)) -> dict[str, Any]:
        try:
            return views.manager_detail(firm, manager_id)
        except KeyError:
            raise HTTPException(404, "unknown manager") from None

    @app.get("/api/capital")
    def capital(_: str = Depends(user)) -> dict[str, Any]:
        return views.capital(firm)

    @app.get("/api/system")
    def system(_: str = Depends(user)) -> dict[str, Any]:
        return views.system(firm, host.health(), host.jobs())

    @app.get("/api/events")
    def events(before_id: int | None = None, manager: str | None = None, type: str | None = None, limit: int = 100, _: str = Depends(user)) -> dict[str, Any]:
        rows = firm.events.recent(before_id=before_id, manager_id=manager, types=[type] if type else None, limit=min(limit, 500))
        return {"events": [e.as_dict() for e in rows]}

    @app.get("/api/stream")
    async def stream(request: Request, after: int = 0, _: str = Depends(user)):
        async def gen():
            last = after or (firm.events.recent(limit=1)[0].id if firm.events.recent(limit=1) else 0)
            yield f"event: hello\ndata: {json.dumps({'after': last})}\n\n"
            while not await request.is_disconnected():
                rows = firm.events.recent(after_id=last, limit=100)
                for e in rows:
                    last = max(last, e.id)
                    yield f"id: {e.id}\nevent: firm\ndata: {json.dumps(e.as_dict(), default=str)}\n\n"
                if not rows:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- owner actions -----------------------------------------------------------
    def _amount(manager_id: str, body: CapitalBody, *, base: Decimal) -> Decimal:
        if body.amount is not None:
            try:
                return cents(D(body.amount))
            except (ValueError, InvalidOperation):
                raise HTTPException(422, "amount must be a number") from None
        if body.percent is not None:
            return cents(base * D(body.percent) / 100)
        raise HTTPException(422, "give an amount or a percent")

    @app.post("/api/capital/allocate")
    def allocate(body: CapitalBody, who: str = Depends(user)) -> dict[str, Any]:
        firm.registry.get(body.manager)
        try:
            total = sum(firm.firm_nav("LIVE").values(), D(0))  # percent of the whole firm's value
        except Exception:  # noqa: BLE001 — no prices: fall back to cash capital
            total = sum(firm.ledger.balances("LIVE").values(), D(0))
        amount = _amount(body.manager, body, base=total)
        try:
            firm.allocate(body.manager, amount, by=who, note=body.note)
        except LedgerError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True, "amount": str(amount)}

    @app.post("/api/capital/deallocate")
    def deallocate(body: CapitalBody, who: str = Depends(user)) -> dict[str, Any]:
        amount = _amount(body.manager, body, base=firm.ledger.free_cash("LIVE", body.manager))
        try:
            firm.deallocate(body.manager, amount, by=who, note=body.note)
        except LedgerError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True, "amount": str(amount)}

    @app.put("/api/settings/deposit_split")
    def deposit_split(body: SplitBody, who: str = Depends(user)) -> dict[str, Any]:
        known = {m.id for m in firm.registry.all()}
        if set(body.split) - known or any(v < 0 for v in body.split.values()) or sum(body.split.values()) > 1.0001:
            raise HTTPException(422, "split must name managers, be non-negative, and total at most 100%")
        firm.settings.put("deposit_split", body.split, by=who)
        firm.events.emit("SETTINGS_CHANGED", f"Deposit split set to {', '.join(f'{k} {v:.0%}' for k, v in body.split.items()) or 'all unallocated'}.", actor=who)
        return {"ok": True}

    @app.put("/api/settings/ai_budget")
    def ai_budget(body: BudgetBody, who: str = Depends(user)) -> dict[str, Any]:
        current = firm.settings.get("ai_budget")
        per_agent = body.per_agent or current.get("per_agent")
        if per_agent and abs(sum(per_agent.values()) - 1) > 0.001:
            raise HTTPException(422, "per-agent shares must total 100%")
        firm.settings.put("ai_budget", {"monthly_usd": body.monthly_usd, "per_agent": per_agent}, by=who)
        firm.events.emit("SETTINGS_CHANGED", f"AI budget set to ${body.monthly_usd:.2f}/month.", actor=who)
        return {"ok": True}

    @app.post("/api/managers/{manager_id}/mode")
    def set_mode(manager_id: str, body: ModeBody, who: str = Depends(user)) -> dict[str, Any]:
        if body.mode not in MODES:
            raise HTTPException(422, f"mode must be one of {MODES}")
        if body.mode == "LIVE":
            if not body.confirm:
                raise HTTPException(422, "switching to LIVE requires confirm=true")
            if manager_id not in firm.managers:
                raise HTTPException(409, "this manager's strategy is not built yet")
        if body.mode in ("SIM", "LIVE") and manager_id not in firm.managers:
            raise HTTPException(409, "this manager's strategy is not built yet")
        firm.registry.set_mode(manager_id, body.mode, by=who, note=body.note)
        if body.mode in ("SIM", "LIVE"):
            firm.request_run(manager_id, "mode_changed")
        return {"ok": True}

    @app.post("/api/managers/{manager_id}/mandate")
    def set_mandate(manager_id: str, body: MandateBody, who: str = Depends(user)) -> dict[str, Any]:
        if not body.note.strip():
            raise HTTPException(422, "a note explaining the change is required")
        if manager_id == "tim":
            problems = validate_mandate(body.params)
            if problems:
                raise HTTPException(422, "; ".join(problems))
        version = firm.registry.update_mandate(manager_id, body.params, by=who, note=body.note)
        firm.request_run(manager_id, "mandate_changed")
        return {"ok": True, "version": version}

    @app.post("/api/managers/{manager_id}/risk/resume")
    def resume_sleeve(manager_id: str, body: NoteBody, who: str = Depends(user)) -> dict[str, Any]:
        if not body.note.strip():
            raise HTTPException(422, "a note is required")
        pauses = firm.settings.get("sleeve_pauses") or {}
        if not (pauses.get(manager_id) or {}).get("engaged"):
            raise HTTPException(409, "this manager is not paused")
        pauses[manager_id] = {"engaged": False, "resumed_by": who, "note": body.note}
        firm.settings.put("sleeve_pauses", pauses, by=who)
        firm.events.emit("SLEEVE_RESUMED", f"{firm.registry.get(manager_id).display_name}'s buying resumed by {who}: {body.note}", actor=who, manager_id=manager_id)
        return {"ok": True}

    @app.post("/api/managers/{manager_id}/run")
    def run_now(manager_id: str, who: str = Depends(user)) -> dict[str, Any]:
        firm.registry.get(manager_id)
        firm.request_run(manager_id, f"manual ({who})")
        return {"ok": True}

    @app.post("/api/kill-switch")
    def kill_switch(body: SwitchBody, who: str = Depends(user)) -> dict[str, Any]:
        if body.action == "engage":
            firm.controls.engage(body.note or "engaged from HQ", by=who)
        elif body.action == "release":
            if firm.controls.kill_switch().source == "env":
                raise HTTPException(409, "the kill switch is forced on by AGENTIC_KILL_SWITCH in the Pi env file")
            try:
                firm.controls.release(by=who, note=body.note)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        else:
            raise HTTPException(422, "action must be engage or release")
        return {"ok": True}

    @app.post("/api/brake/resume")
    def resume_brake(body: NoteBody, who: str = Depends(user)) -> dict[str, Any]:
        firm.controls.resume_brake(by=who, note=body.note)
        return {"ok": True}

    @app.post("/api/suspense/resolve")
    def resolve(body: SuspenseBody, who: str = Depends(user)) -> dict[str, Any]:
        try:
            firm.reconciler.resolve_suspense_cash(as_kind=body.as_kind, manager=body.manager, by=who, note=body.note)
        except LedgerError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"ok": True}

    @app.post("/api/foreign-orders/{broker_order_id}/acknowledge")
    def ack_foreign(broker_order_id: str, body: NoteBody, who: str = Depends(user)) -> dict[str, Any]:
        firm.reconciler.acknowledge_foreign_order(broker_order_id, by=who, note=body.note)
        return {"ok": True}

    # -- CIO office ----------------------------------------------------------
    def cio():
        office = getattr(host, "cio", None)
        if office is None:
            raise HTTPException(503, "the CIO office is not configured")
        return office

    @app.get("/api/cio/conversations")
    def cio_conversations(_: str = Depends(user)) -> dict[str, Any]:
        return {"conversations": cio().conversations(), "provider_ready": cio().provider.available()}

    @app.post("/api/cio/conversations")
    def cio_new(body: TitleBody, _: str = Depends(user)) -> dict[str, Any]:
        return cio().create_conversation(body.title)

    @app.get("/api/cio/conversations/{cid}")
    def cio_conversation(cid: int, _: str = Depends(user)) -> dict[str, Any]:
        try:
            return cio().conversation(cid)
        except KeyError:
            raise HTTPException(404, "no such conversation") from None

    @app.post("/api/cio/conversations/{cid}/messages")
    def cio_ask(cid: int, body: ChatBody, _: str = Depends(user)) -> dict[str, Any]:
        try:
            return cio().ask(cid, body.content)
        except KeyError:
            raise HTTPException(404, "no such conversation") from None

    @app.get("/api/cio/proposals")
    def cio_proposals(status: str | None = None, _: str = Depends(user)) -> dict[str, Any]:
        return {"proposals": cio().proposals.list(status)}

    @app.post("/api/cio/proposals/{pid}/approve")
    def cio_approve(pid: int, body: DecisionBody, who: str = Depends(user)) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(422, "approving a proposal requires confirm=true")
        from firm.cio.proposals import ProposalError

        try:
            return cio().proposals.approve(pid, by=who, note=body.note)
        except ProposalError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/cio/proposals/{pid}/reject")
    def cio_reject(pid: int, body: DecisionBody, who: str = Depends(user)) -> dict[str, Any]:
        from firm.cio.proposals import ProposalError

        try:
            return cio().proposals.reject(pid, by=who, note=body.note)
        except ProposalError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/cio/memory")
    def cio_memory(_: str = Depends(user)) -> dict[str, Any]:
        rows = firm.db.query("SELECT * FROM cio_memory ORDER BY active DESC, id DESC LIMIT 200")
        return {"memories": [dict(r) for r in rows]}

    @app.post("/api/cio/memory")
    def cio_memory_add(body: MemoryBody, who: str = Depends(user)) -> dict[str, Any]:
        if not body.text:
            raise HTTPException(422, "text is required")
        return cio().memory.add(body.kind, body.text, by=who)

    @app.put("/api/cio/memory/{mid}")
    def cio_memory_edit(mid: int, body: MemoryBody, who: str = Depends(user)) -> dict[str, Any]:
        try:
            return cio().memory.update(mid, by=who, text=body.text, active=body.active, kind=body.kind)
        except KeyError:
            raise HTTPException(404, "no such memory") from None

    @app.get("/api/cio/usage")
    def cio_usage(_: str = Depends(user)) -> dict[str, Any]:
        return cio().budget.summary()

    # -- static frontend -------------------------------------------------------
    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        target = (WEB_DIST / path).resolve()
        if path and target.is_file() and WEB_DIST.resolve() in target.parents:
            return FileResponse(target)
        index = WEB_DIST / "index.html"
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return JSONResponse({"detail": "frontend not built (web/dist missing)"}, status_code=503)

    return app
