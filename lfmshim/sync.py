"""Background delivery of opt-in ListenBrainz writes and cache warm-up
(scrobble MBIDs and covers, and Discogs tag lookups deferred by API calls).

Everything a client submits is committed locally together with its outbox
items, in one SQLite transaction: a crash or restart can never keep a scrobble
while losing its forwarding (and a client retry of a rolled-back call stores
and queues everything). The worker delivers queued items with retry/backoff,
so client responses never wait on (or fail because of) ListenBrainz.

The worker applies the *current* configuration to every queued item. Items
whose forwarding has since been disabled, or that belong to a local user other
than ``listenbrainz.local_user``, are held: never sent, never retried, kept in
the outbox so re-enabling forwarding delivers them. Enrichment items are
selected separately, so held or failing ListenBrainz items cannot starve them.

Semantics:
* a track.scrobble call with one accepted, new scrobble -> listen_type "single";
  a batch with several -> one "import" submission (ListenBrainz's documented
  type for multiple listens);
* track.updateNowPlaying -> "playing_now" (no listened_at); dropped rather than
  retried once the track would have finished;
* love/unlove -> recording feedback score 1/0, only when a recording MBID is
  known or can be resolved;
* duplicate scrobbles (client retries) are never forwarded twice.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import __version__
from .errors import UpstreamError
from .upstream.listenbrainz import LISTEN_MINIMUM_TS, listen_payload

log = logging.getLogger("lfmshim.sync")

FORWARD_KINDS = ("listens", "playing_now", "feedback")
PERMANENT_STATUSES = {400, 413, 422}
AUTH_STATUSES = {401, 403}
MAX_BACKOFF = 6 * 3600


class Sync:
    def __init__(self, app: Any):
        self.app = app
        self.cfg = app.config.listenbrainz
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flush_lock = threading.Lock()

    # ---- policy ------------------------------------------------------------
    def _forwards(self, user: str, enabled: bool) -> bool:
        if not enabled:
            return False
        target = self.cfg.local_user
        return not target or target.casefold() == user.casefold()

    def _token(self) -> str:
        return self.cfg.resolved_token()

    def forward_kinds(self) -> list[str]:
        """Outbox kinds the current configuration allows sending."""
        kinds = []
        if self.cfg.scrobble:
            kinds += ["listens", "playing_now"]
        if self.cfg.feedback:
            kinds.append("feedback")
        return kinds

    def deliverable(self, kind: str, user: str | None) -> bool:
        if kind == "enrich":
            return True
        flag = self.cfg.feedback if kind == "feedback" else self.cfg.scrobble
        return kind in FORWARD_KINDS and bool(user) and self._forwards(user, flag)

    # ---- producers -----------------------------------------------------------
    def wake(self) -> None:
        self._wake.set()

    def scrobbled(self, user: str, records: list[dict[str, Any]]) -> None:
        if self._forwards(user, self.cfg.scrobble):
            listens = [listen_payload(r, client="lfmshim", client_version=__version__) for r in records
                       if int(r["timestamp"]) >= LISTEN_MINIMUM_TS]
            if listens:
                self.app.store.enqueue("listens", {"user": user, "listens": listens})
        if self.app.config.upstream.enrich_scrobbles and not self.app.config.upstream.offline:
            for r in records:
                self.app.store.enqueue("enrich", {"user": user, "artist": r["artist"], "track": r["track"],
                                                  "album": r.get("album", ""), "mbid": r.get("mbid", "")})
        self._wake.set()

    def now_playing(self, user: str, record: dict[str, Any]) -> None:
        if not self._forwards(user, self.cfg.scrobble):
            return
        listen = listen_payload(record, include_ts=False, client="lfmshim", client_version=__version__)
        expires = self.app.now() + (record.get("duration") or 600)
        self.app.store.enqueue("playing_now", {"user": user, "listen": listen, "expires": expires}, expires=expires)
        self._wake.set()

    def feedback(self, user: str, artist: str, track: str, mbid: str, score: int) -> None:
        if not self._forwards(user, self.cfg.feedback):
            return
        self.app.store.enqueue("feedback", {"user": user, "artist": artist, "track": track, "mbid": mbid,
                                            "score": score})
        self._wake.set()

    # ---- delivery ------------------------------------------------------------
    def _due(self, limit: int) -> list[Any]:
        store = self.app.store
        store.purge_expired_outbox()
        rows = list(store.due_outbox(self.forward_kinds(), limit, self.cfg.local_user))
        rows += store.due_outbox(["enrich"], limit)
        return rows

    def flush(self, limit: int = 50) -> int:
        """Deliver due outbox items; returns how many were completed."""
        done = 0
        with self._flush_lock:
            for row in self._due(limit):
                if not self.deliverable(row["kind"], row["user"]):
                    continue  # held under the current configuration; keep it queued
                try:
                    payload = json.loads(row["payload"])
                    self._deliver(row["kind"], payload)
                except UpstreamError as exc:
                    if exc.status in PERMANENT_STATUSES:
                        log.warning("dropping %s item %d: rejected by %s", row["kind"], row["id"], exc)
                        self.app.store.outbox_done(row["id"])
                        continue
                    delay = 3600 if exc.status in AUTH_STATUSES else min(
                        self.cfg.retry_interval * (2 ** row["attempts"]), MAX_BACKOFF)
                    log.info("%s item %d failed (%s); retry in %ds", row["kind"], row["id"], exc, delay)
                    self.app.store.outbox_failed(row["id"], str(exc), delay)
                    continue
                except Exception as exc:  # malformed item: never retry forever
                    log.warning("dropping %s item %d: %s", row["kind"], row["id"], type(exc).__name__)
                    self.app.store.outbox_done(row["id"])
                    continue
                self.app.store.outbox_done(row["id"])
                done += 1
        return done

    def _deliver(self, kind: str, p: dict[str, Any]) -> None:
        lb = self.app.lb
        if kind == "listens":
            listens = p["listens"]
            lb.submit(self._token(), "single" if len(listens) == 1 else "import", listens)
        elif kind == "playing_now":
            if self.app.now() > int(p["expires"]):
                return  # stale; nothing to show any more
            lb.submit(self._token(), "playing_now", [p["listen"]])
        elif kind == "feedback":
            mbid = p.get("mbid")
            if not mbid:
                rec = self.app.resolver.track(artist=p["artist"], track=p["track"])
                mbid = (rec or {}).get("id")
            if not mbid:
                log.info("no recording MBID for loved track; feedback not forwarded")
                return
            lb.feedback(self._token(), mbid, int(p["score"]))
        elif kind == "enrich":
            self._enrich(p)
        else:
            raise ValueError(kind)

    def _enrich(self, p: dict[str, Any]) -> None:
        """Resolve MBIDs/cover for a scrobble so later list views have them, or
        fetch the Discogs lookups a tag request deferred (``task: tags``)."""
        if p.get("task") == "tags":
            self.app.tag_sources.warm(p["kind"], p["mbid"])
            return
        r = self.app.resolver
        rec = r.track(artist=p["artist"], track=p["track"], mbid=p.get("mbid") or None)
        track_mbid = artist_mbid = album_mbid = ""
        if rec:
            track_mbid = rec["id"]
            credits = rec.get("artist-credit") or []
            artist_mbid = ((credits[0].get("artist") or {}).get("id") or "") if credits else ""
        if p.get("album"):
            alb = r.album(artist=p["artist"], album=p["album"])
            if alb and alb.get("release"):
                album_mbid = alb["release"].get("id", "")
                rg = (alb.get("release_group") or {}).get("id", "")
                if rg:
                    self.app.caa.front("release-group", rg)
        self.app.store.fill_scrobble_mbids(p["user"], p["artist"], p["track"], p.get("album", ""),
                                           track_mbid, artist_mbid, album_mbid)

    # ---- thread --------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="lfmshim-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.flush()
            except Exception:  # pragma: no cover - keep the worker alive
                log.exception("outbox flush failed")
            self._wake.wait(timeout=30)
            self._wake.clear()
