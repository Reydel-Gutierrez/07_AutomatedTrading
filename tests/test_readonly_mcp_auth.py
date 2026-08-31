"""Standalone Robinhood MCP OAuth. Tokens never enter the repo."""

from __future__ import annotations

import ast
import inspect
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from agentic_portfolio.adapters.portfolio_facts import LiveDataUnavailable
from agentic_portfolio.adapters.readonly_mcp_auth import (
    DEFAULT_MCP_URL,
    LOGIN_HINT,
    authorization_url,
    discover_oauth_metadata,
    load_or_refresh_access_token,
    oauth_store_path,
    path_is_inside_repo,
    pkce_pair,
    run_oauth_login,
    save_oauth_store,
)
from agentic_portfolio.adapters.readonly_runtime import (
    StreamableHttpMcpTransport,
    bootstrap_readonly_broker_runtime,
)
from agentic_portfolio.paths import project_root
from scripts.check_live_candidate_facts import main as check_main
from scripts.login_readonly_mcp import main as login_main
from tests.test_ai_gateway import NOW

RESOURCE_META = {
    "authorization_servers": ["https://agent.robinhood.com/mcp/trading"],
    "resource": DEFAULT_MCP_URL,
    "scopes_supported": ["internal"],
}
AS_META = {
    "issuer": DEFAULT_MCP_URL,
    "authorization_endpoint": "https://robinhood.com/oauth",
    "token_endpoint": "https://api.robinhood.com/oauth2/token/",
    "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
    "scopes_supported": ["internal"],
}


class DummyServer:
    def server_close(self) -> None:
        return


def _http(calls: list):
    def http(method, url, *, headers=None, json_body=None, form=None, timeout=30.0):
        calls.append({"method": method, "url": url, "json_body": json_body, "form": form, "headers": headers or {}})
        if json_body and json_body.get("method") == "initialize":
            return (
                401,
                {"WWW-Authenticate": 'Bearer resource_metadata="https://agent.robinhood.com/.well-known/oauth-protected-resource/mcp/trading"'},
                {},
            )
        if "oauth-protected-resource" in url:
            return 200, {}, RESOURCE_META
        if "oauth-authorization-server" in url:
            return 200, {}, AS_META
        if url.endswith("/register") or url.endswith("/register/"):
            assert json_body["token_endpoint_auth_method"] == "none"
            assert "client_secret" not in json_body
            return 201, {}, {"client_id": "public-client-1"}
        if "oauth2/token" in url:
            grant = (form or {}).get("grant_type")
            if grant == "authorization_code":
                assert (form or {}).get("code_verifier")
                assert (form or {}).get("code") == "auth-code-1"
                return 200, {}, {"access_token": "access-live", "refresh_token": "refresh-live", "expires_in": 3600, "token_type": "Bearer"}
            if grant == "refresh_token":
                return 200, {}, {"access_token": "access-refreshed", "refresh_token": "refresh-2", "expires_in": 3600, "token_type": "Bearer"}
            raise AssertionError(grant)
        raise AssertionError(url)

    return http


