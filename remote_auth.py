"""
Password login for the hosted eLearn connector.

Claude's custom connectors use OAuth. This file is a small OAuth server whose
only "account" is you: when you add the connector, Claude opens a page that
asks for ACCESS_PASSWORD, and after that Claude gets a token that keeps
working until it expires or you change the password.

Everything is stateless: client registrations, login codes and tokens are
signed strings, so they survive restarts (Cloud Run stops idle containers)
without a database. Changing ACCESS_PASSWORD (or SIGNING_KEY) signs you out
everywhere.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import threading
import time
from urllib.parse import urlparse

from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

ACCESS_TTL = 8 * 3600            # Claude refreshes this automatically
REFRESH_TTL = 120 * 86400        # sign in again after ~4 months of not using it
CODE_TTL = 300
LOGIN_PAGE_TTL = 15 * 60

# Only these sites may receive login codes. Claude's callback is
# https://claude.ai/api/mcp/auth_callback (and claude.com in future);
# localhost is for testing with the MCP Inspector.
DEFAULT_REDIRECT_HOSTS = "claude.ai,claude.com,localhost,127.0.0.1"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer:
    """Tamper-proof, expiring strings: base64(json).signature"""

    def __init__(self, key: bytes):
        self.key = key

    def _sig(self, raw: str) -> str:
        return _b64(hmac.new(self.key, raw.encode(), hashlib.sha256).digest()[:24])

    def dump(self, kind: str, data: dict, ttl: int | None = None) -> str:
        body = dict(data, k=kind)
        if ttl:
            body["x"] = int(time.time()) + ttl
        raw = _b64(json.dumps(body, separators=(",", ":")).encode())
        return f"{raw}.{self._sig(raw)}"

    def load(self, kind: str, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        raw, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, self._sig(raw)):
            return None
        try:
            body = json.loads(_unb64(raw))
        except Exception:
            return None
        if body.get("k") != kind:
            return None
        if "x" in body and body["x"] < time.time():
            return None
        return body

    def short_hash(self, text: str) -> str:
        return _b64(hmac.new(self.key, b"id:" + text.encode(), hashlib.sha256).digest()[:12])


class PasswordOAuthProvider:
    """Implements the MCP SDK's OAuthAuthorizationServerProvider protocol."""

    def __init__(self, password: str, public_url: str, key: bytes, redirect_hosts: set[str]):
        self.password = password
        self.public_url = public_url.rstrip("/")
        self.s = Signer(key)
        self.redirect_hosts = redirect_hosts
        self._fails: list[float] = []
        self._lock = threading.Lock()

    # ---- clients (Dynamic Client Registration) -------------------------

    def _client_secret(self, client_id: str) -> str:
        return hmac.new(self.s.key, b"secret:" + client_id.encode(), hashlib.sha256).hexdigest()

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in (client_info.redirect_uris or [])]
        for u in uris:
            host = (urlparse(u).hostname or "").lower()
            if host not in self.redirect_hosts:
                raise RegistrationError("invalid_redirect_uri",
                                        f"Redirect host '{host}' is not allowed on this server.")
        data = {"r": uris, "m": client_info.token_endpoint_auth_method or "client_secret_post",
                "n": client_info.client_name, "s": client_info.scope}
        cid = self.s.dump("client", data)
        # The SDK returns this same object to the client, so these become the issued credentials.
        client_info.client_id = cid
        if client_info.client_secret:
            client_info.client_secret = self._client_secret(cid)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        d = self.s.load("client", client_id)
        if not d:
            return None
        method = d.get("m") or "client_secret_post"
        return OAuthClientInformationFull(
            client_id=client_id,
            client_secret=None if method == "none" else self._client_secret(client_id),
            redirect_uris=d["r"],
            token_endpoint_auth_method=method,
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            client_name=d.get("n"),
            scope=d.get("s"),
        )

    # ---- authorization: send the browser to our password page ----------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        req = self.s.dump("login", {
            "c": self.s.short_hash(client.client_id or ""),
            "n": client.client_name or "an app",
            "r": str(params.redirect_uri),
            "e": params.redirect_uri_provided_explicitly,
            "ch": params.code_challenge,
            "sc": params.scopes or [],
            "st": params.state,
            "res": params.resource,
        }, ttl=LOGIN_PAGE_TTL)
        return f"{self.public_url}/login?req={req}"

    def check_password(self, attempt: str) -> tuple[bool, str]:
        """(ok, message). Locks for 15 minutes after 5 wrong tries in 15 minutes."""
        with self._lock:
            now = time.time()
            self._fails = [t for t in self._fails if now - t < 900]
            if len(self._fails) >= 5:
                wait = int(900 - (now - self._fails[0])) // 60 + 1
                return False, f"Too many wrong passwords. Try again in {wait} minutes."
            if hmac.compare_digest(attempt.encode(), self.password.encode()):
                self._fails.clear()
                return True, ""
            self._fails.append(now)
            return False, "Wrong password."

    def code_redirect(self, login: dict) -> str:
        code = self.s.dump("code", {k: login[k] for k in ("c", "r", "e", "ch", "sc", "res")},
                           ttl=CODE_TTL)
        return construct_redirect_uri(login["r"], code=code, state=login.get("st"))

    # ---- codes and tokens -------------------------------------------------

    async def load_authorization_code(self, client: OAuthClientInformationFull,
                                      authorization_code: str) -> AuthorizationCode | None:
        d = self.s.load("code", authorization_code)
        if not d or d["c"] != self.s.short_hash(client.client_id or ""):
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=d["sc"], expires_at=d["x"],
            client_id=client.client_id, code_challenge=d["ch"], redirect_uri=AnyUrl(d["r"]),
            redirect_uri_provided_explicitly=d["e"], resource=d.get("res"), subject="owner")

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        body = {"c": self.s.short_hash(client_id), "sc": scopes, "res": resource}
        return OAuthToken(
            access_token=self.s.dump("access", body, ttl=ACCESS_TTL),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            refresh_token=self.s.dump("refresh", body, ttl=REFRESH_TTL),
            scope=" ".join(scopes) or None,
        )

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        d = self.s.load("refresh", refresh_token)
        if not d or d["c"] != self.s.short_hash(client.client_id or ""):
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=d["sc"],
                            expires_at=d["x"], resource=d.get("res"), subject="owner")

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        d = self.s.load("access", token)
        if not d:
            return None
        return AccessToken(token=token, client_id=d["c"], scopes=d["sc"], expires_at=d["x"],
                           resource=d.get("res"), subject="owner")

    async def revoke_token(self, token) -> None:
        # Stateless tokens can't be revoked one by one. Change ACCESS_PASSWORD to sign out everywhere.
        return None


