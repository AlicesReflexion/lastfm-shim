import json
import unittest
from urllib.parse import unquote
import xml.etree.ElementTree as ET

from lfmshim.errors import UpstreamError
from lfmshim.metadata import base_title, loose, norm

from . import fixtures as F
from .helpers import FakeClock, FakeTransport, call, make_app


class HttpClientTests(unittest.TestCase):
    def test_rate_limit_user_agent_and_cache(self):
        clock = FakeClock()
        t = FakeTransport().add_json(f"{F.MB}/artist/{F.CHER}", F.cher_artist())
        t.add_json(f"{F.MB}/artist/{F.NIRVANA_US}", F.nirvana(F.NIRVANA_US, ""))
        app = make_app(t, clock)
        app.mb.artist(F.CHER)
        app.mb.artist(F.NIRVANA_US)
        app.mb.artist(F.CHER)  # cached
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(len(clock.slept), 1)  # second MB request was paced (0.9 req/s)
        self.assertAlmostEqual(clock.slept[0], 1 / 0.9, places=4)
        ua = t.calls[0][2]["User-Agent"]
        self.assertIn("lfmshim/", ua)
        self.assertIn("tests@example.invalid", ua)

    def test_503_retry_after_then_success(self):
        clock = FakeClock()
        seq = iter([(503, b"", {"Retry-After": "3"}), (200, {"id": F.CHER, "name": "Cher"})])
        t = FakeTransport().add(f"{F.MB}/artist/{F.CHER}", lambda *a: next(seq))
        app = make_app(t, clock)
        self.assertEqual(app.mb.artist(F.CHER)["name"], "Cher")
        self.assertEqual(sum(clock.slept), 3.0)

    def test_404_negative_cache_and_5xx_error(self):
        t = FakeTransport().add_json(f"{F.MB}/artist/{F.CHER}", {"error": "Not Found"}, status=404)
        app = make_app(t)
        self.assertIsNone(app.mb.artist(F.CHER))
        self.assertIsNone(app.mb.artist(F.CHER))
        self.assertEqual(len(t.calls), 1)
        t2 = FakeTransport().add_json(f"{F.MB}/artist/{F.CHER}", {}, status=500)
        with self.assertRaises(UpstreamError):
            make_app(t2).mb.artist(F.CHER)

    def test_optional_failure_is_cached_and_degrades(self):
        calls = []

        def boom(*a):
            calls.append(a)
            raise UpstreamError("api.listenbrainz.org", "network error: timed out")

        t = FakeTransport().add(f"{F.LB}/1/popularity/artist", boom, method="POST")
        app = make_app(t)
        self.assertEqual(app.lb.artist_popularity([F.CHER], 100), {})
        self.assertEqual(app.lb.artist_popularity([F.CHER], 100), {})
        self.assertEqual(len(calls), 1)  # failure cached: no stall on every request
        self.assertEqual(t.timeouts, [3.0])  # short timeout for optional data

    def test_offline_mode(self):
        app = make_app(upstream={"offline": True})
        with self.assertRaises(UpstreamError):
            app.mb.artist(F.CHER)


