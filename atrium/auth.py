"""Password login: ``AuthMiddleware`` stands in front of every HTTP request and WebSocket.

``$ATRIUM_HOME/auth.json`` (mode 600, not in Settings) holds:

    password_hash       scrypt hash of the password (``atrium --set-password``)
    generated_password  the password made up on the first start, until you set your own
    secret              signs login cookies
    token               ``Authorization: Bearer <token>`` for scripts (``atrium --token``)

Logging in sets a signed cookie for 30 days, renewed while it is used. It survives restarts;
a new password (which also replaces the API token) logs every browser and script out (the password
hash is part of the cookies' signing key), also when it is set while the server runs. There is no
exception for requests from this machine: through a tunnel or a reverse proxy running here,
everyone comes from 127.0.0.1.

Wrong passwords are counted per source, so nobody can lock the owner out: a browser that logged in
here before is known by a device cookie (OWASP's "device cookies") and counted on its own; others
by their address, and all of them together against a higher limit that a known device doesn't hit.

``atrium --no-auth`` (scratch instances) switches the login off; the CLI allows it on a loopback
address only, and requests must then be addressed to localhost (no DNS rebinding). Either way,
browser requests that can change something (WebSockets, POST…) must come from Atrium's own pages.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import secrets
import time
from collections import deque
from pathlib import Path
from string import Template
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from .config import ATRIUM_HOME, atomic_write

log = logging.getLogger("atrium.auth")

AUTH_PATH = ATRIUM_HOME / "auth.json"
MAX_AGE = 30 * 86400  # a login lasts 30 days from its last use...
RENEW_AFTER = 86400  # ...renewed at most once a day
MAX_FAILURES, FAILURE_WINDOW = 10, 600  # wrong passwords from one source, then it is refused for the rest of the window
MAX_UNKNOWN_FAILURES = 100  # from all sources that aren't a known device together (same window)
MAX_SOURCES = 10_000  # failure records kept, the oldest dropped first
DEVICE_AGE = 365 * 86400  # the device cookie: kept when logging out
CHECKS_AT_ONCE = 2  # scrypt takes 32 MiB a check
MAX_FORM_BYTES = 4096
SCRYPT = (2**15, 8, 1)  # n, r, p: 32 MiB and some 40 ms per check
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
OPEN = {("GET", "/login"), ("POST", "/login"), ("POST", "/logout")}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(password: str) -> str:
    n, r, p = SCRYPT
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * r * n)
    return f"scrypt${n}${r}${p}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, dk = stored.split("$")
        want = _unb64(dk)
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p), maxmem=256 * int(r) * int(n), dklen=len(want))
    except ValueError:  # a malformed hash (bad base64 is a ValueError too)
        return False
    return hmac.compare_digest(got, want)


def generate_password() -> str:
    """Four groups of four (80 bits), without look-alikes (l/1, o/0)."""
    alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))


class TooManyFailures(Exception):
    def __init__(self, wait: float):
        super().__init__(f"too many wrong passwords, try again in {wait:.0f} s")
        self.wait = wait


class Auth:
    def __init__(self, path: Path = AUTH_PATH):
        self.path = path
        # set by ``atrium --no-auth``; dropped, so the agents this server starts don't inherit it
        self.enabled = os.environ.pop("ATRIUM_NO_AUTH", "") != "1"
        self._data: dict[str, Any] = {}
        self._mtime: int | None = None
        self.error: str | None = None  # auth.json can't be read
        self._failures: dict[str, deque[float]] = {}  # per source: a known device, else the client's address
        self._unknown: deque[float] = deque()  # every source that isn't a known device
        self._checking = asyncio.Semaphore(CHECKS_AT_ONCE)

    # ---- auth.json -----------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        """auth.json, re-read when it changes (``atrium --set-password`` while the server runs)."""
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._data, self._mtime, self.error = {}, None, None
            return self._data
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                data = json.loads(self.path.read_text())
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                self.error = str(e)
                log.error("can't read %s: %s", self.path, e)
                return self._data  # the last good one
            self._data, self.error = data, None
            self._failures.clear()  # a new password starts with a clean slate
            self._unknown.clear()
        return self._data

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write(self.path, json.dumps(data, indent=2) + "\n", mode=0o600)
        self._data, self._mtime, self.error = data, self.path.stat().st_mtime_ns, None

    def ensure(self) -> dict[str, Any]:
        """Create auth.json or fill in what it lacks; a password is made up when there is none."""
        data = dict(self._load())
        if self.error:  # don't replace the password with a made-up one
            raise RuntimeError(f"can't read {self.path} ({self.error}): fix it, or set a new password with atrium --set-password")
        add: dict[str, str] = {}
        if not data.get("secret"):
            add["secret"] = secrets.token_hex(32)
        if not data.get("token"):
            add["token"] = secrets.token_urlsafe(32)
        if not data.get("password_hash"):
            password = generate_password()
            add.update(password_hash=hash_password(password), generated_password=password)
        if add:
            self._write({**data, **add})
        return self._data

    def set_password(self, password: str) -> None:
        """A new password and a new API token: browsers and scripts logged in before have to log in again."""
        data = {k: v for k, v in self._load().items() if k != "generated_password"}
        self._write({**data, "password_hash": hash_password(password), "token": secrets.token_urlsafe(32)})
        self.ensure()  # on a first run: the secret

    # ---- credentials -----------------------------------------------------------
    @property
    def cookie_name(self) -> str:
        # one per ATRIUM_HOME: cookies ignore ports, so instances on one host would log each other out
        return "atrium_" + hashlib.sha256(self._load().get("secret", "").encode()).hexdigest()[:8]

    def _sign(self, exp: str) -> str | None:
        data = self._load()
        if not data.get("secret") or not data.get("password_hash"):
            return None
        key = hmac.new(data["secret"].encode(), data["password_hash"].encode(), hashlib.sha256).digest()
        return _b64(hmac.new(key, exp.encode(), hashlib.sha256).digest())

    def _cookie(self, value: str, max_age: int, secure: bool) -> str:
        return f"{self.cookie_name}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax" + ("; Secure" if secure else "")

    def login_cookie(self, secure: bool) -> str:
        exp = str(int(time.time()) + MAX_AGE)
        return self._cookie(f"{exp}.{self._sign(exp)}", MAX_AGE, secure)

    def logout_cookie(self, secure: bool) -> str:
        return self._cookie("", 0, secure)

    def _device_sig(self, device: str) -> str | None:
        secret = self._load().get("secret")
        if not secret:
            return None
        key = hmac.new(secret.encode(), b"device", hashlib.sha256).digest()
        return _b64(hmac.new(key, device.encode(), hashlib.sha256).digest())

    def device_cookie(self, secure: bool) -> str:
        """Set at a browser's first login; only /login reads it (and logging out keeps it)."""
        device = secrets.token_urlsafe(12)
        return (f"{self.cookie_name}_device={device}.{self._device_sig(device)}; Max-Age={DEVICE_AGE}; Path=/login; HttpOnly; SameSite=Lax"
                + ("; Secure" if secure else ""))

    def device(self, conn: HTTPConnection) -> str | None:
        """The browser's device id, when it logged in here before."""
        device, _, sig = conn.cookies.get(f"{self.cookie_name}_device", "").partition(".")
        good = self._device_sig(device) if device else None
        return device if good and hmac.compare_digest(sig.encode(), good.encode()) else None

    def login_expiry(self, conn: HTTPConnection) -> int | None:
        """When the request's login cookie expires; None without a valid one (forged, expired, older than the password)."""
        exp, _, sig = conn.cookies.get(self.cookie_name, "").partition(".")
        good = self._sign(exp) if exp.isascii() and exp.isdigit() else None
        if good is None or not hmac.compare_digest(sig.encode(), good.encode()) or int(exp) < time.time():
            return None
        return int(exp)

    def token_ok(self, conn: HTTPConnection) -> bool:
        scheme, _, value = conn.headers.get("authorization", "").partition(" ")
        token = self._load().get("token")
        return scheme.lower() == "bearer" and bool(token) and hmac.compare_digest(value.strip().encode(), str(token).encode())

    async def check_password(self, password: str, source: str, known: bool) -> bool:
        """``source``: a known device (``known``), else the client's address. Raises TooManyFailures when
        it, or (unless known) all unknown sources together, had too many wrong passwords lately."""
        stored = self._load().get("password_hash") or ""  # first: a new password clears the counts
        now = time.monotonic()
        mine = self._failures.pop(source, deque())
        self._failures[source] = mine  # most recent last: the oldest records go first
        while len(self._failures) > MAX_SOURCES:
            del self._failures[next(iter(self._failures))]
        for q, limit in ((mine, MAX_FAILURES), *(() if known else ((self._unknown, MAX_UNKNOWN_FAILURES),))):
            while q and now - q[0] > FAILURE_WINDOW:
                q.popleft()
            if len(q) >= limit:
                raise TooManyFailures(FAILURE_WINDOW - (now - q[0]))
        # counted before checking, so parallel attempts can't slip past the limit; a right one takes it back
        mine.append(now)
        if not known:
            self._unknown.append(now)
        async with self._checking:
            ok = bool(stored) and await asyncio.to_thread(verify_password, password[:1024], stored)
        if ok:  # (a new password may have cleared the counts meanwhile)
            self._failures.pop(source, None)
            if not known and now in self._unknown:
                self._unknown.remove(now)
            return True
        await asyncio.sleep(1)  # outside the semaphore: it slows this source, not the others
        return False


