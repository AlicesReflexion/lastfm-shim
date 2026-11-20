import json
import logging
import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET

from lfmshim.api.dispatch import handle

from . import fixtures as F
from .helpers import API_KEY, NOW, SECRET, FakeClock, FakeTransport, call, make_app, post

TOKEN = "lb-user-token-SECRET-123"


def lb_app(t=None, clock=None, **extra):
    lb = {"scrobble": True, "feedback": True, "token": TOKEN}
    lb.update(extra.pop("listenbrainz", {}))
    app = make_app(t or FakeTransport(), clock, listenbrainz=lb,
                   upstream={"enrich_scrobbles": False, **extra.pop("upstream", {})},
                   api={"clients": {API_KEY: SECRET}, **extra.pop("api", {})})
    app.store.add_user("alice", "correct horse")
    return app


def submit_recorder(t, status=200):
    bodies = []

    def handler(method, url, headers, body):
        bodies.append((json.loads(body), headers))
        return status, {"status": "ok"}

    t.add(f"{F.LB}/1/submit-listens", handler, method="POST")
    return bodies


class ScrobbleTests(unittest.TestCase):
    def setUp(self):
        self.t = FakeTransport()
        self.sent = submit_recorder(self.t)
        self.app = lb_app(self.t)
        self.sk = self.app.store.create_session("alice")

    def test_single_scrobble_forwarded_as_single(self):
        status, body = post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe",
                            timestamp=NOW - 300, album="Believe", duration=239, mbid=F.BELIEVE_REC)
        self.assertEqual(status, 200)
        s = body["scrobbles"]
        self.assertEqual(s["@attr"], {"accepted": "1", "ignored": "0"})
        self.assertEqual(s["scrobble"][0]["track"], {"#text": "Believe", "corrected": "0"})
        self.assertEqual(s["scrobble"][0]["ignoredMessage"], {"#text": "", "code": "0"})
        self.assertEqual(self.app.sync.flush(), 1)
        payload, headers = self.sent[0]
        self.assertEqual(payload["listen_type"], "single")
        listen = payload["payload"][0]
        self.assertEqual(listen["listened_at"], NOW - 300)
        self.assertEqual(listen["track_metadata"]["release_name"], "Believe")
        info = listen["track_metadata"]["additional_info"]
        self.assertEqual((info["duration_ms"], info["recording_mbid"]), (239000, F.BELIEVE_REC))
        self.assertEqual(headers["Authorization"], f"Token {TOKEN}")

    def test_batch_is_one_import_and_ignores_are_reported(self):
        params = {}
        rows = [("Cher", "Believe", NOW - 900), ("Cher", "Strong Enough", NOW - 600), ("", "No Artist", NOW - 500),
                ("Cher", "Ancient", NOW - 30 * 86400), ("Cher", "Future", NOW + 3600), ("Cher", "Dov'è", NOW - 100)]
        for i, (a, tr, ts) in enumerate(rows):
            params.update({f"artist[{i}]": a, f"track[{i}]": tr, f"timestamp[{i}]": ts})
        _, body = post(self.app, "track.scrobble", sk=self.sk, **params)
        s = body["scrobbles"]
        self.assertEqual(s["@attr"], {"accepted": "3", "ignored": "3"})
        codes = [x["ignoredMessage"]["code"] for x in s["scrobble"]]
        self.assertEqual(codes, ["0", "0", "1", "3", "4", "0"])
        self.assertEqual(s["scrobble"][3]["ignoredMessage"]["#text"], "Timestamp was too old")
        self.app.sync.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0]["listen_type"], "import")
        self.assertEqual([l["track_metadata"]["track_name"] for l in self.sent[0][0]["payload"]],
                         ["Believe", "Strong Enough", "Dov'è"])

    def test_duplicate_retry_accepted_once(self):
        for _ in range(2):
            _, body = post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe", timestamp=NOW - 60)
            self.assertEqual(body["scrobbles"]["@attr"]["accepted"], "1")
        self.app.sync.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.app.store.scrobble_count("alice"), 1)

    def test_batch_limit_and_missing_timestamp(self):
        params = {}
        for i in range(51):
            params.update({f"artist[{i}]": "a", f"track[{i}]": "t", f"timestamp[{i}]": NOW - i})
        _, body = post(self.app, "track.scrobble", sk=self.sk, **params)
        self.assertEqual(body["error"], 6)
        _, body = post(self.app, "track.scrobble", sk=self.sk, artist="a", track="t")
        self.assertEqual(body["error"], 6)

    def test_now_playing_semantics(self):
        _, body = post(self.app, "track.updateNowPlaying", sk=self.sk, artist="Cher", track="Believe", duration=239)
        self.assertEqual(body["nowplaying"]["artist"], {"#text": "Cher", "corrected": "0"})
        _, recent = call(self.app, "user.getRecentTracks", user="alice")
        first = recent["recenttracks"]["track"][0]
        self.assertEqual(first["@attr"], {"nowplaying": "true"})
        self.assertNotIn("date", first)
        self.app.sync.flush()
        self.assertEqual(self.sent[0][0]["listen_type"], "playing_now")
        self.assertNotIn("listened_at", self.sent[0][0]["payload"][0])
        # Scrobbling the same track clears now playing.
        post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe", timestamp=NOW - 1)
        _, recent = call(self.app, "user.getRecentTracks", user="alice")
        self.assertNotIn("@attr", recent["recenttracks"]["track"][0])

    def test_stale_now_playing_dropped(self):
        clock = FakeClock()
        t = FakeTransport()
        sent = submit_recorder(t)
        app = lb_app(t, clock)
        sk = app.store.create_session("alice")
        post(app, "track.updateNowPlaying", sk=sk, artist="Cher", track="Believe", duration=100)
        clock.t += 500
        app.sync.flush()
        self.assertEqual(sent, [])
        self.assertEqual(app.store.outbox_size(), 0)

    def test_retry_backoff_and_permanent_drop(self):
        clock = FakeClock()
        t = FakeTransport()
        statuses = iter([500, 200])
        sent = []

        def flaky(method, url, headers, body):
            st = next(statuses)
            sent.append(st)
            return st, {}

        t.add(f"{F.LB}/1/submit-listens", flaky, method="POST")
        app = lb_app(t, clock)
        sk = app.store.create_session("alice")
        post(app, "track.scrobble", sk=sk, artist="Cher", track="Believe", timestamp=NOW - 60)
        self.assertEqual(app.sync.flush(), 0)
        self.assertEqual(app.store.outbox_size(), 1)
        self.assertEqual(app.sync.flush(), 0)  # not due yet
        clock.t += 61
        self.assertEqual(app.sync.flush(), 1)
        self.assertEqual(sent, [500, 200])
        # A 400 from ListenBrainz is permanent: drop instead of retrying forever.
        t2 = FakeTransport()
        submit_recorder(t2, status=400)
        app2 = lb_app(t2)
        sk2 = app2.store.create_session("alice")
        post(app2, "track.scrobble", sk=sk2, artist="Cher", track="Believe", timestamp=NOW - 60)
        app2.sync.flush()
        self.assertEqual(app2.store.outbox_size(), 0)

    def test_not_forwarded_when_disabled_or_other_user(self):
        t = FakeTransport()
        app = make_app(t, api={"clients": {API_KEY: SECRET}}, upstream={"enrich_scrobbles": False})
        app.store.add_user("alice")
        sk = app.store.create_session("alice")
        post(app, "track.scrobble", sk=sk, artist="Cher", track="Believe", timestamp=NOW - 60)
        app.sync.flush()
        self.assertEqual(t.calls, [])
        app2 = lb_app(FakeTransport(), listenbrainz={"local_user": "bob"})
        sk = app2.store.create_session("alice")
        post(app2, "track.scrobble", sk=sk, artist="Cher", track="Believe", timestamp=NOW - 60)
        self.assertEqual(app2.store.outbox_size(), 0)

    def test_love_feedback_resolves_mbid(self):
        F.install_believe(F.install_cher(self.t))
        fb = []
        self.t.add(f"{F.LB}/1/feedback/recording-feedback",
                   lambda m, u, h, b: (fb.append(json.loads(b)), (200, {"status": "ok"}))[1], method="POST")
        status, body = post(self.app, "track.love", sk=self.sk, artist="Cher", track="Believe")
        self.assertEqual((status, body), (200, {}))
        post(self.app, "track.unlove", sk=self.sk, artist="Cher", track="Believe")
        self.app.sync.flush()
        self.assertEqual(fb, [{"recording_mbid": F.BELIEVE_REC, "score": 1},
                              {"recording_mbid": F.BELIEVE_REC, "score": 0}])
        self.assertFalse(self.app.store.is_loved("alice", "Cher", "Believe"))

    def test_token_never_logged(self):
        with self.assertLogs("lfmshim", level=logging.DEBUG) as logs:
            post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe", timestamp=NOW - 60)
            self.app.sync.flush()
            call(self.app, "auth.getMobileSession", http="POST", sign=True, username="alice", password="wrong")
        text = "\n".join(logs.output)
        for secret in (TOKEN, self.sk, "correct horse", "wrong", SECRET):
            self.assertNotIn(secret, text)

    def test_xml_scrobble_response(self):
        _, xml = call(self.app, "track.scrobble", http="POST", sign=True, fmt=None, sk=self.sk,
                      artist="Cher", track="Believe", timestamp=NOW - 60)
        root = ET.fromstring(xml)
        sc = root.find("scrobbles")
        self.assertEqual((sc.get("accepted"), sc.get("ignored")), ("1", "0"))
        self.assertEqual(sc.find("scrobble/track").get("corrected"), "0")
        self.assertEqual(sc.find("scrobble/ignoredMessage").get("code"), "0")


