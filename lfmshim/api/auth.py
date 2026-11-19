"""auth.getToken, auth.getSession, auth.getMobileSession."""

from __future__ import annotations

import threading
from collections import defaultdict, deque

from .. import errors as E
from ..errors import LastFmError
from .registry import Request, method


class LoginThrottle:
    """At most ``limit`` failed logins per user per ``window`` seconds."""

    def __init__(self, limit: int = 5, window: int = 300):
        self.limit, self.window = limit, window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and q[0] < now - self.window:
            q.popleft()
        return q

    def blocked(self, user: str, now: float) -> bool:
        with self._lock:
            return len(self._trim(user.casefold(), now)) >= self.limit

    def failed(self, user: str, now: float) -> None:
        with self._lock:
            self._trim(user.casefold(), now).append(now)

    def reset(self, user: str) -> None:
        with self._lock:
            self._fails.pop(user.casefold(), None)


THROTTLE = LoginThrottle()


def check_login(app, username: str, password: str) -> str:
    """Return the canonical username or raise error 4 / 29."""
    now = app.now()
    throttle = getattr(app, "login_throttle", THROTTLE)
    if throttle.blocked(username, now):
        raise LastFmError(E.RATE_LIMITED, "Rate limit exceeded - Too many failed login attempts, try again later")
    user = app.store.check_password(username, password)
    if user is None:
        throttle.failed(username, now)
        raise LastFmError(E.AUTH_FAILED, "Authentication Failed - Invalid username or password")
    throttle.reset(username)
    return user


def _session(name: str, key: str) -> dict:
    return {"session": {"name": name, "key": key, "subscriber": 0}}


@method("auth.getToken", notes="token for the browser approval page at /api/auth/")
def auth_get_token(req: Request):
    return {"token": req.app.store.create_token(req.get("api_key") or "")}


@method("auth.getSession", auth="signed", notes="exchanges a token approved at /api/auth/ for a session key")
def auth_get_session(req: Request):
    store = req.app.store
    token = req.require("token")
    row = store.get_token(token)
    if row is None:
        raise LastFmError(E.AUTH_FAILED, "Invalid authentication token supplied")
    if store.token_expired(row):
        store.consume_token(token)
        raise LastFmError(E.TOKEN_EXPIRED)
    user = row["user"]
    auto = req.app.config.api.auto_authorize_user
    if not user and auto:
        if store.get_user(auto) is None:
            store.add_user(auto)
        user = store.get_user(auto)["name"]
    if not user:
        raise LastFmError(E.TOKEN_UNAUTHORIZED)
    store.consume_token(token)
    return _session(user, store.create_session(user, req.get("api_key") or ""))


@method("auth.getMobileSession", auth="signed", write=True,
        notes="local username/password (PBKDF2); legacy authToken form unsupported")
def auth_get_mobile_session(req: Request):
    username = req.require("username")
    password = req.get("password")
    if password is None:
        raise LastFmError(E.AUTH_FAILED, "Authentication Failed - password is required (authToken is not supported)")
    user = check_login(req.app, username, password)
    return _session(user, req.app.store.create_session(user, req.get("api_key") or ""))
