<img src="assets/icon.svg" width="64" height="64" alt="">

# Last-FM shim

A local server that speaks the [Last.fm API 2.0](https://www.last.fm/api), so existing
scrobblers and music apps keep working without Last.fm. Metadata comes from open sources:

| Data | Source |
| --- | --- |
| Artist, album and track identity, MBIDs, track tags | MusicBrainz |
| Album and artist tags | Discogs styles and genres (through MusicBrainz links), else TheAudioDB genre, else MusicBrainz |
| Bios and wikis | Wikipedia, through MusicBrainz's Wikipedia/Wikidata links |
| Artist images | TheAudioDB |
| Album covers | Cover Art Archive |
| Popularity, similar artists and tracks, charts | ListenBrainz |
| Your scrobbles, loves, tags and sessions | Local SQLite |
| Scrobble forwarding (opt-in) | ListenBrainz |

From a fresh clone (Python 3.11+, run in the repository folder):

```sh
cp config.example.toml config.toml                # then set upstream.contact in config.toml
python -m lfmshim --config config.toml adduser me # prompts for a password (8+ characters)
python -m lfmshim --config config.toml serve      # http://localhost:9549/2.0/
```

In a second terminal:

```sh
curl "http://localhost:9549/2.0/?method=artist.getinfo&artist=Cher&api_key=x&format=json"
```

`config.toml` is git-ignored. Data goes to `%LOCALAPPDATA%\lfmshim` (Windows) or
`~/.local/share/lfmshim` unless you set `data_dir`. In Windows PowerShell 5.1, `curl` is an
alias for `Invoke-WebRequest`; use `curl.exe` for the output shown by curl. The first lookup of an
artist takes a few seconds, because MusicBrainz allows about one request per second.

- [docs/DEPLOY.md](docs/DEPLOY.md): setup, client sign-in, ListenBrainz forwarding, and HTTPS on a fixed host name.
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): method-by-method support, approximations and unsupported features.
- [config.example.toml](config.example.toml): every setting.

Requires Python 3.11+ and nothing else. Run the tests with `python -m unittest`. They use
fake upstream services and a fake clock, and make no external network requests (some start
a server on 127.0.0.1). The two HTTPS tests (`HttpsFixedHostTests`) are **skipped** unless the
`openssl` command is on `PATH`, because they mint a throwaway certificate with it. Plain Windows
PowerShell usually lacks it, while Git Bash includes it. The run then reports `OK (skipped=2)`.

Created in [T3 Code](https://t3.codes).
