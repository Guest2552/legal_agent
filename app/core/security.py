"""Security middleware (pure ASGI, so streaming responses are never buffered).

* :class:`SecurityHeadersMiddleware` - strict CSP, anti-framing, no-sniff, no referrer,
  microphone limited to this origin, HSTS on HTTPS, ``no-store`` for API responses.
* :class:`OriginCheckMiddleware` - rejects cross-site state-changing requests (CSRF
  defence in depth on top of ``SameSite=Strict`` cookies).
* :class:`BodySizeLimitMiddleware` - caps request bodies before they are parsed.
* :class:`SessionMiddleware` - issues an opaque, HttpOnly session cookie; unknown tokens
  are replaced by fresh server-generated ones (prevents session fixation) and only a
  SHA-256 hash of the token is stored in the database.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.db.repositories import SessionRepository

APP_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
# Swagger UI (/docs) loads its assets from jsDelivr and uses an inline bootstrap script.
DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https:; "
    "frame-ancestors 'none'; object-src 'none'"
)
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect"})

STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "microphone=(self), camera=(), geolocation=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}

SESSION_COOKIE = "lexiguide_sid"
_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_https(scope: Scope) -> bool:
    if scope.get("scheme") == "https":
        return True
    return Headers(scope=scope).get("x-forwarded-proto", "").split(",")[0].strip() == "https"


async def _send_json(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        https = _is_https(scope)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in STATIC_HEADERS.items():
                    headers.setdefault(name, value)
                headers.setdefault("Content-Security-Policy", DOCS_CSP if path in DOCS_PATHS else APP_CSP)
                if path.startswith("/api/"):
                    headers.setdefault("Cache-Control", "no-store")
                elif path.startswith("/static/"):
                    # Revalidate on each load (cheap 304 via ETag) so updates are never served stale.
                    headers.setdefault("Cache-Control", "no-cache")
                if https:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in _UNSAFE_METHODS:
            headers = Headers(scope=scope)
            if headers.get("sec-fetch-site") == "cross-site":
                await _send_json(send, 403, "forbidden_origin", "Cross-site requests are not allowed.")
                return
            origin = headers.get("origin")
            host = headers.get("x-forwarded-host") or headers.get("host")
            if origin and origin != "null" and urlsplit(origin).netloc != host:
                await _send_json(send, 403, "forbidden_origin", "Cross-origin requests are not allowed.")
                return
        await self.app(scope, receive, send)


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            await _send_json(send, 413, "payload_too_large", "The upload is too large.")
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            response_started = response_started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLargeError:
            if not response_started:
                await _send_json(send, 413, "payload_too_large", "The upload is too large.")


def session_key(token: str) -> str:
    """Database key for a cookie token: a stolen database cannot be replayed as cookies."""
    return hashlib.sha256(token.encode()).hexdigest()


class SessionMiddleware:
    """Anonymous, persistent sessions (chat history and documents live under them)."""

    PURGE_INTERVAL_S = 3600

    def __init__(self, app: ASGIApp, sessions: SessionRepository, max_age: int, secure: bool | None) -> None:
        self.app = app
        self.sessions = sessions
        self.max_age = max_age
        self.secure = secure
        self._last_purge = 0.0

    def _read_token(self, scope: Scope) -> str | None:
        raw = Headers(scope=scope).get("cookie")
        if not raw:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw)
        except CookieError:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        value = morsel.value if morsel else None
        return value if value and _SESSION_TOKEN_RE.match(value) else None

    async def _purge_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_purge >= self.PURGE_INTERVAL_S:
            self._last_purge = now
            await self.sessions.purge_expired()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        await self._purge_if_due()
        token = self._read_token(scope)
        status = await self.sessions.touch(session_key(token)) if token else "missing"
        if token is None or status == "missing":
            # Unknown or expired: always issue a fresh server-generated token (no session fixation).
            token = secrets.token_urlsafe(32)
            await self.sessions.create(session_key(token))
            status = "refreshed"
        scope.setdefault("state", {})["session_id"] = session_key(token)

        secure = _is_https(scope) if self.secure is None else self.secure
        cookie = f"{SESSION_COOKIE}={token}; Path=/api; HttpOnly; SameSite=Strict; Max-Age={self.max_age}" + (
            "; Secure" if secure else ""
        )

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and status == "refreshed":
                MutableHeaders(scope=message).append("Set-Cookie", cookie)  # sliding expiry
            await send(message)

        await self.app(scope, receive, send_with_cookie)
