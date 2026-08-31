"""Flask app. Localhost dashboard over existing portfolio modules."""

from __future__ import annotations

from pathlib import Path

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from agentic_portfolio.approval.types import ApprovalStatus
from agentic_portfolio.approval.validate import ApprovalValidationError
from agentic_portfolio.dashboard.accounts import (
    ROLE_ADMIN,
    AccountStore,
    is_viewer_role,
    public_user,
    validate_new_password,
    verify_password,
)
from agentic_portfolio.dashboard.family import (
    current_paper_nav,
    family_admin_view,
    family_member_view,
    parse_amount,
)
from agentic_portfolio.dashboard.queries import (
    activity_log_view,
    agent_runtime_view,
    ai_activity_view,
    ai_view,
    dashboard_state,
    dashboard_view,
    discovery_view,
    get_approval,
    get_report,
    get_thesis,
    journal_view,
    list_approvals,
    notifications_view,
    orders_view,
    record_approval_decision,
    research_view,
    system_view,
    watchlist_view,
)
from agentic_portfolio.dashboard.safety import (
    DashboardSafetyError,
    csrf_tokens_match,
    is_forbidden_action,
    new_csrf_token,
)
from agentic_portfolio.dashboard.settings import resolve_bind, resolve_ui_flags
from agentic_portfolio.notify import NotificationStore
from agentic_portfolio.paths import project_root
from agentic_portfolio.runtime import bootstrap_readonly_broker_runtime

