"""Polite upstream HTTP: User-Agent, per-service rate limits, retries, and a
persistent response cache (including negative caching of 404s)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode

from .. import __version__
from ..errors import UpstreamError

log = logging.getLogger("lfmshim.upstream")

NOT_FOUND = {"__lfmshim_not_found__": True}
FAILED = {"__lfmshim_failed__": True}


class UrllibTransport:
    """Real network transport. Tests substitute a fake with the same interface."""

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read() or b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamError(url.split("/")[2], f"network error: {getattr(exc, 'reason', exc)}") from None


class RateLimiter:
    """Minimum interval between requests to one service (thread-safe)."""

    def __init__(self, per_second: float, clock, sleep):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = self.clock()
            delay = self._next - now
            if delay > 0:
                self.sleep(delay)
                now = self.clock()
            self._next = max(now, self._next) + self.interval

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, self.clock() + seconds)


class HttpClient:
    # Requests/second per service host. MusicBrainz: 1/s average per IP.
    # TheAudioDB free key: 30/min. ListenBrainz: be polite (~1/s; headers tell us more).
    # Discogs: 25/min without a token, 60/min with one (X-Discogs-Ratelimit); a
    # small margin is kept because Discogs counts over a moving 60 s window.
    DEFAULT_RATES = {"musicbrainz": 1.0, "theaudiodb": 0.5, "listenbrainz": 2.0, "labs": 1.0,
                     "coverart": 5.0, "wikipedia": 5.0, "wikidata": 5.0, "discogs": 24 / 60}
    DISCOGS_TOKEN_RATE = 58 / 60

    def __init__(self, app: Any):
        self.app = app
        cfg = app.config.upstream
        self.cfg = cfg
        self.transport = app.transport or UrllibTransport()
        contact = cfg.contact or "no contact configured"
        self.user_agent = f"lfmshim/{__version__} ( {contact} )"
        rates = dict(self.DEFAULT_RATES)
        rates["musicbrainz"] = cfg.musicbrainz_rate
        if cfg.discogs_rate > 0:
            rates["discogs"] = cfg.discogs_rate
        elif cfg.resolved_discogs_token():
            rates["discogs"] = self.DISCOGS_TOKEN_RATE
        self.limiters = {k: RateLimiter(v, app.clock, app.sleep) for k, v in rates.items()}
        self._inflight: dict[str, threading.Lock] = {}
        self._inflight_lock = threading.Lock()

    def _key(self, method: str, url: str, body: bytes | None) -> str:
        h = hashlib.sha256((method + " " + url).encode() + (body or b"")).hexdigest()
        return "http:" + h

    def _lock_for(self, key: str) -> threading.Lock:
        with self._inflight_lock:
            return self._inflight.setdefault(key, threading.Lock())

    @staticmethod
    def _full_url(url: str, params: dict[str, Any] | None) -> str:
        if params:
            url = url + ("&" if "?" in url else "?") + urlencode({k: v for k, v in params.items() if v is not None})
        return url

    def is_cached(self, url: str, params: dict[str, Any] | None = None, method: str = "GET") -> bool:
        """True when a GET would be answered from the cache (including cached
        404s and failures), i.e. without any network request."""
        return self.app.store.cache_get(self._key(method, self._full_url(url, params), None)) is not None

    def get_json(self, service: str, url: str, params: dict[str, Any] | None = None, *, ttl: int | None = None,
                 method: str = "GET", json_body: Any = None, headers: dict[str, str] | None = None,
                 cache: bool = True, cache_only: bool = False, retries: int = 2,
                 optional: bool = False) -> Any | None:
        """Fetch JSON. Returns None for 404/empty (negatively cached).

        ``optional`` marks enrichment data (popularity, images, wikis): it uses
        a short timeout, and a failure is cached briefly and reported as None so
        one slow service cannot stall every request.
        """
        url = self._full_url(url, params)
        body = json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else None
        ttl = self.cfg.cache_ttl if ttl is None else ttl
        key = self._key(method, url, body)
        store = self.app.store
        if cache:
            hit = store.cache_get(key)
            if hit is not None:
                return None if hit in (NOT_FOUND, FAILED) else hit
        if cache_only:
            return None
        if self.cfg.offline:
            raise UpstreamError(service, "offline mode")
        with self._lock_for(key):  # collapse concurrent identical fetches
            if cache:
                hit = store.cache_get(key)
                if hit is not None:
                    return None if hit in (NOT_FOUND, FAILED) else hit
            timeout = min(self.cfg.timeout, self.cfg.optional_timeout) if optional else self.cfg.timeout
            try:
                value = self._fetch(service, method, url, body, headers or {}, 0 if optional else retries,
                                    timeout)
            except UpstreamError as exc:
                if not optional:
                    raise
                log.info("optional %s data unavailable: %s", service, exc)
                if cache:
                    store.cache_put(key, FAILED, self.cfg.failure_cache_ttl)
                return None
            if cache:
                if value is None:
                    store.cache_put(key, NOT_FOUND, min(ttl, self.cfg.negative_cache_ttl))
                else:
                    store.cache_put(key, value, ttl)
            return value

    def _fetch(self, service: str, method: str, url: str, body: bytes | None, extra: dict[str, str],
               retries: int, timeout: float) -> Any | None:
        limiter = self.limiters.get(service)
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra)
        attempt = 0
        while True:
            if limiter:
                limiter.wait()
            status, rheaders, data = self.transport.request(method, url, headers, body, timeout)
            if status in (429, 503) and attempt >= retries and limiter:
                # Out of retries (always, for optional data): still slow down,
                # so the next request to this service does not hit the limit too.
                limiter.penalize(_retry_after(rheaders, default=2.0 * (attempt + 1)))
            if status in (429, 503) and attempt < retries:
                attempt += 1
                retry_after = _retry_after(rheaders, default=2.0 * attempt)
                log.info("%s throttled (HTTP %d); backing off %.1fs", service, status, retry_after)
                if limiter:
                    limiter.penalize(retry_after)
                else:
                    self.app.sleep(retry_after)
                continue
            if status == 404 or status == 204:
                return None
            if status >= 400:
                raise UpstreamError(service, f"HTTP {status}", status)
            if not data.strip():
                return None
            try:
                value = json.loads(data)
            except ValueError:
                raise UpstreamError(service, "invalid JSON") from None
            return value


def _retry_after(headers: dict[str, str], default: float) -> float:
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in ("retry-after", "x-ratelimit-reset-in"):
        raw = lowered.get(name)
        if raw:
            try:
                return max(0.0, min(float(raw), 60.0))
            except ValueError:
                pass
    return default
