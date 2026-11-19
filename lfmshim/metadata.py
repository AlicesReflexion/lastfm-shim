"""Canonical entity resolution on top of MusicBrainz.

Given free-text Last.fm parameters (artist / album / track names) or MBIDs,
pick one canonical MusicBrainz entity:

* exact (normalised) name or alias matches beat fuzzy matches;
* ambiguous exact matches (e.g. several artists called "Nirvana") are
  disambiguated by ListenBrainz popularity, falling back to MusicBrainz's
  search ranking;
* fuzzy matches are accepted only above a similarity threshold, which is how
  ``autocorrect`` style misspellings ("the beatels") still resolve.

Resolutions are cached (name -> MBID) in addition to the HTTP cache.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from typing import Any

from .errors import UpstreamError
from .upstream.musicbrainz import is_mbid

log = logging.getLogger("lfmshim.metadata")

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_TRAILING_QUALIFIER = re.compile(
    r"\s*(?:[\(\[][^\)\]]*[\)\]]|\s-\s.*(?:remaster|version|mix|edit|live|mono|stereo|demo|deluxe|bonus).*)\s*$",
    re.IGNORECASE,
)
SECONDARY_PENALTY = {"compilation": 3, "live": 2, "soundtrack": 1, "remix": 2, "dj-mix": 3, "mixtape/street": 1,
                     "demo": 2, "interview": 4, "spokenword": 2, "audiobook": 4}
PRIMARY_RANK = {"album": 0, "ep": 1, "single": 2, "broadcast": 3, "other": 4}


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("&", " and ")
    return _NON_WORD.sub(" ", text).strip()


def loose(text: str | None) -> str:
    n = norm(text)
    if n.startswith("the "):
        n = n[4:]
    return n.replace(" ", "")


def base_title(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_QUALIFIER.sub("", text).strip() or prev
    return text


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, loose(a), loose(b)).ratio()


def credit_name(credits: list[dict[str, Any]] | None) -> str:
    return "".join((c.get("name") or (c.get("artist") or {}).get("name", "")) + (c.get("joinphrase") or "")
                   for c in credits or [])


def primary_artist(credits: list[dict[str, Any]] | None) -> dict[str, str]:
    for c in credits or []:
        a = c.get("artist") or {}
        return {"name": a.get("name") or c.get("name") or "", "mbid": a.get("id") or ""}
    return {"name": "", "mbid": ""}


def artist_matches(wanted: str | None, credits: list[dict[str, Any]] | None) -> bool:
    if not wanted:
        return True
    w = loose(wanted)
    names = [credit_name(credits)] + [c.get("name", "") for c in credits or []] + \
            [(c.get("artist") or {}).get("name", "") for c in credits or []]
    return any(loose(n) == w for n in names if n) or any(similar(wanted, n) >= 0.9 for n in names if n)


def merged_tags(entity: dict[str, Any] | None) -> list[tuple[str, int]]:
    counts: dict[str, tuple[str, int]] = {}
    for key in ("genres", "tags"):
        for t in (entity or {}).get(key) or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            k = name.casefold()
            c = int(t.get("count") or 0)
            if k not in counts or counts[k][1] < c:
                counts[k] = (name, c)
    return sorted(counts.values(), key=lambda x: (-x[1], x[0]))


def _date_key(date: str | None) -> str:
    return date if date else "9999"


class Resolver:
    def __init__(self, app: Any):
        self.app = app
        self.mb = app.mb
        self.lb = app.lb
        self.cfg = app.config.upstream

    # ---- helpers -----------------------------------------------------------
    def _cached_choice(self, key: str) -> str | None:
        return self.app.store.cache_get("resolve:" + key)

    def _remember(self, key: str, mbid: str) -> None:
        ttl = self.cfg.cache_ttl if mbid else self.cfg.negative_cache_ttl
        self.app.store.cache_put("resolve:" + key, mbid, ttl)

    def _popularity(self, kind: str, mbids: list[str]) -> dict[str, dict[str, int]]:
        if self.lb is None or self.cfg.offline:
            return {}
        fn = {"artist": self.lb.artist_popularity, "recording": self.lb.recording_popularity,
              "release-group": self.lb.release_group_popularity}[kind]
        try:
            return fn(mbids, self.cfg.popularity_cache_ttl)
        except UpstreamError:
            return {}

    def _pick_by_popularity(self, kind: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        if len(candidates) == 1:
            return candidates[0]
        pop = self._popularity(kind, [c["id"] for c in candidates])
        if not pop:
            return candidates[0]
        # Stable: ties keep MusicBrainz order.
        return max(candidates, key=lambda c: (pop.get(c["id"], {}).get("playcount", 0),
                                              -candidates.index(c)))

    # ---- artists -----------------------------------------------------------
    def artist(self, name: str | None = None, mbid: str | None = None, cache_only: bool = False
               ) -> dict[str, Any] | None:
        """Full MB artist (url-rels, tags, genres, aliases) or None."""
        if mbid and is_mbid(mbid):
            data = self.mb.artist(mbid.lower(), cache_only=cache_only)
            if data:
                return data
        if not name:
            return None
        key = "artist:" + norm(name)
        chosen = self._cached_choice(key)
        if chosen is None:
            if cache_only:
                return None
            chosen = self._search_artist(name)
            self._remember(key, chosen)
        return self.mb.artist(chosen, cache_only=cache_only) if chosen else None

    def _search_artist(self, name: str) -> str:
        results = (self.mb.search_artists(name, limit=15).get("artists")) or []
        if not results:
            return ""
        n, ln = norm(name), loose(name)

        def exact(a: dict[str, Any]) -> bool:
            names = [a.get("name", ""), a.get("sort-name", "")] + [al.get("name", "") for al in a.get("aliases") or []]
            return any(norm(x) == n for x in names) or any(loose(x) == ln for x in names if x)

        exacts = [a for a in results if exact(a)]
        if exacts:
            primary = [a for a in exacts if norm(a.get("name")) == n] or exacts
            return self._pick_by_popularity("artist", primary)["id"]
        best = results[0]
        if int(best.get("score") or 0) >= 85 and similar(name, best.get("name", "")) >= 0.8:
            return best["id"]
        return ""

    def correction(self, name: str) -> dict[str, Any] | None:
        return self.artist(name=name)

    # ---- albums ------------------------------------------------------------
    def album(self, artist: str | None = None, album: str | None = None, mbid: str | None = None,
              cache_only: bool = False) -> dict[str, Any] | None:
        """Returns {"release_group": rg, "release": release-with-tracks} or None."""
        if mbid and is_mbid(mbid):
            mbid = mbid.lower()
            release = self.mb.release(mbid, cache_only=cache_only)
            if release:
                rgid = (release.get("release-group") or {}).get("id")
                rg = self.mb.release_group(rgid, cache_only=cache_only) if rgid else None
                return {"release_group": rg or release.get("release-group") or {}, "release": release}
            rg = self.mb.release_group(mbid, cache_only=cache_only)
            if rg:
                return self._with_release(rg, cache_only)
        if not album:
            return None
        key = "album:" + norm(artist) + "\x1f" + norm(album)
        chosen = self._cached_choice(key)
        if chosen is None:
            if cache_only:
                return None
            chosen = self._search_album(artist, album)
            self._remember(key, chosen)
        if not chosen:
            return None
        rg = self.mb.release_group(chosen, cache_only=cache_only)
        return self._with_release(rg, cache_only) if rg else None

    def _search_album(self, artist: str | None, album: str) -> str:
        results = (self.mb.search_release_groups(album, artist, limit=25).get("release-groups")) or []
        results = [r for r in results if artist_matches(artist, r.get("artist-credit"))]
        if not results:
            return ""
        n, nb = norm(album), norm(base_title(album))

        def rank(r: dict[str, Any]) -> tuple:
            title = norm(r.get("title"))
            exactness = 0 if title == n else (1 if norm(base_title(r.get("title", ""))) == nb else 2)
            primary = PRIMARY_RANK.get((r.get("primary-type") or "other").lower(), 5)
            secondary = sum(SECONDARY_PENALTY.get(s.lower(), 1) for s in r.get("secondary-types") or [])
            return (exactness, secondary, primary, -int(r.get("score") or 0),
                    _date_key(r.get("first-release-date")))

        results.sort(key=rank)
        best = results[0]
        if rank(best)[0] < 2 or similar(album, best.get("title", "")) >= 0.85:
            return best["id"]
        return ""

    def _with_release(self, rg: dict[str, Any], cache_only: bool) -> dict[str, Any]:
        releases = [r for r in rg.get("releases") or [] if (r.get("status") or "").lower() in ("official", "")]
        releases = releases or rg.get("releases") or []
        releases.sort(key=lambda r: (_date_key(r.get("date")), 0 if r.get("country") in ("XW", "US", "GB") else 1))
        release = None
        if releases:
            release = self.mb.release(releases[0]["id"], cache_only=cache_only)
        return {"release_group": rg, "release": release or {}}

    # ---- tracks ------------------------------------------------------------
    def track(self, artist: str | None = None, track: str | None = None, mbid: str | None = None,
              cache_only: bool = False) -> dict[str, Any] | None:
        """Full MB recording (artist-credits, releases w/ release-groups, tags) or None."""
        if mbid and is_mbid(mbid):
            rec = self.mb.recording(mbid.lower(), cache_only=cache_only)
            if rec:
                return rec
        if not track:
            return None
        key = "track:" + norm(artist) + "\x1f" + norm(track)
        chosen = self._cached_choice(key)
        if chosen is None:
            if cache_only:
                return None
            chosen = self._search_track(artist, track)
            self._remember(key, chosen)
        return self.mb.recording(chosen, cache_only=cache_only) if chosen else None

    def _search_track(self, artist: str | None, track: str) -> str:
        results = (self.mb.search_recordings(track, artist, limit=25).get("recordings")) or []
        results = [r for r in results if not r.get("video") and artist_matches(artist, r.get("artist-credit"))]
        if not results:
            return ""
        n, nb = norm(track), norm(base_title(track))
        exact = [r for r in results if norm(r.get("title")) == n]
        loose_hits = exact or [r for r in results if norm(base_title(r.get("title", ""))) == nb]
        if loose_hits:
            with_releases = [r for r in loose_hits if r.get("releases")] or loose_hits
            return self._pick_by_popularity("recording", with_releases[:10])["id"]
        best = results[0]
        if int(best.get("score") or 0) >= 85 and similar(track, best.get("title", "")) >= 0.85:
            return best["id"]
        return ""

    @staticmethod
    def best_release(recording: dict[str, Any], album_hint: str | None = None) -> dict[str, Any] | None:
        """Pick the canonical album release for a recording."""
        releases = recording.get("releases") or []
        if not releases:
            return None
        if album_hint:
            hinted = [r for r in releases if norm(r.get("title")) == norm(album_hint)]
            if hinted:
                releases = hinted

        def rank(r: dict[str, Any]) -> tuple:
            rg = r.get("release-group") or {}
            status = 0 if (r.get("status") or "").lower() == "official" else 1
            primary = PRIMARY_RANK.get((rg.get("primary-type") or "other").lower(), 5)
            secondary = sum(SECONDARY_PENALTY.get(s.lower(), 1) for s in rg.get("secondary-types") or [])
            return (status, secondary, primary, _date_key(r.get("date")))

        return sorted(releases, key=rank)[0]
