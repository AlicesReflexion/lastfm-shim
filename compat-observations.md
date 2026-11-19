# Last.fm compatibility observations

Read-only probes of `https://ws.audioscrobbler.com/2.0/` on 2026-11-19 used an authorized API key. Each request was sent twice, with `format=json` and with the format omitted. Omitting the format selected XML. These observations describe Last.fm and serve as comparison points for the shim.

| Request | HTTP | Observed contract |
| --- | --- | --- |
| `artist.getInfo`, Cher | 200 | JSON artist fields include name, mbid, url, image, streamable, ontour, stats, similar, tags and bio. XML repeats image six times. |
| `album.getInfo`, Cher / Believe | 200 | Album includes artist, mbid, tags, playcount, image, tracks, url, name, listeners and wiki. XML repeats image six times. In JSON, `tracks.track[].duration` and `tracks.track[].@attr.rank` are **integers**, not strings. |
| `artist.getTopTracks`, Cher, limit 1 | 200 | `toptracks.track` is a one-element **array**. `@attr.rank` is a **string** here. |
| `track.getInfo`, Cher / Believe | 200 | Track includes name, mbid, url, duration, streamable, listeners, playcount, artist, album, toptags and wiki. |
| `artist.search`, Cher, limit 1 | 200 | JSON results include opensearch:Query, opensearch:totalResults, opensearch:startIndex, opensearch:itemsPerPage, artistmatches and @attr. `artistmatches.artist` is a one-element **array**. Python ElementTree rejected the live XML with an unbound prefix. |
| `artist.getSimilar`, Cher, limit 1 | 200 | JSON similarartists contains artist and @attr; `artist` is a one-element **array**. XML similarartists has attribute artist="Cher" and an artist child. |
| `chart.getTopTracks`, limit 1 | 200 | `tracks.track` is a one-element **array**. |
| `user.getRecentTracks`, limit 1 | 200 | `recenttracks.track` is a one-element **array**. |
| `artist.getInfo`, omitted or unknown artist | 200 | JSON has numeric error 6, message "The artist you supplied could not be found" and an empty `links` array. XML lfm status="failed" contains error code="6". |
| Unrecognized method | 400 | JSON is exactly `{"message": "Invalid Method - No method with that name in this package", "error": 3}`: no `links`. XML lfm status="failed" contains error code="3". |
| `artist.getInfo`, no `api_key` (key-free probe) | 400 | JSON is exactly `{"message": "Invalid parameters - Your request is missing a required parameter", "error": 6}`: no `links`. XML has error code="6", also HTTP 400. |
| Unrecognized method, no `api_key` (key-free probe) | 400 | Same error 6 as above: the key is checked before the method name. |
| `artist.getInfo`, invalid `api_key` (key-free probe) | 403 | JSON is exactly `{"message": "Invalid API key - You must be granted a valid key by last.fm", "error": 10}`. |

Successful XML responses in the info and similarity probes had an lfm root with status="ok". Live write methods were not exercised.

The rows marked "key-free probe" were repeated on 2026-11-19 without any API key. The other rows
come from the keyed probes. Only the fields named above were checked for JSON number versus
string; every other value the shim emits stays a string. An unknown `duration` in
`album.getInfo` is emitted as `null`: this was not observed live (all Believe tracks had one).
