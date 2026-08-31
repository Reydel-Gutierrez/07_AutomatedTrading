"""Shared read-only Robinhood MCP bootstrap for the app, scheduler, and CLI.

CLI processes and the production runtime bind the same authorized observation
transport. This module never exposes place/review/cancel or transfer tools.
Credentials come from a user-level OAuth store or the process environment,
never from source or config. Cursor's MCP session is a different host and
is not reused.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.request import Request

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable, LiveErrorCode
from agentic_portfolio.adapters.readonly_mcp_auth import LOGIN_HINT, load_or_refresh_access_token
from agentic_portfolio.adapters.robinhood_read import (
    CLASSIFICATION_READ_TOOLS,
    FORBIDDEN_MCP_TOOLS,
    READONLY_MCP_UNREACHABLE,
    RECONCILIATION_READ_TOOLS,
    AuthorizedMcpReadAdapter,
    ReadOnlyFetcher,
)
from agentic_portfolio.policy import load_runtime_config

SHARED_PRODUCTION_TRANSPORT = "shared production transport"
READONLY_MODE = "READ_ONLY"
DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
MCP_PROTOCOL_VERSION = "2025-03-26"

# Keep in sync with discovery.safety.DISCOVERY_READ_TOOLS (do not import that module here).
DISCOVERY_OBSERVATION_TOOLS = frozenset(
    {
        "search",
        "get_scans",
        "run_scan",
        "get_scanner_filter_specs",
        "get_equity_quotes",
        "get_equity_historicals",
        "get_equity_technical_indicators",
        "get_equity_fundamentals",
        "get_financials",
        "get_earnings_calendar",
        "get_earnings_results",
        "get_equity_news",
        "get_sec_filing",
        "get_sec_filing_facts",
        "get_sec_filing_facts_catalog",
        "get_sec_filing_index",
        "get_index_quotes",
        "get_index_historicals",
        "get_indexes",
        "get_watchlists",
        "get_watchlist_items",
        "get_popular_watchlists",
        "get_equity_tradability",
        "get_equity_positions",
        "get_portfolio",
        "get_accounts",
        "get_equity_price_book",
        "get_equity_orders",
    }
)

SHARED_PRODUCTION_TRANSPORT = "shared production transport"
READONLY_MODE = "READ_ONLY"
DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
MCP_PROTOCOL_VERSION = "2025-03-26"

TRANSFER_MARKERS = (
    "deposit",
    "withdrawal",
    "withdraw",
    "transfer_between",
    "inter_account_transfer",
    "initiate_deposits",
    "initiate_withdrawals",
)

WRITE_ORDER_TOOLS = frozenset(FORBIDDEN_MCP_TOOLS) | {
    "review_equity_order",
    "review_option_order",
    "preview_crypto_order",
}

READONLY_OBSERVATION_TOOLS = (
    frozenset(CLASSIFICATION_READ_TOOLS)
    | frozenset(RECONCILIATION_READ_TOOLS)
    | DISCOVERY_OBSERVATION_TOOLS
    | {
        "get_equity_quotes",
        "get_equity_orders",
        "get_financials",
    }
)

_BOUND: "ReadonlyBrokerRuntime | None" = None


def _truthy_env(environ: Mapping[str, str], names: list[str]) -> str:
    for name in names:
        value = str(environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _runtime_mcp_config(runtime_config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(runtime_config if runtime_config is not None else load_runtime_config())
    return dict(cfg.get("readonly_mcp") or {})


def resolve_readonly_mcp_url(*, environ: Mapping[str, str] | None = None, runtime_config: Mapping[str, Any] | None = None) -> str:
    env = environ if environ is not None else os.environ
    mcp = _runtime_mcp_config(runtime_config)
    env_names = list((mcp.get("env") or {}).get("url") or ["AGENTIC_READONLY_MCP_URL"])
    return _truthy_env(env, env_names) or str(mcp.get("url") or DEFAULT_MCP_URL).strip() or DEFAULT_MCP_URL


def _read_token(*, environ: Mapping[str, str]) -> tuple[str, str | None]:
    mcp = _runtime_mcp_config()
    env_cfg = dict(mcp.get("env") or {})
    token_names = list(env_cfg.get("token") or ["AGENTIC_READONLY_MCP_TOKEN"])
    file_names = list(env_cfg.get("token_file") or ["AGENTIC_READONLY_MCP_TOKEN_FILE"])
    token = _truthy_env(environ, token_names)
    if token:
        return token, None
    path = _truthy_env(environ, file_names)
    if path:
        from pathlib import Path

        token_path = Path(path)
        if not token_path.is_file():
            return "", f"{READONLY_MCP_UNREACHABLE}: AGENTIC_READONLY_MCP_TOKEN_FILE does not exist ({path})"
        try:
            text = token_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            return "", f"{READONLY_MCP_UNREACHABLE}: cannot read AGENTIC_READONLY_MCP_TOKEN_FILE: {exc}"
        if not text:
            return "", f"{READONLY_MCP_UNREACHABLE}: AGENTIC_READONLY_MCP_TOKEN_FILE is empty"
        return text, None
    access, oauth_error = load_or_refresh_access_token(environ=environ)
    if access:
        return access, None
    if oauth_error:
        code = (
            LiveErrorCode.OAUTH_REFRESH_FAILED
            if "refresh_token" in oauth_error or "expired" in oauth_error.lower()
            else LiveErrorCode.OAUTH_TOKEN_UNAVAILABLE
        )
        return "", f"{code}: {oauth_error}"
    return "", (
        f"{LiveErrorCode.OAUTH_TOKEN_UNAVAILABLE}: {READONLY_MCP_UNREACHABLE}: "
        f"authorized Robinhood MCP token is not configured ({LOGIN_HINT})"
    )


def is_forbidden_observation_tool(tool: str) -> bool:
    name = str(tool or "").strip()
    lowered = name.lower().replace("-", "_")
    if name in WRITE_ORDER_TOOLS or lowered in {t.lower() for t in WRITE_ORDER_TOOLS}:
        return True
    if any(marker in lowered for marker in TRANSFER_MARKERS):
        return True
    return name not in READONLY_OBSERVATION_TOOLS


class GuardedReadonlyTransport:
    """Callable MCP transport that allowlists observation tools only."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __call__(self, tool: str, **kwargs: Any) -> Any:
        name = str(tool or "").strip()
        self.calls.append(name)
        if is_forbidden_observation_tool(name):
            raise LiveDataUnavailable(f"refused forbidden MCP tool: {name}")
        if callable(self._inner):
            return self._inner(name, **kwargs)
        invoke = getattr(self._inner, "invoke", None)
        if callable(invoke):
            return invoke(name, **kwargs)
        method = getattr(self._inner, name, None)
        if callable(method):
            return method(**kwargs)
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: transport cannot invoke {name}")