class OutboxAtomicityTests(unittest.TestCase):
    """Scrobbles and their outbox items commit together; the worker obeys the
    configuration current at delivery time."""

    def setUp(self):
        self.t = FakeTransport()
        self.sent = submit_recorder(self.t)
        self.app = lb_app(self.t)
        self.sk = self.app.store.create_session("alice")

    def _batch(self, app=None, sk=None, **extra):
        return post(app or self.app, "track.scrobble", sk=sk or self.sk,
                    **{"artist[0]": "Cher", "track[0]": "Believe", "timestamp[0]": NOW - 600,
                       "artist[1]": "Cher", "track[1]": "Strong Enough", "timestamp[1]": NOW - 300}, **extra)

    def test_outbox_failure_rolls_back_whole_batch_and_retry_recovers(self):
        store = self.app.store
        real = store.enqueue

        def broken(*a, **k):
            raise sqlite3.OperationalError("disk I/O error")

        store.enqueue = broken
        with self.assertLogs("lfmshim.api", "ERROR"):
            status, body = self._batch()
        self.assertEqual(body["error"], 8)
        self.assertEqual((store.scrobble_count("alice"), store.outbox_size()), (0, 0))
        store.enqueue = real
        _, body = self._batch()  # the client retries the same batch
        self.assertEqual(body["scrobbles"]["@attr"]["accepted"], "2")
        self.assertEqual(store.scrobble_count("alice"), 2)
        self.assertEqual(self.app.sync.flush(), 1)
        self.assertEqual(self.sent[0][0]["listen_type"], "import")
        self.assertEqual(len(self.sent[0][0]["payload"]), 2)

    def test_failure_mid_batch_stores_nothing(self):
        store = self.app.store
        real, calls = store.add_scrobble, []

        def second_fails(user, s):
            calls.append(s["track"])
            if len(calls) == 2:
                raise sqlite3.OperationalError("database is locked")
            return real(user, s)

        store.add_scrobble = second_fails
        with self.assertLogs("lfmshim.api", "ERROR"):
            self._batch()
        self.assertEqual((store.scrobble_count("alice"), store.outbox_size()), (0, 0))

    def test_love_and_now_playing_are_atomic(self):
        store = self.app.store
        store.enqueue = lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("full"))
        with self.assertLogs("lfmshim.api", "ERROR"):
            post(self.app, "track.love", sk=self.sk, artist="Cher", track="Believe")
            post(self.app, "track.updateNowPlaying", sk=self.sk, artist="Cher", track="Believe")
        self.assertFalse(store.is_loved("alice", "Cher", "Believe"))
        self.assertIsNone(store.get_now_playing("alice"))

    def test_disabled_after_queueing_is_held_not_sent(self):
        post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe", timestamp=NOW - 60)
        post(self.app, "track.love", sk=self.sk, artist="Cher", track="Believe", mbid=F.BELIEVE_REC)
        self.app.config.listenbrainz.scrobble = False
        self.app.config.listenbrainz.feedback = False
        self.assertEqual(self.app.sync.flush(), 0)
        self.assertEqual(self.t.calls, [])
        rows = self.app.store.outbox_items()
        self.assertEqual([(r["kind"], r["attempts"]) for r in rows], [("listens", 0), ("feedback", 0)])
        # Re-enabling delivers what was held.
        self.app.config.listenbrainz.scrobble = True
        self.assertEqual(self.app.sync.flush(), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([r["kind"] for r in self.app.store.outbox_items()], ["feedback"])

    def test_local_user_change_holds_other_users_items(self):
        post(self.app, "track.scrobble", sk=self.sk, artist="Cher", track="Believe", timestamp=NOW - 60)
        self.app.store.add_user("bob")
        bob = self.app.store.create_session("bob")
        self.app.config.listenbrainz.local_user = "Bob"
        post(self.app, "track.scrobble", sk=bob, artist="ABBA", track="SOS", timestamp=NOW - 60)
        self.assertEqual(self.app.sync.flush(), 1)
        self.assertEqual([p["payload"][0]["track_metadata"]["artist_name"] for p, _ in self.sent], ["ABBA"])
        held = self.app.store.outbox_items()
        self.assertEqual([(r["kind"], r["user"], r["attempts"]) for r in held], [("listens", "alice", 0)])

    def test_enrichment_not_starved_by_held_or_failing_items(self):
        t = F.install_believe(F.install_cher(FakeTransport()))
        t.add_json(f"{F.LB}/1/submit-listens", {}, status=500, method="POST")
        app = lb_app(t, upstream={"enrich_scrobbles": True})
        sk = app.store.create_session("alice")
        for i in range(60):  # more than one flush's worth of forwarding items
            app.store.enqueue("listens", {"user": "alice", "listens": [{"listened_at": NOW - i}]})
        post(app, "track.scrobble", sk=sk, artist="Cher", track="Believe", album="Believe", timestamp=NOW - 60)
        app.sync.flush(limit=50)  # ListenBrainz failing: forwards back off
        self.assertEqual(app.store.recent_scrobbles("alice", 1)[0][0]["track_mbid"], F.BELIEVE_REC)
        app.config.listenbrainz.scrobble = False  # now all 61 are held
        post(app, "track.scrobble", sk=sk, artist="Cher", track="Believe", album="Believe", timestamp=NOW - 30)
        app.sync.flush(limit=50)
        self.assertFalse([r for r in app.store.outbox_items() if r["kind"] == "enrich"])

    def test_stale_held_now_playing_is_purged(self):
        clock = FakeClock()
        app = lb_app(FakeTransport(), clock)
        sk = app.store.create_session("alice")
        post(app, "track.updateNowPlaying", sk=sk, artist="Cher", track="Believe", duration=200)
        app.config.listenbrainz.scrobble = False
        app.sync.flush()
        self.assertEqual(app.store.outbox_size(), 1)
        clock.t += 201
        app.sync.flush()
        self.assertEqual(app.store.outbox_size(), 0)

    def test_v1_outbox_is_migrated(self):
        from lfmshim.store import Store
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite3")
            db = sqlite3.connect(path)
            db.executescript("""CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                payload TEXT NOT NULL, created INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '');""")
            db.execute("INSERT INTO outbox(kind, payload, created) VALUES('playing_now', ?, 0)",
                       (json.dumps({"user": "alice", "listen": {}, "expires": NOW + 5}),))
            db.commit()
            db.close()
            store = Store(path, clock=FakeClock())
            row = store.outbox_items()[0]
            self.assertEqual((row["user"], row["expires"]), ("alice", NOW + 5))
            self.assertEqual(len(store.due_outbox(["playing_now"], 10, "ALICE")), 1)
            self.assertEqual(store.due_outbox(["playing_now"], 10, "bob"), [])
            store.close()


class EnrichTests(unittest.TestCase):
    def test_enrichment_fills_mbids_and_covers_list_views(self):
        t = F.install_believe(F.install_cher(FakeTransport()))
        app = make_app(t, api={"clients": {API_KEY: SECRET}})
        app.store.add_user("alice")
        sk = app.store.create_session("alice")
        post(app, "track.scrobble", sk=sk, artist="Cher", track="Believe", album="Believe", timestamp=NOW - 60)
        app.sync.flush()
        _, body = call(app, "user.getRecentTracks", user="alice")
        tr = body["recenttracks"]["track"][0]
        self.assertEqual(tr["mbid"], F.BELIEVE_REC)
        self.assertEqual(tr["artist"], {"mbid": F.CHER, "#text": "Cher"})
        self.assertEqual(tr["album"], {"mbid": F.BELIEVE_REL, "#text": "Believe"})
        self.assertTrue(tr["image"][0]["#text"].endswith("1-250.jpg"))


class UserMethodTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.app = make_app(FakeTransport(), self.clock, upstream={"enrich_scrobbles": False})
        self.app.store.add_user("alice")
        plays = [("A", "x", "Al1", NOW - 10 * 86400), ("A", "y", "Al1", NOW - 3 * 86400),
                 ("B", "z", "Al2", NOW - 2 * 86400), ("A", "x", "Al1", NOW - 86400), ("C", "w", "", NOW - 3600)]
        for a, tr, al, ts in plays:
            self.app.store.add_scrobble("alice", {"artist": a, "track": tr, "album": al, "timestamp": ts})

    def test_recent_tracks_paging_and_extended(self):
        _, body = call(self.app, "user.getRecentTracks", user="alice", limit=2, page=2)
        rt = body["recenttracks"]
        self.assertEqual(rt["@attr"], {"user": "alice", "page": "2", "perPage": "2", "totalPages": "3",
                                       "total": "5"})
        self.assertEqual([t["name"] for t in rt["track"]], ["z", "y"])
        self.assertEqual(rt["track"][0]["date"]["uts"], str(NOW - 2 * 86400))
        self.app.store.love("alice", "C", "w")
        _, body = call(self.app, "user.getRecentTracks", user="alice", limit=1, extended=1)
        t = body["recenttracks"]["track"][0]
        self.assertEqual((t["loved"], t["artist"]["name"]), ("1", "C"))
        _, body = call(self.app, "user.getRecentTracks", user="alice", **{"from": NOW - 2 * 86400 - 1})
        self.assertEqual(len(body["recenttracks"]["track"]), 3)

    def test_recent_tracks_xml_artist_attr(self):
        _, xml = call(self.app, "user.getRecentTracks", fmt=None, user="alice", limit=1)
        root = ET.fromstring(xml)
        self.assertEqual(root.find("recenttracks/track/artist").text, "C")
        self.assertEqual(root.find("recenttracks/track/artist").get("mbid"), "")
        self.assertEqual(root.find("recenttracks").get("total"), "5")

    def test_top_charts_with_period(self):
        _, body = call(self.app, "user.getTopArtists", user="alice")
        arts = body["topartists"]["artist"]
        self.assertEqual([(a["name"], a["playcount"]) for a in arts], [("A", "3"), ("C", "1"), ("B", "1")])
        self.assertEqual(arts[0]["@attr"], {"rank": "1"})
        _, body = call(self.app, "user.getTopArtists", user="alice", period="7day")
        self.assertEqual(body["topartists"]["@attr"]["total"], "3")
        self.assertEqual(body["topartists"]["artist"][0]["playcount"], "2")
        _, body = call(self.app, "user.getTopAlbums", user="alice")
        self.assertEqual([a["name"] for a in body["topalbums"]["album"]], ["Al1", "Al2"])
        _, body = call(self.app, "user.getTopTracks", user="alice", limit=1)
        self.assertEqual(body["toptracks"]["track"][0]["name"], "x")
        _, body = call(self.app, "user.getTopArtists", user="alice", period="bogus")
        self.assertEqual(body["error"], 6)

    def test_info_loved_friends_library(self):
        _, body = call(self.app, "user.getInfo", user="ALICE")
        u = body["user"]
        self.assertEqual((u["name"], u["playcount"], u["artist_count"], u["track_count"]), ("alice", "5", "3", "4"))
        self.assertEqual(u["registered"]["unixtime"], str(NOW))
        self.app.store.love("alice", "A", "x")
        _, body = call(self.app, "user.getLovedTracks", user="alice")
        self.assertEqual(body["lovedtracks"]["track"][0]["name"], "x")
        _, body = call(self.app, "user.getFriends", user="alice")
        self.assertEqual(body["friends"]["user"], [])
        _, body = call(self.app, "library.getArtists", user="alice")
        self.assertEqual(body["artists"]["artist"][0]["playcount"], "3")
        _, body = call(self.app, "user.getInfo", user="nobody")
        self.assertEqual((body["error"], body["message"]), (6, "User not found"))

    def test_weekly_charts(self):
        _, body = call(self.app, "user.getWeeklyChartList", user="alice")
        charts = body["weeklychartlist"]["chart"]
        self.assertTrue(charts)
        for c in charts:
            self.assertEqual(int(c["to"]) - int(c["from"]), 7 * 86400)
            self.assertEqual((int(c["from"]) - 1_108_296_000) % (7 * 86400), 0)
        frm, to = NOW - 4 * 86400, NOW
        _, body = call(self.app, "user.getWeeklyTrackChart", user="alice", **{"from": frm, "to": to})
        names = sorted(t["name"] for t in body["weeklytrackchart"]["track"])
        self.assertEqual(names, ["w", "x", "y", "z"])
        self.assertEqual(body["weeklytrackchart"]["@attr"], {"from": str(frm), "user": "alice", "to": str(to)})
        _, body = call(self.app, "user.getWeeklyArtistChart", user="alice", **{"from": frm, "to": to})
        self.assertEqual(body["weeklyartistchart"]["artist"][0], {
            "mbid": "", "url": "https://www.last.fm/music/A", "name": "A", "@attr": {"rank": "1"}, "playcount": "2"})

    def test_personal_tags(self):
        sk = self.app.store.create_session("alice")
        _, body = call(self.app, "artist.addTags", http="POST", sign=True, sk=sk, artist="A", tags="rock, Indie")
        self.assertEqual(body, {})
        call(self.app, "track.addTags", http="POST", sign=True, sk=sk, artist="A", track="x", tags="rock")
        _, body = call(self.app, "artist.getTags", user="alice", artist="A")
        self.assertEqual([t["name"] for t in body["tags"]["tag"]], ["rock", "Indie"])
        _, body = call(self.app, "user.getTopTags", user="alice")
        self.assertEqual(body["toptags"]["tag"][0]["name"], "rock")
        _, body = call(self.app, "user.getPersonalTags", user="alice", tag="rock", taggingtype="track")
        self.assertEqual(body["taggings"]["tracks"]["track"][0]["url"], "https://www.last.fm/music/A/_/x")
        call(self.app, "artist.removeTag", http="POST", sign=True, sk=sk, artist="A", tag="indie")
        _, body = call(self.app, "artist.getTags", sk=sk, artist="A")
        self.assertEqual([t["name"] for t in body["tags"]["tag"]], ["rock"])


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app(api={"clients": {API_KEY: SECRET}})
        self.app.store.add_user("alice", "correct horse")
        self.host = {"Host": "localhost:9549"}

    def _approve(self, token, password="correct horse", headers=None, csrf=None):
        row = self.app.store.get_token(token)
        params = {"token": token, "csrf": csrf or row["csrf"], "username": "alice", "password": password,
                  "action": "approve", "api_key": API_KEY}
        return self.app.web_auth.handle("POST", params, headers or {"Origin": "http://localhost:9549", **self.host})

    def test_desktop_flow(self):
        _, body = call(self.app, "auth.getToken")
        token = body["token"]
        self.assertEqual(len(token), 32)
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["error"], 14)  # not yet authorized
        page = self.app.web_auth.handle("GET", {"api_key": API_KEY, "token": token}, self.host)
        self.assertEqual(page.status, 200)
        self.assertIn(b'name="csrf"', page.body)
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", page.headers["Content-Security-Policy"])
        self.assertEqual(self._approve(token).status, 200)
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["session"]["name"], "alice")
        self.assertEqual(self.app.store.session_user(body["session"]["key"]), "alice")
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["error"], 4)  # tokens are single use

    def test_web_flow_redirects_with_token(self):
        page = self.app.web_auth.handle("GET", {"api_key": API_KEY, "cb": "http://app.example/cb?x=1"}, self.host)
        html = page.body.decode()
        token = html.split('name="token" value="')[1].split('"')[0]
        params = {"token": token, "csrf": self.app.store.get_token(token)["csrf"], "username": "alice",
                  "password": "correct horse", "action": "approve", "cb": "http://app.example/cb?x=1"}
        resp = self.app.web_auth.handle("POST", params, {"Origin": "http://localhost:9549", **self.host})
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], f"http://app.example/cb?x=1&token={token}")

    def test_web_auth_configured_origin(self):
        origin = "https://gonic.byalice.dev"
        self.app.config.api.allowed_origins = [origin + "/"]
        _, body = call(self.app, "auth.getToken")
        token = body["token"]
        headers = {**self.host, "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self._approve(token, headers=headers).status, 403)
        self.assertEqual(self._approve(token, headers={**headers, "Origin": origin + ".example"}).status, 403)
        headers["Origin"] = origin
        self.assertEqual(self._approve(token, headers=headers, csrf="forged").status, 403)
        self.assertEqual(self._approve(token, headers=headers).status, 200)
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["session"]["name"], "alice")

    def test_browser_default_submit_returns_to_gonic(self):
        cb = "https://gonic.byalice.dev/admin/link_lastfm_do"
        page = self.app.web_auth.handle("GET", {"api_key": API_KEY, "cb": cb}, self.host)
        form = page.body.decode()
        self.assertLess(form.index('value="approve"'), form.index('value="deny"'))
        token = form.split('name="token" value="')[1].split('"')[0]
        params = {"token": token, "csrf": self.app.store.get_token(token)["csrf"], "cb": cb,
                  "username": "alice", "password": "correct horse", "action": "approve"}
        response = self.app.web_auth.handle("POST", params, {**self.host, "Accept": "text/html"})
        location = f"{cb}?token={token}"
        self.assertEqual(response.headers["Refresh"], "0; url=" + location)
        self.assertIn(f'href="{location}"'.encode(), response.body)
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["session"]["name"], "alice")

    def test_web_auth_rejections(self):
        _, body = call(self.app, "auth.getToken")
        token = body["token"]
        self.assertEqual(self._approve(token, headers={"Origin": "https://evil.example", **self.host}).status, 403)
        self.assertEqual(self._approve(token, csrf="forged").status, 403)
        self.assertEqual(self._approve(token, password="nope").status, 403)
        _, body = call(self.app, "auth.getSession", sign=True, token=token)
        self.assertEqual(body["error"], 14)
        self.assertEqual(self.app.web_auth.handle("GET", {"token": "unknown"}, self.host).status, 400)

    def test_token_expiry(self):
        clock = FakeClock()
        app = make_app(clock=clock)
        _, body = call(app, "auth.getToken")
        clock.t += 3601
        _, body = call(app, "auth.getSession", sign=True, token=body["token"])
        self.assertEqual(body["error"], 15)

    def test_mobile_session_and_throttle(self):
        _, body = call(self.app, "auth.getMobileSession", http="POST", sign=True, username="Alice",
                       password="correct horse")
        self.assertEqual(body["session"]["name"], "alice")
        _, body = call(self.app, "auth.getMobileSession", sign=True, username="alice", password="correct horse")
        self.assertEqual(body["error"], 3)  # must be POST
        for _ in range(5):
            _, body = call(self.app, "auth.getMobileSession", http="POST", sign=True, username="alice",
                           password="bad")
            self.assertEqual(body["error"], 4)
        _, body = call(self.app, "auth.getMobileSession", http="POST", sign=True, username="alice",
                       password="correct horse")
        self.assertEqual(body["error"], 29)
        _, body = call(self.app, "auth.getMobileSession", http="POST", sign=True, username="ghost", password="x")
        self.assertEqual(body["error"], 4)

    def test_auto_authorize(self):
        app = make_app(api={"auto_authorize_user": "me"})
        _, body = call(app, "auth.getToken")
        _, body = call(app, "auth.getSession", sign=True, token=body["token"])
        self.assertEqual(body["session"]["name"], "me")

    def test_unsigned_get_session_rejected(self):
        _, body = call(self.app, "auth.getToken")
        resp = handle(self.app, "GET", {"method": "auth.getSession", "api_key": API_KEY, "token": body["token"],
                                        "format": "json"})
        self.assertEqual(json.loads(resp.body)["error"], 13)


