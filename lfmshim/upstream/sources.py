"""Wikipedia/Wikidata summaries, TheAudioDB artist art, Cover Art Archive."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from ..errors import UpstreamError

log = logging.getLogger("lfmshim.upstream")


def _rels(entity: dict[str, Any] | None, rel_type: str) -> list[str]:
    out = []
    for rel in (entity or {}).get("relations") or []:
        if rel.get("type") == rel_type and (rel.get("url") or {}).get("resource"):
            if rel.get("ended"):
                continue
            out.append(rel["url"]["resource"])
    return out


class Wikipedia:
    """Resolves an MB entity's Wikipedia/Wikidata relations to a page summary."""

    def __init__(self, http, lang: str, wikipedia_url: str, wikidata_url: str):
        self.http = http
        self.lang = lang
        self.wp_base = (wikipedia_url or f"https://{lang}.wikipedia.org").rstrip("/")
        self.wd_base = wikidata_url.rstrip("/")

    def title_for(self, entity: dict[str, Any] | None) -> str | None:
        host = urlsplit(self.wp_base).hostname
        for url in _rels(entity, "wikipedia"):
            parts = urlsplit(url)
            if parts.hostname == host and parts.path.startswith("/wiki/"):
                return unquote(parts.path[len("/wiki/"):])
        for url in _rels(entity, "wikidata"):
            qid = url.rstrip("/").rsplit("/", 1)[-1]
            if not qid.startswith("Q"):
                continue
            # wbgetentities with a sitelink filter is a few hundred bytes; the
            # full Special:EntityData dump can be megabytes for famous artists.
            data = self.http.get_json("wikidata", f"{self.wd_base}/w/api.php",
                                      {"action": "wbgetentities", "ids": qid, "props": "sitelinks",
                                       "sitefilter": f"{self.lang}wiki", "format": "json"}, optional=True)
            entity_data = ((data or {}).get("entities") or {})
            # Wikidata may redirect the QID; take whichever entity came back.
            for ent in entity_data.values():
                link = ((ent.get("sitelinks") or {}).get(f"{self.lang}wiki") or {}).get("title")
                if link:
                    return link
        return None

    def summary(self, entity: dict[str, Any] | None) -> dict[str, Any] | None:
        """Returns {"title", "extract", "url", "timestamp"} or None."""
        try:
            title = self.title_for(entity)
            if not title:
                return None
            data = self.http.get_json("wikipedia", f"{self.wp_base}/api/rest_v1/page/summary/"
                                      + quote(title.replace(" ", "_"), safe=""), optional=True)
        except UpstreamError as exc:
            log.info("wikipedia lookup failed: %s", exc)
            return None
        if not data or data.get("type") == "disambiguation" or not data.get("extract"):
            return None
        url = (((data.get("content_urls") or {}).get("desktop") or {}).get("page")
               or f"{self.wp_base}/wiki/{quote(title.replace(' ', '_'))}")
        return {"title": data.get("title") or title, "extract": data["extract"], "url": url,
                "timestamp": data.get("timestamp") or ""}


class TheAudioDB:
    def __init__(self, http, base_url: str, key: str, enabled: bool = True):
        self.http = http
        self.base = base_url.rstrip("/")
        self.key = key
        self.enabled = enabled

    WEB = "https://www.theaudiodb.com"

    def _lookup(self, endpoint: str, list_key: str, mbid: str, **kw) -> dict[str, Any]:
        if not self.enabled or not mbid:
            return {}
        try:
            data = self.http.get_json("theaudiodb", f"{self.base}/{self.key}/{endpoint}", {"i": mbid},
                                      optional=True, **kw)
        except UpstreamError as exc:
            log.info("theaudiodb lookup failed: %s", exc)
            return {}
        items = (data or {}).get(list_key) if isinstance(data, dict) else None
        return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}

    def artist(self, mbid: str, **kw) -> dict[str, Any]:
        """Raw TheAudioDB artist for an MB artist MBID (the request artist_images makes)."""
        return self._lookup("artist-mb.php", "artists", mbid, **kw)

    def album(self, rg_mbid: str, **kw) -> dict[str, Any]:
        """Raw TheAudioDB album for an MB release-group MBID."""
        return self._lookup("album-mb.php", "album", rg_mbid, **kw)

    def genre(self, kind: str, item: dict[str, Any]) -> tuple[str, str] | None:
        """(strGenre, public page URL) of an artist/album lookup, or None."""
        name = (item.get("strGenre") or "").strip() if isinstance(item.get("strGenre"), str) else ""
        ident = str(item.get("idArtist" if kind == "artist" else "idAlbum") or "")
        if not name:
            return None
        return name, f"{self.WEB}/{kind}/{ident}" if ident.isdigit() else self.WEB

    def artist_images(self, mbid: str, **kw) -> dict[str, str]:
        a = self.artist(mbid, **kw)
        if not a:
            return {}
        out = {}
        for field in ("strArtistThumb", "strArtistFanart", "strArtistWideThumb", "strArtistBanner",
                      "strArtistLogo", "strArtistCutout"):
            if a.get(field):
                out[field] = a[field]
        return out


class CoverArtArchive:
    def __init__(self, http, base_url: str):
        self.http = http
        self.base = base_url.rstrip("/")

    def front(self, kind: str, mbid: str, **kw) -> dict[str, str]:
        """kind: 'release-group' | 'release'. Returns {"250","500","1200","image"}."""
        if not mbid:
            return {}
        try:
            data = self.http.get_json("coverart", f"{self.base}/{kind}/{mbid}", optional=True, **kw)
        except UpstreamError as exc:
            log.info("cover art lookup failed: %s", exc)
            return {}
        for img in (data or {}).get("images") or []:
            if img.get("front"):
                thumbs = img.get("thumbnails") or {}
                out = {"image": img.get("image", "")}
                for size, alias in (("250", "small"), ("500", "large"), ("1200", None)):
                    url = thumbs.get(size) or (thumbs.get(alias) if alias else None)
                    if url:
                        out[size] = url
                return {k: _https(v) for k, v in out.items() if v}
        return {}


def _https(url: str) -> str:
    return "https://" + url[len("http://"):] if url.startswith("http://") else url
