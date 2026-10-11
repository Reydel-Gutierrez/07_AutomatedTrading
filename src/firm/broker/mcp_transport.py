"""Streamable-HTTP JSON-RPC client for the Robinhood Trading MCP endpoint.

Ported from the legacy runtime: same protocol version, session handling,
401 → one token refresh → retry, and result unwrapping.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.request import Request

from firm.broker.errors import UNREACHABLE, BrokerUnavailable
from firm.broker.mcp_auth import DEFAULT_MCP_URL, LOGIN_HINT, load_or_refresh_access_token

MCP_PROTOCOL_VERSION = "2025-03-26"

Post = Callable[[dict[str, Any]], tuple[int, dict[str, str], bytes]]


def unwrap_tool_result(payload: Any) -> Any:
    if not isinstance(payload, Mapping):
        return payload
    if payload.get("error"):
        err = payload["error"]
        message = err.get("message") if isinstance(err, Mapping) else err
        raise BrokerUnavailable(f"{UNREACHABLE}: {message}")
    result = payload.get("result", payload)
    if not isinstance(result, Mapping):
        return result
    if result.get("isError"):
        text = ""
        for item in result.get("content") or []:
            if isinstance(item, Mapping) and item.get("type") == "text":
                text = str(item.get("text") or "")[:500]
                break
        raise BrokerUnavailable(f"{UNREACHABLE}: tool error {text}".strip(), code="TOOL_ERROR")
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    for item in result.get("content") or []:
        if isinstance(item, Mapping) and item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"text": text}
    return dict(result)


def _parse_body(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="replace")
    try:
        if "text/event-stream" in (content_type or "").lower() or text.lstrip().startswith(("event:", "data:")):
            for line in text.splitlines():
                if line.startswith("data:") and line[5:].strip():
                    return json.loads(line[5:].strip())
            return {}
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        raise BrokerUnavailable(f"{UNREACHABLE}: MCP response is not JSON") from exc


class McpTransport:
    """Callable: ``transport(tool, **arguments)`` → unwrapped tool result."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        client_name: str,
        timeout: float = 30.0,
        post: Post | None = None,
        refresh_token: Callable[[], str] | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.client_name = client_name
        self.timeout = timeout
        self._post = post or self._http_post
        self._refresh = refresh_token
        self.session_id: str | None = None
        self._initialized = False
        self._next_id = 0

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

    def _http_post(self, body: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        req = Request(self.url, data=json.dumps(body).encode("utf-8"), headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return int(resp.status), {str(k): str(v) for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            headers = {str(k): str(v) for k, v in (exc.headers.items() if exc.headers else [])}
            return int(exc.code), headers, exc.read() or b""
        except TimeoutError:
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower():
                raise TimeoutError(str(exc.reason)) from exc
            raise BrokerUnavailable(f"{UNREACHABLE}: HTTP transport failed: {exc.reason}") from exc

    def _rpc(self, method: str, params: Mapping[str, Any] | None = None, *, notification: bool = False, retried: bool = False) -> Any:
        self._next_id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self._next_id
        if params is not None:
            body["params"] = dict(params)
        status, headers, raw = self._post(body)
        session = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id")
        if session:
            self.session_id = session
        if status == 401 and not retried and self._try_refresh():
            if method != "initialize":
                self.initialize()
            return self._rpc(method, params, notification=notification, retried=True)
        if status >= 400:
            raise BrokerUnavailable(f"{UNREACHABLE}: MCP HTTP {status} for {method}", code=f"HTTP_{status}")
        if notification:
            return None
        parsed = _parse_body(raw, headers.get("Content-Type") or headers.get("content-type") or "")
        if method == "tools/call":
            return unwrap_tool_result(parsed)
        if isinstance(parsed, Mapping) and parsed.get("error"):
            raise BrokerUnavailable(f"{UNREACHABLE}: {parsed['error']}")
        return parsed

    def _try_refresh(self) -> bool:
        if self._refresh is None:
            return False
        try:
            token = self._refresh()
        except Exception:  # noqa: BLE001
            return False
        if not token:
            return False
        self._token = token
        self.session_id = None
        self._initialized = False
        return True

    def initialize(self) -> None:
        self._rpc(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": self.client_name, "version": "2.0.0"}},
        )
        self._rpc("notifications/initialized", {}, notification=True)
        self._initialized = True

    def __call__(self, tool: str, **arguments: Any) -> Any:
        if not self._initialized:
            self.initialize()
        args = {k: v for k, v in arguments.items() if v is not None}
        return self._rpc("tools/call", {"name": tool, "arguments": args})


def connect(*, client_name: str, environ: Mapping[str, str] | None = None) -> McpTransport:
    """Transport using the persisted OAuth session of the current OS user."""
    env = environ if environ is not None else os.environ
    token, error = load_or_refresh_access_token(environ=env)
    if not token:
        raise BrokerUnavailable(error or f"{UNREACHABLE}: no Robinhood MCP session ({LOGIN_HINT})")

    def refresh() -> str:
        access, err = load_or_refresh_access_token(environ=env, force_refresh=True)
        if not access:
            raise BrokerUnavailable(err or f"{UNREACHABLE}: token refresh failed")
        return access

    url = str(env.get("AGENTIC_READONLY_MCP_URL") or DEFAULT_MCP_URL)
    transport = McpTransport(url, token, client_name=client_name, refresh_token=refresh)
    transport.initialize()
    return transport
