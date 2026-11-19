"""artist.*, album.* and track.* read methods (and their personal-tag writes)."""

from __future__ import annotations

from typing import Any

from ..errors import INVALID_PARAMS, LastFmError, UpstreamError, not_found
from ..metadata import Resolver, credit_name, merged_tags, norm, primary_artist
from .common import (artist_images, cover_images, images, lfm_url, opensearch, paging_attr, tag_url, toptags,
                     user_url, wiki)
from .registry import Request, method

STREAMABLE = {"#text": "0", "fulltrack": "0"}


# ---------------------------------------------------------------------------
# shared views
# ---------------------------------------------------------------------------
def _resolver(req: Request) -> Resolver:
    return req.app.resolver


def _user_for(req: Request) -> str | None:
    """The user for personalised fields: ``username`` param, else the session."""
    name = req.get("username") or req.user
    if name and req.app.store.get_user(name):
        return req.app.store.get_user(name)["name"]
    return None


def artist_ref(name: str, mbid: str = "") -> dict[str, Any]:
    return {"name": name, "mbid": mbid or "", "url": lfm_url(name)}


def artist_image_list(req: Request, mbid: str, cache_only: bool = False) -> list[dict[str, str]]:
    return artist_images(req.app.tadb.artist_images(mbid, cache_only=cache_only)) if mbid else images()


def album_cover(req: Request, rg_mbid: str = "", release_mbid: str = "", cache_only: bool = False
                ) -> list[dict[str, str]]:
    caa = req.app.caa
    cover = caa.front("release-group", rg_mbid, cache_only=cache_only) if rg_mbid else {}
    if not cover and release_mbid:
        cover = caa.front("release", release_mbid, cache_only=cache_only)
    return cover_images(cover)


CAA_BASE = "https://coverartarchive.org"


def caa_direct(release_mbid: str | None, caa_id: Any) -> list[dict[str, str]]:
    """Image URLs from ListenBrainz's caa_release_mbid/caa_id without a lookup."""
    if not release_mbid or not caa_id:
        return images()
    base = f"{CAA_BASE}/release/{release_mbid}/{caa_id}"
    return images(f"{base}-250.jpg", f"{base}-500.jpg", f"{base}-1200.jpg")


def _popularity(req: Request, kind: str, mbid: str) -> dict[str, int]:
    lb = req.app.lb
    if not mbid or lb is None or req.app.config.upstream.offline:
        return {"listeners": 0, "playcount": 0}
    fn = {"artist": lb.artist_popularity, "recording": lb.recording_popularity,
          "release-group": lb.release_group_popularity}[kind]
    try:
        return fn([mbid], req.app.config.upstream.popularity_cache_ttl).get(mbid) or {"listeners": 0, "playcount": 0}
    except UpstreamError:
        return {"listeners": 0, "playcount": 0}


def _bulk_popularity(req: Request, kind: str, mbids: list[str]) -> dict[str, dict[str, int]]:
    lb = req.app.lb
    if lb is None or req.app.config.upstream.offline:
        return {}
    fn = {"artist": lb.artist_popularity, "recording": lb.recording_popularity,
          "release-group": lb.release_group_popularity}[kind]
    try:
        return fn(mbids, req.app.config.upstream.popularity_cache_ttl)
    except UpstreamError:
        return {}


def resolve_artist(req: Request) -> dict[str, Any]:
    mbid, name = req.get("mbid"), req.get("artist")
    if not mbid and not name:
        raise not_found("artist")
    data = _resolver(req).artist(name=name, mbid=mbid)
    if not data:
        raise not_found("artist")
    return data


def resolve_album(req: Request) -> dict[str, Any]:
    mbid, artist, album = req.get("mbid"), req.get("artist"), req.get("album")
    if not mbid and not (artist and album):
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - Your request is missing a required parameter")
    data = _resolver(req).album(artist=artist, album=album, mbid=mbid)
    if not data:
        raise not_found("album")
    return data


def resolve_track(req: Request) -> dict[str, Any]:
    mbid, artist, track = req.get("mbid"), req.get("artist"), req.get("track")
    if not mbid and not (artist and track):
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - Your request is missing a required parameter")
    data = _resolver(req).track(artist=artist, track=track, mbid=mbid)
    if not data:
        raise not_found("track")
    return data


