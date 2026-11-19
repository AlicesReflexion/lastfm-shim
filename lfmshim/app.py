"""Application object wiring configuration, storage and upstream services."""

from __future__ import annotations

import time
from typing import Any, Callable

from .config import Config
from .store import Store


class App:
    def __init__(self, config: Config, store: Store | None = None, transport: Any = None,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep):
        self.config = config
        self.clock = clock
        self.sleep = sleep
        if store is None:
            data = config.effective_data_dir()
            store = Store(data / "lfmshim.sqlite3", clock=clock)
        self.store = store
        self.transport = transport
        self.web_auth = None
        self._init_services()

    def _init_services(self) -> None:
        from . import api  # noqa: F401  (registers methods)
        from .metadata import Resolver
        from .upstream.http import HttpClient
        from .upstream.listenbrainz import ListenBrainz
        from .upstream.musicbrainz import MusicBrainz
        from .upstream.sources import CoverArtArchive, TheAudioDB, Wikipedia

        up = self.config.upstream
        self.http = HttpClient(self)
        self.mb = MusicBrainz(self.http, up.musicbrainz_url)
        self.lb = ListenBrainz(self.http, self.config.listenbrainz)
        self.wiki = Wikipedia(self.http, up.wikipedia_lang, up.wikipedia_url, up.wikidata_url)
        self.tadb = TheAudioDB(self.http, up.theaudiodb_url, up.theaudiodb_key, up.theaudiodb_enabled)
        self.caa = CoverArtArchive(self.http, up.coverart_url)
        self.resolver = Resolver(self)

    def now(self) -> int:
        return int(self.clock())

    def close(self) -> None:
        self.store.close()
