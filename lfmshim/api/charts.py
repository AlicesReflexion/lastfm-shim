"""chart.*, geo.* and tag.* methods."""

from __future__ import annotations

from typing import Any

from ..metadata import credit_name, primary_artist
from ..upstream.musicbrainz import lucene
from .common import images, lfm_url, paging_attr, tag_url
from .music import STREAMABLE, artist_image_list, artist_ref, caa_direct
from .registry import Request, method
from .user import week_ranges


def _sitewide(req: Request, entity: str, page: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    if req.app.lb is None or req.app.config.upstream.offline:
        return [], 0
    payload = req.app.lb.sitewide(entity, limit, (page - 1) * limit, req.app.config.upstream.chart_range,
                                  req.app.config.upstream.popularity_cache_ttl)
    key = entity.replace("-", "_")
    rows = payload.get(key) or []
    total = int(payload.get(f"total_{key[:-1]}_count") or len(rows))
    return rows, total


def _chart_artists(req: Request, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": r.get("artist_name", ""), "playcount": int(r.get("listen_count") or 0), "listeners": 0,
             "mbid": r.get("artist_mbid") or "", "url": lfm_url(r.get("artist_name", "")), "streamable": "0",
             "image": artist_image_list(req, r.get("artist_mbid") or "", cache_only=True)} for r in rows]


def _chart_tracks(rows: list[dict[str, Any]], start_rank: int | None = None) -> list[dict[str, Any]]:
    out = []
    for i, r in enumerate(rows, start=start_rank or 1):
        artist = r.get("artist_name", "")
        mbids = r.get("artist_mbids") or []
        t = {"name": r.get("track_name", ""), "duration": 0, "playcount": int(r.get("listen_count") or 0),
             "listeners": 0, "mbid": r.get("recording_mbid") or "", "url": lfm_url(artist, "_", r.get("track_name", "")),
             "streamable": dict(STREAMABLE), "artist": artist_ref(artist, mbids[0] if mbids else ""),
             "image": caa_direct(r.get("caa_release_mbid"), r.get("caa_id"))}
        if start_rank is not None:
            t["@attr"] = {"rank": i}
        out.append(t)
    return out


@method("chart.getTopArtists", support="approximate",
        notes="ListenBrainz sitewide artist stats (upstream.chart_range); listeners unavailable (0)")
def chart_get_top_artists(req: Request):
    page, limit = req.paging(50, 1000)
    rows, total = _sitewide(req, "artists", page, limit)
    return {"artists": {"artist": _chart_artists(req, rows), "@attr": paging_attr(page, limit, total)}}


@method("chart.getTopTracks", support="approximate",
        notes="ListenBrainz sitewide recording stats; listeners unavailable (0)")
def chart_get_top_tracks(req: Request):
    page, limit = req.paging(50, 1000)
    rows, total = _sitewide(req, "recordings", page, limit)
    return {"tracks": {"track": _chart_tracks(rows), "@attr": paging_attr(page, limit, total)}}


@method("chart.getTopTags", support="stub", notes="no global tag ranking upstream; tags used locally")
def chart_get_top_tags(req: Request):
    page, limit = req.paging(50, 1000)
    rows = req.app.store.global_tags(limit, (page - 1) * limit)
    tags = [{"name": r["tag"], "url": tag_url(r["tag"]), "reach": r["reach"], "taggings": r["n"],
             "streamable": "0", "wiki": {}} for r in rows]
    return {"tags": {"tag": tags, "@attr": paging_attr(page, limit, len(tags))}}


@method("geo.getTopArtists", support="approximate", notes="no per-country data upstream: global chart")
def geo_get_top_artists(req: Request):
    country = req.require("country")
    page, limit = req.paging(50, 1000)
    rows, total = _sitewide(req, "artists", page, limit)
    return {"topartists": {"artist": _chart_artists(req, rows),
                           "@attr": paging_attr(page, limit, total, country=country)}}


@method("geo.getTopTracks", support="approximate", notes="no per-country data upstream: global chart")
def geo_get_top_tracks(req: Request):
    country = req.require("country")
    page, limit = req.paging(50, 1000)
    rows, total = _sitewide(req, "recordings", page, limit)
    return {"tracks": {"track": _chart_tracks(rows, (page - 1) * limit + 1),
                       "@attr": paging_attr(page, limit, total, country=country)}}


