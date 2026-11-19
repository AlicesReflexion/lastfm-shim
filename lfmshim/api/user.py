"""user.* and library.* from local persistent state."""

from __future__ import annotations

from typing import Any

from ..errors import INVALID_PARAMS, LastFmError, not_found
from .common import images, lfm_url, paging_attr, period_since, tag_url, user_url, uts_date
from .music import STREAMABLE, album_cover, artist_image_list
from .registry import Request, method

WEEK = 7 * 86400
# Last.fm weekly charts run Sunday 12:00 UTC to Sunday 12:00 UTC.
WEEK_ANCHOR = 1_108_296_000  # Sunday 2005-02-13 12:00:00 UTC


def _user(req: Request, param: str = "user") -> str:
    name = req.get(param) or req.user
    if not name:
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - Your request is missing a required parameter (user)")
    row = req.app.store.get_user(name)
    if row is None:
        raise not_found("user")
    return row["name"]


def _cached_cover(req: Request, artist: str, album: str, album_mbid: str) -> list[dict[str, str]]:
    """Cover from cache only: list views must not trigger slow upstream lookups."""
    data = None
    if album_mbid:
        data = req.app.resolver.album(mbid=album_mbid, cache_only=True)
        if not data:
            return album_cover(req, release_mbid=album_mbid, cache_only=True)
    elif album:
        data = req.app.resolver.album(artist=artist, album=album, cache_only=True)
    if data:
        rg = (data.get("release_group") or {}).get("id", "")
        rel = (data.get("release") or {}).get("id", "")
        return album_cover(req, rg, rel, cache_only=True)
    return images()


def _artist_obj(req: Request, name: str, mbid: str, extended: bool) -> dict[str, Any]:
    if extended:
        return {"url": lfm_url(name), "name": name, "image": artist_image_list(req, mbid, cache_only=True),
                "mbid": mbid}
    return {"mbid": mbid, "#text": name}


def _recent_entry(req: Request, user: str, s: dict[str, Any], extended: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "artist": _artist_obj(req, s["artist"], s.get("artist_mbid", ""), extended),
        "streamable": "0",
        "image": _cached_cover(req, s.get("album_artist") or s["artist"], s.get("album", ""),
                               s.get("album_mbid", "")),
        "mbid": s.get("track_mbid", ""),
        "album": {"mbid": s.get("album_mbid", ""), "#text": s.get("album", "")},
        "name": s["track"],
        "url": lfm_url(s["artist"], "_", s["track"]),
    }
    if extended:
        entry["loved"] = "1" if req.app.store.is_loved(user, s["artist"], s["track"]) else "0"
    return entry


@method("user.getRecentTracks", notes="local scrobbles; now playing first on page 1")
def user_get_recent_tracks(req: Request):
    user = _user(req)
    page, limit = req.paging(50, 200)
    extended = req.bool("extended")
    since = req.int("from", 0) or None
    until = req.int("to", 0) or None
    rows, total = req.app.store.recent_scrobbles(user, limit, (page - 1) * limit, since, until)
    tracks = []
    np = req.app.store.get_now_playing(user) if page == 1 and not until else None
    if np:
        e = _recent_entry(req, user, {"artist": np["artist"], "track": np["track"], "album": np.get("album", ""),
                                      "album_artist": np.get("albumArtist", ""), "track_mbid": np.get("mbid", "")},
                          extended)
        e["@attr"] = {"nowplaying": "true"}
        tracks.append(e)
    for r in rows:
        e = _recent_entry(req, user, dict(r), extended)
        e["date"] = uts_date(r["ts"])
        tracks.append(e)
    return {"recenttracks": {"track": tracks, "@attr": paging_attr(page, limit, total, user=user)}}


@method("user.getInfo", notes="local user record and counts")
def user_get_info(req: Request):
    user = _user(req)
    row = req.app.store.get_user(user)
    store = req.app.store
    _, artists = store.top(user, "artist", 1)
    _, albums = store.top(user, "album", 1)
    _, tracks = store.top(user, "track", 1)
    return {"user": {
        "name": user, "age": "0", "subscriber": "0", "realname": row["realname"], "bootstrap": "0",
        "playcount": store.scrobble_count(user), "artist_count": artists, "playlists": "0",
        "track_count": tracks, "album_count": albums, "image": images()[:4],
        "registered": {"unixtime": str(row["registered"]), "#text": row["registered"]},
        "country": row["country"] or "None", "gender": "n", "url": user_url(user), "type": "user",
    }}