def _similar_artists(req: Request, mbid: str, limit: int, cache_only_images: bool = True) -> list[dict[str, Any]]:
    lb = req.app.lb
    if not mbid or lb is None or req.app.config.upstream.offline:
        return []
    try:
        rows = lb.similar_artists(mbid, req.app.config.upstream.popularity_cache_ttl)
    except UpstreamError:
        return []
    rows = [r for r in rows if r.get("name") or r.get("artist_name")][:limit]
    top = max((float(r.get("score") or 0) for r in rows), default=0) or 1
    out = []
    for r in rows:
        name = r.get("name") or r.get("artist_name")
        m = r.get("artist_mbid") or ""
        out.append({"name": name, "mbid": m, "match": f"{float(r.get('score') or 0) / top:.6f}".rstrip("0"),
                    "url": lfm_url(name), "image": artist_image_list(req, m, cache_only=cache_only_images),
                    "streamable": "0"})
    return out


# ---------------------------------------------------------------------------
# artist.*
# ---------------------------------------------------------------------------
@method("artist.getInfo", notes="MusicBrainz + Wikipedia bio + TheAudioDB image + ListenBrainz stats/similar")
def artist_get_info(req: Request):
    a = resolve_artist(req)
    name, mbid = a["name"], a["id"]
    pop = _popularity(req, "artist", mbid)
    stats: dict[str, Any] = {"listeners": pop["listeners"], "playcount": pop["playcount"]}
    user = _user_for(req)
    if user:
        stats["userplaycount"] = req.app.store.playcount(user, name)
    out: dict[str, Any] = {
        "name": name, "mbid": mbid, "url": lfm_url(name), "image": artist_image_list(req, mbid),
        "streamable": "0", "ontour": "0", "stats": stats,
        "similar": {"artist": [{k: v for k, v in s.items() if k in ("name", "url", "image")}
                               for s in _similar_artists(req, mbid, 5)]},
        "tags": {"tag": toptags(merged_tags(a), 5)},
    }
    bio = wiki(req.app.wiki.summary(a), lfm_url(name), req.app.now())
    out["bio"] = bio or {"links": {"link": {"#text": "", "rel": "original", "href": lfm_url(name) + "/+wiki"}},
                         "published": "", "summary": "", "content": ""}
    return {"artist": out}


@method("artist.getCorrection")
def artist_get_correction(req: Request):
    name = req.require("artist")
    a = _resolver(req).artist(name=name)
    if not a:
        raise not_found("artist")
    if a["name"] == name:
        return {"corrections": {}}
    return {"corrections": {"correction": {"artist": artist_ref(a["name"], a["id"]), "@attr": {"index": "0"}}}}


@method("artist.getSimilar", support="approximate",
        notes="ListenBrainz Labs session-based similarity; match is score normalised to the top result")
def artist_get_similar(req: Request):
    a = resolve_artist(req)
    limit = req.int("limit", 100, minimum=1, maximum=250)
    return {"similarartists": {"artist": _similar_artists(req, a["id"], limit), "@attr": {"artist": a["name"]}}}


@method("artist.getTopTracks", support="approximate", notes="ListenBrainz top recordings for the artist")
def artist_get_top_tracks(req: Request):
    a = resolve_artist(req)
    page, limit = req.paging(50, 1000)
    rows = []
    if req.app.lb is not None and not req.app.config.upstream.offline:
        try:
            rows = req.app.lb.top_recordings_for_artist(a["id"], req.app.config.upstream.popularity_cache_ttl)
        except UpstreamError:
            rows = []
    total = len(rows)
    chunk = rows[(page - 1) * limit: page * limit]
    tracks = []
    for i, r in enumerate(chunk, start=(page - 1) * limit + 1):
        name = r.get("recording_name") or ""
        tracks.append({
            "name": name, "playcount": int(r.get("total_listen_count") or 0),
            "listeners": int(r.get("total_user_count") or 0), "mbid": r.get("recording_mbid") or "",
            "url": lfm_url(a["name"], "_", name), "streamable": dict(STREAMABLE),
            "artist": artist_ref(a["name"], a["id"]),
            "image": caa_direct(r.get("caa_release_mbid"), r.get("caa_id")),
            "@attr": {"rank": i},
        })
    return {"toptracks": {"track": tracks, "@attr": paging_attr(page, limit, total, artist=a["name"])}}


