"""Shared test fixtures: an App with in-memory storage, a fake clock and a
fake HTTP transport, so no test touches the network or real user data."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from lfmshim.app import App
from lfmshim.api.dispatch import handle, signature
from lfmshim.config import Config
from lfmshim.store import Store

API_KEY = "testkey0000000000000000000000000"
SECRET = "testsecret000000000000000000000"
NOW = 1_763_500_000  # fixed "now" (2025-11-18)


class FakeClock:
    def __init__(self, t: float = NOW):
        self.t = float(t)
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class FakeTransport:
    """Routes requests to canned responses; unknown URLs fail the test.

    Routes are (method, url-prefix) -> response or callable(method, url, headers, body).
    A response is (status, body) where body is a dict/list (JSON) or bytes.
    """

    def __init__(self):
        self.routes: list[tuple[str, str, Any]] = []
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def add(self, url_prefix: str, response: Any, method: str = "GET") -> "FakeTransport":
        self.routes.insert(0, (method, url_prefix, response))
        return self

    def add_json(self, url_prefix: str, data: Any, status: int = 200, method: str = "GET") -> "FakeTransport":
        return self.add(url_prefix, (status, data), method)

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        self.calls.append((method, url, dict(headers), body))
        for m, prefix, resp in self.routes:
            if m == method and _matches(url, prefix):
                if callable(resp):
                    resp = resp(method, url, headers, body)
                status, payload = resp[0], resp[1]
                rheaders = resp[2] if len(resp) > 2 else {}
                if not isinstance(payload, (bytes, bytearray)):
                    payload = json.dumps(payload).encode()
                return status, rheaders, bytes(payload)
        raise AssertionError(f"unexpected upstream request: {method} {url}")

    def urls(self, contains: str = "") -> list[str]:
        return [u for _, u, _, _ in self.calls if contains in u]


def _matches(url: str, prefix: str) -> bool:
    """Prefix match where query params in the prefix must all be present."""
    if "?" not in prefix:
        return url.split("?")[0] == prefix or url.startswith(prefix)
    pbase, pq = prefix.split("?", 1)
    if url.split("?")[0] != pbase:
        return False
    have = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    return all(have.get(k) == v for k, v in parse_qsl(pq, keep_blank_values=True))


def make_config(**sections: dict[str, Any]) -> Config:
    cfg = Config()
    cfg.upstream.contact = "tests@example.invalid"
    for section, values in sections.items():
        obj = getattr(cfg, section)
        for k, v in values.items():
            setattr(obj, k, v)
    cfg.validate()
    return cfg


def make_app(transport: FakeTransport | None = None, clock: FakeClock | None = None, **sections) -> App:
    clock = clock or FakeClock()
    cfg = make_config(**sections)
    store = Store(":memory:", clock=clock)
    return App(cfg, store=store, transport=transport or FakeTransport(), clock=clock, sleep=clock.sleep)


def call(app: App, method: str, http: str = "GET", headers: dict[str, str] | None = None,
         fmt: str | None = "json", sign: bool = False, secret: str = SECRET, **params: Any):
    p = {"method": method, "api_key": API_KEY}
    p.update({k: str(v) for k, v in params.items()})
    if fmt:
        p["format"] = fmt
    if sign:
        p["api_sig"] = signature(p, secret)
    resp = handle(app, http, p, headers)
    if fmt == "json":
        return resp.status, json.loads(resp.body)
    return resp.status, resp.body.decode("utf-8")


def post(app: App, method: str, **params: Any):
    return call(app, method, http="POST", sign=True, **params)