class GuardedFetcherProxy:
    """Expose only observation methods from an injected fetcher. No write methods."""

    _ALLOWED = frozenset(
        {
            "get_equity_tradability",
            "get_equity_fundamentals",
            "search_instrument",
            "search",
            "get_equity_quotes",
            "quotes",
            "get_accounts",
            "get_portfolio",
            "get_equity_positions",
            "get_equity_orders",
            "get_scans",
            "run_scan",
            "get_watchlists",
            "get_watchlist_items",
            "get_popular_watchlists",
            "get_earnings_calendar",
            "get_earnings_results",
            "get_equity_historicals",
            "get_equity_technical_indicators",
            "get_equity_news",
            "get_financials",
            "get_indexes",
            "get_index_quotes",
            "get_sec_filing_index",
            "scans",
            "watchlists",
            "watchlist_items",
            "popular_watchlists",
            "earnings_calendar",
            "fundamentals",
            "financials",
            "historicals",
            "tradability",
            "news",
            "positions",
            "portfolio",
            "calls",
        }
    )

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in WRITE_ORDER_TOOLS:
            raise AttributeError(name)
        if name not in self._ALLOWED:
            raise AttributeError(name)
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_inner":
            object.__setattr__(self, name, value)
            return
        raise AttributeError(name)