@method("artist.getTopAlbums", support="approximate", notes="ListenBrainz top release groups for the artist")
def artist_get_top_albums(req: Request):
    a = resolve_artist(req)
    page, limit = req.paging(50, 1000)
    rows = []
    if req.app.lb is not None and not req.app.config.upstream.offline:
        try:
            rows = req.app.lb.top_release_groups_for_artist(a["id"], req.app.config.upstream.popularity_cache_ttl)
        except UpstreamError:
            rows = []
    total = len(rows)
    albums = []
    for i, r in enumerate(rows[(page - 1) * limit: page * limit], start=(page - 1) * limit + 1):
        rg = r.get("release_group") or {}
        name = rg.get("name") or (r.get("release") or {}).get("name") or ""
        albums.append({
            "name": name, "playcount": int(r.get("total_listen_count") or 0),
            "mbid": r.get("release_group_mbid") or "", "url": lfm_url(a["name"], name),
            "artist": artist_ref(a["name"], a["id"]),
            "image": caa_direct(rg.get("caa_release_mbid"), rg.get("caa_id")),
            "@attr": {"rank": i},
        })
    return {"topalbums": {"album": albums, "@attr": paging_attr(page, limit, total, artist=a["name"])}}


@method("artist.getTopTags", notes="MusicBrainz tags+genres; count scaled so the top tag is 100")
def artist_get_top_tags(req: Request):
    a = resolve_artist(req)
    return {"toptags": {"tag": toptags(merged_tags(a), 100, with_count=True), "@attr": {"artist": a["name"]}}}


@method("artist.search", notes="MusicBrainz artist search; listeners from ListenBrainz")
def artist_search(req: Request):
    q = req.require("artist")
    page, limit = req.paging(30, 100)
    data = req.app.mb.search_artists(q, limit=limit, offset=(page - 1) * limit)
    arts = data.get("artists") or []
    pop = _bulk_popularity(req, "artist", [a["id"] for a in arts])
    matches = [{"name": a["name"], "listeners": pop.get(a["id"], {}).get("listeners", 0), "mbid": a["id"],
                "url": lfm_url(a["name"]), "streamable": "0",
                "image": artist_image_list(req, a["id"], cache_only=True)} for a in arts]
    return {"results": opensearch({"searchTerms": q}, int(data.get("count") or len(arts)), page, limit,
                                  "artistmatches", {"artist": matches}, q)}


# ---------------------------------------------------------------------------
# album.*
# ---------------------------------------------------------------------------
def _album_names(data: dict[str, Any]) -> tuple[str, str, str, str]:
    rg, rel = data["release_group"] or {}, data["release"] or {}
    title = rel.get("title") or rg.get("title") or ""
    credits = rg.get("artist-credit") or rel.get("artist-credit")
    artist = credit_name(credits) or primary_artist(credits)["name"]
    return title, artist, rel.get("id") or "", rg.get("id") or ""


@method("album.getInfo", notes="MusicBrainz release group + earliest official release; CAA cover; Wikipedia wiki")
def album_get_info(req: Request):
    data = resolve_album(req)
    title, artist, release_mbid, rg_mbid = _album_names(data)
    rel = data["release"] or {}
    pop = _popularity(req, "release-group", rg_mbid)
    tracks = []
    rank = 0
    for medium in rel.get("media") or []:
        for t in medium.get("tracks") or []:
            rank += 1
            credits = t.get("artist-credit") or (t.get("recording") or {}).get("artist-credit")
            pa = primary_artist(credits) if credits else {"name": artist, "mbid": ""}
            tname = t.get("title") or (t.get("recording") or {}).get("title") or ""
            length = t.get("length") or (t.get("recording") or {}).get("length")
            tracks.append({"streamable": dict(STREAMABLE), "duration": int(length / 1000) if length else None,
                           "url": lfm_url(pa["name"] or artist, "_", tname), "name": tname,
                           "@attr": {"rank": rank}, "artist": artist_ref(pa["name"] or artist, pa["mbid"])})
    out: dict[str, Any] = {
        "artist": artist, "mbid": release_mbid or rg_mbid, "tags": {"tag": toptags(merged_tags(data["release_group"]
                                                                                               or rel), 5)},
        "name": title, "image": album_cover(req, rg_mbid, release_mbid),
        "tracks": {"track": tracks}, "listeners": pop["listeners"], "playcount": pop["playcount"],
        "url": lfm_url(artist, title),
    }
    user = _user_for(req)
    if user:
        out["userplaycount"] = req.app.store.playcount(user, artist, album=title)
    w = wiki(req.app.wiki.summary(data["release_group"]), lfm_url(artist, title), req.app.now())
    if w:
        out["wiki"] = {k: w[k] for k in ("published", "summary", "content")}
    return {"album": out}