class NormalisationTests(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(norm("  Simon & Garfunkel "), "simon and garfunkel")
        self.assertEqual(norm("Don’t Stop"), norm("Don't stop"))
        self.assertEqual(loose("The Beatles"), loose("beatles"))
        self.assertEqual(base_title("Believe (Remastered 2009) [Live]"), "Believe")
        self.assertEqual(base_title("Song - 2011 Remaster"), "Song")


class ArtistTests(unittest.TestCase):
    def setUp(self):
        self.t = F.install_cher(FakeTransport())
        self.app = make_app(self.t)

    def test_get_info_json(self):
        status, body = call(self.app, "artist.getInfo", artist="cher")
        self.assertEqual(status, 200)
        a = body["artist"]
        self.assertEqual((a["name"], a["mbid"]), ("Cher", F.CHER))
        self.assertEqual(a["url"], "https://www.last.fm/music/Cher")
        self.assertEqual(len(a["image"]), 6)
        self.assertEqual(a["image"][3], {"#text": "https://r2.theaudiodb.com/cher.jpg", "size": "extralarge"})
        self.assertEqual(a["stats"], {"listeners": "900000", "playcount": "25000000"})
        self.assertEqual([s["name"] for s in a["similar"]["artist"]], ["Madonna", "Kylie Minogue"])
        self.assertEqual([t["name"] for t in a["tags"]["tag"]], ["pop", "dance-pop", "dance"])
        self.assertIn("Cher is an American singer &amp; actress.", a["bio"]["summary"])
        self.assertIn("https://en.wikipedia.org/wiki/Cher", a["bio"]["summary"])
        self.assertIn("Creative Commons", a["bio"]["content"])
        self.assertEqual(a["bio"]["published"], "01 Oct 2026, 12:00")
        # Cached: a second call makes no upstream requests.
        n = len(self.t.calls)
        call(self.app, "artist.getInfo", artist="Cher")
        self.assertEqual(len(self.t.calls), n)

    def test_get_info_xml(self):
        status, body = call(self.app, "artist.getInfo", fmt=None, artist="Cher")
        root = ET.fromstring(body)
        self.assertEqual(root.get("status"), "ok")
        self.assertEqual(len(root.findall("artist/image")), 6)
        self.assertEqual(root.findtext("artist/stats/listeners"), "900000")
        self.assertEqual(root.find("artist/image[@size='mega']").text, "https://r2.theaudiodb.com/cher.jpg")

    def test_disambiguation_by_popularity(self):
        _, body = call(self.app, "artist.getInfo", artist="Nirvana")
        self.assertEqual(body["artist"]["mbid"], F.NIRVANA_US)

    def test_mbid_lookup_and_alias_and_fuzzy(self):
        _, body = call(self.app, "artist.getInfo", mbid=F.NIRVANA_UK)
        self.assertEqual(body["artist"]["mbid"], F.NIRVANA_UK)
        _, body = call(self.app, "artist.getCorrection", artist="Cherilyn Sarkisian")
        self.assertEqual(body["corrections"]["correction"]["artist"]["name"], "Cher")
        _, body = call(self.app, "artist.getCorrection", artist="Cherr")
        self.assertEqual(body["corrections"]["correction"]["artist"]["mbid"], F.CHER)
        _, body = call(self.app, "artist.getCorrection", artist="Cher")
        self.assertEqual(body["corrections"], {})

    def test_unknown_artist_is_error6_http200(self):
        status, body = call(self.app, "artist.getInfo", artist="zzzz unknown")
        self.assertEqual((status, body["error"]), (200, 6))
        self.assertEqual(body["message"], "The artist you supplied could not be found")
        status, body = call(self.app, "artist.getInfo")
        self.assertEqual((status, body["error"]), (200, 6))

    def test_similar_and_tags(self):
        _, body = call(self.app, "artist.getSimilar", artist="Cher", limit=1)
        sim = body["similarartists"]
        self.assertEqual(sim["@attr"], {"artist": "Cher"})
        self.assertEqual(sim["artist"][0]["name"], "Madonna")
        self.assertEqual(sim["artist"][0]["match"], "1")  # not "1."
        _, body = call(self.app, "artist.getTopTags", artist="Cher")
        self.assertEqual(body["toptags"]["tag"][0], {"count": "100", "name": "pop",
                                                     "url": "https://www.last.fm/tag/pop"})
        self.assertEqual(body["toptags"]["tag"][1]["count"], "50")

    def test_search_opensearch_xml_parses(self):
        _, body = call(self.app, "artist.search", artist="Cher", limit=2)
        res = body["results"]
        self.assertEqual(res["opensearch:totalResults"], "2")
        self.assertEqual(res["artistmatches"]["artist"][0]["listeners"], "900000")
        _, xml = call(self.app, "artist.search", fmt=None, artist="Cher", limit=2)
        root = ET.fromstring(xml)  # namespace is declared, unlike live Last.fm
        ns = {"opensearch": "http://a9.com/-/spec/opensearch/1.1/"}
        self.assertEqual(root.findtext("results/opensearch:totalResults", namespaces=ns), "2")

    def test_upstream_failure_is_reported(self):
        t = FakeTransport().add_json(f"{F.MB}/artist", {}, status=500)
        app = make_app(t)
        _, body = call(app, "artist.getInfo", artist="Cher")
        self.assertEqual(body["error"], 16)


class AlbumTrackTests(unittest.TestCase):
    def setUp(self):
        self.t = F.install_believe(F.install_cher(FakeTransport()))
        self.app = make_app(self.t)

    def test_album_get_info(self):
        _, body = call(self.app, "album.getInfo", artist="Cher", album="Believe")
        a = body["album"]
        self.assertEqual(a["mbid"], F.BELIEVE_REL)  # earliest official release
        self.assertEqual((a["name"], a["artist"]), ("Believe", "Cher"))
        self.assertEqual(a["image"][0]["#text"], "https://coverartarchive.org/release/x/1-250.jpg")
        self.assertEqual(a["image"][4]["#text"], "https://coverartarchive.org/release/x/1-1200.jpg")
        self.assertEqual([t["name"] for t in a["tracks"]["track"]], ["Believe", "The Power"])
        # Observed live (Cher / Believe): JSON integers, not strings.
        self.assertEqual(a["tracks"]["track"][0]["duration"], 239)
        self.assertEqual(a["tracks"]["track"][1]["@attr"], {"rank": 2})
        self.assertEqual(a["listeners"], "7000")
        self.assertEqual(a["tags"]["tag"][0]["name"], "dance-pop")

    def test_album_by_release_and_rg_mbid(self):
        _, body = call(self.app, "album.getInfo", mbid=F.BELIEVE_REL)
        self.assertEqual(body["album"]["name"], "Believe")
        self.t.add_json(f"{F.MB}/release/{F.BELIEVE_RG}", {}, status=404)
        _, body = call(self.app, "album.getInfo", mbid=F.BELIEVE_RG)
        self.assertEqual(body["album"]["mbid"], F.BELIEVE_REL)

    def test_track_get_info(self):
        self.app.store.add_user("alice")
        self.app.store.love("alice", "Cher", "Believe")
        _, body = call(self.app, "track.getInfo", artist="Cher", track="Believe", username="alice")
        t = body["track"]
        self.assertEqual(t["mbid"], F.BELIEVE_REC)  # disambiguated by popularity, video excluded
        self.assertEqual(t["duration"], "239000")
        self.assertEqual(t["album"]["title"], "Believe")  # studio album beats compilation
        self.assertEqual(t["album"]["mbid"], F.BELIEVE_REL)
        self.assertEqual(t["userloved"], "1")
        self.assertEqual(t["userplaycount"], "0")
        self.assertEqual(t["listeners"], "500000")

    def test_track_search_and_correction(self):
        _, body = call(self.app, "track.search", track="Believe", artist="Cher")
        matches = body["results"]["trackmatches"]["track"]
        self.assertEqual(len(matches), 2)  # video excluded
        _, body = call(self.app, "track.getCorrection", artist="cher", track="believe")
        c = body["corrections"]["correction"]
        self.assertEqual(c["@attr"], {"index": "0", "artistcorrected": "1", "trackcorrected": "1"})

    def test_track_choice_without_popularity_prefers_studio_album(self):
        # Regression from a live check: with ListenBrainz popularity timing out,
        # MusicBrainz search order picked live/bootleg versions.
        t = F.install_cher(FakeTransport())  # provides the Nirvana artist search
        credit = [{"name": "Nirvana", "artist": {"id": F.NIRVANA_US, "name": "Nirvana"}}]
        live = {"id": "aaaaaaaa-0000-0000-0000-00000000000a", "title": "Smells Like Teen Spirit", "score": 100,
                "artist-credit": credit, "disambiguation": "live, 1991-09-16: Seattle",
                "releases": [{"id": "r-live", "status": "Bootleg", "date": "1991",
                              "release-group": {"primary-type": "Album", "secondary-types": ["Live"]}}]}
        comp = {"id": "aaaaaaaa-0000-0000-0000-00000000000b", "title": "Smells Like Teen Spirit", "score": 100,
                "artist-credit": credit,
                "releases": [{"id": "r-comp", "status": "Official", "date": "2002",
                              "release-group": {"primary-type": "Album", "secondary-types": ["Compilation"]}}]}
        studio = {"id": "aaaaaaaa-0000-0000-0000-00000000000c", "title": "Smells Like Teen Spirit", "score": 98,
                  "artist-credit": credit,
                  "releases": [{"id": "r-nev", "status": "Official", "date": "1991-09-24",
                                "release-group": {"primary-type": "Album", "secondary-types": []}}]}
        t.add_json(f"{F.MB}/recording", {"count": 3, "recordings": [live, comp, studio]})
        t.add_json(f"{F.LB}/1/popularity/recording", {}, status=500, method="POST")
        t.add_json(f"{F.MB}/recording/{studio['id']}", dict(studio, length=301000))
        app = make_app(t)
        rec = app.resolver.track(artist="Nirvana", track="Smells Like Teen Spirit")
        self.assertEqual(rec["id"], studio["id"])
        first_query = unquote(t.urls("/ws/2/recording?")[0]).replace("+", " ")
        self.assertIn(f"arid:{F.NIRVANA_US} AND status:official AND primarytype:album", first_query)

    def test_missing_params(self):
        _, body = call(self.app, "album.getInfo", artist="Cher")
        self.assertEqual(body["error"], 6)
        _, body = call(self.app, "track.getInfo", track="x")
        self.assertEqual(body["error"], 6)


class LiveShapeRegressionTests(unittest.TestCase):
    """JSON shapes observed in read-only live Last.fm probes (compat-observations.md)."""

    def setUp(self):
        self.t = F.install_believe(F.install_cher(FakeTransport()))
        self.t.add_json(f"{F.LB}/1/popularity/top-recordings-for-artist/{F.CHER}", [
            {"recording_mbid": F.BELIEVE_REC, "recording_name": "Believe", "total_listen_count": 10,
             "total_user_count": 5}])
        self.t.add_json(f"{F.LB}/1/stats/sitewide/recordings", {"payload": {
            "recordings": [{"track_name": "Song", "artist_name": "X", "artist_mbids": ["m1"],
                            "recording_mbid": "r1", "listen_count": 50}], "total_recording_count": 1}})
        self.app = make_app(self.t)

    def test_album_track_duration_and_rank_are_json_numbers(self):
        _, body = call(self.app, "album.getInfo", artist="Cher", album="Believe")
        track = body["album"]["tracks"]["track"][0]
        self.assertIs(type(track["duration"]), int)
        self.assertIs(type(track["@attr"]["rank"]), int)
        self.assertEqual(body["album"]["listeners"], "7000")  # other numbers stay strings
        _, xml = call(self.app, "album.getInfo", fmt=None, artist="Cher", album="Believe")
        root = ET.fromstring(xml)
        self.assertEqual(root.findtext("album/tracks/track/duration"), "239")
        self.assertEqual(root.find("album/tracks/track").get("rank"), "1")

    def test_album_track_unknown_duration_is_null(self):
        release = json.loads(self.t.request("GET", f"{F.MB}/release/{F.BELIEVE_REL}", {}, None, 1)[2])
        release["media"][0]["tracks"].append({"position": 3, "title": "Untimed",
                                              "recording": {"id": "c3f6e0b2-0000-0000-0000-000000000009",
                                                            "title": "Untimed"}})
        self.t.add_json(f"{F.MB}/release/{F.BELIEVE_REL}", release)
        _, body = call(self.app, "album.getInfo", artist="Cher", album="Believe")
        self.assertIsNone(body["album"]["tracks"]["track"][2]["duration"])
        _, xml = call(self.app, "album.getInfo", fmt=None, artist="Cher", album="Believe")
        self.assertIn("<duration/>", xml)

    def test_artist_top_tracks_rank_is_string(self):
        _, body = call(self.app, "artist.getTopTracks", artist="Cher", limit=1)
        self.assertEqual(body["toptracks"]["track"][0]["@attr"], {"rank": "1"})

    def test_single_item_results_are_arrays(self):
        # Observed live with limit=1: each of these returned a one-element array.
        app = self.app
        app.store.add_user("alice")
        app.store.add_scrobble("alice", {"artist": "Cher", "track": "Believe", "timestamp": app.now() - 60})
        cases = [
            (("artist.search", {"artist": "Cher"}), ("results", "artistmatches", "artist")),
            (("artist.getSimilar", {"artist": "Cher"}), ("similarartists", "artist")),
            (("artist.getTopTracks", {"artist": "Cher"}), ("toptracks", "track")),
            (("chart.getTopTracks", {}), ("tracks", "track")),
            (("user.getRecentTracks", {"user": "alice"}), ("recenttracks", "track")),
        ]
        for (name, params), path in cases:
            with self.subTest(name):
                if name == "artist.search":  # the shared fake ignores limit
                    self.t.add_json(f"{F.MB}/artist?limit=1", {"count": 38, "artists": [F.cher_artist()]})
                _, body = call(app, name, limit=1, **params)
                if name == "artist.search":
                    self.t.routes.pop(0)
                node = body
                for key in path:
                    node = node[key]
                self.assertIsInstance(node, list)
                self.assertEqual(len(node), 1)


if __name__ == "__main__":
    unittest.main()