# ---- tag.* -------------------------------------------------------------------
def _tag_search(req: Request, entity: str, extra: str = "") -> tuple[list[dict[str, Any]], int, int, int, str]:
    tag = req.require("tag")
    page, limit = req.paging(50, 100)
    q = f'tag:"{lucene(tag.lower())}"' + extra
    data = req.app.mb.search(entity, q, limit=limit, offset=(page - 1) * limit)
    key = {"artist": "artists", "release-group": "release-groups", "recording": "recordings"}[entity]
    return data.get(key) or [], int(data.get("count") or 0), page, limit, tag


@method("tag.getInfo", support="approximate", notes="reach/total = MusicBrainz tagged-artist count; no wiki")
def tag_get_info(req: Request):
    tag = req.require("tag")
    data = req.app.mb.search("artist", f'tag:"{lucene(tag.lower())}"', limit=1)
    count = int(data.get("count") or 0)
    return {"tag": {"name": tag.lower(), "total": count, "reach": count,
                    "wiki": {"summary": "", "content": ""}}}


@method("tag.getTopArtists", support="approximate", notes="MusicBrainz artists with the tag, by search rank")
def tag_get_top_artists(req: Request):
    rows, total, page, limit, tag = _tag_search(req, "artist")
    out = [{"name": a["name"], "mbid": a["id"], "url": lfm_url(a["name"]), "streamable": "0",
            "image": artist_image_list(req, a["id"], cache_only=True), "@attr": {"rank": i}}
           for i, a in enumerate(rows, start=(page - 1) * limit + 1)]
    return {"topartists": {"artist": out, "@attr": paging_attr(page, limit, total, tag=tag)}}


@method("tag.getTopAlbums", support="approximate", notes="MusicBrainz album release groups with the tag")
def tag_get_top_albums(req: Request):
    rows, total, page, limit, tag = _tag_search(req, "release-group", " AND primarytype:album")
    out = []
    for i, r in enumerate(rows, start=(page - 1) * limit + 1):
        pa = primary_artist(r.get("artist-credit"))
        artist = credit_name(r.get("artist-credit")) or pa["name"]
        out.append({"name": r.get("title", ""), "mbid": r["id"], "url": lfm_url(artist, r.get("title", "")),
                    "artist": artist_ref(artist, pa["mbid"]), "image": images(), "@attr": {"rank": i}})
    return {"albums": {"album": out, "@attr": paging_attr(page, limit, total, tag=tag)}}


@method("tag.getTopTracks", support="approximate", notes="MusicBrainz recordings with the tag")
def tag_get_top_tracks(req: Request):
    rows, total, page, limit, tag = _tag_search(req, "recording")
    out = []
    for i, r in enumerate(rows, start=(page - 1) * limit + 1):
        pa = primary_artist(r.get("artist-credit"))
        artist = credit_name(r.get("artist-credit")) or pa["name"]
        out.append({"name": r.get("title", ""), "duration": int((r.get("length") or 0) / 1000), "mbid": r["id"],
                    "url": lfm_url(artist, "_", r.get("title", "")), "streamable": dict(STREAMABLE),
                    "artist": artist_ref(artist, pa["mbid"]), "image": images(), "@attr": {"rank": i}})
    return {"tracks": {"track": out, "@attr": paging_attr(page, limit, total, tag=tag)}}


@method("tag.getSimilar", support="stub", notes="no tag similarity upstream; always empty")
def tag_get_similar(req: Request):
    return {"similartags": {"tag": [], "@attr": {"tag": req.require("tag")}}}


@method("tag.getTopTags", support="stub", notes="tags used locally, not a global ranking")
def tag_get_top_tags(req: Request):
    rows = req.app.store.global_tags(50, 0)
    return {"toptags": {"@attr": {"offset": 0, "num_res": len(rows), "total": len(rows)},
                        "tag": [{"name": r["tag"], "count": r["n"], "reach": r["reach"]} for r in rows]}}


@method("tag.getWeeklyChartList", support="approximate", notes="generated Sunday-noon weeks; no tag charts")
def tag_get_weekly_chart_list(req: Request):
    tag = req.require("tag")
    now = req.app.now()
    weeks = week_ranges(now - 52 * 7 * 86400, now)
    return {"weeklychartlist": {"chart": [{"#text": "", "from": f, "to": t} for f, t in weeks],
                                "@attr": {"tag": tag}}}