def unwrap_mcp_tool_result(payload: Any) -> Any:
    """Normalize MCP tools/call JSON-RPC results into adapter payloads."""
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        return payload
    data = dict(payload)
    if "error" in data and data.get("error"):
        err = data["error"]
        if isinstance(err, Mapping):
            message = err.get("message") or err
        else:
            message = err
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: {message}", code=LiveErrorCode.MCP_TOOLS_CALL_FAILED)
    result = data.get("result", data)
    if not isinstance(result, Mapping):
        return result
    if result.get("isError"):
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: tool error", code=LiveErrorCode.MCP_TOOLS_CALL_FAILED)
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    contents = result.get("content")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text":
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return {"text": text}
                return parsed
    if "data" in result or "results" in result:
        return dict(result)
    return dict(result) if result else data


def _parse_http_body(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    ctype = (content_type or "").lower()
    try:
        if "text/event-stream" in ctype or text.lstrip().startswith(("event:", "data:")):
            for line in text.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk:
                        return json.loads(chunk)
            return {}
        if not text.strip():
            return {}
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LiveDataUnavailable(
            f"{READONLY_MCP_UNREACHABLE}: MCP response is not JSON",
            code=LiveErrorCode.MCP_TOOLS_CALL_FAILED,
        ) from exc


class StreamableHttpMcpTransport:
    """Streamable HTTP JSON-RPC client for the authorized Robinhood MCP endpoint."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout: float = 30.0,
        post: Any | None = None,
        refresh_token_fn: Any | None = None,
    ) -> None:
        self.url = str(url).rstrip("/")
        self._token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 0
        self._initialized = False
        self._post = post or self._default_post
        self._refresh_token_fn = refresh_token_fn

    def update_token(self, token: str) -> None:
        self._token = token
        self.session_id = None
        self._initialized = False

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _default_post(self, body: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        req = Request(self.url, data=json.dumps(body).encode("utf-8"), headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                headers = {str(k): str(v) for k, v in resp.headers.items()}
                return int(resp.status), headers, resp.read()
        except urllib.error.HTTPError as exc:
            headers = {str(k): str(v) for k, v in (exc.headers.items() if exc.headers else [])}
            return int(exc.code), headers, exc.read() or b""
        except TimeoutError as exc:
            raise LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: MCP HTTP timeout",
                code=LiveErrorCode.MCP_HTTP_TIMEOUT,
            ) from exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LiveDataUnavailable(
                    f"{READONLY_MCP_UNREACHABLE}: MCP HTTP timeout: {reason}",
                    code=LiveErrorCode.MCP_HTTP_TIMEOUT,
                ) from exc
            raise LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: HTTP transport failed: {reason}",
                code=LiveErrorCode.MCP_TOOLS_CALL_FAILED,
            ) from exc

    def _try_refresh_token(self) -> bool:
        if not callable(self._refresh_token_fn):
            return False
        try:
            token = self._refresh_token_fn()
        except LiveDataUnavailable:
            return False
        except Exception:  # noqa: BLE001
            return False
        if not str(token or "").strip():
            return False
        self.update_token(str(token).strip())
        return True

    def _http_status_error(self, status: int, method: str) -> LiveDataUnavailable:
        if status == 401:
            return LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: MCP HTTP 401 for {method}",
                code=LiveErrorCode.MCP_HTTP_401,
            )
        if method == "initialize":
            return LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: MCP HTTP {status} for {method}",
                code=LiveErrorCode.MCP_INITIALIZE_FAILED,
            )
        return LiveDataUnavailable(
            f"{READONLY_MCP_UNREACHABLE}: MCP HTTP {status} for {method}",
            code=LiveErrorCode.MCP_HTTP_ERROR if status >= 400 else LiveErrorCode.MCP_TOOLS_CALL_FAILED,
        )

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None, *, notification: bool = False, _retried: bool = False) -> Any:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self._next_id
        if params is not None:
            body["params"] = dict(params)
        try:
            status, headers, raw = self._post(body)
        except TimeoutError as exc:
            raise LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: MCP HTTP timeout",
                code=LiveErrorCode.MCP_HTTP_TIMEOUT,
            ) from exc
        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if status == 401 and not _retried and self._try_refresh_token():
            if method != "initialize" and not self._initialized:
                self.initialize()
            return self._rpc(method, params, notification=notification, _retried=True)
        if status >= 400:
            raise self._http_status_error(status, method)
        if notification:
            return None
        parsed = _parse_http_body(raw, headers.get("Content-Type") or headers.get("content-type") or "")
        if isinstance(parsed, Mapping) and parsed.get("error"):
            err = parsed["error"]
            message = err.get("message") if isinstance(err, Mapping) else err
            code = LiveErrorCode.MCP_INITIALIZE_FAILED if method == "initialize" else LiveErrorCode.MCP_TOOLS_CALL_FAILED
            raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: {message}", code=code)
        return unwrap_mcp_tool_result(parsed) if method == "tools/call" else parsed

    def initialize(self) -> None:
        self._rpc(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentic-portfolio-readonly", "version": "1.0.0"},
            },
        )
        self._rpc("notifications/initialized", {}, notification=True)
        self._initialized = True

    def __call__(self, tool: str, **kwargs: Any) -> Any:
        if not self._initialized:
            self.initialize()
        arguments = {key: value for key, value in kwargs.items() if value is not None}
        return self._rpc("tools/call", {"name": tool, "arguments": arguments})


@dataclass
class ReadonlyBrokerRuntime:
    """Bound or failed-closed read-only broker runtime. Never a write client."""

    bound: bool
    mode: str = READONLY_MODE
    source: str = SHARED_PRODUCTION_TRANSPORT
    initialization_error: str | None = None
    fetcher: ReadOnlyFetcher | None = None
    transport: Any | None = None
    calls: list[str] = field(default_factory=list)

    def as_report(self) -> dict[str, Any]:
        return {
            "bound": bool(self.bound),
            "mode": READONLY_MODE,
            "source": self.source,
            "initialization_error": self.initialization_error,
        }

    def invoke(self, tool: str, **kwargs: Any) -> Any:
        if not self.bound or self.transport is None:
            raise LiveDataUnavailable(self.initialization_error or f"{READONLY_MCP_UNREACHABLE}: authorized Robinhood MCP transport is not bound")
        return self.transport(tool, **kwargs)


def reset_readonly_broker_runtime() -> None:
    """Test helper. Clears the process-wide bound transport."""
    global _BOUND
    _BOUND = None


def _fetcher_from_transport(transport: Any, *, account_number: str | None) -> tuple[Any, ReadOnlyFetcher]:
    if hasattr(transport, "get_equity_quotes") and hasattr(transport, "get_equity_tradability"):
        return GuardedReadonlyTransport(transport), GuardedFetcherProxy(transport)
    guarded = transport if isinstance(transport, GuardedReadonlyTransport) else GuardedReadonlyTransport(transport)
    return guarded, AuthorizedMcpReadAdapter(transport=guarded, account_number=account_number)


def _bind(transport: Any, *, account_number: str | None = None, error: str | None = None) -> ReadonlyBrokerRuntime:
    global _BOUND
    if error:
        _BOUND = ReadonlyBrokerRuntime(
            bound=False,
            mode=READONLY_MODE,
            source=SHARED_PRODUCTION_TRANSPORT,
            initialization_error=error,
        )
        return _BOUND
    guarded, fetcher = _fetcher_from_transport(transport, account_number=account_number)
    _BOUND = ReadonlyBrokerRuntime(
        bound=True,
        mode=READONLY_MODE,
        source=SHARED_PRODUCTION_TRANSPORT,
        initialization_error=None,
        fetcher=fetcher,
        transport=guarded,
    )
    return _BOUND


def bind_readonly_broker_transport(transport: Any, *, account_number: str | None = None) -> ReadonlyBrokerRuntime:
    """Test/app helper: bind an already-authorized observation transport."""
    return bootstrap_readonly_broker_runtime(transport=transport, account_number=account_number)


def reconnect_readonly_broker_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    account_number: str | None = None,
) -> ReadonlyBrokerRuntime:
    """Force a fresh bind from persisted OAuth/env. Fail closed. Never a write client."""
    return bootstrap_readonly_broker_runtime(environ=environ, account_number=account_number, force=True)


def _make_http_transport(url: str, token: str, *, environ: Mapping[str, str]) -> StreamableHttpMcpTransport:
    def refresh() -> str:
        access, error = load_or_refresh_access_token(environ=environ, force_refresh=True)
        if not access:
            raise LiveDataUnavailable(
                error or f"{READONLY_MCP_UNREACHABLE}: refresh_token grant failed",
                code=LiveErrorCode.OAUTH_REFRESH_FAILED,
            )
        return access

    return StreamableHttpMcpTransport(url, token, refresh_token_fn=refresh)


def _coded_error(exc: BaseException, *, default: str = LiveErrorCode.MCP_INITIALIZE_FAILED) -> str:
    code = getattr(exc, "code", None) or default
    text = str(exc)
    if text.startswith(str(code)):
        return text
    return f"{code}: {text}"


def bootstrap_readonly_broker_runtime(
    *,
    transport: Any | None = None,
    account_number: str | None = None,
    environ: Mapping[str, str] | None = None,
    force: bool = False,
) -> ReadonlyBrokerRuntime:
    """Bind the authorized read-only Robinhood MCP transport.

    App, scheduler, and CLI must call this. Fail closed when the transport
    cannot initialize. Never binds place/review/cancel.
    """
    global _BOUND
    previous = _BOUND
    if transport is not None:
        return _bind(transport, account_number=account_number)
    if _BOUND is not None and _BOUND.bound and not force:
        return _BOUND
    env = environ if environ is not None else os.environ
    token, token_error = _read_token(environ=env)
    if token_error:
        if previous is not None and previous.bound and not force:
            _BOUND = previous
            return previous
        return _bind(None, error=token_error)
    url = resolve_readonly_mcp_url(environ=env)
    http = _make_http_transport(url, token, environ=env)
    try:
        http.initialize()
    except LiveDataUnavailable as exc:
        if getattr(exc, "code", None) != LiveErrorCode.MCP_HTTP_401 and "401" not in str(exc):
            if previous is not None and previous.bound and not force:
                _BOUND = previous
                return previous
            return _bind(None, error=_coded_error(exc))
        refreshed, refresh_error = load_or_refresh_access_token(environ=env, force_refresh=True)
        if not refreshed:
            if previous is not None and previous.bound and not force:
                _BOUND = previous
                return previous
            return _bind(
                None,
                error=(
                    f"{LiveErrorCode.OAUTH_REFRESH_FAILED}: {refresh_error}"
                    if refresh_error
                    else _coded_error(exc, default=LiveErrorCode.OAUTH_REFRESH_FAILED)
                ),
            )
        http = _make_http_transport(url, refreshed, environ=env)
        try:
            http.initialize()
        except LiveDataUnavailable as retry_exc:
            if previous is not None and previous.bound and not force:
                _BOUND = previous
                return previous
            return _bind(None, error=_coded_error(retry_exc))
        except Exception as retry_exc:
            if previous is not None and previous.bound and not force:
                _BOUND = previous
                return previous
            return _bind(
                None,
                error=(
                    f"{LiveErrorCode.MCP_INITIALIZE_FAILED}: {READONLY_MCP_UNREACHABLE}: "
                    f"authorized Robinhood MCP initialize failed: {type(retry_exc).__name__}: {retry_exc}"
                ),
            )
    except Exception as exc:
        if previous is not None and previous.bound and not force:
            _BOUND = previous
            return previous
        return _bind(
            None,
            error=(
                f"{LiveErrorCode.MCP_INITIALIZE_FAILED}: {READONLY_MCP_UNREACHABLE}: "
                f"authorized Robinhood MCP initialize failed: {type(exc).__name__}: {exc}"
            ),
        )
    return _bind(http, account_number=account_number)