def _top(req: Request, kind: str):
    user = _user(req)
    page, limit = req.paging(50, 1000)
    since = period_since(req.get("period"), req.app.now())
    rows, total = req.app.store.top(user, kind, limit, (page - 1) * limit, since)
    return user, page, limit, rows, total


@method("user.getTopArtists", notes="aggregated local scrobbles; period supported")
def user_get_top_artists(req: Request):
    user, page, limit, rows, total = _top(req, "artist")
    out = [{"streamable": "0", "image": artist_image_list(req, r["artist_mbid"] or "", cache_only=True),
            "mbid": r["artist_mbid"] or "", "url": lfm_url(r["artist"]), "playcount": r["playcount"],
            "@attr": {"rank": i}, "name": r["artist"]}
           for i, r in enumerate(rows, start=(page - 1) * limit + 1)]
    return {"topartists": {"artist": out, "@attr": paging_attr(page, limit, total, user=user)}}


@method("user.getTopAlbums", notes="aggregated local scrobbles; period supported")
def user_get_top_albums(req: Request):
    user, page, limit, rows, total = _top(req, "album")
    out = []
    for i, r in enumerate(rows, start=(page - 1) * limit + 1):
        artist = r["album_artist_eff"]
        out.append({"artist": {"url": lfm_url(artist), "name": artist, "mbid": r["artist_mbid"] or ""},
                    "image": _cached_cover(req, artist, r["album"], r["album_mbid"] or ""),
                    "mbid": r["album_mbid"] or "", "url": lfm_url(artist, r["album"]), "playcount": r["playcount"],
                    "@attr": {"rank": i}, "name": r["album"]})
    return {"topalbums": {"album": out, "@attr": paging_attr(page, limit, total, user=user)}}


@method("user.getTopTracks", notes="aggregated local scrobbles; period supported")
def user_get_top_tracks(req: Request):
    user, page, limit, rows, total = _top(req, "track")
    out = []
    for i, r in enumerate(rows, start=(page - 1) * limit + 1):
        out.append({"streamable": dict(STREAMABLE), "mbid": r["track_mbid"] or "", "name": r["track"],
                    "image": _cached_cover(req, r["artist"], r["album"], r["album_mbid"] or ""),
                    "artist": {"url": lfm_url(r["artist"]), "name": r["artist"], "mbid": r["artist_mbid"] or ""},
                    "url": lfm_url(r["artist"], "_", r["track"]), "duration": r["duration"] or 0,
                    "@attr": {"rank": i}, "playcount": r["playcount"]})
    return {"toptracks": {"track": out, "@attr": paging_attr(page, limit, total, user=user)}}


@method("user.getLovedTracks", notes="local loves")
def user_get_loved_tracks(req: Request):
    user = _user(req)
    page, limit = req.paging(50, 1000)
    rows, total = req.app.store.loved(user, limit, (page - 1) * limit)
    out = [{"artist": {"url": lfm_url(r["artist"]), "name": r["artist"], "mbid": r["artist_mbid"]},
            "date": uts_date(r["ts"]), "mbid": r["track_mbid"], "url": lfm_url(r["artist"], "_", r["track"]),
            "name": r["track"], "image": images(), "streamable": dict(STREAMABLE)} for r in rows]
    return {"lovedtracks": {"track": out, "@attr": paging_attr(page, limit, total, user=user)}}


@method("user.getFriends", support="stub", notes="no social graph; always empty")
def user_get_friends(req: Request):
    user = _user(req)
    page, limit = req.paging(50, 1000)
    return {"friends": {"user": [], "@attr": paging_attr(page, limit, 0, user=user)}}


@method("user.getTopTags", notes="local personal tags")
def user_get_top_tags(req: Request):
    user = _user(req)
    limit = req.int("limit", 50, minimum=1, maximum=1000)
    rows = req.app.store.top_user_tags(user, limit)
    return {"toptags": {"tag": [{"name": r["tag"], "count": r["n"], "url": tag_url(r["tag"])} for r in rows],
                        "@attr": {"user": user}}}


@method("user.getPersonalTags", notes="local personal tags")
def user_get_personal_tags(req: Request):
    user = _user(req)
    tag = req.require("tag")
    kind = req.require("taggingtype").lower()
    if kind not in ("artist", "album", "track"):
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - taggingtype must be artist, album or track")
    page, limit = req.paging(50, 1000)
    rows, total = req.app.store.tagged_items(user, tag, kind, limit, (page - 1) * limit)
    items = []
    for r in rows:
        if kind == "artist":
            items.append({"name": r["artist"], "mbid": "", "url": lfm_url(r["artist"]), "streamable": "0",
                          "image": images()})
        else:
            url = lfm_url(r["artist"], r["name"]) if kind == "album" else lfm_url(r["artist"], "_", r["name"])
            items.append({"name": r["name"], "mbid": "", "url": url,
                          "artist": {"name": r["artist"], "mbid": "", "url": lfm_url(r["artist"])},
                          "image": images()})
    return {"taggings": {f"{kind}s": {kind: items},
                         "@attr": paging_attr(page, limit, total, user=user, tag=tag)}}


# ---- weekly charts ---------------------------------------------------------
def week_ranges(start: int, now: int) -> list[tuple[int, int]]:
    first = WEEK_ANCHOR + ((start - WEEK_ANCHOR) // WEEK) * WEEK
    out = []
    t = first
    while t + WEEK <= now:
        out.append((t, t + WEEK))
        t += WEEK
    return out


def _last_week(now: int) -> tuple[int, int]:
    end = WEEK_ANCHOR + ((now - WEEK_ANCHOR) // WEEK) * WEEK
    return end - WEEK, end


@method("user.getWeeklyChartList", notes="Sunday-noon UTC weeks since first local scrobble")
def user_get_weekly_chart_list(req: Request):
    user = _user(req)
    first = req.app.store.first_scrobble_ts(user)
    weeks = week_ranges(first, req.app.now()) if first else []
    return {"weeklychartlist": {"chart": [{"#text": "", "from": f, "to": t} for f, t in weeks],
                                "@attr": {"user": user}}}


def _weekly(req: Request, kind: str):
    user = _user(req)
    default_from, default_to = _last_week(req.app.now())
    frm = req.int("from", default_from)
    to = req.int("to", default_to)
    limit = req.int("limit", 50, minimum=1, maximum=1000) if req.get("limit") else 1000
    rows, _ = req.app.store.top(user, kind, limit, 0, frm, to - 1)
    return user, frm, to, rows


@method("user.getWeeklyArtistChart")
def user_get_weekly_artist_chart(req: Request):
    user, frm, to, rows = _weekly(req, "artist")
    out = [{"mbid": r["artist_mbid"] or "", "url": lfm_url(r["artist"]), "name": r["artist"],
            "@attr": {"rank": i}, "playcount": r["playcount"]} for i, r in enumerate(rows, 1)]
    return {"weeklyartistchart": {"artist": out, "@attr": {"from": frm, "user": user, "to": to}}}


@method("user.getWeeklyAlbumChart")
def user_get_weekly_album_chart(req: Request):
    user, frm, to, rows = _weekly(req, "album")
    out = [{"artist": {"mbid": r["artist_mbid"] or "", "#text": r["album_artist_eff"]}, "mbid": r["album_mbid"] or "",
            "url": lfm_url(r["album_artist_eff"], r["album"]), "name": r["album"], "@attr": {"rank": i},
            "playcount": r["playcount"]} for i, r in enumerate(rows, 1)]
    return {"weeklyalbumchart": {"album": out, "@attr": {"from": frm, "user": user, "to": to}}}


@method("user.getWeeklyTrackChart")
def user_get_weekly_track_chart(req: Request):
    user, frm, to, rows = _weekly(req, "track")
    out = [{"artist": {"mbid": r["artist_mbid"] or "", "#text": r["artist"]}, "image": images(),
            "mbid": r["track_mbid"] or "", "url": lfm_url(r["artist"], "_", r["track"]), "name": r["track"],
            "@attr": {"rank": i}, "playcount": r["playcount"]} for i, r in enumerate(rows, 1)]
    return {"weeklytrackchart": {"track": out, "@attr": {"from": frm, "user": user, "to": to}}}


@method("library.getArtists", notes="aggregated local scrobbles")
def library_get_artists(req: Request):
    user = _user(req)
    page, limit = req.paging(50, 2000)
    rows, total = req.app.store.library_artists(user, limit, (page - 1) * limit)
    out = [{"tagcount": len(req.app.store.get_tags(user, "artist", r["artist"], "")),
            "image": artist_image_list(req, r["artist_mbid"] or "", cache_only=True), "mbid": r["artist_mbid"] or "",
            "url": lfm_url(r["artist"]), "playcount": r["playcount"], "name": r["artist"], "streamable": "0"}
           for r in rows]
    return {"artists": {"artist": out, "@attr": paging_attr(page, limit, total, user=user)}}