@method("album.getTopTags", notes="MusicBrainz release-group tags+genres")
def album_get_top_tags(req: Request):
    data = resolve_album(req)
    title, artist, _, _ = _album_names(data)
    tags = merged_tags(data["release_group"] or data["release"])
    return {"toptags": {"tag": toptags(tags, 100, with_count=True), "@attr": {"artist": artist, "album": title}}}


@method("album.search", notes="MusicBrainz release-group search")
def album_search(req: Request):
    q = req.require("album")
    page, limit = req.paging(30, 100)
    data = req.app.mb.search_release_groups(q, None, limit=limit, offset=(page - 1) * limit)
    rgs = data.get("release-groups") or []
    matches = []
    for r in rgs:
        artist = credit_name(r.get("artist-credit"))
        matches.append({"name": r.get("title", ""), "artist": artist, "url": lfm_url(artist, r.get("title", "")),
                        "image": album_cover(req, r["id"], cache_only=True), "streamable": "0", "mbid": r["id"]})
    return {"results": opensearch({"searchTerms": q}, int(data.get("count") or len(rgs)), page, limit,
                                  "albummatches", {"album": matches}, q)}


# ---------------------------------------------------------------------------
# track.*
# ---------------------------------------------------------------------------
def _track_core(rec: dict[str, Any]) -> tuple[str, dict[str, str]]:
    credits = rec.get("artist-credit")
    pa = primary_artist(credits)
    return rec.get("title") or "", {"name": credit_name(credits) or pa["name"], "mbid": pa["mbid"]}


@method("track.getInfo", notes="MusicBrainz recording; album = canonical official album release")
def track_get_info(req: Request):
    rec = resolve_track(req)
    name, artist = _track_core(rec)
    pop = _popularity(req, "recording", rec["id"])
    out: dict[str, Any] = {
        "name": name, "mbid": rec["id"], "url": lfm_url(artist["name"], "_", name),
        "duration": rec.get("length") or 0, "streamable": dict(STREAMABLE),
        "listeners": pop["listeners"], "playcount": pop["playcount"],
        "artist": artist_ref(artist["name"], artist["mbid"]),
    }
    release = Resolver.best_release(rec, req.get("album"))
    if release:
        rg = release.get("release-group") or {}
        album_artist = credit_name(release.get("artist-credit")) or artist["name"]
        out["album"] = {"artist": album_artist, "title": release.get("title", ""), "mbid": release.get("id", ""),
                        "url": lfm_url(album_artist, release.get("title", "")),
                        "image": album_cover(req, rg.get("id", ""), release.get("id", ""))}
    user = _user_for(req)
    if user:
        out["userplaycount"] = req.app.store.playcount(user, artist["name"], track=name)
        out["userloved"] = "1" if req.app.store.is_loved(user, artist["name"], name) else "0"
    out["toptags"] = {"tag": toptags(merged_tags(rec), 5)}
    w = wiki(req.app.wiki.summary(rec), out["url"], req.app.now())
    if w:
        out["wiki"] = {k: w[k] for k in ("published", "summary", "content")}
    return {"track": out}


@method("track.getCorrection")
def track_get_correction(req: Request):
    artist, track = req.require("artist"), req.require("track")
    rec = _resolver(req).track(artist=artist, track=track)
    if not rec:
        raise not_found("track")
    name, a = _track_core(rec)
    if name == track and a["name"] == artist:
        return {"corrections": {}}
    return {"corrections": {"correction": {
        "track": {"name": name, "mbid": rec["id"], "url": lfm_url(a["name"], "_", name),
                  "artist": artist_ref(a["name"], a["mbid"])},
        "@attr": {"index": "0", "artistcorrected": "1" if a["name"] != artist else "0",
                  "trackcorrected": "1" if name != track else "0"}}}}