class ChartTagTests(unittest.TestCase):
    def setUp(self):
        t = FakeTransport()
        t.add_json(f"{F.LB}/1/stats/sitewide/artists", {"payload": {
            "artists": [{"artist_name": "Taylor Swift", "artist_mbid": "20244d07-534f-4eff-b4d4-930878889970",
                         "listen_count": 1000}], "total_artist_count": 5000, "range": "week"}})
        t.add_json(f"{F.LB}/1/stats/sitewide/recordings", {"payload": {
            "recordings": [{"track_name": "Song", "artist_name": "X", "artist_mbids": ["m1"],
                            "recording_mbid": "r1", "listen_count": 50, "caa_release_mbid": "rel", "caa_id": 42}],
            "total_recording_count": 1}})

        def tag_search(method, url, headers, body):
            return 200, {"count": 2, "artists": [{"id": F.CHER, "name": "Cher"}, {"id": "m2", "name": "Madonna"}]}

        t.add(f"{F.MB}/artist", tag_search)
        self.t = t
        self.app = make_app(t)

    def test_charts(self):
        _, body = call(self.app, "chart.getTopArtists", limit=1)
        self.assertEqual(body["artists"]["artist"][0]["name"], "Taylor Swift")
        self.assertEqual(body["artists"]["@attr"]["total"], "5000")
        self.assertIn("range=week", self.t.urls("sitewide/artists")[0])
        _, body = call(self.app, "chart.getTopTracks")
        tr = body["tracks"]["track"][0]
        self.assertEqual(tr["image"][0]["#text"], "https://coverartarchive.org/release/rel/42-250.jpg")
        _, body = call(self.app, "geo.getTopTracks", country="Poland")
        self.assertEqual(body["tracks"]["@attr"]["country"], "Poland")
        self.assertEqual(body["tracks"]["track"][0]["@attr"], {"rank": "1"})
        _, body = call(self.app, "geo.getTopArtists")
        self.assertEqual(body["error"], 6)

    def test_tags(self):
        _, body = call(self.app, "tag.getTopArtists", tag="Pop", limit=2)
        self.assertEqual([a["name"] for a in body["topartists"]["artist"]], ["Cher", "Madonna"])
        from urllib.parse import unquote
        self.assertIn('tag:"pop"', unquote(self.t.urls("/artist")[0]))  # query lower-cased
        _, body = call(self.app, "tag.getInfo", tag="Pop")
        self.assertEqual((body["tag"]["name"], body["tag"]["reach"]), ("pop", "2"))
        _, body = call(self.app, "tag.getSimilar", tag="pop")
        self.assertEqual(body["similartags"]["tag"], [])


if __name__ == "__main__":
    unittest.main()
