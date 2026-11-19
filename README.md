<img src="assets/icon.svg" width="64" height="64" alt="">

# Last-FM shim

A local server that speaks the [Last.fm API 2.0](https://www.last.fm/api), so existing
scrobblers and music apps keep working without Last.fm. Metadata comes from open sources:

| Data | Source |
| --- | --- |
| Artist, album and track identity, tags, MBIDs | MusicBrainz |
| Bios and wikis | Wikipedia, through MusicBrainz's Wikipedia/Wikidata links |
| Artist images | TheAudioDB |
| Album covers | Cover Art Archive |
| Popularity, similar artists and tracks, charts | ListenBrainz |
| Your scrobbles, loves, tags and sessions | Local SQLite |
| Scrobble forwarding (opt-in) | ListenBrainz |

```sh
python -m lfmshim adduser me
python -m lfmshim --config config.toml serve     # http://localhost:9549/2.0/
curl "http://localhost:9549/2.0/?method=artist.getinfo&artist=Cher&api_key=x&format=json"
```

- [docs/DEPLOY.md](docs/DEPLOY.md): setup, client sign-in, ListenBrainz forwarding, and HTTPS on a fixed host name.
- [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md): method-by-method support, approximations and unsupported features.
- [config.example.toml](config.example.toml): every setting.

Requires Python 3.11+ and nothing else. Run the tests with `python -m unittest`. They use
fake upstream services and a fake clock, and make no network requests.

Created in [T3 Code](https://t3.codes).
