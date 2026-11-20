# Compatibility matrix

What the shim implements of the [Last.fm API 2.0](https://www.last.fm/api), and where it
behaves differently. The shim is **not** Last.fm. It returns Last.fm-shaped responses
built from open data: MusicBrainz, ListenBrainz, Wikipedia/Wikidata, TheAudioDB, Discogs
and the Cover Art Archive. Your listening history, loves and tags come from local storage. Popularity
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
| JSON errors | `{"message": …, "error": N}`. Error 6 raised by a method (HTTP 200) also carries `"links": []`; HTTP 400/403 errors do not. | full (observed for errors 3, 6, 10) |
| HTTP status | Error 6 from a method → **200**. Missing `api_key` → error **6** with **400** (checked before the method name). Error 3 → **400**. Error 10 → **403**. All observed. Others (not yet observed): 403 for auth/session/signature, 429 for error 29, 503 for errors 11/16, 500 for error 8. | partly observed |
| JSON scalars | Strings, as on Last.fm, except where Last.fm was seen to send numbers: `album.getInfo` `tracks.track[].duration` and `@attr.rank` are JSON integers (an unknown duration is `null`). Ranks elsewhere, such as `artist.getTopTracks`, stay strings. Other fields were not checked. | partly observed |
| One-item lists | Rendered as JSON arrays by default. This matches live Last.fm for `artist.search`, `artist.getSimilar`, `artist.getTopTracks`, `chart.getTopTracks` and `user.getRecentTracks` with `limit=1` (observed). `api.json_collapse_single = true` renders them as objects instead, for old clients written against that quirk. | full (observed) |
| `opensearch:` XML | The shim declares `xmlns:opensearch`. Live Last.fm does not, so strict parsers reject its XML (observed). | intentional difference |
| `api_key` | Required (error 6, HTTP 400 if missing, as observed). Any key is accepted unless `api.api_keys` allowlists some; others get error 10. | full |
| `api_sig` | Verified when the client's secret is in `api.clients`. Policies: `verify-known` (default), `strict`, `off`. | full |
| Write methods | Must be POST (error 3 otherwise). Signed, with a valid `sk` (error 9 otherwise). | full |
| Cross-site browser requests | Write and auth calls carrying a foreign `Origin` or `Sec-Fetch-Site: cross-site` are refused (error 4). Unknown `Host` headers get HTTP 421 (DNS-rebinding guard). | shim-specific |
| `tagsources=1` | Shim extension, off by default. On `album.getInfo`, `album.getTopTags`, `artist.getInfo` and `artist.getTopTags`, adds `source` elements to the tag list for attribution (see [Tag sources](#tag-sources-and-attribution)). Without it, responses keep the standard shape. | shim-specific |
| Images | Six `image` entries (`small`…`mega`, plus an unsized one), as observed in live XML. | full |
| Upstream outage | When MusicBrainz is down: error 16 (temporary error), so clients retry. Enrichment (popularity, similarity, images, wikis, covers) has a 3 s timeout; on failure those fields are empty or 0, and the failure is cached for 5 min. | shim choice |

## Methods

| Method | Status | Source / notes |
| --- | --- | --- |
| `album.addTags` | full | Stored locally only. |
| `album.getInfo` | full | MusicBrainz release group, plus its earliest official release for the tracklist. `mbid` is the release MBID; release or release-group MBIDs are accepted as input. CAA cover, Wikipedia wiki, ListenBrainz release-group stats. `tags` as in `album.getTopTags`. |
| `album.getTags` | full | Local personal tags. |
| `album.getTopTags` | approximate | Discogs styles, then genres, of the master linked from the MusicBrainz release group (else of the release linked from the chosen MB release). Else TheAudioDB's album genre, else MusicBrainz release-group tags and genres. Discogs tags are unranked, so each has count 100. See [Tag sources](#tag-sources-and-attribution). |
| `album.removeTag` | full | Local. |
| `album.search` | approximate | MusicBrainz relevance ranking. Covers only when cached. |
| `artist.addTags` | full | Local. |
| `artist.getCorrection` | full | Canonical MusicBrainz name. Returns an empty `corrections` when the input is already canonical. |
| `artist.getInfo` | full | MusicBrainz identity and tags. Bio from Wikipedia via the MusicBrainz wikipedia/wikidata relations (CC BY-SA attribution included). Image from TheAudioDB. `stats` from ListenBrainz. `similar` from ListenBrainz Labs. `ontour` is always 0. `bio.published` is the Wikipedia revision time. Similar artists come without images (see `artist.getSimilar`). `tags` as in `artist.getTopTags`. |
| `artist.getSimilar` | approximate | ListenBrainz Labs session-based similarity. `match` is the score normalised to the top result (`"1"` for the first). Images appear only for artists whose TheAudioDB image is already cached (for example from an earlier `artist.getInfo`); fetching them would cost about 2 s per artist at TheAudioDB's free-key rate, so the list is usually imageless. |
| `artist.getTags` | full | Local. |
| `artist.getTopAlbums` | approximate | ListenBrainz top release groups (all-time listens). **Needs `listenbrainz.token`**: this endpoint answers HTTP 401 without one (observed 2026-11-19), and the list is then empty. |
| `artist.getTopTags` | approximate | Discogs styles, then genres, each counted once per distinct album over the artist's most-listened studio albums (up to `upstream.discogs_artist_albums`, default 10). Else TheAudioDB's artist genre, else MusicBrainz tags and genres. See [Tag sources](#tag-sources-and-attribution). |
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
| `track.scrobble` | full | Array notation, at most 50 per call. Ignore codes 1 (no artist), 2 (no track), 3 (older than `api.scrobble_max_age`, default 14 days), 4 (more than 10 min in the future). Duplicates are accepted once (safe client retries). Each call, batch included, is stored together with its forwarding in one transaction. `corrected` is always 0. Optional ListenBrainz forwarding: one accepted scrobble is sent as `single`, a batch as one `import`. |
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

## Tag sources and attribution

Album and artist tags (`album.getInfo`, `album.getTopTags`, `artist.getInfo`,
`artist.getTopTags`) come from **one** source per list, the first that has data:

| Source | Albums | Artists |
| --- | --- | --- |
| 1. Discogs | The master linked from the MusicBrainz release group, else the release linked from the chosen MB release. Styles first, then genres, in Discogs order. Every count is 100, because Discogs does not rank them. | Styles, then genres, of the artist's studio albums: MB release groups with a Discogs link, most ListenBrainz listeners first, up to `upstream.discogs_artist_albums`. A tag counts once per distinct album, and pressings of one master count once. Counts are scaled so the most common tag is 100. Genres follow styles, so counts are not always descending (`alternative rock` 78 can precede `rock` 100). |
| 2. TheAudioDB | Album `strGenre`. | Artist `strGenre`, from the same lookup as the artist image. |
| 3. MusicBrainz | Release-group tags and genres, as before. | Artist tags and genres, as before. |

The shim reaches Discogs only through links curated on MusicBrainz, never by a name search,
and reads it without a token. Without a token Discogs allows 25 requests per minute, so one
API call makes at most `upstream.discogs_request_budget` (default 2) uncached Discogs lookups.
The rest are queued once for the background worker. Until the worker has fetched them, an
artist's tags are counted over the albums cached so far, or come from the fallback sources.
Repeated calls can therefore show more tags during the first minute or so. Cached lookups last
`upstream.cache_ttl` (7 days). Track tags (`track.getInfo`, `track.getTopTags`) are still
MusicBrainz tags. Personal tags (`*.getTags`, `*.addTags`, `*.removeTag`,
`user.getPersonalTags`) are local and unaffected.

Every tag's `url` is still its Last.fm-style tag page. To show where the tags came from, add
`tagsources=1` to the request. The tag list then also carries one `source` element per page
that supplied tags:

```json
"toptags": {
  "tag": [{"count": "100", "name": "alternative rock", "url": "https://www.last.fm/tag/alternative+rock"}],
  "source": [{"name": "Discogs", "provider": "discogs",
              "url": "https://www.discogs.com/master/3334-Coldplay-Parachutes"}],
  "@attr": {"artist": "Coldplay", "album": "Parachutes"}
}
```

In XML this is `<source><name>Discogs</name><provider>discogs</provider><url>…</url></source>`
inside `<toptags>` or `<tags>`. `provider` is `discogs`, `theaudiodb` or `musicbrainz`. For
an artist the list starts with the Discogs artist page (when MusicBrainz links one), followed
by each album master that contributed. TheAudioDB sources link to `theaudiodb.com/artist/{id}`
or `/album/{id}`, and MusicBrainz sources to the artist or release-group page. Clients that do
not send the parameter never see `source`.

## Session compatibility for existing clients

Clients that already hold a **real Last.fm session key** can keep it. Set
`api.accept_unknown_sessions = true` and `api.default_user`, and any unrecognised `sk`
is adopted for that local user. The key's hash is stored; the key itself never is.
Only enable this on a single-user loopback install: any local process that can reach the
port can then write as that user.

## Live smoke test

On 2026-11-19 the shim ran against the real key-free upstream services (MusicBrainz,
ListenBrainz and ListenBrainz Labs without a token, TheAudioDB's public key `123`, Wikipedia,
Cover Art Archive), with read-only requests only:

| Check | Result |
| --- | --- |
| `artist.getInfo` Cher, Radiohead | MBID, TheAudioDB image, Wikipedia bio, ListenBrainz `stats` and 5 `similar` names all present. |
| `artist.getSimilar` Cher, Radiohead | Works: Madonna, Britney Spears, Michael Jackson… for Cher; Nirvana, Red Hot Chili Peppers, Muse for Radiohead. |
| Similar-artist images | **Gap**: empty in both methods, because they read only cached images. |
| Top `match` | Was `"1."`; fixed to `"1"`. |
| `artist.search` Cher, limit 1 | One-element `artistmatches.artist` array, as live Last.fm. |
| MusicBrainz pacing | Several HTTP 503 responses at 0.9 requests/s, each retried successfully after the backoff. Cold lookups are slow, not failing. |

A second read-only check the same day covered the Discogs tag sources, without a Discogs token:

| Check | Result |
| --- | --- |
| `album.getTopTags` Coldplay / Parachutes | Discogs master 3334: `alternative rock`, `pop rock`, `rock`, with its source URL. 5.9 s cold. |
| `album.getInfo` Cher / Believe | Tags from Discogs master 69294 (`dance-pop`, `downtempo`, `techno`, …). |
| `artist.getTopTags` Radiohead | Cold: 2 albums counted, 6.3 s. The background pass then fetched the queued lookups in 29.8 s. Afterwards: 9 albums, `alternative rock` 78, `experimental` 56, …, `rock` 100, `electronic` 78, answered from the cache. |
| Cold `artist.getInfo` Kylie Minogue | 7.6 s with Discogs (tags `synth-pop`, `house`, `disco`, …). 4.0 s with `discogs_enabled = false` (tags: `pop`). |

`artist.getTopTracks` and `artist.getTopAlbums` were not re-checked: they need a ListenBrainz
token (see their rows above).

## Observations used

`compat-observations.md` (read-only live probes, 2026-11-19) set these choices: HTTP 200
for method-level error 6 with `links: []`, HTTP 400 without `links` for error 3 and for a
missing `api_key` (error 6), HTTP 403 for error 10, integer `duration`/`rank` in
`album.getInfo` tracks, one-element arrays for single results, six images, the
`opensearch:` keys and `@attr.for` on searches, and `similarartists@artist`. Write methods
were not probed live. Their envelopes follow the published documentation.
