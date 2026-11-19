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
        """Hook for upstream services (filled in by later modules)."""
        from . import api  # noqa: F401  (registers methods)

    def now(self) -> int:
        return int(self.clock())

    def close(self) -> None:
        self.store.close()
