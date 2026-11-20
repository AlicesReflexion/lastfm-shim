"""Choosing the public (non-personal) tags shown for albums and artists.

One source supplies each tag list, the first that has data:

* albums: the Discogs master linked from the MusicBrainz release group, else the
  Discogs release linked from the chosen MB release (styles first, then
  genres), else TheAudioDB's album genre, else MusicBrainz tags and genres;
* artists: Discogs styles then genres counted once per distinct album over the
  artist's most-listened studio albums (MB release groups with a Discogs
  link), else TheAudioDB's artist genre, else MusicBrainz tags and genres.

Discogs is slow without a token (24 requests/minute here), so one API call
makes at most ``upstream.discogs_request_budget`` uncached Discogs lookups.
Lookups beyond that are queued for the background worker and used, from the
cache, by later calls. Results are never mixed across sources, so each list
has one clear attribution (``TagResult.links``).

Personal tags (``*.getTags``, ``*.addTags``) are local and untouched here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .errors import UpstreamError
from .metadata import merged_tags
from .upstream.discogs import discogs_links, genres_styles, page_url, web_url

log = logging.getLogger("lfmshim.tags")

PROVIDERS = {"discogs": "Discogs", "theaudiodb": "TheAudioDB", "musicbrainz": "MusicBrainz"}
MB_WEB = "https://musicbrainz.org"
WARM_MARK_TTL = 3600


@dataclass
class TagResult:
    tags: list[tuple[str, int]]
    provider: str  # a PROVIDERS key
    links: list[str] = field(default_factory=list)  # pages the tags came from, for attribution
    complete: bool = True  # False while Discogs lookups are still queued


class Budget:
    """Uncached Discogs lookups one API call may still make (None = unlimited)."""

    def __init__(self, n: int | None):
        self.left = n

    def take(self) -> bool:
        if self.left is None:
            return True
        if self.left <= 0:
            return False
        self.left -= 1
        return True


class TagSources:
    def __init__(self, app: Any):
        self.app = app
        self.cfg = app.config.upstream

    # ---- helpers -----------------------------------------------------------
    def budget(self) -> Budget:
        return Budget(self.cfg.discogs_request_budget)

    def _discogs(self, kind: str, discogs_id: int, budget: Budget) -> tuple[dict[str, Any] | None, bool]:
        """(data, pending): pending means an uncached lookup was deferred."""
        d = self.app.discogs
        if not d.enabled:
            return None, False
        if not d.is_cached(kind, discogs_id):
            if self.cfg.offline:
                return None, False
            if not budget.take():
                return None, True
        return d.lookup(kind, discogs_id, cache_only=self.cfg.offline), False

    def _warm(self, kind: str, mbid: str) -> None:
        """Queue one background pass that fetches the deferred Discogs lookups."""
        if not mbid or self.cfg.offline:
            return
        mark = f"tagwarm:{kind}:{mbid}"
        store = self.app.store
        if store.cache_get(mark) is not None:
            return
        store.cache_put(mark, 1, WARM_MARK_TTL)
        store.enqueue("enrich", {"task": "tags", "kind": kind, "mbid": mbid})
        self.app.sync.wake()

    def warm(self, kind: str, mbid: str) -> None:
        """Background task: resolve the entity again and fetch all its lookups."""
        r = self.app.resolver
        if kind == "artist":
            a = r.artist(mbid=mbid)
            if a:
                self.artist(a, Budget(None))
        elif kind == "album":
            data = r.album(mbid=mbid)
            if data:
                self.album(data, Budget(None))

    @staticmethod
    def _discogs_result(styles: list[str], genres: list[str], links: list[str]) -> TagResult:
        return TagResult([(t, 1) for t in styles + genres], "discogs", links)

    # ---- albums ------------------------------------------------------------
    def album(self, data: dict[str, Any], budget: Budget | None = None) -> TagResult:
        budget = budget or self.budget()
        rg, rel = data.get("release_group") or {}, data.get("release") or {}
        links = [link for link in discogs_links(rg) if link[0] == "master"]
        links += [link for link in discogs_links(rel) if link[0] == "release"]
        pending = False
        for kind, discogs_id in links[:2]:
            d, deferred = self._discogs(kind, discogs_id, budget)
            pending = pending or deferred
            styles, genres = genres_styles(d)
            if styles or genres:
                return self._discogs_result(styles, genres, [page_url(d, kind, discogs_id)])
        if pending:
            self._warm("album", rg.get("id") or rel.get("id") or "")
        fallback = self._album_fallback(rg, rel)
        fallback.complete = not pending
        return fallback

    def _album_fallback(self, rg: dict[str, Any], rel: dict[str, Any]) -> TagResult:
        if rg.get("id"):
            hit = self.app.tadb.genre("album", self.app.tadb.album(rg["id"], cache_only=self.cfg.offline))
            if hit:
                return TagResult([(hit[0], 1)], "theaudiodb", [hit[1]])
        entity = rg or rel
        kind = "release-group" if rg else "release"
        link = [f"{MB_WEB}/{kind}/{entity['id']}"] if entity.get("id") else []
        return TagResult(merged_tags(entity), "musicbrainz", link)

    # ---- artists -----------------------------------------------------------
    def artist(self, a: dict[str, Any], budget: Budget | None = None) -> TagResult:
        budget = budget or self.budget()
        result = self._artist_discogs(a, budget)
        if result and result.tags:
            return result
        pending = bool(result and not result.complete)
        fallback = self._artist_fallback(a)
        fallback.complete = not pending
        return fallback

    def _artist_fallback(self, a: dict[str, Any]) -> TagResult:
        mbid = a.get("id") or ""
        if mbid:
            hit = self.app.tadb.genre("artist", self.app.tadb.artist(mbid, cache_only=self.cfg.offline))
            if hit:
                return TagResult([(hit[0], 1)], "theaudiodb", [hit[1]])
        return TagResult(merged_tags(a), "musicbrainz", [f"{MB_WEB}/artist/{mbid}"] if mbid else [])

    def _artist_albums(self, mbid: str) -> list[dict[str, Any]]:
        """Distinct studio albums with a Discogs link, most listened first."""
        try:
            rgs = self.app.mb.artist_albums(mbid, cache_only=self.cfg.offline)
        except UpstreamError as exc:
            log.info("artist albums unavailable: %s", exc)
            return []
        rgs = [r for r in rgs if r.get("id")]
        rgs = [r for r in rgs if not r.get("secondary-types")] or rgs
        pop: dict[str, dict[str, int]] = {}
        lb = self.app.lb
        if lb is not None and not self.cfg.offline and len(rgs) > 1:
            try:
                pop = lb.release_group_popularity([r["id"] for r in rgs], self.cfg.popularity_cache_ttl)
            except UpstreamError:
                pop = {}
        order = {r["id"]: i for i, r in enumerate(rgs)}
        rgs.sort(key=lambda r: (-(pop.get(r["id"]) or {}).get("listeners", 0),
                                r.get("first-release-date") or "9999", order[r["id"]]))
        return rgs

    def _artist_discogs(self, a: dict[str, Any], budget: Budget) -> TagResult | None:
        mbid = a.get("id") or ""
        limit = self.cfg.discogs_artist_albums
        if not mbid or not limit or not self.app.discogs.enabled:
            return None
        picked: list[tuple[str, int]] = []
        for rg in self._artist_albums(mbid):
            links = discogs_links(rg)
            link = next((x for x in links if x[0] == "master"), None) or next(
                (x for x in links if x[0] == "release"), None)
            if link and link not in picked:
                picked.append(link)
            if len(picked) >= limit:
                break
        styles: dict[str, list[Any]] = {}
        genres: dict[str, list[Any]] = {}
        counted: set[tuple[str, int]] = set()
        used: list[str] = []
        pending = False
        for kind, discogs_id in picked:
            d, deferred = self._discogs(kind, discogs_id, budget)
            pending = pending or deferred
            if not d:
                continue
            # Two release groups may point into the same Discogs master.
            key = ("master", int(d["master_id"])) if kind == "release" and d.get("master_id") else (kind, discogs_id)
            album_styles, album_genres = genres_styles(d)
            if key in counted or not (album_styles or album_genres):
                continue
            counted.add(key)
            used.append(page_url(d, kind, discogs_id))
            for names, bucket in ((album_styles, styles), (album_genres, genres)):
                for name in names:  # once per album: genres_styles de-duplicates
                    entry = bucket.setdefault(name.casefold(), [name, 0, len(bucket)])
                    entry[1] += 1
        if pending:
            self._warm("artist", mbid)
        if not counted:
            return TagResult([], "discogs", [], complete=not pending)

        def ranked(bucket: dict[str, list[Any]], skip: set[str]) -> list[tuple[str, int]]:
            rows = sorted((e for k, e in bucket.items() if k not in skip), key=lambda e: (-e[1], e[2]))
            return [(e[0], e[1]) for e in rows]

        tags = ranked(styles, set()) + ranked(genres, set(styles))
        artist_pages = [web_url("artist", i) for k, i in discogs_links(a) if k == "artist"][:1]
        return TagResult(tags, "discogs", artist_pages + used, complete=not pending)


def sources_view(result: TagResult) -> list[dict[str, str]]:
    """The opt-in ``source`` elements (``tagsources=1``) for a tag list."""
    name = PROVIDERS[result.provider]
    return [{"name": name, "provider": result.provider, "url": url} for url in result.links]
