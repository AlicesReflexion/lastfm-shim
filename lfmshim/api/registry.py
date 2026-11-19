"""Method registry and per-request context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import INVALID_PARAMS, LastFmError, missing


@dataclass(frozen=True)
class MethodSpec:
    name: str  # canonical name, e.g. "artist.getInfo"
    func: Callable[["Request"], Any]
    # "none": anonymous; "session": requires sk + signature; "signed": requires
    # a signature but no session (auth.getToken/getSession/getMobileSession).
    auth: str = "none"
    write: bool = False
    # Support level for the compatibility matrix: full | approximate | stub.
    support: str = "full"
    notes: str = ""


REGISTRY: dict[str, MethodSpec] = {}


def method(name: str, *, auth: str = "none", write: bool = False, support: str = "full", notes: str = ""):
    def deco(func):
        REGISTRY[name.lower()] = MethodSpec(name, func, auth, write, support, notes)
        return func

    return deco


def lookup(name: str) -> MethodSpec | None:
    return REGISTRY.get(name.lower())


@dataclass
class Request:
    params: dict[str, str]
    app: Any  # lfmshim.app.App
    http_method: str = "GET"
    user: str | None = None  # authenticated local username, when sk was valid
    session_key: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: str | None = None) -> str | None:
        value = self.params.get(name)
        if value is None or value == "":
            return default
        return value

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise missing(name)
        return value

    def int(self, name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        raw = self.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise LastFmError(INVALID_PARAMS, f"Invalid parameters - {name} must be an integer") from None
        if minimum is not None and value < minimum:
            value = minimum if name != "page" else max(value, minimum)
        if maximum is not None and value > maximum:
            value = maximum
        return value

    def bool(self, name: str, default: bool = False) -> bool:
        raw = self.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in ("1", "true", "yes")

    def paging(self, default_limit: int = 50, max_limit: int = 1000) -> tuple[int, int]:
        page = self.int("page", 1, minimum=1)
        limit = self.int("limit", default_limit, minimum=1, maximum=max_limit)
        return page, limit

    def indexed(self, name: str) -> dict[int, str]:
        """Collect array-notation params such as artist[0], artist[1]."""
        out: dict[int, str] = {}
        prefix = name + "["
        for key, value in self.params.items():
            if key.startswith(prefix) and key.endswith("]"):
                idx = key[len(prefix):-1]
                if idx.isdigit():
                    out[int(idx)] = value
        return out
