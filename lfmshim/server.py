"""Loopback HTTP(S) front end: /2.0/ API, the browser auth page, health."""

from __future__ import annotations

import logging
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .api.dispatch import Response, handle

log = logging.getLogger("lfmshim.http")

API_PATHS = {"/2.0", "/2.0/"}
AUTH_PATHS = {"/api/auth", "/api/auth/"}


def allowed_hosts(app: Any) -> set[str]:
    s = app.config.server
    hosts = {"localhost", "127.0.0.1", "[::1]", s.host}
    pub = urlsplit(s.effective_public_url()).hostname
    if pub:
        hosts.add(pub)
    hosts.update(h.lower() for h in s.allowed_hosts)
    return {h.lower() for h in hosts}


def host_ok(app: Any, host_header: str | None) -> bool:
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        name = host.split("]")[0] + "]"
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return name in allowed_hosts(app)


class Handler(BaseHTTPRequestHandler):
    server_version = "lfmshim"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> Any:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default stderr access log
        pass

    def _send(self, resp: Response, head_only: bool = False) -> None:
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        self.send_header("Content-Length", str(len(resp.body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (resp.headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head_only:
            self.wfile.write(resp.body)

    def _plain(self, status: int, text: str) -> None:
        self._send(Response(status, "text/plain; charset=utf-8", text.encode("utf-8")))

    def _read_body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > self.app.config.server.max_body_bytes:
            self.close_connection = True
            self._plain(413, "request body too large")
            return None
        return self.rfile.read(length) if length else b""

    def _route(self, verb: str) -> None:
        if not host_ok(self.app, self.headers.get("Host")):
            self.close_connection = True
            self._plain(421, "unrecognised Host header")
            return
        parts = urlsplit(self.path)
        path = parts.path
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        body = b""
        if verb == "POST":
            raw = self._read_body()
            if raw is None:
                return
            body = raw
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype in ("", "application/x-www-form-urlencoded", "text/plain"):
                params.update(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        headers = {k: v for k, v in self.headers.items()}
        if path in API_PATHS:
            self._send(handle(self.app, verb if verb != "HEAD" else "GET", params, headers), verb == "HEAD")
        elif path in AUTH_PATHS and getattr(self.app, "web_auth", None):
            self._send(self.app.web_auth.handle(verb, params, headers))
        elif path == "/healthz":
            self._plain(200, "ok")
        elif path == "/":
            self._plain(200, "lfmshim: Last.fm-compatible API at /2.0/\n")
        else:
            self._plain(404, "not found")

    def do_GET(self) -> None:
        self._route("GET")

    def do_HEAD(self) -> None:
        self._route("HEAD")

    def do_POST(self) -> None:
        self._route("POST")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, app: Any):
        s = app.config.server
        self.app = app
        super().__init__((s.host, s.port), Handler)
        if s.tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(s.tls_cert, s.tls_key)
            self.socket = ctx.wrap_socket(self.socket, server_side=True)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        scheme = "https" if self.app.config.server.tls else "http"
        return f"{scheme}://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}"


def serve_in_thread(app: Any) -> tuple[ShimServer, threading.Thread]:
    srv = ShimServer(app)
    t = threading.Thread(target=srv.serve_forever, name="lfmshim-http", daemon=True)
    t.start()
    return srv, t