auth = Auth()


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------
def _hostname(host: str) -> str:
    try:
        return urlsplit(f"//{host}").hostname or ""
    except ValueError:
        return ""


def _same_origin(conn: HTTPConnection) -> bool:
    """No Origin (not a browser), or the one the request is addressed to: pages of other sites, or of
    other apps on this host (cookies ignore ports), can't use the login. Behind a reverse proxy this
    needs the original Host header, or X-Forwarded-Host."""
    origin = conn.headers.get("origin")
    if origin is None:
        return True
    try:
        netloc = urlsplit(origin).netloc.lower()
    except ValueError:  # e.g. "http://[": no page of ours sends that
        return False
    hosts = {conn.headers.get("host", ""), *conn.headers.get("x-forwarded-host", "").split(",")}
    return netloc in {h.strip().lower() for h in hosts if h.strip()}


async def _refuse(scope: Scope, receive: Receive, send: Send, status: int, reason: str) -> None:
    if scope["type"] == "websocket":
        await WebSocketClose(code=1008, reason=reason)(scope, receive, send)  # before the handshake: HTTP 403
    else:
        code = "auth_required" if status == 401 else "forbidden"
        await JSONResponse({"error": reason, "code": code}, status_code=status)(scope, receive, send)


def _adding_cookie(send: Send, cookie: str) -> Send:
    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            message = {**message, "headers": [*message.get("headers", []), (b"set-cookie", cookie.encode())]}
        await send(message)

    return wrapped


