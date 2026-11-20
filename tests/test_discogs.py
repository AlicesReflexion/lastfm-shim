"""Discogs client: MusicBrainz link parsing, auth header, rate limits."""

import os
import tempfile
import unittest

from lfmshim.config import load_config
from lfmshim.upstream.discogs import discogs_links, genres_styles, page_url

from .helpers import FakeClock, FakeTransport, make_app

DG = "https://api.discogs.com"


def rel(url, ended=False):
    return {"type": "discogs", "ended": ended, "url": {"resource": url}}


def master(mid, styles, genres, **extra):
    return {"id": mid, "title": f"M{mid}", "styles": styles, "genres": genres,
            "uri": f"https://www.discogs.com/master/{mid}-Cher-M{mid}", **extra}


class LinkParsingTests(unittest.TestCase):
    def test_discogs_links(self):
        entity = {"relations": [
            rel("https://www.discogs.com/master/3334"),
            rel("https://www.discogs.com/fr/release/12-Some-Slug"),
            rel("http://discogs.com/artist/3840-Radiohead"),
            rel("https://www.discogs.com/master/3334"),          # duplicate
            rel("https://www.discogs.com/master/99", ended=True),  # ended link
            rel("https://www.discogs.com/label/5"),              # not a lookup target
            {"type": "wikidata", "url": {"resource": "https://www.discogs.com/master/7"}},
        ]}
        self.assertEqual(discogs_links(entity), [("master", 3334), ("release", 12), ("artist", 3840)])
        self.assertEqual(discogs_links(None), [])

    def test_genres_styles(self):
        styles, genres = genres_styles({"styles": ["Pop Rock", "pop rock", " ", None], "genres": ["Rock", "Pop Rock"]})
        self.assertEqual((styles, genres), (["Pop Rock"], ["Rock"]))
        self.assertEqual(genres_styles(None), ([], []))

    def test_page_url_only_trusts_matching_discogs_pages(self):
        ok = "https://www.discogs.com/master/3334-Coldplay-Parachutes"
        self.assertEqual(page_url({"uri": ok}, "master", 3334), ok)
        for bad in ("https://evil.example/master/3334", "https://www.discogs.com/master/1-Other",
                    'https://www.discogs.com/master/3334-"x', None):
            self.assertEqual(page_url({"uri": bad}, "master", 3334), "https://www.discogs.com/master/3334")


class DiscogsClientTests(unittest.TestCase):
    def test_token_header_and_rate(self):
        t = FakeTransport().add_json(f"{DG}/masters/1", master(1, ["A"], []))
        app = make_app(t, upstream={"discogs_token": "tok123"})
        self.assertEqual(app.discogs.lookup("master", 1)["styles"], ["A"])
        self.assertEqual(t.calls[0][2]["Authorization"], "Discogs token=tok123")
        self.assertAlmostEqual(app.http.limiters["discogs"].interval, 60 / 58)
        self.assertAlmostEqual(make_app().http.limiters["discogs"].interval, 60 / 24)
        self.assertNotIn("tok123", repr(app.config.upstream))

    def test_rate_limited_lookup_slows_later_requests(self):
        clock = FakeClock()
        t = FakeTransport().add(f"{DG}/masters/1", (429, b"", {"Retry-After": "30"}))
        t.add_json(f"{DG}/masters/2", master(2, ["B"], []))
        app = make_app(t, clock)
        self.assertIsNone(app.discogs.lookup("master", 1))
        self.assertEqual(app.discogs.lookup("master", 2)["styles"], ["B"])
        self.assertGreaterEqual(sum(clock.slept), 30)

    def test_token_from_env_and_file(self):
        cfg = load_config(env={"LFMSHIM_UPSTREAM_DISCOGS_TOKEN": " abc "})
        self.assertEqual(cfg.upstream.resolved_discogs_token(), "abc")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "tok")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("filetok\n")
            cfg = load_config(env={"LFMSHIM_UPSTREAM_DISCOGS_TOKEN_FILE": path})
            self.assertEqual(cfg.upstream.resolved_discogs_token(), "filetok")


if __name__ == "__main__":
    unittest.main()
