"""ListenBrainz API (popularity, sitewide stats, submissions, feedback) and
ListenBrainz Labs (similar artists / recordings)."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..errors import UpstreamError

log = logging.getLogger("lfmshim.upstream")

MAX_LISTENS_PER_REQUEST = 1000
LISTEN_MINIMUM_TS = 1033430400


def _flatten(data: Any) -> list[dict[str, Any]]:
    """Labs endpoints have returned both flat lists and lists of lists."""
    out: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for key in ("payload", "data", "results"):
            if key in data:
                return _flatten(data[key])
        return [data]
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list):
                out.extend(_flatten(item))
            elif isinstance(item, dict):
                out.append(item)
    return out


class ListenBrainz:
    def __init__(self, http, cfg):
        self.http = http
        self.cfg = cfg
        self.api = cfg.api_url.rstrip("/")
        self.labs = cfg.labs_url.rstrip("/")

    # ---- popularity --------------------------------------------------------
    def _popularity(self, entity: str, field: str, mbids: list[str], ttl: int) -> dict[str, dict[str, int]]:
        mbids = [m for m in dict.fromkeys(mbids) if m]
        if not mbids:
            return {}
        out: dict[str, dict[str, int]] = {}
        for i in range(0, len(mbids), 100):
            chunk = mbids[i:i + 100]
            try:
                data = self.http.get_json("listenbrainz", f"{self.api}/1/popularity/{entity}", method="POST",
                                          json_body={f"{field}s": chunk}, ttl=ttl)
            except UpstreamError as exc:
                log.info("listenbrainz popularity failed: %s", exc)
                continue
            for row in _flatten(data):
                mbid = row.get(field)
                if mbid:
                    out[mbid] = {"listeners": int(row.get("total_user_count") or 0),
                                 "playcount": int(row.get("total_listen_count") or 0)}
        return out

    def artist_popularity(self, mbids: list[str], ttl: int) -> dict[str, dict[str, int]]:
        return self._popularity("artist", "artist_mbid", mbids, ttl)

    def recording_popularity(self, mbids: list[str], ttl: int) -> dict[str, dict[str, int]]:
        return self._popularity("recording", "recording_mbid", mbids, ttl)

    def release_group_popularity(self, mbids: list[str], ttl: int) -> dict[str, dict[str, int]]:
        return self._popularity("release-group", "release_group_mbid", mbids, ttl)

    def top_recordings_for_artist(self, artist_mbid: str, ttl: int) -> list[dict[str, Any]]:
        data = self.http.get_json("listenbrainz", f"{self.api}/1/popularity/top-recordings-for-artist/{artist_mbid}",
                                  ttl=ttl)
        return _flatten(data)

    def top_release_groups_for_artist(self, artist_mbid: str, ttl: int) -> list[dict[str, Any]]:
        data = self.http.get_json(
            "listenbrainz", f"{self.api}/1/popularity/top-release-groups-for-artist/{artist_mbid}", ttl=ttl)
        return _flatten(data)

    # ---- stats -------------------------------------------------------------
    def sitewide(self, entity: str, count: int, offset: int, range_: str, ttl: int) -> dict[str, Any]:
        data = self.http.get_json("listenbrainz", f"{self.api}/1/stats/sitewide/{entity}",
                                  {"count": count, "offset": offset, "range": range_}, ttl=ttl)
        return (data or {}).get("payload") or {}

    # ---- labs similarity ---------------------------------------------------
    def similar_artists(self, artist_mbid: str, ttl: int) -> list[dict[str, Any]]:
        data = self.http.get_json("labs", f"{self.labs}/similar-artists/json",
                                  {"artist_mbids": artist_mbid, "algorithm": self.cfg.similar_artists_algorithm},
                                  ttl=ttl)
        rows = [r for r in _flatten(data) if (r.get("artist_mbid") or "") != artist_mbid]
        rows.sort(key=lambda r: -float(r.get("score") or 0))
        return rows

    def similar_recordings(self, recording_mbid: str, ttl: int) -> list[dict[str, Any]]:
        data = self.http.get_json("labs", f"{self.labs}/similar-recordings/json",
                                  {"recording_mbids": recording_mbid,
                                   "algorithm": self.cfg.similar_recordings_algorithm}, ttl=ttl)
        rows = [r for r in _flatten(data) if (r.get("recording_mbid") or "") != recording_mbid]
        rows.sort(key=lambda r: -float(r.get("score") or 0))
        return rows

    # ---- writes (opt-in) ---------------------------------------------------
    def _auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Token {token}"}

    def submit(self, token: str, listen_type: str, payload: list[dict[str, Any]]) -> None:
        if listen_type not in ("single", "import", "playing_now"):
            raise ValueError(listen_type)
        if listen_type != "import" and len(payload) != 1:
            raise ValueError(f"{listen_type} takes exactly one listen")
        if len(payload) > MAX_LISTENS_PER_REQUEST:
            raise ValueError("too many listens")
        self.http.get_json("listenbrainz", f"{self.api}/1/submit-listens", method="POST",
                           json_body={"listen_type": listen_type, "payload": payload},
                           headers=self._auth(token), cache=False, retries=1)

    def feedback(self, token: str, recording_mbid: str, score: int) -> None:
        self.http.get_json("listenbrainz", f"{self.api}/1/feedback/recording-feedback", method="POST",
                           json_body={"recording_mbid": recording_mbid, "score": score},
                           headers=self._auth(token), cache=False, retries=1)

    def validate_token(self, token: str) -> dict[str, Any]:
        return self.http.get_json("listenbrainz", f"{self.api}/1/validate-token", headers=self._auth(token),
                                  cache=False) or {}


def listen_payload(s: dict[str, Any], *, include_ts: bool = True, client: str = "lfmshim",
                   client_version: str = "") -> dict[str, Any]:
    """Convert a Last.fm scrobble dict into a ListenBrainz listen."""
    info: dict[str, Any] = {"submission_client": client}
    if client_version:
        info["submission_client_version"] = client_version
    if s.get("duration"):
        info["duration_ms"] = int(s["duration"]) * 1000
    if s.get("trackNumber"):
        info["tracknumber"] = str(s["trackNumber"])
    if s.get("mbid"):
        info["recording_mbid"] = s["mbid"]
    if s.get("album_mbid"):
        info["release_mbid"] = s["album_mbid"]
    if s.get("artist_mbid"):
        info["artist_mbids"] = [s["artist_mbid"]]
    meta: dict[str, Any] = {"artist_name": s["artist"], "track_name": s["track"], "additional_info": info}
    if s.get("album"):
        meta["release_name"] = s["album"]
    listen: dict[str, Any] = {"track_metadata": meta}
    if include_ts:
        listen["listened_at"] = int(s["timestamp"])
    # MAX_LISTEN_SIZE is 10240 bytes; trim optional data if a listen is huge.
    if len(json.dumps(listen)) > 10000:
        meta["additional_info"] = {"submission_client": client}
    return listen
