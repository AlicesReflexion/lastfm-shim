# Compatibility matrix

What the shim implements of the [Last.fm API 2.0](https://www.last.fm/api), and where it
behaves differently. The shim is **not** Last.fm. It returns Last.fm-shaped responses
built from open data: MusicBrainz, ListenBrainz, Wikipedia/Wikidata, TheAudioDB and the
Cover Art Archive. Your listening history, loves and tags come from local storage. Popularity
numbers (`listeners`, `playcount`) are ListenBrainz figures, not Last.fm figures.

`python -m lfmshim methods` prints the same list from the running code. A test
(`tests/test_integration.py`) fails if a registered method is missing here.

## Status levels

| Status | Meaning |
| --- | --- |
| **full** | The Last.fm parameters and response shape are implemented, and the semantics match. Values come from the source listed. |
| **approximate** | The response shape is implemented, but ranking or meaning differs materially (see notes). |
| **stub** | Returns a valid, empty or minimal response, because no upstream data exists. |
| **unsupported** | Returns error 3 (Invalid Method). |

## Protocol

| Area | Behaviour | Status |
| --- | --- | --- |
| Endpoint | `/2.0/` and `/2.0`, GET query or `application/x-www-form-urlencoded` POST (query and body are merged). | full |
| Method names | Case-insensitive (`artist.getinfo` works). | full |
| Format | XML by default. `format=json` gives JSON. `callback=` gives JSONP (identifier-validated). | full |
| XML envelope | `<lfm status="ok">` / `<lfm status="failed"><error code="N">`. | full (observed) |
| JSON errors | `{"message": …, "error": N, "links": []}`. | full (observed for error 6) |
| HTTP status | Error 6 → **200**, error 3 → **400** (both observed). Others: 403 for auth/session/signature/key, 429 for error 29, 503 for errors 11/16, 500 for error 8 (not yet observed). | partly observed |
| JSON scalars | All values are strings, as on Last.fm. | full |
| One-item lists | Rendered as JSON arrays by default. `api.json_collapse_single = true` renders them as objects (the classic Last.fm quirk). Live behaviour not yet observed. | configurable |
| `opensearch:` XML | The shim declares `xmlns:opensearch`. Live Last.fm does not, so strict parsers reject its XML (observed). | intentional difference |
| `api_key` | Required (error 10 if missing). Any key is accepted unless `api.api_keys` allowlists some. | full |
| `api_sig` | Verified when the client's secret is in `api.clients`. Policies: `verify-known` (default), `strict`, `off`. | full |
| Write methods | Must be POST (error 3 otherwise). Signed, with a valid `sk` (error 9 otherwise). | full |
| Cross-site browser requests | Write and auth calls carrying a foreign `Origin` or `Sec-Fetch-Site: cross-site` are refused (error 4). Unknown `Host` headers get HTTP 421 (DNS-rebinding guard). | shim-specific |
| Images | Six `image` entries (`small`…`mega`, plus an unsized one), as observed in live XML. | full |
| Upstream outage | When MusicBrainz is down: error 16 (temporary error), so clients retry. Enrichment (popularity, similarity, images, wikis, covers) has a 3 s timeout; on failure those fields are empty or 0, and the failure is cached for 5 min. | shim choice |

## Methods

| Method | Status | Source / notes |
| --- | --- | --- |
| `album.addTags` | full | Stored locally only. |
| `album.getInfo` | full | MusicBrainz release group, plus its earliest official release for the tracklist. `mbid` is the release MBID; release or release-group MBIDs are accepted as input. CAA cover, Wikipedia wiki, ListenBrainz release-group stats. |
| `album.getTags` | full | Local personal tags. |
| `album.getTopTags` | full | MusicBrainz release-group tags and genres. The top tag has count 100. |
| `album.removeTag` | full | Local. |
| `album.search` | approximate | MusicBrainz relevance ranking. Covers only when cached. |
| `artist.addTags` | full | Local. |
| `artist.getCorrection` | full | Canonical MusicBrainz name. Returns an empty `corrections` when the input is already canonical. |
| `artist.getInfo` | full | MusicBrainz identity and tags. Bio from Wikipedia via the MusicBrainz wikipedia/wikidata relations (CC BY-SA attribution included). Image from TheAudioDB. `stats` from ListenBrainz. `similar` from ListenBrainz Labs. `ontour` is always 0. `bio.published` is the Wikipedia revision time. |
| `artist.getSimilar` | approximate | ListenBrainz Labs session-based similarity. `match` is the score normalised to the top result. |
| `artist.getTags` | full | Local. |
| `artist.getTopAlbums` | approximate | ListenBrainz top release groups (all-time listens). **Needs `listenbrainz.token`**: this endpoint answers HTTP 401 without one (observed 2026-11-19), and the list is then empty. |
| `artist.getTopTags` | full | MusicBrainz tags and genres. |
| `artist.getTopTracks` | approximate | ListenBrainz top recordings (all-time listens). **Needs `listenbrainz.token`** (HTTP 401 without one); the list is empty otherwise. |
| `artist.removeTag` | full | Local. |
| `artist.search` | approximate | MusicBrainz relevance, not popularity. `listeners` from ListenBrainz. |
| `auth.getMobileSession` | full | Local username and password, PBKDF2-SHA256 (600k iterations), constant-work checks, 5 failures per 5 minutes then error 29. The legacy `authToken` form is unsupported (error 4). |
| `auth.getSession` | full | Exchanges a token approved at `/api/auth/`. Tokens are single-use and expire after 1 h (error 15). Unapproved tokens give error 14. `api.auto_authorize_user` can skip the browser step. |
| `auth.getToken` | full | 32-hex token. |
| `chart.getTopArtists` | approximate | ListenBrainz sitewide stats (`upstream.chart_range`, default `week`). `listeners` is 0. |
| `chart.getTopTags` | stub | No global tag ranking upstream; lists tags used locally. |
| `chart.getTopTracks` | approximate | ListenBrainz sitewide recordings. `listeners` is 0. |
| `geo.getTopArtists` | approximate | No per-country data upstream: returns the global chart. `country` is still required. |
| `geo.getTopTracks` | approximate | Same as above. |
| `library.getArtists` | full | Local scrobbles. |
| `tag.getInfo` | approximate | `reach`/`total` are the count of MusicBrainz artists with the tag. Wiki is empty. |
| `tag.getSimilar` | stub | Always empty. |
| `tag.getTopAlbums` | approximate | MusicBrainz album release groups carrying the tag, by search rank. |
| `tag.getTopArtists` | approximate | MusicBrainz artists carrying the tag, by search rank. |
| `tag.getTopTags` | stub | Tags used locally. |
| `tag.getTopTracks` | approximate | MusicBrainz recordings carrying the tag. |
| `tag.getWeeklyChartList` | approximate | Generated Sunday 12:00 UTC weeks for the last year; there are no tag charts behind them. |
| `track.addTags` | full | Local. |
| `track.getCorrection` | full | Canonical MusicBrainz recording and artist, with `artistcorrected`/`trackcorrected`. |
| `track.getInfo` | full | MusicBrainz recording. Ambiguous titles are resolved by ListenBrainz popularity; videos are excluded. `album` is the canonical official studio release (an `album` parameter is honoured as a hint). `duration` is in ms. `userplaycount`/`userloved` come from local data. |
| `track.getSimilar` | approximate | ListenBrainz Labs similar recordings. `playcount` is 0. |
| `track.getTags` | full | Local. |
| `track.getTopTags` | full | MusicBrainz recording tags and genres. |
| `track.love` | full | Local. Optional ListenBrainz feedback (score 1); the recording MBID is resolved if not given. |
| `track.removeTag` | full | Local. |
| `track.scrobble` | full | Array notation, at most 50 per call. Ignore codes 1 (no artist), 2 (no track), 3 (older than `api.scrobble_max_age`, default 14 days), 4 (more than 10 min in the future). Duplicates are accepted once (safe client retries). `corrected` is always 0. Optional ListenBrainz forwarding: one accepted scrobble is sent as `single`, a batch as one `import`. |
| `track.search` | approximate | MusicBrainz relevance. `artist` narrows results. Images are empty. |
| `track.unlove` | full | Local. Optional ListenBrainz feedback score 0. |
| `track.updateNowPlaying` | full | Local, expires after `duration` (default 10 min), and is cleared when the track is scrobbled. Optional ListenBrainz `playing_now`; dropped rather than retried once stale. |
| `user.getFriends` | stub | No social graph; always empty. |
| `user.getInfo` | full | Local user. `image` is empty. `country` is "None" unless set. |
| `user.getLovedTracks` | full | Local. |
| `user.getPersonalTags` | full | Local. |
| `user.getRecentTracks` | full | Local scrobbles. Now playing comes first on page 1 with `@attr.nowplaying`. Supports `from`, `to` and `extended`. Covers appear once cached; background enrichment fetches them. |
| `user.getTopAlbums` | full | Local. Periods `overall`, `7day`, `1month`, `3month`, `6month`, `12month`. |
| `user.getTopArtists` | full | Local, same periods. |
| `user.getTopTags` | full | Local. |
| `user.getTopTracks` | full | Local, same periods. |
| `user.getWeeklyAlbumChart` | full | Local. The default range is the last complete Sunday-noon week. |
| `user.getWeeklyArtistChart` | full | Local. |
| `user.getWeeklyChartList` | full | Weeks since the first local scrobble. |
| `user.getWeeklyTrackChart` | full | Local. |

### Unsupported

- The legacy Audioscrobbler submissions protocol (`post.audioscrobbler.com`, handshake/1.2). Only API 2.0 scrobbling is supported.
- Undocumented or deprecated methods such as `user.getArtistTracks`, `user.getTrackScrobbles`, `user.getRecentStations`, and anything for radio, events, venues, groups or playlists (most of these were removed from Last.fm itself). These return error 3.
- `multipart/form-data` POST bodies.
- `lang` (bio language). Wikipedia language is set globally with `upstream.wikipedia_lang`.
- Spelling corrections in scrobble responses (`corrected="1"`).
- Real Last.fm accounts and data. The shim never contacts Last.fm.

## Session compatibility for existing clients

Clients that already hold a **real Last.fm session key** can keep it. Set
`api.accept_unknown_sessions = true` and `api.default_user`, and any unrecognised `sk`
is adopted for that local user. The key's hash is stored; the key itself never is.
Only enable this on a single-user loopback install: any local process that can reach the
port can then write as that user.

## Observations used

`compat-observations.md` (read-only live probes, 2026-11-19) set these choices: HTTP 200
for error 6, HTTP 400 for error 3, `links: []` in JSON errors, six images, the
`opensearch:` keys and `@attr.for` on searches, and `similarartists@artist`. Write methods
were not probed live. Their envelopes follow the published documentation.