# ---- the password page ---------------------------------------------------

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ISB eLearn connector</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;background:#f4f4f2;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center}}
.card{{background:#fff;border-radius:14px;padding:28px;max-width:360px;width:90%;
box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:#555;font-size:14px;line-height:1.45}}
input{{width:100%;box-sizing:border-box;font-size:16px;padding:12px;border:1px solid #ccc;
border-radius:9px;margin:10px 0}}
button{{width:100%;font-size:16px;padding:12px;border:0;border-radius:9px;background:#1f3a5f;
color:#fff}} .err{{color:#b3261e;font-size:14px}}
</style></head><body><div class="card">
<h1>ISB eLearn connector</h1>
<p>{who} wants to read your eLearn courses. Enter your connector password to allow it.</p>
{error}
<form method="post" action="/login">
<input type="hidden" name="req" value="{req}">
<input type="password" name="password" placeholder="Connector password" autofocus required>
<button type="submit">Allow access</button>
</form></div></body></html>"""


def _page(req: str, who: str, error: str = "", status: int = 200) -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(PAGE.format(req=html.escape(req), who=html.escape(who), error=err),
                        status_code=status)


def setup(mcp_kwargs: dict, public_url: str, password: str, elearn_token: str) -> PasswordOAuthProvider:
    """Adds auth settings to the FastMCP constructor kwargs and returns the provider."""
    key_src = os.environ.get("SIGNING_KEY") or f"elearn-mcp|{password}|{elearn_token}"
    key = hashlib.sha256(key_src.encode()).digest()
    hosts = {h.strip().lower() for h in
             os.environ.get("ALLOWED_REDIRECT_HOSTS", DEFAULT_REDIRECT_HOSTS).split(",") if h.strip()}
    provider = PasswordOAuthProvider(password, public_url, key, hosts)
    mcp_kwargs["auth_server_provider"] = provider
    mcp_kwargs["auth"] = AuthSettings(
        issuer_url=public_url,
        resource_server_url=f"{public_url}/mcp",
        client_registration_options=ClientRegistrationOptions(enabled=True),
        validate_token_resource=False,
    )
    return provider


def add_routes(mcp, provider: PasswordOAuthProvider) -> None:
    @mcp.custom_route("/login", methods=["GET", "POST"])
    async def login(request: Request):
        if request.method == "GET":
            req = request.query_params.get("req", "")
            d = provider.s.load("login", req)
            if not d:
                return PlainTextResponse("This sign-in link has expired. Go back to Claude and "
                                         "click Connect again.", status_code=400)
            return _page(req, d.get("n") or "An app")
        form = await request.form()
        req = str(form.get("req", ""))
        d = provider.s.load("login", req)
        if not d:
            return PlainTextResponse("This sign-in link has expired. Go back to Claude and "
                                     "click Connect again.", status_code=400)
        ok, msg = provider.check_password(str(form.get("password", "")))
        if not ok:
            return _page(req, d.get("n") or "An app", msg, status=401)
        return RedirectResponse(provider.code_redirect(d), status_code=302)

    @mcp.custom_route("/", methods=["GET"])
    async def home(request: Request):
        return PlainTextResponse("ISB eLearn connector is running. Add "
                                 f"{provider.public_url}/mcp to Claude as a custom connector.")

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request: Request):
        return PlainTextResponse("ok")
