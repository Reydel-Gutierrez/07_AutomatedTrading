"""Standalone Robinhood Trading MCP OAuth for the local Python runtime.

Cursor authenticates the same MCP URL in its own host credential store.
That session is not a supported credential for this process: each MCP host
completes OAuth separately. This module implements the public-client flow
Robinhood advertises for the endpoint:

  OAuth 2.1 authorization code + PKCE (S256)
  dynamic client registration (no client secret)
  loopback redirect on 127.0.0.1
  tokens persisted outside the repo

It never asks for a Robinhood password and never calls place/review/cancel.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.request import Request

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
from agentic_portfolio.adapters.robinhood_read import READONLY_MCP_UNREACHABLE
from agentic_portfolio.paths import project_root

DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"
DEFAULT_LOOPBACK_PORT = 33418
OAUTH_FILENAME = "oauth.json"
LOGIN_HINT = "run python scripts/login_readonly_mcp.py, or set AGENTIC_READONLY_MCP_TOKEN"
USER_AGENT = "agentic-portfolio-readonly/1.0"
SKEW_SECONDS = 60.0
CALLBACK_TIMEOUT_SECONDS = 300.0

PROTECTED_RESOURCE_CANDIDATES = (
    "https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading",
    "https://agent.robinhood.com/.well-known/oauth-protected-resource",
)
AUTHORIZATION_SERVER_CANDIDATES = (
    "https://agent.robinhood.com/.well-known/oauth-authorization-server/mcp/trading",
    "https://agent.robinhood.com/.well-known/oauth-authorization-server",
)

HttpFn = Callable[..., tuple[int, dict[str, str], Any]]
OpenBrowser = Callable[[str], None]


@dataclass
class OAuthMetadata:
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes: list[str] = field(default_factory=lambda: ["internal"])


@dataclass
class OAuthSessionStatus:
    present: bool
    store_path: Path
    expires_at: float | None = None
    expired: bool = False
    has_refresh_token: bool = False
    error: str | None = None


def _truthy(environ: Mapping[str, str], names: list[str]) -> str:
    for name in names:
        value = str(environ.get(name) or "").strip()
        if value:
            return value
    return ""


def oauth_home(*, environ: Mapping[str, str] | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = _truthy(env, ["AGENTIC_READONLY_MCP_HOME"])
    if override:
        return Path(override)
    # LOCALAPPDATA is Windows-only. systemd on the Pi must not follow a stray
    # Windows path if that variable is accidentally exported.
    if os.name == "nt":
        local = str(env.get("LOCALAPPDATA") or "").strip()
        if local:
            return Path(local) / "agentic-portfolio" / "readonly-mcp"
    xdg = str(env.get("XDG_STATE_HOME") or "").strip()
    if xdg:
        return Path(xdg) / "agentic-portfolio" / "readonly-mcp"
    home = str(env.get("HOME") or "").strip()
    if home:
        return Path(home) / ".agentic-portfolio" / "readonly-mcp"
    return Path.home() / ".agentic-portfolio" / "readonly-mcp"


def oauth_store_path(*, environ: Mapping[str, str] | None = None) -> Path:
    return oauth_home(environ=environ) / OAUTH_FILENAME


def path_is_inside_repo(path: Path, *, root: Path | None = None) -> bool:
    try:
        resolved = path.resolve()
        repo = (root or project_root()).resolve()
    except OSError:
        return False
    return resolved == repo or repo in resolved.parents


def _refuse_repo_store(path: Path) -> str | None:
    if path_is_inside_repo(path) or path_is_inside_repo(path.parent):
        return (
            f"{READONLY_MCP_UNREACHABLE}: refusing to read or write Robinhood MCP "
            f"credentials inside the repository ({path})"
        )
    return None


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _default_http(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Mapping[str, Any] | None = None,
    form: Mapping[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict[str, str], Any]:
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        hdrs.update({str(k): str(v) for k, v in headers.items()})
    data: bytes | None = None
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode("utf-8")
    elif json_body is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(dict(json_body)).encode("utf-8")
    req = Request(url, data=data, headers=hdrs, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read() or b""
            out_headers = {str(k): str(v) for k, v in resp.headers.items()}
            return int(resp.status), out_headers, _decode_body(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        out_headers = {str(k): str(v) for k, v in (exc.headers.items() if exc.headers else [])}
        return int(exc.code), out_headers, _decode_body(raw)
    except urllib.error.URLError as exc:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: OAuth HTTP failed: {exc.reason}") from exc


def _decode_body(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def parse_resource_metadata_url(www_authenticate: str) -> str | None:
    match = re.search(r'resource_metadata="([^"]+)"', www_authenticate or "", re.I)
    if match:
        return match.group(1).strip()
    return None


def _as_dict(payload: Any) -> dict[str, Any]:
    return dict(payload) if isinstance(payload, Mapping) else {}


def discover_oauth_metadata(
    mcp_url: str,
    *,
    http: HttpFn | None = None,
) -> OAuthMetadata:
    """RFC 9728 + RFC 8414 discovery for the Robinhood Trading MCP resource."""
    post = http or _default_http
    resource = mcp_url.rstrip("/")
    resource_meta: dict[str, Any] = {}
    try:
        status, headers, _body = post(
            "POST",
            resource,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json_body={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        if status == 401:
            meta_url = parse_resource_metadata_url(headers.get("WWW-Authenticate") or headers.get("www-authenticate") or "")
            if meta_url:
                _st, _h, resource_meta = post("GET", meta_url)
                resource_meta = _as_dict(resource_meta)
    except LiveDataUnavailable:
        resource_meta = {}
    if not resource_meta:
        for url in PROTECTED_RESOURCE_CANDIDATES:
            status, _h, payload = post("GET", url)
            if status == 200 and isinstance(payload, Mapping) and payload.get("authorization_servers"):
                resource_meta = dict(payload)
                break
    if not resource_meta:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: OAuth protected-resource metadata is unavailable")
    servers = [str(s) for s in (resource_meta.get("authorization_servers") or []) if str(s).strip()]
    resource = str(resource_meta.get("resource") or resource).rstrip("/")
    scopes = [str(s) for s in (resource_meta.get("scopes_supported") or ["internal"])]
    as_meta: dict[str, Any] = {}
    for candidate in list(servers) + list(AUTHORIZATION_SERVER_CANDIDATES):
        urls = []
        if candidate.startswith("http") and "/.well-known/" in candidate:
            urls.append(candidate)
        elif candidate.startswith("http"):
            parsed = urllib.parse.urlparse(candidate)
            if parsed.path and parsed.path not in {"", "/"}:
                urls.append(f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}")
            urls.append(f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server")
        for url in urls:
            status, _h, payload = post("GET", url)
            if status == 200 and isinstance(payload, Mapping) and payload.get("authorization_endpoint") and payload.get("token_endpoint"):
                as_meta = dict(payload)
                break
        if as_meta:
            break
    if not as_meta:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: OAuth authorization-server metadata is unavailable")
    registration = str(as_meta.get("registration_endpoint") or "").strip()
    if not registration:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: OAuth registration_endpoint is missing")
    return OAuthMetadata(
        resource=resource,
        issuer=str(as_meta.get("issuer") or (servers[0] if servers else resource)),
        authorization_endpoint=str(as_meta["authorization_endpoint"]),
        token_endpoint=str(as_meta["token_endpoint"]),
        registration_endpoint=registration,
        scopes=scopes or ["internal"],
    )


def _restrict_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def load_oauth_store(*, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    path = oauth_store_path(environ=environ)
    refused = _refuse_repo_store(path)
    if refused:
        raise LiveDataUnavailable(refused)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: cannot read OAuth store: {exc}") from exc
    return dict(data) if isinstance(data, Mapping) else {}


def save_oauth_store(payload: Mapping[str, Any], *, environ: Mapping[str, str] | None = None) -> Path:
    path = oauth_store_path(environ=environ)
    refused = _refuse_repo_store(path)
    if refused:
        raise LiveDataUnavailable(refused)
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(dict(payload), indent=2, sort_keys=True)
    path.write_text(serialized, encoding="utf-8")
    _restrict_file(path)
    return path


def oauth_session_status(*, environ: Mapping[str, str] | None = None, now: float | None = None) -> OAuthSessionStatus:
    path = oauth_store_path(environ=environ)
    refused = _refuse_repo_store(path)
    if refused:
        return OAuthSessionStatus(present=False, store_path=path, error=refused)
    if not path.is_file():
        return OAuthSessionStatus(present=False, store_path=path)
    try:
        data = load_oauth_store(environ=environ)
    except LiveDataUnavailable as exc:
        return OAuthSessionStatus(present=False, store_path=path, error=str(exc))
    tokens = dict(data.get("tokens") or {})
    access = str(tokens.get("access_token") or "").strip()
    expires_at = tokens.get("expires_at")
    try:
        expiry = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expiry = None
    stamp = time.time() if now is None else float(now)
    expired = bool(expiry is not None and expiry <= stamp + SKEW_SECONDS)
    return OAuthSessionStatus(
        present=bool(access),
        store_path=path,
        expires_at=expiry,
        expired=expired,
        has_refresh_token=bool(str(tokens.get("refresh_token") or "").strip()),
    )


def _token_payload(body: Mapping[str, Any], *, now: float) -> dict[str, Any]:
    access = str(body.get("access_token") or "").strip()
    if not access:
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: token endpoint returned no access_token")
    expires_in = body.get("expires_in")
    expires_at = None
    try:
        if expires_in is not None:
            expires_at = now + float(expires_in)
    except (TypeError, ValueError):
        expires_at = None
    return {
        "access_token": access,
        "refresh_token": str(body.get("refresh_token") or "").strip() or None,
        "token_type": str(body.get("token_type") or "Bearer"),
        "expires_in": expires_in,
        "expires_at": expires_at,
        "scope": body.get("scope"),
    }


def _register_client(
    metadata: OAuthMetadata,
    redirect_uri: str,
    *,
    http: HttpFn,
) -> dict[str, Any]:
    status, _h, body = http(
        "POST",
        metadata.registration_endpoint,
        json_body={
            "client_name": "agentic-portfolio-readonly",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "application_type": "native",
            "scope": " ".join(metadata.scopes or ["internal"]),
        },
    )
    payload = _as_dict(body)
    if status >= 400 or not str(payload.get("client_id") or "").strip():
        message = payload.get("error_description") or payload.get("error") or f"HTTP {status}"
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: dynamic client registration failed: {message}")
    return {
        "client_id": str(payload["client_id"]).strip(),
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",
    }


def authorization_url(
    metadata: OAuthMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "scope": " ".join(metadata.scopes or ["internal"]),
        "resource": metadata.resource,
    }
    return f"{metadata.authorization_endpoint}?{urllib.parse.urlencode(params)}"


def exchange_authorization_code(
    metadata: OAuthMetadata,
    *,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    http: HttpFn | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    post = http or _default_http
    status, _h, body = post(
        "POST",
        metadata.token_endpoint,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": code_verifier,
            "resource": metadata.resource,
        },
    )
    payload = _as_dict(body)
    if status >= 400 or not payload.get("access_token"):
        message = payload.get("error_description") or payload.get("error") or f"HTTP {status}"
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: authorization-code exchange failed: {message}")
    return _token_payload(payload, now=time.time() if now is None else now)


def refresh_access_token(
    metadata: OAuthMetadata,
    *,
    client_id: str,
    refresh_token: str,
    http: HttpFn | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    post = http or _default_http
    status, _h, body = post(
        "POST",
        metadata.token_endpoint,
        form={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "resource": metadata.resource,
        },
    )
    payload = _as_dict(body)
    if status >= 400 or not payload.get("access_token"):
        message = payload.get("error_description") or payload.get("error") or f"HTTP {status}"
        raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: refresh_token grant failed: {message}")
    tokens = _token_payload(payload, now=time.time() if now is None else now)
    if not tokens.get("refresh_token"):
        tokens["refresh_token"] = refresh_token
    return tokens


def load_or_refresh_access_token(
    *,
    environ: Mapping[str, str] | None = None,
    http: HttpFn | None = None,
    now: float | None = None,
    force_refresh: bool = False,
) -> tuple[str, str | None]:
    path = oauth_store_path(environ=environ)
    refused = _refuse_repo_store(path)
    if refused:
        return "", refused
    if not path.is_file():
        return "", None
    try:
        data = load_oauth_store(environ=environ)
    except LiveDataUnavailable as exc:
        return "", str(exc)
    tokens = dict(data.get("tokens") or {})
    access = str(tokens.get("access_token") or "").strip()
    refresh = str(tokens.get("refresh_token") or "").strip()
    client = dict(data.get("client") or {})
    client_id = str(client.get("client_id") or "").strip()
    stamp = time.time() if now is None else float(now)
    expires_at = tokens.get("expires_at")
    try:
        expiry = float(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expiry = None
    fresh = bool(access) and not force_refresh and (expiry is None or expiry > stamp + SKEW_SECONDS)
    if fresh:
        return access, None
    if not refresh or not client_id:
        return "", f"{READONLY_MCP_UNREACHABLE}: stored Robinhood MCP OAuth session is expired; {LOGIN_HINT}"
    authz = dict(data.get("authorization_server") or {})
    token_endpoint = str(authz.get("token_endpoint") or "").strip()
    resource = str(data.get("resource") or data.get("endpoint") or DEFAULT_MCP_URL).rstrip("/")
    if not token_endpoint:
        try:
            discovered = discover_oauth_metadata(resource, http=http)
        except LiveDataUnavailable as exc:
            return "", str(exc)
        token_endpoint = discovered.token_endpoint
        metadata = discovered
    else:
        metadata = OAuthMetadata(
            resource=resource,
            issuer=str(authz.get("issuer") or resource),
            authorization_endpoint=str(authz.get("authorization_endpoint") or ""),
            token_endpoint=token_endpoint,
            registration_endpoint=str(authz.get("registration_endpoint") or ""),
            scopes=["internal"],
        )
    try:
        updated = refresh_access_token(metadata, client_id=client_id, refresh_token=refresh, http=http, now=stamp)
    except LiveDataUnavailable as exc:
        return "", str(exc)
    data["tokens"] = updated
    save_oauth_store(data, environ=environ)
    return str(updated["access_token"]), None


def _pick_loopback_port(preferred: int = DEFAULT_LOOPBACK_PORT) -> tuple[HTTPServer, int]:
    try:
        server = HTTPServer(("127.0.0.1", preferred), _CallbackHandler)
        return server, preferred
    except OSError:
        server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
        return server, int(server.server_address[1])


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict[str, str] | None = None
    expected_state: str = ""

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in {"/callback", "/"}:
            self.send_response(404)
            self.end_headers()
            return
        query = urllib.parse.parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        error = (query.get("error") or [""])[0]
        if state != self.expected_state:
            self._html(400, "Authorization failed: state mismatch. You can close this tab.")
            _CallbackHandler.result = {"error": "state_mismatch"}
            return
        if error:
            self._html(400, "Authorization failed. You can close this tab.")
            _CallbackHandler.result = {"error": error}
            return
        if not code:
            self._html(400, "Authorization failed: missing code. You can close this tab.")
            _CallbackHandler.result = {"error": "missing_code"}
            return
        self._html(200, "Authorized. You can close this tab and return to PowerShell.")
        _CallbackHandler.result = {"code": code}

    def _html(self, status: int, message: str) -> None:
        body = f"<!doctype html><html><body><p>{message}</p></body></html>".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def wait_for_authorization_code(
    server: HTTPServer,
    *,
    state: str,
    timeout: float = CALLBACK_TIMEOUT_SECONDS,
) -> str:
    _CallbackHandler.result = None
    _CallbackHandler.expected_state = state
    server.timeout = 1.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        result = _CallbackHandler.result
        if result:
            if result.get("code"):
                return result["code"]
            raise LiveDataUnavailable(
                f"{READONLY_MCP_UNREACHABLE}: OAuth callback error: {result.get('error') or 'unknown'}"
            )
    raise LiveDataUnavailable(f"{READONLY_MCP_UNREACHABLE}: OAuth callback timed out after {int(timeout)}s")


def _open_browser(url: str) -> None:
    import webbrowser

    webbrowser.open(url, new=1, autoraise=True)


def run_oauth_login(
    *,
    mcp_url: str = DEFAULT_MCP_URL,
    environ: Mapping[str, str] | None = None,
    http: HttpFn | None = None,
    open_browser: OpenBrowser | None = None,
    wait_for_code: Callable[[HTTPServer, str], str] | None = None,
    bind_server: Callable[[], tuple[HTTPServer, int]] | None = None,
    now: float | None = None,
) -> Path:
    """Complete Robinhood MCP OAuth and persist tokens outside the repo."""
    env = environ if environ is not None else os.environ
    post = http or _default_http
    metadata = discover_oauth_metadata(mcp_url, http=post)
    server, port = (bind_server or _pick_loopback_port)()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    try:
        client = _register_client(metadata, redirect_uri, http=post)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        url = authorization_url(
            metadata,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=challenge,
        )
        opener = open_browser or _open_browser
        opener(url)
        waiter = wait_for_code or (lambda srv, st: wait_for_authorization_code(srv, state=st))
        code = waiter(server, state)
        tokens = exchange_authorization_code(
            metadata,
            client_id=client["client_id"],
            redirect_uri=redirect_uri,
            code=code,
            code_verifier=verifier,
            http=post,
            now=now,
        )
    finally:
        try:
            server.server_close()
        except OSError:
            pass
    payload = {
        "endpoint": mcp_url.rstrip("/"),
        "resource": metadata.resource,
        "mode": "READ_ONLY",
        "client": client,
        "authorization_server": {
            "issuer": metadata.issuer,
            "authorization_endpoint": metadata.authorization_endpoint,
            "token_endpoint": metadata.token_endpoint,
            "registration_endpoint": metadata.registration_endpoint,
        },
        "tokens": tokens,
    }
    return save_oauth_store(payload, environ=env)