def test_pkce_is_s256_and_authorization_url_is_public_client():
    verifier, challenge = pkce_pair()
    assert verifier != challenge
    from agentic_portfolio.adapters.readonly_mcp_auth import OAuthMetadata

    url = authorization_url(
        OAuthMetadata(
            resource=DEFAULT_MCP_URL,
            issuer=DEFAULT_MCP_URL,
            authorization_endpoint="https://robinhood.com/oauth",
            token_endpoint="https://api.robinhood.com/oauth2/token/",
            registration_endpoint="https://agent.robinhood.com/oauth/trading/register",
        ),
        client_id="cid",
        redirect_uri="http://127.0.0.1:33418/callback",
        state="st",
        code_challenge=challenge,
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "robinhood.com"
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [challenge]
    assert params["resource"] == [DEFAULT_MCP_URL]
    assert "client_secret" not in params
    assert params["redirect_uri"][0].startswith("http://127.0.0.1:")


def test_discover_oauth_metadata_from_401_challenge():
    calls: list = []
    meta = discover_oauth_metadata(DEFAULT_MCP_URL, http=_http(calls))
    assert meta.authorization_endpoint == "https://robinhood.com/oauth"
    assert meta.token_endpoint == "https://api.robinhood.com/oauth2/token/"
    assert meta.registration_endpoint.endswith("/oauth/trading/register")
    assert meta.scopes == ["internal"]


def test_login_persists_outside_repo_and_bootstrap_uses_store(monkeypatch):
    calls: list = []
    opened: list[str] = []

    path = run_oauth_login(
        mcp_url=DEFAULT_MCP_URL,
        http=_http(calls),
        open_browser=opened.append,
        wait_for_code=lambda server, state: "auth-code-1",
        bind_server=lambda: (DummyServer(), 33418),
        now=time.time(),
    )
    assert opened
    assert "code_challenge_method=S256" in opened[0]
    assert path_is_inside_repo(path) is False
    assert path == oauth_store_path()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] == "access-live"
    assert stored["client"]["client_id"] == "public-client-1"
    assert stored["mode"] == "READ_ONLY"
    for call in calls:
        blob = json.dumps(call)
        assert "place_equity_order" not in blob
        assert "review_equity_order" not in blob

        monkeypatch.setattr(StreamableHttpMcpTransport, "initialize", lambda self: None)
        runtime = bootstrap_readonly_broker_runtime(force=True)
    assert runtime.bound is True
    assert runtime.mode == "READ_ONLY"
    assert runtime.initialization_error is None
    assert not hasattr(runtime.fetcher, "place_equity_order")


def test_expired_session_refreshes_without_env_token():
    save_oauth_store(
        {
            "endpoint": DEFAULT_MCP_URL,
            "resource": DEFAULT_MCP_URL,
            "client": {"client_id": "public-client-1"},
            "authorization_server": {"token_endpoint": "https://api.robinhood.com/oauth2/token/"},
            "tokens": {
                "access_token": "old-access",
                "refresh_token": "refresh-live",
                "expires_at": 10.0,
            },
        }
    )
    calls: list = []
    access, err = load_or_refresh_access_token(http=_http(calls), now=1_000_000.0)
    assert err is None
    assert access == "access-refreshed"
    stored = json.loads(oauth_store_path().read_text(encoding="utf-8"))
    assert stored["tokens"]["access_token"] == "access-refreshed"


def test_oauth_store_refuses_repository_path(monkeypatch):
    monkeypatch.setenv("AGENTIC_READONLY_MCP_HOME", str(project_root() / "state" / "mcp-auth"))
    with pytest.raises(LiveDataUnavailable, match="inside the repository"):
        save_oauth_store({"tokens": {"access_token": "must-not-write"}})
    runtime = bootstrap_readonly_broker_runtime(force=True)
    assert runtime.bound is False
    assert "repository" in (runtime.initialization_error or "")
    assert not (project_root() / "state" / "mcp-auth" / "oauth.json").exists()


def test_unauthenticated_cli_fails_closed_with_login_hint(capsys):
    runtime = bootstrap_readonly_broker_runtime(force=True)
    assert runtime.bound is False
    assert "AGENTIC_READONLY_MCP_TOKEN" in (runtime.initialization_error or "")
    assert "login_readonly_mcp.py" in (runtime.initialization_error or "")
    assert LOGIN_HINT.split(",")[0] in (runtime.initialization_error or "")
    code = check_main(["QUAL"], now=NOW)
    assert code == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readonly_broker_transport"]["bound"] is False
    assert report["eligible_for_ai"] is False
    assert report["ai_provider_called"] is False


def test_login_script_has_no_write_or_password_collection():
    src = (project_root() / "scripts" / "login_readonly_mcp.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden = {
        "place_equity_order",
        "cancel_equity_order",
        "review_equity_order",
        "place_option_order",
        "place_crypto_order",
        "input",
        "getpass",
    }
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in forbidden:
            hits.append(node.func.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in forbidden:
            hits.append(node.func.attr)
    assert hits == []
    assert "password" not in src.lower() or "never asks for it" in src
    assert inspect.getsource(login_main)
    assert "robinhood.com" in src
