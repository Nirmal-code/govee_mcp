"""Single-owner OAuth for running the server over HTTP.

Clients (claude.ai, Claude Desktop, the mobile apps) register themselves, send the user
to /login, and get tokens once the owner types GOVEE_MCP_PASSWORD. Clients and tokens
are kept in a JSON file so connectors survive restarts.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "govee"
ACCESS_TTL = 60 * 60  # 1 hour
REFRESH_TTL = 60 * 60 * 24 * 60  # 60 days, renewed on every refresh
CODE_TTL = 5 * 60
PENDING_TTL = 10 * 60
MAX_FAILURES = 5  # wrong passwords allowed per FAILURE_WINDOW before /login locks
FAILURE_WINDOW = 15 * 60


class OwnerOAuthProvider:
    def __init__(self, public_url: str, password: str, state_file: Path):
        self.public_url = public_url.rstrip("/")
        self.resource = f"{self.public_url}/mcp"
        self._password = password
        self._file = state_file
        self._pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._failures: list[float] = []
        self._clients: dict[str, dict[str, Any]] = {}
        self._access: dict[str, dict[str, Any]] = {}
        self._refresh: dict[str, dict[str, Any]] = {}
        self._load()

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        data = json.loads(self._file.read_text())
        now = time.time()
        self._clients = data.get("clients", {})
        self._access = {k: v for k, v in data.get("access", {}).items() if v["expires_at"] > now}
        self._refresh = {k: v for k, v in data.get("refresh", {}).items() if v["expires_at"] > now}

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": self._clients, "access": self._access, "refresh": self._refresh}))
        os.chmod(tmp, 0o600)
        tmp.replace(self._file)

    # --- clients -----------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json", exclude_none=True)
        self._save()

    # --- authorization -----------------------------------------------------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", f"This server only issues tokens for {self.resource}")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        req = secrets.token_urlsafe(32)
        self._pending[req] = (client.client_id, params, now + PENDING_TTL)
        return f"{self.public_url}/login?req={req}"

    async def login(self, request: Request) -> Response:
        """GET shows the password form; POST checks it and sends the browser back to the client."""
        form = await request.form() if request.method == "POST" else {}
        req = str(form.get("req") or request.query_params.get("req") or "")
        pending = self._pending.get(req)
        if not pending or pending[2] < time.time():
            return _page("This sign-in link has expired. Start connecting again from your app.", status=400)
        client_id, params, _ = pending
        client = self._clients.get(client_id, {})
        who = html.escape(client.get("client_name") or "An app")
        dest = html.escape(urlparse(str(params.redirect_uri)).netloc)

        if request.method == "GET":
            return _form(req, who, dest)

        now = time.time()
        self._failures = [t for t in self._failures if t > now - FAILURE_WINDOW]
        if len(self._failures) >= MAX_FAILURES:
            return _page("Too many wrong passwords. Wait 15 minutes and try again.", status=429)
        if not hmac.compare_digest(str(form.get("password", "")).encode(), self._password.encode()):
            self._failures.append(now)
            return _form(req, who, dest, error="Wrong password.")

        del self._pending[req]
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=now + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject="owner",
        )
        return RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=302,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Authorization code already used")
        return self._issue(client.client_id)

    # --- tokens ------------------------------------------------------------

    def _issue(self, client_id: str) -> OAuthToken:
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._access[access] = {"client_id": client_id, "expires_at": now + ACCESS_TTL, "refresh": refresh}
        self._refresh[refresh] = {"client_id": client_id, "expires_at": now + REFRESH_TTL}
        self._save()
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=SCOPE, refresh_token=refresh)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        t = self._refresh.get(refresh_token)
        if not t or t["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=t["client_id"],
            scopes=[SCOPE],
            expires_at=t["expires_at"],
            resource=self.resource,
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        self._drop_refresh(refresh_token.token)
        return self._issue(client.client_id)

    async def load_access_token(self, token: str) -> AccessToken | None:
        t = self._access.get(token)
        if not t or t["expires_at"] < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=t["client_id"],
            scopes=[SCOPE],
            expires_at=t["expires_at"],
            resource=self.resource,
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            t = self._access.pop(token.token, None)
            if t:
                self._drop_refresh(t["refresh"])
        else:
            self._drop_refresh(token.token)
        self._save()

    def _drop_refresh(self, refresh: str) -> None:
        """Remove a refresh token and every access token issued alongside it."""
        self._refresh.pop(refresh, None)
        self._access = {k: v for k, v in self._access.items() if v["refresh"] != refresh}


def _form(req: str, who: str, dest: str, error: str = "") -> HTMLResponse:
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return _page(
        f"""<p><b>{who}</b> wants to control your Govee lights.<br>
        After you sign in you'll be sent back to <b>{dest}</b>.</p>
        {err}
        <form method="post" action="/login">
          <input type="hidden" name="req" value="{html.escape(req)}">
          <input type="password" name="password" placeholder="Password" autofocus required>
          <button type="submit">Allow</button>
        </form>""",
        status=401 if error else 200,
    )


def _page(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Govee MCP sign-in</title>
<style>
body{{font:16px system-ui,sans-serif;max-width:360px;margin:15vh auto;padding:0 16px;color:#222;background:#fafafa}}
input,button{{font:inherit;width:100%;box-sizing:border-box;padding:10px;margin-top:10px;border-radius:8px}}
input{{border:1px solid #bbb}} button{{border:0;background:#2563eb;color:#fff;cursor:pointer}}
.err{{color:#b91c1c}}
@media (prefers-color-scheme:dark){{body{{background:#111;color:#eee}} input{{background:#222;color:#eee;border-color:#444}}}}
</style></head><body><h2>Govee MCP</h2>{body}</body></html>""",
        status_code=status,
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )
