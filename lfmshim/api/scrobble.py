"""track.scrobble, track.updateNowPlaying, track.love, track.unlove."""

from __future__ import annotations

from typing import Any

from ..errors import INVALID_PARAMS, LastFmError
from .registry import Request, method

MAX_BATCH = 50
IGNORED = {
    1: "Artist was ignored",
    2: "Track was ignored",
    3: "Timestamp was too old",
    4: "Timestamp was too new",
    5: "Daily scrobble limit exceeded",
}
FIELDS = ("artist", "track", "timestamp", "album", "albumArtist", "trackNumber", "mbid", "duration",
          "chosenByUser", "context", "streamId")


def _items(req: Request) -> list[dict[str, str]]:
    """Collect scrobbles from array notation (artist[0]...) or plain params."""
    indexed = {f: req.indexed(f) for f in FIELDS}
    indices = sorted(set().union(*[set(v) for v in indexed.values()]))
    if indices:
        if len(indices) > MAX_BATCH:
            raise LastFmError(INVALID_PARAMS, f"Invalid parameters - A maximum of {MAX_BATCH} scrobbles per request")
        return [{f: (indexed[f].get(i) or "").strip() for f in FIELDS} for i in indices]
    return [{f: (req.get(f) or "").strip() for f in FIELDS}]


def _corrected(value: str) -> dict[str, str]:
    return {"#text": value, "corrected": "0"}


def _ignore_code(app: Any, item: dict[str, str]) -> int:
    if not item["artist"]:
        return 1
    if not item["track"]:
        return 2
    cfg = app.config.api
    ts = int(item["timestamp"])
    now = app.now()
    if cfg.scrobble_max_age and ts < now - cfg.scrobble_max_age:
        return 3
    if ts > now + cfg.scrobble_max_future:
        return 4
    return 0


def _duration(raw: str) -> int | None:
    try:
        d = int(float(raw))
        return d if d > 0 else None
    except (TypeError, ValueError):
        return None


@method("track.scrobble", auth="session", write=True,
        notes="stored locally; optionally forwarded to ListenBrainz (single/import) via a retrying outbox")
def track_scrobble(req: Request):
    app = req.app
    items = _items(req)
    for it in items:
        if not it["timestamp"].lstrip("-").isdigit():
            raise LastFmError(INVALID_PARAMS, "Invalid parameters - timestamp is required and must be an integer")
    results, fresh = [], []
    accepted = ignored = 0
    # One transaction for the whole batch: the scrobbles and their outbox items
    # (forwarding, enrichment) are committed together or not at all, so a
    # failure can never leave a stored scrobble that will not be forwarded,
    # and the client's retry of a failed call stores and queues everything.
    with app.store.transaction():
        for it in items:
            code = _ignore_code(app, it)
            record = {
                "artist": it["artist"], "track": it["track"], "timestamp": int(it["timestamp"]),
                "album": it["album"], "albumArtist": it["albumArtist"], "trackNumber": it["trackNumber"],
                "mbid": it["mbid"], "duration": _duration(it["duration"]),
            }
            if code == 0:
                accepted += 1
                # Duplicate submissions (client retries) are accepted but stored
                # and forwarded once.
                if app.store.add_scrobble(req.user, record):
                    fresh.append(record)
                app.store.clear_now_playing(req.user, record["artist"], record["track"])
            else:
                ignored += 1
            results.append({
                "track": _corrected(it["track"]), "artist": _corrected(it["artist"]),
                "album": _corrected(it["album"]), "albumArtist": _corrected(it["albumArtist"]),
                "timestamp": it["timestamp"],
                "ignoredMessage": {"#text": IGNORED.get(code, ""), "code": str(code)},
            })
        if fresh:
            app.scrobbled(req.user, fresh)
    return {"scrobbles": {"scrobble": results, "@attr": {"accepted": accepted, "ignored": ignored}}}


@method("track.updateNowPlaying", auth="session", write=True,
        notes="stored locally; optionally forwarded to ListenBrainz as playing_now")
def track_update_now_playing(req: Request):
    app = req.app
    item = {f: (req.get(f) or "").strip() for f in FIELDS}
    if not item["artist"]:
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - artist is required")
    if not item["track"]:
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - track is required")
    record = {"artist": item["artist"], "track": item["track"], "album": item["album"],
              "albumArtist": item["albumArtist"], "trackNumber": item["trackNumber"], "mbid": item["mbid"],
              "duration": _duration(item["duration"])}
    with app.store.transaction():
        app.store.set_now_playing(req.user, record, record["duration"])
        app.now_playing(req.user, record)
    return {"nowplaying": {
        "track": _corrected(item["track"]), "artist": _corrected(item["artist"]),
        "album": _corrected(item["album"]), "albumArtist": _corrected(item["albumArtist"]),
        "ignoredMessage": {"#text": "", "code": "0"},
    }}


@method("track.love", auth="session", write=True, notes="stored locally; optional ListenBrainz feedback")
def track_love(req: Request):
    artist, track = req.require("artist"), req.require("track")
    with req.app.store.transaction():
        req.app.store.love(req.user, artist, track, req.get("mbid") or "")
        req.app.feedback(req.user, artist, track, req.get("mbid") or "", 1)
    return {}


@method("track.unlove", auth="session", write=True, notes="stored locally; optional ListenBrainz feedback")
def track_unlove(req: Request):
    artist, track = req.require("artist"), req.require("track")
    with req.app.store.transaction():
        req.app.store.unlove(req.user, artist, track)
        req.app.feedback(req.user, artist, track, req.get("mbid") or "", 0)
    return {}
