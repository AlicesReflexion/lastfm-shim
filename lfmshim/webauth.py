"""Browser approval page for the Last.fm desktop/web auth flows (/api/auth/).

Desktop flow: client calls auth.getToken, opens /api/auth/?api_key=K&token=T,
the user signs in with their *local* shim account and approves, then the
client calls auth.getSession(token=T).

Web flow: /api/auth/?api_key=K&cb=URL creates the token on approval and
redirects to URL?token=T.

Protections: per-token CSRF value, same-origin check on POST, failed-login
throttling, strict CSP / no framing, no credentials in logs.
"""

from __future__ import annotations

import hmac
import html
import logging
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

from .api.auth import check_login
from .api.dispatch import Response
from .errors import LastFmError
from .store import digest

log = logging.getLogger("lfmshim.auth")

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize application</title>
<style>
:root{{color-scheme:light dark;--bg:#f7f7f8;--fg:#1d1d1f;--card:#fff;--line:#d9d9de;--accent:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#ececf1;--card:#202024;--line:#3a3a40;--accent:#ef5350}}}}
body{{margin:0;font:16px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg)}}
main{{max-width:26rem;margin:4rem auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.5rem}}
h1{{font-size:1.25rem;margin:0 0 .5rem}} label{{display:block;margin:.75rem 0 .25rem}}
input[type=text],input[type=password]{{box-sizing:border-box;width:100%;padding:.55rem;border:1px solid var(--line);
border-radius:8px;background:transparent;color:inherit;font:inherit}}
.row{{display:flex;gap:.5rem;margin-top:1.25rem}} button{{flex:1;padding:.6rem;border-radius:8px;font:inherit;
border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}}
.err{{color:var(--accent)}} code{{font-size:.85em;word-break:break-all}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""


def _page(status: int, body: str) -> Response:
    return Response(status, "text/html; charset=utf-8", PAGE.format(body=body).encode("utf-8"), dict(HEADERS))


def _safe_cb(cb: str | None) -> str | None:
    if not cb:
        return None
    parts = urlsplit(cb)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return cb


def _with_token(cb: str, token: str) -> str:
    parts = urlsplit(cb)
    q = parse_qsl(parts.query, keep_blank_values=True) + [("token", token)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


class WebAuth:
    def __init__(self, app: Any):
        self.app = app

    def handle(self, verb: str, params: dict[str, str], headers: dict[str, str]) -> Response:
        if verb == "POST":
            return self._post(params, {k.lower(): v for k, v in headers.items()})
        return self._form(params)

    def _token_row(self, token: str | None):
        if not token:
            return None
        row = self.app.store.get_token(token)
        if row is None or self.app.store.token_expired(row):
            return None
        return row

    def _form(self, params: dict[str, str], error: str = "", status: int = 200) -> Response:
        token, cb = params.get("token"), _safe_cb(params.get("cb"))
        api_key = params.get("api_key", "")
        if not token and cb:
            # Web flow: mint the token now so the form has a CSRF value.
            token = self.app.store.create_token(api_key)
        row = self._token_row(token)
        if row is None:
            return _page(400, "<h1>Invalid or expired token</h1><p>Restart sign-in from your application.</p>")
        app_name = html.escape(api_key or "an application")
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        hidden = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(v, quote=True)}">'
            for k, v in (("token", token), ("csrf", row["csrf"]), ("api_key", api_key), ("cb", cb or "")))
        body = (f"<h1>Authorize application</h1><p>Allow <code>{app_name}</code> to scrobble and read your "
                f"local listening history on this shim?</p>{err}"
                f'<form method="post" action="/api/auth/" autocomplete="on">{hidden}'
                '<label for="u">Local username</label><input id="u" type="text" name="username" required '
                'autocomplete="username"><label for="p">Password</label><input id="p" type="password" '
                'name="password" required autocomplete="current-password"><div class="row">'
                '<button type="submit" name="action" value="deny">Deny</button>'
                '<button class="primary" type="submit" name="action" value="approve">Allow access</button>'
                "</div></form>")
        return _page(status, body)

    def _same_origin(self, headers: dict[str, str]) -> bool:
        origin = headers.get("origin")
        if not origin:
            return headers.get("sec-fetch-site") in (None, "same-origin", "none")
        allowed = {self.app.config.server.effective_public_url()}
        host = headers.get("host")
        if host:
            allowed.update({f"http://{host}", f"https://{host}"})
        return origin.rstrip("/") in allowed

    def _post(self, params: dict[str, str], headers: dict[str, str]) -> Response:
        if not self._same_origin(headers):
            return _page(403, "<h1>Request blocked</h1><p>Cross-site form submission refused.</p>")
        token = params.get("token", "")
        row = self._token_row(token)
        if row is None:
            return _page(400, "<h1>Invalid or expired token</h1><p>Restart sign-in from your application.</p>")
        if not hmac.compare_digest(params.get("csrf", ""), row["csrf"]):
            return _page(403, "<h1>Request blocked</h1><p>Form expired; reload and try again.</p>")
        if params.get("action") == "deny":
            self.app.store.consume_token(token)
            return _page(200, "<h1>Access denied</h1><p>You can close this window.</p>")
        try:
            user = check_login(self.app, params.get("username", ""), params.get("password", ""))
        except LastFmError as exc:
            log.info("web auth login failed for token %s…", digest(token)[:8])
            return self._form(params, exc.message.split(" - ")[-1], 403)
        self.app.store.authorize_token(token, user)
        log.info("token authorized for user %s", user)
        cb = _safe_cb(params.get("cb"))
        if cb:
            return Response(302, "text/plain; charset=utf-8", b"", {**HEADERS, "Location": _with_token(cb, token)})
        return _page(200, f"<h1>Application authorized</h1><p>Signed in as <b>{html.escape(user)}</b>. "
                          "You can close this window and return to your application.</p>")
