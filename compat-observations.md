# Last.fm compatibility observations

Read-only probes of `https://ws.audioscrobbler.com/2.0/` on 2026-11-19 used an authorized API key. Each request was sent twice, with `format=json` and with the format omitted. Omitting the format selected XML. These observations describe Last.fm and serve as comparison points for the shim.

| Request | HTTP | Observed contract |
| --- | --- | --- |
| `artist.getInfo`, Cher | 200 | JSON artist fields include name, mbid, url, image, streamable, ontour, stats, similar, tags and bio. XML repeats image six times. |
| `album.getInfo`, Cher / Believe | 200 | Album includes artist, mbid, tags, playcount, image, tracks, url, name, listeners and wiki. XML repeats image six times. |
| `track.getInfo`, Cher / Believe | 200 | Track includes name, mbid, url, duration, streamable, listeners, playcount, artist, album, toptags and wiki. |
| `artist.search`, Cher, limit 1 | 200 | JSON results include opensearch:Query, opensearch:totalResults, opensearch:startIndex, opensearch:itemsPerPage, artistmatches and @attr. Python ElementTree rejected the live XML with an unbound prefix. |
| `artist.getSimilar`, Cher, limit 1 | 200 | JSON similarartists contains artist and @attr. XML similarartists has attribute artist="Cher" and an artist child. |
| `artist.getInfo`, omitted or unknown artist | 200 | JSON has numeric error 6, message "The artist you supplied could not be found" and an empty links array. XML lfm status="failed" contains error code="6". |
| Unrecognized method | 400 | JSON has numeric error 3 and message "Invalid Method - No method with that name in this package". XML lfm status="failed" contains error code="3". |

Successful XML responses in the info and similarity probes had an lfm root with status="ok". Live write methods were not exercised.
