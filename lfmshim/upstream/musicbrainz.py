"""MusicBrainz WS/2 JSON client (search, lookup, browse)."""

from __future__ import annotations

import re
from typing import Any

MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def is_mbid(value: str | None) -> bool:
    return bool(value) and bool(MBID_RE.match(value.strip().lower()))


def lucene(value: str) -> str:
    """Escape a phrase for a quoted Lucene term."""
    return _LUCENE_SPECIAL.sub(r"\\\1", value)


class MusicBrainz:
    def __init__(self, http, base_url: str):
        self.http = http
        self.base = base_url.rstrip("/")

    def _get(self, path: str, params: dict[str, Any] | None = None, **kw) -> Any | None:
        p = {"fmt": "json"}
        p.update(params or {})
        return self.http.get_json("musicbrainz", f"{self.base}/{path}", p, **kw)

    # ---- search ------------------------------------------------------------
    def search(self, entity: str, query: str, limit: int = 10, offset: int = 0, **kw) -> dict[str, Any]:
        return self._get(entity, {"query": query, "limit": limit, "offset": offset}, **kw) or {}

    def search_artists(self, name: str, limit: int = 10, offset: int = 0, **kw) -> dict[str, Any]:
        q = f'artist:"{lucene(name)}" OR alias:"{lucene(name)}" OR sortname:"{lucene(name)}"'
        return self.search("artist", q, limit, offset, **kw)

    def search_release_groups(self, album: str, artist: str | None, limit: int = 10, offset: int = 0,
                              **kw) -> dict[str, Any]:
        q = f'releasegroup:"{lucene(album)}"'
        if artist:
            q += f' AND (artist:"{lucene(artist)}" OR artistname:"{lucene(artist)}")'
        return self.search("release-group", q, limit, offset, **kw)

    def search_recordings(self, track: str, artist: str | None, limit: int = 25, offset: int = 0,
                          **kw) -> dict[str, Any]:
        q = f'recording:"{lucene(track)}"'
        if artist:
            q += f' AND (artist:"{lucene(artist)}" OR artistname:"{lucene(artist)}")'
        return self.search("recording", q, limit, offset, **kw)

    # ---- lookup ------------------------------------------------------------
    def artist(self, mbid: str, **kw) -> dict[str, Any] | None:
        return self._get(f"artist/{mbid}", {"inc": "url-rels+tags+genres+aliases"}, **kw)

    def release_group(self, mbid: str, **kw) -> dict[str, Any] | None:
        return self._get(f"release-group/{mbid}", {"inc": "artist-credits+tags+genres+url-rels+releases"}, **kw)

    def release(self, mbid: str, **kw) -> dict[str, Any] | None:
        return self._get(f"release/{mbid}",
                         {"inc": "recordings+artist-credits+release-groups+tags+genres+media"}, **kw)

    def recording(self, mbid: str, **kw) -> dict[str, Any] | None:
        return self._get(f"recording/{mbid}",
                         {"inc": "artist-credits+releases+release-groups+tags+genres+url-rels"}, **kw)

    def releases_of_group(self, rg_mbid: str, **kw) -> list[dict[str, Any]]:
        data = self._get("release", {"release-group": rg_mbid, "status": "official", "inc": "media",
                                     "limit": 100}, **kw) or {}
        return data.get("releases") or []
