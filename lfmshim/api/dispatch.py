"""Transport-independent handling of one /2.0/ API call."""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

from .. import errors as E
from ..errors import LastFmError, UpstreamError
from ..render import error_json, error_xml, to_json, to_xml, valid_callback
from .registry import Request, lookup

log = logging.getLogger("lfmshim.api")

# Parameters never written to logs.
SENSITIVE = {"password", "api_sig", "sk", "token", "authtoken", "authToken", "secret"}


@dataclass
class Response:
    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] | None = None


def signature(params: dict[str, str], secret: str) -> str:
    """Last.fm api_sig: md5 of sorted name+value pairs plus the secret.

    ``format`` and ``callback`` are excluded, as the Last.fm docs specify.
    """
    parts = [k + params[k] for k in sorted(params) if k not in ("format", "callback", "api_sig")]
    return hashlib.md5(("".join(parts) + secret).encode("utf-8")).hexdigest()


def redact(params: dict[str, str]) -> dict[str, str]:
    return {k: ("<redacted>" if k in SENSITIVE else v) for k, v in params.items()}


def _render(result: Any, fmt: str, callback: str | None, collapse: bool) -> Response:
    if fmt == "json":
        body = to_json(result, collapse)
        if callback:
            return Response(200, "text/javascript; charset=utf-8", callback.encode() + b"(" + body + b");")
        return Response(200, "application/json; charset=utf-8", body)
    return Response(200, "text/xml; charset=utf-8", to_xml(result))


def _render_error(err: LastFmError, fmt: str, callback: str | None) -> Response:
    if fmt == "json":
        body = error_json(err.code, err.message, err.links)
        if callback:
            return Response(200, "text/javascript; charset=utf-8", callback.encode() + b"(" + body + b");")
        return Response(err.http_status, "application/json; charset=utf-8", body)
    return Response(err.http_status, "text/xml; charset=utf-8", error_xml(err.code, err.message))


def handle(app: Any, http_method: str, params: dict[str, str], headers: dict[str, str] | None = None) -> Response:
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    fmt = "json" if (params.get("format") or "").lower() == "json" else "xml"
    callback = params.get("callback") if fmt == "json" else None
    if callback and not valid_callback(callback):
        callback = None
    cfg = app.config.api
    name = params.get("method") or ""
    try:
        result = _dispatch(app, http_method, params, headers, name)
        log.info("%s %s ok", http_method, name)
        return _render(result, fmt, callback, cfg.json_collapse_single)
    except LastFmError as err:
        log.info("%s %s error %d", http_method, name, err.code)
        return _render_error(err, fmt, callback)
    except UpstreamError as err:
        log.warning("%s %s upstream failure: %s", http_method, name, err)
        return _render_error(LastFmError(E.TEMPORARY_ERROR),
                             fmt, callback)
    except Exception:  # pragma: no cover - defensive
        log.exception("%s %s crashed (params=%r)", http_method, name, redact(params))
        return _render_error(LastFmError(E.OPERATION_FAILED), fmt, callback)


def _check_origin(app: Any, headers: dict[str, str]) -> None:
    """Block browser cross-site requests from driving write/auth methods.

    A web page can make a browser send a form POST to localhost; such requests
    carry Origin and/or Sec-Fetch-Site headers that native clients don't send.
    """
    allowed = {o.rstrip("/") for o in app.config.api.allowed_origins}
    allowed.add(app.config.server.effective_public_url())
    origin = headers.get("origin")
    if origin and origin.rstrip("/") not in allowed and origin != "null":
        raise LastFmError(E.AUTH_FAILED, "Authentication Failed - Cross-origin write requests are not allowed")
    if origin == "null":
        raise LastFmError(E.AUTH_FAILED, "Authentication Failed - Cross-origin write requests are not allowed")
    if headers.get("sec-fetch-site") in ("cross-site", "same-site") and not origin:
        raise LastFmError(E.AUTH_FAILED, "Authentication Failed - Cross-origin write requests are not allowed")


def _check_signature(app: Any, params: dict[str, str]) -> None:
    cfg = app.config.api
    if cfg.signature_policy == "off":
        return
    api_sig = params.get("api_sig")
    if not api_sig:
        raise LastFmError(E.INVALID_SIGNATURE)
    secret = cfg.clients.get(params.get("api_key", ""))
    if secret is None:
        if cfg.signature_policy == "strict":
            raise LastFmError(E.INVALID_SIGNATURE)
        return
    if not hmac.compare_digest(signature(params, secret), api_sig.lower()):
        raise LastFmError(E.INVALID_SIGNATURE)


def _dispatch(app: Any, http_method: str, params: dict[str, str], headers: dict[str, str], name: str) -> Any:
    cfg = app.config.api
    api_key = params.get("api_key", "")
    if not api_key:
        raise E.missing_api_key()
    if not name:
        raise LastFmError(E.INVALID_METHOD)
    spec = lookup(name)
    if spec is None:
        raise LastFmError(E.INVALID_METHOD)
    if cfg.api_keys and api_key not in cfg.api_keys:
        raise LastFmError(E.INVALID_API_KEY)
    req = Request(params=params, app=app, http_method=http_method)
    if spec.write and cfg.require_post_for_writes and http_method != "POST":
        raise LastFmError(E.INVALID_METHOD, "Invalid Method - This method requires an HTTP POST")
    if spec.auth != "none" or spec.write:
        _check_origin(app, headers)
    if spec.auth in ("session", "signed"):
        _check_signature(app, params)
    if spec.auth == "session":
        sk = params.get("sk")
        if not sk:
            raise LastFmError(E.INVALID_SESSION)
        user = app.store.session_user(sk)
        if user is None and cfg.accept_unknown_sessions:
            user = cfg.default_user
            if app.store.get_user(user) is None:
                app.store.add_user(user)
            app.store.adopt_session(sk, user, api_key)
            log.info("adopted an existing-client session for user %s", user)
        if user is None:
            raise LastFmError(E.INVALID_SESSION)
        req.user, req.session_key = user, sk
    elif params.get("sk"):
        # Optional session on read methods (e.g. personalised info).
        req.user = app.store.session_user(params["sk"])
    return spec.func(req)
