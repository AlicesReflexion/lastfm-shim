"""Canned upstream payloads (shapes follow the real services' JSON)."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlsplit

MB = "https://musicbrainz.org/ws/2"
LB = "https://api.listenbrainz.org"
LABS = "https://labs.api.listenbrainz.org"
CAA = "https://coverartarchive.org"
TADB = "https://www.theaudiodb.com/api/v1/json/123"

CHER = "bfcc6d75-a6a5-4bc6-8282-47aec8531818"
NIRVANA_US = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
NIRVANA_UK = "9282c8b4-ca0b-4c6b-b7e3-4f7762dfc4d6"
BELIEVE_RG = "a8c19a6b-dc4d-3ad8-b8a1-4ad8e8b1e3a3"
BELIEVE_REL = "b1a1e8a4-bd2b-4c1b-9e1b-1f0c4f5e9a01"
BELIEVE_REL_LATER = "b1a1e8a4-bd2b-4c1b-9e1b-1f0c4f5e9a02"
BELIEVE_REC = "c3f6e0b2-2b4a-4a3c-8d6e-0a1b2c3d4e01"
BELIEVE_REC_LIVE = "c3f6e0b2-2b4a-4a3c-8d6e-0a1b2c3d4e02"
COMPILATION_REL = "d1d1d1d1-2b4a-4a3c-8d6e-0a1b2c3d4e03"


def query_of(url: str) -> str:
    return dict(parse_qsl(urlsplit(url).query)).get("query", "")


def cher_artist():
    return {
        "id": CHER, "name": "Cher", "sort-name": "Cher", "type": "Person", "country": "US",
        "disambiguation": "American singer and actress",
        "tags": [{"name": "pop", "count": 12}, {"name": "dance", "count": 4}],
        "genres": [{"name": "pop", "count": 12, "id": "g1"}, {"name": "dance-pop", "count": 6, "id": "g2"}],
        "aliases": [{"name": "Cherilyn Sarkisian"}],
        "relations": [
            {"type": "wikidata", "url": {"resource": "https://www.wikidata.org/wiki/Q12003"}},
            {"type": "official homepage", "url": {"resource": "https://cher.com"}},
        ],
    }


def nirvana(mbid, disamb):
    return {"id": mbid, "name": "Nirvana", "sort-name": "Nirvana", "disambiguation": disamb,
            "tags": [], "genres": [], "relations": []}


def install_cher(t):
    """Artist + Wikipedia + TheAudioDB + ListenBrainz for Cher."""
    def artist_search(method, url, headers, body):
        q = query_of(url)
        if '"Cher"' in q or '"cher"' in q:
            return 200, {"count": 2, "artists": [
                {"id": CHER, "name": "Cher", "sort-name": "Cher", "score": 100},
                {"id": "00000000-0000-0000-0000-00000000c4e1", "name": "Cher Lloyd", "score": 70}]}
        if "Nirvana" in q:
            return 200, {"count": 2, "artists": [
                {"id": NIRVANA_US, "name": "Nirvana", "score": 100, "disambiguation": "90s US grunge band"},
                {"id": NIRVANA_UK, "name": "Nirvana", "score": 100, "disambiguation": "60s band from the UK"}]}
        if "Cherilyn" in q:
            return 200, {"count": 1, "artists": [
                {"id": CHER, "name": "Cher", "score": 95, "aliases": [{"name": "Cherilyn Sarkisian"}]}]}
        if "Cherr" in q:
            return 200, {"count": 1, "artists": [{"id": CHER, "name": "Cher", "score": 88}]}
        return 200, {"count": 0, "artists": []}

    t.add(f"{MB}/artist", artist_search)
    t.add_json(f"{MB}/artist/{CHER}", cher_artist())
    t.add_json(f"{MB}/artist/{NIRVANA_US}", nirvana(NIRVANA_US, "90s US grunge band"))
    t.add_json(f"{MB}/artist/{NIRVANA_UK}", nirvana(NIRVANA_UK, "60s band from the UK"))
    t.add_json("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=Q12003",
               {"entities": {"Q12003": {"sitelinks": {"enwiki": {"title": "Cher"}}}}})
    t.add_json("https://en.wikipedia.org/api/rest_v1/page/summary/Cher", {
        "type": "standard", "title": "Cher", "extract": "Cher is an American singer & actress.",
        "timestamp": "2026-10-01T12:00:00Z",
        "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Cher"}}})
    t.add_json(f"{TADB}/artist-mb.php", {"artists": [{"strArtist": "Cher",
                                                       "strArtistThumb": "https://r2.theaudiodb.com/cher.jpg"}]})

    def artist_pop(method, url, headers, body):
        ids = json.loads(body)["artist_mbids"]
        counts = {CHER: (900000, 25000000), NIRVANA_US: (2000000, 90000000), NIRVANA_UK: (5000, 40000)}
        return 200, [{"artist_mbid": m, "total_user_count": counts.get(m, (0, 0))[0],
                      "total_listen_count": counts.get(m, (0, 0))[1]} for m in ids]

    t.add(f"{LB}/1/popularity/artist", artist_pop, method="POST")
    t.add_json(f"{LABS}/similar-artists/json", [
        {"artist_mbid": "11111111-0000-0000-0000-000000000001", "name": "Madonna", "score": 800},
        {"artist_mbid": "11111111-0000-0000-0000-000000000002", "name": "Kylie Minogue", "score": 400},
    ])
    return t


def install_believe(t):
    """Album + track resolution for Cher - Believe."""
    def rg_search(method, url, headers, body):
        return 200, {"count": 3, "release-groups": [
            {"id": "eeeeeeee-0000-0000-0000-000000000001", "title": "Believe", "primary-type": "Single",
             "score": 100, "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}]},
            {"id": BELIEVE_RG, "title": "Believe", "primary-type": "Album", "score": 100,
             "first-release-date": "1998-10-22",
             "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}]},
            {"id": "eeeeeeee-0000-0000-0000-000000000003", "title": "Believe", "primary-type": "Album",
             "score": 100, "artist-credit": [{"name": "Someone Else", "artist": {"id": "x", "name": "Someone"}}]},
        ]}

    t.add(f"{MB}/release-group", rg_search)
    rg = {"id": BELIEVE_RG, "title": "Believe", "primary-type": "Album", "secondary-types": [],
          "artist-credit": [{"name": "Cher", "joinphrase": "", "artist": {"id": CHER, "name": "Cher"}}],
          "tags": [{"name": "pop", "count": 3}], "genres": [{"name": "dance-pop", "count": 5}],
          "relations": [],
          "releases": [{"id": BELIEVE_REL_LATER, "status": "Official", "date": "2000-01-01", "country": "DE"},
                       {"id": BELIEVE_REL, "status": "Official", "date": "1998-10-22", "country": "GB"},
                       {"id": "ffffffff-0000-0000-0000-00000000b007", "status": "Bootleg", "date": "1990"}]}
    t.add_json(f"{MB}/release-group/{BELIEVE_RG}", rg)
    release = {
        "id": BELIEVE_REL, "title": "Believe", "status": "Official", "date": "1998-10-22",
        "artist-credit": rg["artist-credit"], "release-group": {"id": BELIEVE_RG, "title": "Believe"},
        "media": [{"position": 1, "tracks": [
            {"position": 1, "title": "Believe", "length": 239000,
             "recording": {"id": BELIEVE_REC, "title": "Believe", "length": 239000}},
            {"position": 2, "title": "The Power", "length": 236000,
             "recording": {"id": "c3f6e0b2-0000-0000-0000-000000000003", "title": "The Power"}},
        ]}],
    }
    t.add_json(f"{MB}/release/{BELIEVE_REL}", release)
    t.add_json(f"{CAA}/release-group/{BELIEVE_RG}", {"images": [
        {"front": False, "image": "http://coverartarchive.org/release/x/back.jpg", "thumbnails": {}},
        {"front": True, "image": "http://coverartarchive.org/release/x/1.jpg",
         "thumbnails": {"250": "http://coverartarchive.org/release/x/1-250.jpg",
                        "500": "http://coverartarchive.org/release/x/1-500.jpg",
                        "1200": "http://coverartarchive.org/release/x/1-1200.jpg",
                        "small": "http://coverartarchive.org/release/x/1-250.jpg"}}]})
    t.add_json(f"{LB}/1/popularity/release-group", [{"release_group_mbid": BELIEVE_RG, "total_user_count": 7000,
                                                      "total_listen_count": 99000}], method="POST")

    def rec_search(method, url, headers, body):
        return 200, {"count": 3, "recordings": [
            {"id": BELIEVE_REC_LIVE, "title": "Believe", "score": 100, "releases": [{"id": "r"}],
             "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}]},
            {"id": BELIEVE_REC, "title": "Believe", "score": 100, "releases": [{"id": "r2"}],
             "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}]},
            {"id": "c3f6e0b2-0000-0000-0000-0000000000ff", "title": "Believe", "score": 100, "video": True,
             "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}]},
        ]}

    t.add(f"{MB}/recording", rec_search)

    def rec_pop(method, url, headers, body):
        ids = json.loads(body)["recording_mbids"]
        counts = {BELIEVE_REC: (500000, 3000000), BELIEVE_REC_LIVE: (100, 300)}
        return 200, [{"recording_mbid": m, "total_user_count": counts.get(m, (0, 0))[0],
                      "total_listen_count": counts.get(m, (0, 0))[1]} for m in ids]

    t.add(f"{LB}/1/popularity/recording", rec_pop, method="POST")
    t.add_json(f"{MB}/recording/{BELIEVE_REC}", {
        "id": BELIEVE_REC, "title": "Believe", "length": 239000,
        "artist-credit": [{"name": "Cher", "joinphrase": "", "artist": {"id": CHER, "name": "Cher"}}],
        "tags": [{"name": "dance", "count": 2}], "genres": [{"name": "dance-pop", "count": 4}],
        "releases": [
            {"id": COMPILATION_REL, "title": "Greatest Hits", "status": "Official", "date": "1999",
             "release-group": {"id": "99999999-0000-0000-0000-000000000001", "primary-type": "Album",
                               "secondary-types": ["Compilation"]}},
            {"id": BELIEVE_REL, "title": "Believe", "status": "Official", "date": "1998-10-22",
             "artist-credit": [{"name": "Cher", "artist": {"id": CHER, "name": "Cher"}}],
             "release-group": {"id": BELIEVE_RG, "primary-type": "Album", "secondary-types": []}},
        ],
    })
    return t