@method("track.getSimilar", support="approximate", notes="ListenBrainz Labs similar recordings")
def track_get_similar(req: Request):
    rec = resolve_track(req)
    name, artist = _track_core(rec)
    limit = req.int("limit", 100, minimum=1, maximum=250)
    rows = []
    if req.app.lb is not None and not req.app.config.upstream.offline:
        try:
            rows = req.app.lb.similar_recordings(rec["id"], req.app.config.upstream.popularity_cache_ttl)
        except UpstreamError:
            rows = []
    rows = [r for r in rows if r.get("recording_name") or r.get("name")][:limit]
    top = max((float(r.get("score") or 0) for r in rows), default=0) or 1
    tracks = []
    for r in rows:
        tname = r.get("recording_name") or r.get("name")
        aname = r.get("artist_credit_name") or r.get("artist_name") or ""
        ambids = r.get("artist_credit_mbids") or r.get("artist_mbids") or []
        tracks.append({"name": tname, "playcount": 0, "mbid": r.get("recording_mbid") or "",
                       "match": f"{float(r.get('score') or 0) / top:.6f}".rstrip("0"),
                       "url": lfm_url(aname, "_", tname), "streamable": dict(STREAMABLE),
                       "duration": int((r.get("length") or 0) / 1000) or None,
                       "artist": artist_ref(aname, ambids[0] if ambids else ""),
                       "image": caa_direct(r.get("caa_release_mbid"), r.get("caa_id"))})
    return {"similartracks": {"track": tracks, "@attr": {"artist": artist["name"]}}}


@method("track.getTopTags", notes="MusicBrainz recording tags+genres")
def track_get_top_tags(req: Request):
    rec = resolve_track(req)
    name, artist = _track_core(rec)
    return {"toptags": {"tag": toptags(merged_tags(rec), 100, with_count=True),
                        "@attr": {"artist": artist["name"], "track": name}}}


@method("track.search", notes="MusicBrainz recording search; listeners from ListenBrainz")
def track_search(req: Request):
    q = req.require("track")
    artist = req.get("artist")
    page, limit = req.paging(30, 100)
    data = req.app.mb.search_recordings(q, artist, limit=limit, offset=(page - 1) * limit)
    recs = [r for r in data.get("recordings") or [] if not r.get("video")]
    pop = _bulk_popularity(req, "recording", [r["id"] for r in recs])
    matches = []
    for r in recs:
        name, a = _track_core(r)
        matches.append({"name": name, "artist": a["name"], "url": lfm_url(a["name"], "_", name),
                        "streamable": "0", "listeners": pop.get(r["id"], {}).get("listeners", 0),
                        "image": images(), "mbid": r["id"]})
    query = {"searchTerms": q}
    return {"results": opensearch(query, int(data.get("count") or len(recs)), page, limit, "trackmatches",
                                  {"track": matches}, q)}


# ---------------------------------------------------------------------------
# personal tags: {artist,album,track}.{getTags,addTags,removeTag}
# ---------------------------------------------------------------------------
def _tag_target(req: Request, kind: str) -> tuple[str, str]:
    artist = req.require("artist")
    if kind == "artist":
        return artist, ""
    return artist, req.require("album" if kind == "album" else "track")


def _split_tags(raw: str) -> list[str]:
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    if len(tags) > 10:
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - A maximum of 10 tags may be added at once")
    return tags


def _make_tag_methods(kind: str) -> None:
    attr_name = {"artist": "artist", "album": "album", "track": "track"}[kind]

    def get_tags(req: Request):
        user = req.get("user") or req.user
        if not user:
            raise LastFmError(INVALID_PARAMS, "Invalid parameters - user or sk required")
        row = req.app.store.get_user(user)
        if row is None:
            raise not_found("user")
        artist, name = _tag_target(req, kind)
        tags = req.app.store.get_tags(row["name"], kind, artist, name)
        attr = {"artist": artist}
        if kind != "artist":
            attr[attr_name] = name
        return {"tags": {"tag": [{"name": t, "url": tag_url(t)} for t in tags], "@attr": attr}}

    def add_tags(req: Request):
        artist, name = _tag_target(req, kind)
        req.app.store.add_tags(req.user, kind, artist, name, _split_tags(req.require("tags")))
        return {}

    def remove_tag(req: Request):
        artist, name = _tag_target(req, kind)
        req.app.store.remove_tag(req.user, kind, artist, name, req.require("tag"))
        return {}

    method(f"{kind}.getTags", notes="local personal tags")(get_tags)
    method(f"{kind}.addTags", auth="session", write=True, notes="stored locally only")(add_tags)
    method(f"{kind}.removeTag", auth="session", write=True, notes="stored locally only")(remove_tag)


for _kind in ("artist", "album", "track"):
    _make_tag_methods(_kind)

__all__ = ["artist_ref", "artist_image_list", "album_cover", "caa_direct", "user_url", "norm"]