HERE = Path(__file__).resolve().parent
DECIDE_ENDPOINTS = {"approve_packet", "reject_packet", "api_approve", "api_reject"}
PUBLIC_ENDPOINTS = {"login_page", "login_post", "static", "healthz"}
FAMILY_ALLOWED_ENDPOINTS = {
    "dashboard_page",
    "api_family_me",
    "logout",
    "password_page",
    "password_change",
    "discovery_page",
    "api_discovery",
}
CSRF_ENDPOINTS = DECIDE_ENDPOINTS | {
    "login_post",
    "family_create",
    "family_enable",
    "family_disable",
    "family_assign",
    "password_change",
    "family_reset_password",
    "notifications_read",
    "api_notifications_read",
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def create_app(root: Path | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(HERE / "templates"),
        static_folder=str(HERE / "static"),
    )
    app.config["ROOT"] = Path(root) if root is not None else project_root()
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = False
    app.secret_key = "localhost-dashboard-not-a-broker-credential"
    app.config["READONLY_BROKER_RUNTIME"] = bootstrap_readonly_broker_runtime()
    accounts = AccountStore(app.config["ROOT"])

    def state():
        return dashboard_state(app.config["ROOT"])

    def _csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = new_csrf_token()
            session["_csrf_token"] = token
        return str(token)

    def _provided_csrf() -> str | None:
        header = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
        if header and str(header).strip():
            return str(header).strip()
        form = request.form.get("csrf_token")
        if form and str(form).strip():
            return str(form).strip()
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            token = payload.get("csrf_token")
            if token and str(token).strip():
                return str(token).strip()
        return None

    def _confirmed() -> bool:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            value = payload.get("confirm")
            if value is None:
                value = payload.get("confirmed")
        else:
            value = request.form.get("confirm")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "confirm"}

    def _json_request() -> bool:
        return bool(request.is_json or request.path.startswith("/api/"))

    def _current_user() -> dict | None:
        user = accounts.get(session.get("user_id"))
        if user is None:
            return None
        if not user.get("enabled", True):
            _clear_login()
            return None
        nonce = user.get("session_nonce")
        if nonce and session.get("session_nonce") != nonce:
            _clear_login()
            return None
        return user

    def _login_user(user: dict) -> None:
        session["user_id"] = user["id"]
        session["role"] = user["role"]
        session["username"] = user.get("username")
        if user.get("session_nonce"):
            session["session_nonce"] = user["session_nonce"]

    def _clear_login() -> None:
        session.pop("user_id", None)
        session.pop("role", None)
        session.pop("username", None)
        session.pop("session_nonce", None)

    def _csrf_failure():
        if _json_request():
            payload = {"ok": False, "error": "csrf_rejected"}
            if request.endpoint in DECIDE_ENDPOINTS:
                payload["placed_order"] = False
            return jsonify(payload), 403
        abort(403)

    def _unauthenticated():
        if _json_request():
            return jsonify({"ok": False, "error": "authentication_required"}), 401
        return redirect(url_for("login_page", next=request.path))

    def _forbidden():
        if _json_request():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        abort(403)

    def _api_action_name() -> str:
        parts = [part for part in request.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "api":
            return parts[1]
        return ""

    def _field(*names: str) -> str:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            for name in names:
                if payload.get(name) is not None:
                    return str(payload.get(name))
        for name in names:
            if request.form.get(name) is not None:
                return str(request.form.get(name))
        return ""

    @app.before_request
    def _protect_writes():
        _csrf_token()
        if request.endpoint == "static":
            return None
        if request.method in WRITE_METHODS and is_forbidden_action(_api_action_name()):
            return jsonify({"ok": False, "error": "forbidden", "placed_order": False}), 403
        needs_csrf = request.method in WRITE_METHODS and request.endpoint in CSRF_ENDPOINTS
        if request.endpoint in PUBLIC_ENDPOINTS:
            if needs_csrf and not csrf_tokens_match(session.get("_csrf_token"), _provided_csrf()):
                return _csrf_failure()
            return None
        user = _current_user()
        if user is None:
            return _unauthenticated()
        if needs_csrf and not csrf_tokens_match(session.get("_csrf_token"), _provided_csrf()):
            return _csrf_failure()
        if is_viewer_role(user.get("role")) and request.endpoint not in FAMILY_ALLOWED_ENDPOINTS:
            return _forbidden()
        if user.get("role") == ROLE_ADMIN:
            return None
        if request.endpoint not in FAMILY_ALLOWED_ENDPOINTS:
            return _forbidden()
        return None

    @app.context_processor
    def inject_flags():
        from agentic_portfolio.dashboard.queries import execution_flags

        flags = execution_flags()
        ui = resolve_ui_flags()
        user = public_user(_current_user())
        name = str((user or {}).get("name") or (user or {}).get("username") or "").strip()
        parts = [part for part in name.split() if part]
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[1][0]).upper()
        else:
            initials = name[:2].upper()
        pending_count = 0
        unread_count = 0
        if user and user.get("role") == ROLE_ADMIN:
            pending_count = int(list_approvals(state()).get("pending_count") or 0)
            unread_count = int(notifications_view(state()).get("unread_count") or 0)
        return {
            "autonomous_disabled": flags["autonomous_trading_disabled"],
            "execution_flags": flags,
            "environment": ui["environment"],
            "environment_banner": ui["environment_banner"],
            "paper_book_label": ui["paper_book_label"],
            "live_account_label": ui["live_account_label"],
            "active_book_label": ui["active_book_label"],
            "risk_book_label": ui["risk_book_label"],
            "active_runtime": ui["environment"],
            "live_account_status": ui["live_account_status"],
            "paper_book_status": ui["paper_book_status"],
            "book_kind": ui["book_kind"],
            "live_order_placement_enabled": False,
            "no_live_placement_banner": ui["no_live_placement_banner"],
            "csrf_token": _csrf_token(),
            "allow_paper_packet_decisions": ui["allow_paper_packet_decisions"],
            "allow_demo_packet_decisions": ui["allow_demo_packet_decisions"],
            "current_user": user,
            "is_admin": bool(user and user.get("role") == ROLE_ADMIN),
            "is_family": bool(user and is_viewer_role(user.get("role"))),
            "is_user": bool(user and is_viewer_role(user.get("role"))),
            "user_initials": initials,
            "pending_count": pending_count,
            "unread_count": unread_count,
        }

    @app.get("/login")
    def login_page():
        if _current_user():
            return redirect(url_for("dashboard_page"))
        return render_template("login.html", page="login")

    @app.post("/login")
    def login_post():
        if _current_user():
            return redirect(url_for("dashboard_page"))
        identifier = _field("username", "email", "login").strip()
        password = _field("password")
        user = accounts.authenticate(identifier, password)
        if user is None:
            if _json_request():
                return jsonify({"ok": False, "error": "invalid_credentials"}), 401
            flash("Invalid username/email or password, or the account is disabled.")
            return render_template("login.html", page="login"), 401
        _login_user(user)
        nxt = request.args.get("next") or _field("next")
        target = nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard_page")
        if _json_request():
            return jsonify({"ok": True, "role": user["role"], "name": user.get("name"), "redirect": target})
        return redirect(target)

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        _clear_login()
        if _json_request():
            return jsonify({"ok": True})
        return redirect(url_for("login_page"))

    def _password_error(message: str, *, template: str, page: str, extra: dict | None = None):
        if _json_request():
            return jsonify({"ok": False, "error": message}), 409
        flash(message)
        return render_template(template, page=page, **(extra or {})), 409

    @app.get("/password")
    def password_page():
        return render_template("password.html", page="password")

    @app.post("/password")
    def password_change():
        user = _current_user()
        if user is None:
            return _unauthenticated()
        try:
            new_password = validate_new_password(
                _field("new_password"),
                _field("confirm_password", "new_password_confirm"),
            )
            accounts.change_own_password(user["id"], _field("current_password", "password"), new_password)
        except ValueError as exc:
            return _password_error(str(exc), template="password.html", page="password")
        _clear_login()
        if _json_request():
            return jsonify({"ok": True, "logged_out": True})
        flash("Password changed. Sign in again.")
        return redirect(url_for("login_page"))

    @app.get("/")
    def dashboard_page():
        user = _current_user()
        if user and is_viewer_role(user.get("role")):
            view = family_member_view(state(), user)
            return render_template("family_dashboard.html", view=view, page="dashboard")
        return render_template("dashboard.html", view=dashboard_view(state()), page="dashboard")

    @app.get("/users")
    @app.get("/family")
    def family_page():
        view = family_admin_view(state(), accounts.family_users())
        return render_template("family.html", view=view, page="users")

    @app.post("/users")
    @app.post("/family/users")
    def family_create():
        try:
            user = accounts.create_family_user(
                name=_field("name"),
                login=_field("username", "email", "login"),
                password=_field("password"),
            )
        except ValueError as exc:
            if _json_request():
                return jsonify({"ok": False, "error": str(exc)}), 409
            flash(str(exc))
            return redirect(url_for("family_page"))
        if _json_request():
            return jsonify({"ok": True, "user": public_user(user)})
        flash(f"Created user {user['name']}.")
        return redirect(url_for("family_page"))

    @app.post("/users/<user_id>/enable")
    @app.post("/family/users/<user_id>/enable")
    def family_enable(user_id: str):
        return _set_family_enabled(user_id, True)

    @app.post("/users/<user_id>/disable")
    @app.post("/family/users/<user_id>/disable")
    def family_disable(user_id: str):
        return _set_family_enabled(user_id, False)

    def _set_family_enabled(user_id: str, enabled: bool):
        try:
            user = accounts.set_enabled(user_id, enabled)
        except KeyError:
            abort(404)
        except ValueError as exc:
            if _json_request():
                return jsonify({"ok": False, "error": str(exc)}), 409
            flash(str(exc))
            return redirect(url_for("family_page"))
        if _json_request():
            return jsonify({"ok": True, "user": public_user(user)})
        flash(f"{user['name']} {'enabled' if enabled else 'disabled'}.")
        return redirect(url_for("family_page"))

    @app.post("/users/<user_id>/assign")
    @app.post("/family/users/<user_id>/assign")
    def family_assign(user_id: str):
        nav = current_paper_nav(state())
        try:
            amount = parse_amount(_field("amount", "assigned_amount"))
            if nav is None:
                raise ValueError("LIVE NAV is not available" if resolve_ui_flags()["environment"] == "LIVE" else "paper book NAV is not available")
            user = accounts.assign_amount(user_id, amount, nav)
        except KeyError:
            abort(404)
        except (TypeError, ValueError) as exc:
            if _json_request():
                return jsonify({"ok": False, "error": str(exc)}), 409
            flash(str(exc))
            return redirect(url_for("family_page"))
        if _json_request():
            return jsonify({"ok": True, "user": member_public(user, nav)})
        flash(f"Assigned {_field('amount', 'assigned_amount')} to {user['name']}. Baseline reset at current NAV.")
        return redirect(url_for("family_page"))

    @app.post("/users/<user_id>/password")
    @app.post("/family/users/<user_id>/password")
    def family_reset_password(user_id: str):
        admin = _current_user()
        if not admin or admin.get("role") != ROLE_ADMIN:
            return _forbidden()
        if not verify_password(admin.get("password_hash"), _field("admin_password", "current_password")):
            return _password_error(
                "admin password is incorrect",
                template="family.html",
                page="users",
                extra={"view": family_admin_view(state(), accounts.family_users())},
            )
        try:
            new_password = validate_new_password(
                _field("new_password", "password"),
                _field("confirm_password", "new_password_confirm"),
            )
            user = accounts.reset_family_password(user_id, new_password)
        except KeyError:
            abort(404)
        except ValueError as exc:
            return _password_error(
                str(exc),
                template="family.html",
                page="users",
                extra={"view": family_admin_view(state(), accounts.family_users())},
            )
        if _json_request():
            return jsonify({"ok": True, "user": public_user(user)})
        flash(f"Password reset for {user['name']}. They must sign in with the new password.")
        return redirect(url_for("family_page"))

    def member_public(user: dict, nav: float | None):
        from agentic_portfolio.dashboard.family import member_row

        return member_row(user, nav)

    @app.get("/api/users")
    @app.get("/api/family")
    def api_family():
        return jsonify(family_admin_view(state(), accounts.family_users()))

    @app.get("/api/users/me")
    @app.get("/api/family/me")
    def api_family_me():
        user = _current_user()
        if not user or not is_viewer_role(user.get("role")):
            return _forbidden()
        payload = family_member_view(state(), user)
        assert "nav" not in payload
        return jsonify(payload)

    @app.get("/healthz")
    def healthz():
        from agentic_portfolio.agent.heartbeat import load_health

        payload = load_health(app.config["ROOT"])
        payload["live_order_placement_enabled"] = False
        return jsonify(payload)

    @app.get("/watchlist")
    def watchlist_page():
        return render_template("watchlist.html", view=watchlist_view(state()), page="watchlist")

    @app.get("/notifications")
    def notifications_page():
        return render_template("notifications.html", view=notifications_view(state()), page="notifications")

    @app.post("/notifications/read")
    def notifications_read():
        NotificationStore(app.config["ROOT"]).mark_read(request.form.get("notification_id") or None)
        if _json_request():
            return jsonify({"ok": True})
        return redirect(url_for("notifications_page"))

    @app.get("/api/watchlist")
    def api_watchlist():
        return jsonify(watchlist_view(state()))

    @app.get("/api/notifications")
    def api_notifications():
        return jsonify(notifications_view(state()))

    @app.post("/api/notifications/read")
    def api_notifications_read():
        payload = request.get_json(silent=True) or {}
        NotificationStore(app.config["ROOT"]).mark_read(payload.get("notification_id"))
        return jsonify({"ok": True, **notifications_view(state())})

    @app.get("/api/agent")
    def api_agent():
        return jsonify(agent_runtime_view(state()))

    @app.get("/api/activity")
    def api_activity():
        return jsonify(activity_log_view(state()))

    @app.get("/approvals")
    def approvals_page():
        return render_template("approvals.html", view=list_approvals(state()), page="approvals")

    @app.get("/approvals/<approval_id>")
    def approval_detail_page(approval_id: str):
        packet = get_approval(state(), approval_id)
        if packet is None:
            abort(404)
        return render_template("approval_detail.html", packet=packet, page="approvals")

    def _note() -> str | None:
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            value = payload.get("note")
        else:
            value = request.form.get("note")
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _decide(approval_id: str, status: ApprovalStatus):
        note = _note()
        packet = get_approval(state(), approval_id)
        if packet is None:
            abort(404)
        if not _confirmed():
            if _json_request():
                return jsonify(
                    {
                        "ok": False,
                        "error": "confirmation_required",
                        "needs_confirm": True,
                        "placed_order": False,
                        "approved_does_not_place_order": True,
                    }
                ), 409
            return render_template(
                "confirm_decision.html",
                packet=packet,
                intent=status.value,
                note=note or "",
                page="approvals",
            )
        try:
            packet = record_approval_decision(state(), approval_id, status, note=note)
        except KeyError:
            abort(404)
        except ApprovalValidationError as exc:
            if _json_request():
                return jsonify({"ok": False, "error": str(exc), "placed_order": False}), 409
            flash(str(exc))
            return redirect(url_for("approval_detail_page", approval_id=approval_id))
        if _json_request():
            return jsonify({"ok": True, "packet": packet, "placed_order": False, "approved_does_not_place_order": True})
        flash(
            f"{packet['symbol']} {packet.get('status')}. This still does not place an order."
        )
        return redirect(url_for("approval_detail_page", approval_id=approval_id))

    @app.post("/approvals/<approval_id>/approve")
    def approve_packet(approval_id: str):
        return _decide(approval_id, ApprovalStatus.APPROVED)

    @app.post("/approvals/<approval_id>/reject")
    def reject_packet(approval_id: str):
        return _decide(approval_id, ApprovalStatus.REJECTED)

    @app.get("/research")
    def research_page():
        return render_template("research.html", view=research_view(state()), page="research")

    @app.get("/discovery")
    def discovery_page():
        return render_template("discovery.html", view=discovery_view(state()), page="discovery")

    @app.get("/ai")
    def ai_page():
        return render_template("ai.html", view=ai_view(state()), page="ai")

    @app.get("/ai/activity")
    def ai_activity_page():
        return render_template("ai_activity.html", view=ai_activity_view(state()), page="ai")

    @app.get("/research/theses/<thesis_id>")
    def thesis_detail_page(thesis_id: str):
        thesis = get_thesis(state(), thesis_id)
        if thesis is None:
            abort(404)
        report = get_report(state(), thesis["research_id"]) if thesis.get("research_id") else None
        return render_template("thesis_detail.html", thesis=thesis, report=report, page="research")

    @app.get("/research/reports/<research_id>")
    def report_detail_page(research_id: str):
        report = get_report(state(), research_id)
        if report is None:
            abort(404)
        return render_template("report_detail.html", report=report, page="research")

    @app.get("/orders")
    def orders_page():
        return render_template("orders.html", view=orders_view(state()), page="orders")

    @app.get("/journal")
    def journal_page():
        return render_template("journal.html", view=journal_view(state()), page="journal")

    @app.get("/system")
    def system_page():
        bind = resolve_bind()
        return render_template("system.html", view=system_view(state()), bind=bind, page="system")

    @app.get("/api/dashboard")
    def api_dashboard():
        return jsonify(dashboard_view(state()))

    @app.get("/api/approvals")
    def api_approvals():
        return jsonify(list_approvals(state()))

    @app.get("/api/approvals/<approval_id>")
    def api_approval(approval_id: str):
        packet = get_approval(state(), approval_id)
        if packet is None:
            abort(404)
        return jsonify(packet)

    @app.post("/api/approvals/<approval_id>/approve")
    def api_approve(approval_id: str):
        return _decide(approval_id, ApprovalStatus.APPROVED)

    @app.post("/api/approvals/<approval_id>/reject")
    def api_reject(approval_id: str):
        return _decide(approval_id, ApprovalStatus.REJECTED)

    @app.get("/api/research")
    def api_research():
        return jsonify(research_view(state()))

    @app.get("/api/discovery")
    def api_discovery():
        return jsonify(discovery_view(state()))

    @app.get("/api/ai")
    def api_ai():
        return jsonify(ai_view(state()))

    @app.get("/api/ai/activity")
    def api_ai_activity():
        return jsonify(ai_activity_view(state()))

    @app.get("/api/orders")
    def api_orders():
        return jsonify(orders_view(state()))

    @app.get("/api/journal")
    def api_journal():
        return jsonify(journal_view(state()))

    @app.get("/api/system")
    def api_system():
        payload = system_view(state())
        payload["bind"] = resolve_bind()
        return jsonify(payload)

    @app.route("/api/<action>", methods=["POST", "PUT", "PATCH", "DELETE"])
    def api_forbidden(action: str):
        if is_forbidden_action(action):
            return jsonify({"ok": False, "error": "forbidden", "placed_order": False}), 403
        abort(404)

    @app.errorhandler(DashboardSafetyError)
    def safety_error(exc: DashboardSafetyError):
        return jsonify({"ok": False, "error": str(exc)}), 403

    @app.errorhandler(403)
    def forbidden_page(exc):
        if _json_request():
            return jsonify({"ok": False, "error": "forbidden"}), 403
        return render_template("forbidden.html", page="forbidden"), 403

    @app.errorhandler(Exception)
    def uncaught(exc):
        if isinstance(exc, HTTPException):
            return exc
        from agentic_portfolio.runtime import RuntimeMode, get_active_runtime

        if get_active_runtime() is RuntimeMode.LIVE:
            app.logger.exception("LIVE dashboard failed closed")
            if _json_request():
                return jsonify(
                    {
                        "ok": False,
                        "error": "LIVE DATA UNAVAILABLE",
                        "live_data_unavailable": True,
                        "live_order_placement_enabled": False,
                    }
                ), 500
            try:
                return render_template(
                    "unavailable.html",
                    page="system",
                    message="LIVE DATA UNAVAILABLE",
                ), 500
            except Exception:
                app.logger.exception("LIVE fail-closed page also failed")
                return (
                    "<!DOCTYPE html><title>LIVE DATA UNAVAILABLE</title>"
                    "<h1>LIVE DATA UNAVAILABLE</h1>"
                    "<p>The dashboard failed closed. Live order placement remains disabled.</p>",
                    500,
                    {"Content-Type": "text/html; charset=utf-8"},
                )
        raise

    return app
