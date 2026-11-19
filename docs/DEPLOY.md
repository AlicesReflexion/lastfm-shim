# Deploying lfmshim

lfmshim needs only Python 3.11 or newer (standard library only, no packages to install).
Its state is one SQLite file in the data directory.

## 1. Local loopback (recommended)

```sh
python -m lfmshim adduser me              # prompts for a password (min. 8 chars)
python -m lfmshim --config config.toml serve
# lfmshim 0.1.0 listening on http://localhost:9549/2.0/
```

Set `upstream.contact` to an email address or URL. MusicBrainz asks every client to identify
itself in the User-Agent.

Then point the client's API base URL at `http://localhost:9549/2.0/`. Many scrobblers have a
"custom Last.fm/Libre.fm server" setting; set its auth URL to `http://localhost:9549/api/auth/`.

### Signing in from a client

| Client flow | What to do |
| --- | --- |
| Desktop auth (`auth.getToken`, browser, `auth.getSession`) | When the client opens `/api/auth/?api_key=…&token=…`, sign in with your **local** shim account. If the client hardcodes `www.last.fm/api/auth`, set `api.auto_authorize_user = "me"`; `auth.getSession` then approves tokens itself. |
| Web auth (`/api/auth/?api_key=…&cb=…`) | Approve on the page. You are redirected to `cb?token=…`. |
| Mobile auth (`auth.getMobileSession`) | Use your local username and password. |
| Client already holding a real Last.fm session key | Set `api.accept_unknown_sessions = true` and `api.default_user = "me"`. Single-user loopback installs only. |

If you know the client's API secret, add it to `api.clients` so signatures are verified. With
`signature_policy = "verify-known"`, a signature from an unknown key is required but cannot
be checked. Sessions are what really authorise writes.

### Forwarding to ListenBrainz (optional)

1. Copy your user token from https://listenbrainz.org/settings/ into a file only you can read.
2. Set `listenbrainz.scrobble = true` (and `feedback = true` for loves), plus
   `listenbrainz.token_file = "…"` or the environment variable `LFMSHIM_LISTENBRAINZ_TOKEN`.
3. If several local users share the install, set `listenbrainz.local_user`.

Scrobbles are always committed locally first. Delivery happens in the background, with
retries recorded in the `outbox` table. ListenBrainz downtime never fails a client's
scrobble.

## 2. HTTPS on a fixed host name

Some clients hardcode `ws.audioscrobbler.com` (or `https://…`) and cannot be pointed
elsewhere. lfmshim can serve that name over TLS:

```toml
[server]
host = "127.0.0.1"
port = 443
tls_cert = "certs/ws.audioscrobbler.com.pem"
tls_key  = "certs/ws.audioscrobbler.com-key.pem"
public_url = "https://ws.audioscrobbler.com"
```

The shim **does not** change your system for you. Diverting the name takes two steps,
which you perform yourself, and both have system-wide effects:

1. **Name resolution.** Map the host to loopback, for example in
   `C:\Windows\System32\drivers\etc\hosts` or `/etc/hosts`:
   `127.0.0.1 ws.audioscrobbler.com`. Every program on the machine then stops reaching
   real Last.fm under that name. Revert it by removing the line.
2. **Certificate trust.** The client must trust a certificate for that name. Issue one from
   a private CA you control (for example `mkcert ws.audioscrobbler.com`) and trust that CA
   only where needed. Prefer the client's own trust setting over the OS store. Protect the
   CA key: anyone holding it can impersonate any site to that machine.

Binding port 443 may need elevated rights. Alternatively, keep the shim on 9549 and use a
local reverse proxy you already run.

`tests/test_integration.py::HttpsFixedHostTests` exercises this mode with a throwaway
certificate that only the test trusts.

## 3. Operations

| Task | Command |
| --- | --- |
| List users | `python -m lfmshim users` |
| Set a password | `python -m lfmshim adduser me` (existing users keep their data) |
| Revoke all sessions | `python -m lfmshim revoke me` |
| List methods | `python -m lfmshim methods` |
| Back up | Copy `lfmshim.sqlite3` while the server is stopped, or use `sqlite3 .backup`. |

What lfmshim logs: method names, error codes, and upstream failures. It never logs
passwords, session keys, tokens, `api_sig` values or the ListenBrainz token. Session keys and
tokens are stored as SHA-256 digests; passwords as salted PBKDF2.

### Upstream etiquette

| Service | Limit enforced by the shim |
| --- | --- |
| MusicBrainz | 0.9 requests/s (policy: 1/s average), honours 503 + `Retry-After` |
| TheAudioDB | 0.5 requests/s (free key: 30/min) |
| ListenBrainz | 2 requests/s, honours 429 + `X-RateLimit-Reset-In` |
| Cover Art Archive, Wikipedia, Wikidata | 5 requests/s |

Successful responses are cached for 7 days (popularity for 1 day) and 404s for 1 day, in
SQLite. In a live check (2026-11-19), a cold `artist.getInfo` made about 8 upstream requests and
took about 3.5 s. A cold `track.getInfo` took 4–10 s, because MusicBrainz pacing serialises the
artist search, recording search(es) and lookup. Cached responses are immediate, and background
enrichment warms the cache for everything you scrobble. Enrichment sources (ListenBrainz popularity
and similarity, TheAudioDB, Wikipedia, CAA) get a 3 s timeout (`upstream.optional_timeout`). If one
is slow or down, its fields come back empty for `upstream.failure_cache_ttl` seconds.

The ListenBrainz token, if configured, is also sent on the read endpoints that now require
it (`artist.getTopTracks`/`getTopAlbums`).