def _guarded(send: Send, valid: Callable[[], bool]) -> Send:
    """A WebSocket outlives its handshake: it closes at its next message once the login it was opened
    with is gone (a new password or token, expiry)."""
    closed, checked = False, time.monotonic()

    async def wrapped(message: Message) -> None:
        nonlocal closed, checked
        if closed:
            return
        if message["type"] == "websocket.send" and time.monotonic() - checked > 1:
            checked = time.monotonic()
            if not valid():
                closed = True
                message = {"type": "websocket.close", "code": 1008, "reason": "logged out"}
        await send(message)

    return wrapped


class AuthMiddleware:
    """Lets a request through with a valid login cookie or API token; only /login and /logout are open."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        conn = HTTPConnection(scope)
        ws = scope["type"] == "websocket"
        method, path = ("GET" if ws else scope["method"]), conn.url.path
        if (ws or method not in SAFE_METHODS) and not _same_origin(conn):
            log.warning("refused %s %s from %s (host %s)", method, path, conn.headers.get("origin"), conn.headers.get("host"))
            return await _refuse(scope, receive, send, 403, "cross-origin request")
        if not auth.enabled:
            if _hostname(conn.headers.get("host", "")) in LOOPBACK:
                return await self.app(scope, receive, send)
            return await _refuse(scope, receive, send, 403, "Atrium runs without a login (--no-auth): it only answers to localhost")
        if not ws and (method, path) in OPEN:
            return await self.app(scope, receive, send)
        by_token = auth.token_ok(conn)
        exp = None if by_token else auth.login_expiry(conn)
        if not by_token and exp is None:
            if not ws and method in ("GET", "HEAD") and not path.startswith("/api/") and "text/html" in conn.headers.get("accept", ""):
                target = path + (f"?{conn.url.query}" if conn.url.query else "")
                return await RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=303)(scope, receive, send)
            return await _refuse(scope, receive, send, 401, "not logged in")
        if ws:
            send = _guarded(send, lambda: auth.token_ok(conn) if by_token else auth.login_expiry(conn) is not None)
        elif exp is not None and path.startswith("/api/") and exp - time.time() < MAX_AGE - RENEW_AFTER:
            send = _adding_cookie(send, auth.login_cookie(secure=scope.get("scheme") == "https"))
        return await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# /login, /logout
# ---------------------------------------------------------------------------
router = APIRouter(include_in_schema=False)


def _safe_next(value: str) -> str:
    """Where to go after logging in: a path on this site (not //elsewhere), other than the login page."""
    ok = value.startswith("/") and not value.startswith(("//", "/login")) and "\\" not in value and value.isascii() and value.isprintable()
    return value if ok else "/"


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"  # behind a reverse proxy: from X-Forwarded-Proto (uvicorn trusts 127.0.0.1)


@router.get("/login")
async def login_page(request: Request, nxt: str = Query("/", alias="next")):
    nxt = _safe_next(nxt)
    if not auth.enabled or auth.login_expiry(request) is not None:
        return RedirectResponse(nxt, status_code=303)
    return _page(nxt)


@router.post("/login")
async def login(request: Request):
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            return PlainTextResponse("too large", status_code=413)
    form = parse_qs(body.decode(errors="replace"))
    nxt = _safe_next(form.get("next", ["/"])[0])
    if not auth.enabled:
        return RedirectResponse(nxt, status_code=303)
    device = auth.device(request)
    address = request.client.host if request.client else "?"  # behind a reverse proxy here: X-Forwarded-For's
    try:
        ok = await auth.check_password(form.get("password", [""])[0], f"device {device}" if device else address, known=bool(device))
    except TooManyFailures as e:
        return _page(nxt, f"Too many wrong passwords: try again in {math.ceil(e.wait / 60)} min.", 429)
    if not ok:
        log.warning("wrong password on the login page, from %s%s", address, " (a known device)" if device else "")
        return _page(nxt, "Wrong password.", 401)
    resp = RedirectResponse(nxt, status_code=303)
    resp.headers.append("set-cookie", auth.login_cookie(_secure(request)))
    if not device:
        resp.headers.append("set-cookie", auth.device_cookie(_secure(request)))
    return resp


@router.post("/logout")
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=303)
    resp.headers.append("set-cookie", auth.logout_cookie(_secure(request)))
    return resp


