"""Discogs database lookups (genres and styles) through MusicBrainz links.

The shim never searches Discogs by name. It follows the ``discogs`` URL
relationships MusicBrainz already curates (release group -> master, release ->
release, artist -> artist) and reads ``/masters/{id}`` or ``/releases/{id}``.
Both endpoints work without authentication (25 requests/minute, observed via
``X-Discogs-Ratelimit``); a personal access token raises that to 60/minute.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..errors import UpstreamError

log = logging.getLogger("lfmshim.upstream")

DISCOGS_LINK = re.compile(r"^https?://(?:www\.)?discogs\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?"
                          r"(master|release|artist)/(\d+)(?:[-/?#].*)?$", re.IGNORECASE)


def discogs_links(entity: dict[str, Any] | None) -> list[tuple[str, int]]:
    """(kind, id) pairs from an MB entity's ``discogs`` URL relationships, in MB order."""
    out: list[tuple[str, int]] = []
    for rel in (entity or {}).get("relations") or []:
        if rel.get("type") != "discogs" or rel.get("ended"):
            continue
        m = DISCOGS_LINK.match(((rel.get("url") or {}).get("resource") or "").strip())
        if m and (m.group(1).lower(), int(m.group(2))) not in out:
            out.append((m.group(1).lower(), int(m.group(2))))
    return out


def web_url(kind: str, discogs_id: int) -> str:
    return f"https://www.discogs.com/{kind}/{discogs_id}"


def page_url(data: dict[str, Any], kind: str, discogs_id: int) -> str:
    """The entity's Discogs page: its ``uri`` (with the title slug) when that is
    a plain discogs.com page for this entity, else the canonical short form."""
    uri = data.get("uri") if isinstance(data.get("uri"), str) else ""
    m = DISCOGS_LINK.match(uri)
    if m and uri.startswith("https://www.discogs.com/") and m.group(1).lower() == kind and             int(m.group(2)) == int(discogs_id) and not any(c in uri for c in "\"'<> "):
        return uri
    return web_url(kind, discogs_id)


class Discogs:
    def __init__(self, http, base_url: str, token: str = "", enabled: bool = True):
        self.http = http
        self.base = base_url.rstrip("/")
        self.token = token
        self.enabled = enabled

    def _path(self, kind: str, discogs_id: int) -> str:
        return f"{self.base}/{'masters' if kind == 'master' else 'releases'}/{int(discogs_id)}"

    def is_cached(self, kind: str, discogs_id: int) -> bool:
        return self.http.is_cached(self._path(kind, discogs_id))

    def lookup(self, kind: str, discogs_id: int, **kw) -> dict[str, Any] | None:
        """A master (kind 'master') or release (kind 'release'), or None.

        Optional enrichment: failures, 404s and rate limiting yield None.
        """
        if not self.enabled or kind not in ("master", "release"):
            return None
        headers = {"Authorization": f"Discogs token={self.token}"} if self.token else None
        try:
            data = self.http.get_json("discogs", self._path(kind, discogs_id), headers=headers, optional=True,
                                      **kw)
        except UpstreamError as exc:
            log.info("discogs lookup failed: %s", exc)
            return None
        return data if isinstance(data, dict) else None


def genres_styles(data: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    """(styles, genres) of a Discogs master/release, de-duplicated, in Discogs order."""
    def clean(values: Any) -> list[str]:
        out, seen = [], set()
        for v in values or []:
            v = (v or "").strip() if isinstance(v, str) else ""
            if v and v.casefold() not in seen:
                seen.add(v.casefold())
                out.append(v)
        return out

    styles = clean((data or {}).get("styles"))
    genres = [g for g in clean((data or {}).get("genres")) if g.casefold() not in {s.casefold() for s in styles}]
    return styles, genres
