"""Album/artist public tags: Discogs (via MusicBrainz links), TheAudioDB, MusicBrainz."""

import json
import unittest
import xml.etree.ElementTree as ET


from . import fixtures as F
from .helpers import FakeClock, FakeTransport, call, make_app
from .test_discogs import DG, master, rel


def rg(mbid, discogs_url=None, secondary=(), date="1990"):
    return {"id": mbid, "title": mbid[-4:], "primary-type": "Album", "secondary-types": list(secondary),
            "first-release-date": date, "relations": [rel(discogs_url)] if discogs_url else []}


class AlbumTagTests(unittest.TestCase):
    def setUp(self):
        self.t = F.install_believe(F.install_cher(FakeTransport()))
        self.rg = json.loads(self.t.request("GET", f"{F.MB}/release-group/{F.BELIEVE_RG}", {}, None, 1)[2])
        self.t.calls.clear()

    def link_rg(self, url):
        self.t.add_json(f"{F.MB}/release-group/{F.BELIEVE_RG}", dict(self.rg, relations=[rel(url)]))

    def test_master_styles_then_genres(self):
        self.link_rg("https://www.discogs.com/master/3334")
        self.t.add_json(f"{DG}/masters/3334", master(3334, ["Euro House", "Synth-pop"], ["Electronic", "Pop"]))
        app = make_app(self.t)
        _, body = call(app, "album.getTopTags", artist="Cher", album="Believe")
        top = body["toptags"]
        self.assertEqual([t["name"] for t in top["tag"]], ["euro house", "synth-pop", "electronic", "pop"])
        self.assertEqual({t["count"] for t in top["tag"]}, {"100"})  # unranked attributes, equal weight
        self.assertEqual(top["tag"][0]["url"], "https://www.last.fm/tag/euro+house")  # standard meaning kept
        self.assertEqual(top["@attr"], {"artist": "Cher", "album": "Believe"})
        self.assertNotIn("source", top)  # strict shape unless opted in
        _, body = call(app, "album.getInfo", artist="Cher", album="Believe")
        self.assertEqual([t["name"] for t in body["album"]["tags"]["tag"]],
                         ["euro house", "synth-pop", "electronic", "pop"])
        dg_calls = [c for c in self.t.calls if c[1].startswith(DG)]
        self.assertEqual(len(dg_calls), 1)  # cached after the first lookup
        self.assertNotIn("Authorization", dg_calls[0][2])  # public lookup: no token needed
        self.assertIn("lfmshim/", dg_calls[0][2]["User-Agent"])

    def test_opt_in_sources_json_and_xml(self):
        self.link_rg("https://www.discogs.com/master/3334")
        self.t.add_json(f"{DG}/masters/3334", master(3334, ["Synth-pop"], ["Electronic"]))
        app = make_app(self.t)
        _, body = call(app, "album.getTopTags", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual(body["toptags"]["source"], [{"name": "Discogs", "provider": "discogs",
                                                      "url": "https://www.discogs.com/master/3334-Cher-M3334"}])
        _, body = call(app, "album.getInfo", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual(body["album"]["tags"]["source"][0]["provider"], "discogs")
        _, xml = call(app, "album.getTopTags", fmt=None, artist="Cher", album="Believe", tagsources=1)
        root = ET.fromstring(xml)
        self.assertEqual(root.find("toptags").get("album"), "Believe")
        self.assertEqual(root.findtext("toptags/source/url"), "https://www.discogs.com/master/3334-Cher-M3334")
        self.assertEqual(root.findtext("toptags/tag/url"), "https://www.last.fm/tag/synth-pop")

    def test_release_link_when_group_has_none(self):
        release = json.loads(self.t.request("GET", f"{F.MB}/release/{F.BELIEVE_REL}", {}, None, 1)[2])
        release["relations"] = [rel("https://www.discogs.com/release/42")]
        self.t.add_json(f"{F.MB}/release/{F.BELIEVE_REL}", release)
        self.t.add_json(f"{DG}/releases/42", {"id": 42, "styles": [], "genres": ["Electronic"],
                                              "uri": "https://www.discogs.com/release/42-Believe"})
        app = make_app(self.t)
        _, body = call(app, "album.getTopTags", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["electronic"])
        self.assertEqual(body["toptags"]["source"][0]["url"], "https://www.discogs.com/release/42-Believe")
        self.assertTrue(any("inc=" in u and "url-rels" in u for u in self.t.urls(f"/release/{F.BELIEVE_REL}")))

    def test_master_without_tags_falls_back_to_release(self):
        self.link_rg("https://www.discogs.com/master/3334")
        self.t.add_json(f"{DG}/masters/3334", master(3334, [], []))
        release = json.loads(self.t.request("GET", f"{F.MB}/release/{F.BELIEVE_REL}", {}, None, 1)[2])
        release["relations"] = [rel("https://www.discogs.com/release/42")]
        self.t.add_json(f"{F.MB}/release/{F.BELIEVE_REL}", release)
        self.t.add_json(f"{DG}/releases/42", {"id": 42, "styles": ["Vocal"], "genres": []})
        _, body = call(make_app(self.t), "album.getTopTags", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["vocal"])
        self.assertEqual(body["toptags"]["source"][0]["url"], "https://www.discogs.com/release/42")

    def test_theaudiodb_then_musicbrainz_fallback(self):
        self.t.add_json(f"{F.TADB}/album-mb.php", {"album": [{"idAlbum": "2109615", "strGenre": "Dance",
                                                              "strStyle": "Pop/Rock"}]})
        _, body = call(make_app(self.t), "album.getTopTags", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual(body["toptags"]["tag"], [{"count": "100", "name": "dance",
                                                   "url": "https://www.last.fm/tag/dance"}])
        self.assertEqual(body["toptags"]["source"], [{"name": "TheAudioDB", "provider": "theaudiodb",
                                                      "url": "https://www.theaudiodb.com/album/2109615"}])
        self.t.add_json(f"{F.TADB}/album-mb.php", {"album": None})
        _, body = call(make_app(self.t), "album.getTopTags", artist="Cher", album="Believe", tagsources=1)
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["dance-pop", "pop"])
        self.assertEqual(body["toptags"]["source"][0]["url"], f"https://musicbrainz.org/release-group/{F.BELIEVE_RG}")

    def test_discogs_failure_degrades_and_disable_flag(self):
        self.link_rg("https://www.discogs.com/master/3334")
        self.t.add_json(f"{DG}/masters/3334", {"message": "boom"}, status=500)
        _, body = call(make_app(self.t), "album.getTopTags", artist="Cher", album="Believe")
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["dance-pop", "pop"])
        self.t.calls.clear()
        app = make_app(self.t, upstream={"discogs_enabled": False})
        call(app, "album.getTopTags", artist="Cher", album="Believe")
        self.assertEqual(self.t.urls(DG), [])

    def test_personal_tags_untouched(self):
        self.link_rg("https://www.discogs.com/master/3334")
        self.t.add_json(f"{DG}/masters/3334", master(3334, ["Synth-pop"], ["Electronic"]))
        app = make_app(self.t)
        app.store.add_user("alice")
        app.store.add_tags("alice", "album", "Cher", "Believe", ["mine"])
        _, body = call(app, "album.getTags", artist="Cher", album="Believe", user="alice", tagsources=1)
        self.assertEqual(body["tags"]["tag"], [{"name": "mine", "url": "https://www.last.fm/tag/mine"}])
        self.assertEqual(self.t.urls(DG), [])


class ArtistTagTests(unittest.TestCase):
    RG = ["aaaaaaaa-0000-0000-0000-00000000000%d" % i for i in range(1, 7)]

    def setUp(self):
        self.clock = FakeClock()
        self.t = F.install_cher(FakeTransport())
        cher = dict(F.cher_artist(), relations=F.cher_artist()["relations"] + [
            rel("https://www.discogs.com/artist/96076-Cher")])
        self.t.add_json(f"{F.MB}/artist/{F.CHER}", cher)
        self.t.add_json(f"{F.MB}/release-group?type=album", {"release-group-count": 6, "release-groups": [
            rg(self.RG[0], "https://www.discogs.com/master/1", date="1998"),
            rg(self.RG[1], "https://www.discogs.com/master/2", date="1987"),
            rg(self.RG[2], "https://www.discogs.com/master/3", secondary=["Compilation"]),  # excluded
            rg(self.RG[3], "https://www.discogs.com/release/9"),  # a pressing of master 1: not counted twice
            rg(self.RG[4], "https://www.discogs.com/master/2"),   # same master as RG[1]
            rg(self.RG[5]),                                         # no Discogs link
        ]})
        listeners = {self.RG[0]: 900, self.RG[1]: 50, self.RG[3]: 10}
        self.t.add(f"{F.LB}/1/popularity/release-group", lambda m, u, h, b: (200, [
            {"release_group_mbid": x, "total_user_count": listeners.get(x, 0), "total_listen_count": 1}
            for x in json.loads(b)["release_group_mbids"]]), method="POST")
        self.t.add_json(f"{DG}/masters/1", master(1, ["Euro House", "Synth-pop"], ["Electronic", "Pop"]))
        self.t.add_json(f"{DG}/masters/2", master(2, ["Synth-pop", "Soft Rock"], ["Rock", "Electronic"]))
        self.t.add_json(f"{DG}/masters/3", master(3, ["Ballad"], ["Pop"]))
        self.t.add_json(f"{DG}/releases/9", {"id": 9, "master_id": 1, "styles": ["Euro House"],
                                             "genres": ["Electronic"]})

    def app(self, budget=10, **upstream):
        return make_app(self.t, self.clock, upstream={"discogs_request_budget": budget, **upstream})

    def test_counts_once_per_distinct_album(self):
        _, body = call(self.app(), "artist.getTopTags", artist="Cher", tagsources=1)
        top = body["toptags"]
        self.assertEqual([(t["name"], t["count"]) for t in top["tag"]], [
            ("synth-pop", "100"), ("euro house", "50"), ("soft rock", "50"),  # styles by album count
            ("electronic", "100"), ("pop", "50"), ("rock", "50")])            # then genres
        self.assertEqual([s["url"] for s in top["source"]], [
            "https://www.discogs.com/artist/96076", "https://www.discogs.com/master/1-Cher-M1",
            "https://www.discogs.com/master/2-Cher-M2"])
        self.assertEqual(top["@attr"], {"artist": "Cher"})
        self.assertNotIn(f"{DG}/masters/3", self.t.urls(DG))  # compilation skipped
        self.assertEqual(self.t.urls(DG).count(f"{DG}/masters/2"), 1)

    def test_get_info_uses_same_tags(self):
        app = self.app()
        _, body = call(app, "artist.getInfo", artist="Cher")
        self.assertEqual([t["name"] for t in body["artist"]["tags"]["tag"]],
                         ["synth-pop", "euro house", "soft rock", "electronic", "pop"])
        self.assertNotIn("source", body["artist"]["tags"])

    def test_album_limit_and_popularity_order(self):
        _, body = call(self.app(discogs_artist_albums=1), "artist.getTopTags", artist="Cher")
        # Only the most-listened album (RG[0] -> master 1) is sampled.
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["euro house", "synth-pop", "electronic", "pop"])
        self.assertEqual(self.t.urls(DG), [f"{DG}/masters/1"])

    def test_budget_defers_and_background_warms(self):
        app = self.app(budget=1)
        _, body = call(app, "artist.getTopTags", artist="Cher")
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["euro house", "synth-pop", "electronic", "pop"])
        self.assertEqual(len(self.t.urls(DG)), 1)  # bounded: one uncached Discogs lookup
        queued = [json.loads(r["payload"]) for r in app.store.outbox_items()]
        self.assertEqual(queued, [{"task": "tags", "kind": "artist", "mbid": F.CHER}])
        call(app, "artist.getTopTags", artist="Cher")
        self.assertEqual(len(app.store.outbox_items()), 1)  # not queued twice
        self.assertEqual(len(self.t.urls(DG)), 2)  # each call gets its own budget of one lookup
        self.assertEqual(app.sync.flush(), 1)
        self.assertEqual(len(self.t.urls(DG)), 3)  # masters 1, 2 and release 9
        n = len(self.t.calls)
        _, body = call(app, "artist.getTopTags", artist="Cher")
        self.assertEqual(body["toptags"]["tag"][0], {"count": "100", "name": "synth-pop",
                                                     "url": "https://www.last.fm/tag/synth-pop"})
        self.assertEqual(len(self.t.calls), n)  # fully cached now

    def test_zero_budget_falls_back_then_warms(self):
        app = self.app(budget=0)
        _, body = call(app, "artist.getTopTags", artist="Cher", tagsources=1)
        self.assertEqual(body["toptags"]["source"][0]["provider"], "musicbrainz")
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]][:2], ["pop", "dance-pop"])
        self.assertEqual(self.t.urls(DG), [])
        app.sync.flush()
        _, body = call(app, "artist.getTopTags", artist="Cher", tagsources=1)
        self.assertEqual(body["toptags"]["source"][0]["provider"], "discogs")

    def test_theaudiodb_artist_genre_fallback(self):
        self.t.add_json(f"{F.MB}/release-group?type=album", {"release-group-count": 0, "release-groups": []})
        self.t.add_json(f"{F.TADB}/artist-mb.php", {"artists": [{"idArtist": "111279", "strGenre": "Pop",
                                                                 "strArtistThumb": "https://r2.theaudiodb.com/c.jpg"}]})
        app = self.app()
        _, body = call(app, "artist.getTopTags", artist="Cher", tagsources=1)
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["pop"])
        self.assertEqual(body["toptags"]["source"][0]["url"], "https://www.theaudiodb.com/artist/111279")
        n = len(self.t.urls("artist-mb.php"))
        call(app, "artist.getInfo", artist="Cher")
        self.assertEqual(len(self.t.urls("artist-mb.php")), n)  # images reuse the same cached lookup

    def test_offline_uses_cache_only(self):
        app = self.app(budget=1)
        call(app, "artist.getTopTags", artist="Cher")  # caches master 1 (and the MB data)
        app.config.upstream.offline = True
        app.store.outbox_delete([r["id"] for r in app.store.outbox_items()])
        n = len(self.t.calls)
        _, body = call(app, "artist.getTopTags", artist="Cher")
        self.assertEqual([t["name"] for t in body["toptags"]["tag"]], ["euro house", "synth-pop", "electronic", "pop"])
        self.assertEqual(len(self.t.calls), n)
        self.assertEqual(app.store.outbox_items(), [])  # nothing queued while offline


if __name__ == "__main__":
    unittest.main()