def _page(nxt: str, error: str = "", status: int = 200) -> HTMLResponse:
    body = LOGIN_PAGE.substitute(next=html.escape(nxt), error=f'<p class="error" role="alert">{html.escape(error)}</p>' if error else "")
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


# Self-contained (nothing else is served before logging in); colours from web/src/styles.css.
LOGIN_PAGE = Template("""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Atrium · Log in</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%23D97757'/><path d='M9 23 16 8l7 15M12 17h8' stroke='white' stroke-width='2.6' fill='none' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<script>
  // the theme picked in Atrium, else the system's
  try {
    var t = localStorage.getItem("atrium.theme");
    if (t === "dark" || (t !== "light" && matchMedia("(prefers-color-scheme: dark)").matches)) document.documentElement.dataset.theme = "dark";
  } catch (e) {}
</script>
<style>
  :root { --bg: #faf9f5; --surface: #ffffff; --border: #d4d1c4; --text: #1f1e1d; --muted: #73726c; --accent: #d97757; --accent-strong: #c6613f; --err: #b5443a; --err-soft: #f8e4e1; color-scheme: light; }
  [data-theme="dark"] { --bg: #262624; --surface: #30302e; --border: #4c4b47; --text: #f5f4ef; --muted: #a6a39a; --accent-strong: #e08a6d; --err: #e8857a; --err-soft: #3d2724; color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--bg); color: var(--text);
    font: 14px/1.5 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", "PingFang SC", sans-serif; -webkit-font-smoothing: antialiased; }
  form { width: min(340px, calc(100vw - 32px)); padding: 28px; display: flex; flex-direction: column; gap: 14px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: 0 10px 40px rgba(0, 0, 0, 0.08); }
  h1 { margin: 0 0 4px; display: flex; align-items: center; gap: 10px; font: 500 22px/1.2 "Tiempos Text", "Iowan Old Style", Charter, Georgia, serif; }
  label { display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; color: var(--muted); }
  input { height: 36px; padding: 0 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--bg); color: var(--text); font: inherit; font-size: 14px; outline: none; }
  input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
  button { height: 36px; border: 0; border-radius: 10px; background: var(--accent-strong); color: #fff; font: inherit; font-weight: 500; cursor: pointer; }
  button:hover { background: var(--accent); }
  .error { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--err-soft); color: var(--err); font-size: 13px; }
  .hint { margin: 0; font-size: 12px; color: var(--muted); }
  code { font: 11.5px ui-monospace, "SF Mono", Menlo, monospace; }
</style>
</head>
<body>
<form method="post" action="/login">
  <h1><svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#D97757"/><path d="M9 23 16 8l7 15M12 17h8" stroke="#fff" stroke-width="2.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>Atrium</h1>
  $error
  <input type="hidden" name="next" value="$next">
  <input type="text" name="username" value="atrium" autocomplete="username" hidden>
  <label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
  <button type="submit">Log in</button>
  <p class="hint">Forgot it? Run <code>atrium --set-password</code> where Atrium runs.</p>
</form>
</body>
</html>
""")
