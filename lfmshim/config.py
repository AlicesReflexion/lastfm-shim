"""Configuration: defaults < TOML file < environment variables.

Secrets (ListenBrainz token, client secrets) may be supplied via files or
environment variables and are never included in ``repr``/logs.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_PORT = 9549


class ConfigError(ValueError):
    pass


def _secret_repr(value: str | None) -> str:
    return "<unset>" if not value else "<redacted>"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    # Base URL clients and browsers use to reach the shim; used for auth URLs.
    public_url: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    # Extra Host header values accepted (DNS-rebinding protection).
    allowed_hosts: list[str] = field(default_factory=list)
    allow_remote: bool = False
    max_body_bytes: int = 1_000_000

    @property
    def tls(self) -> bool:
        return bool(self.tls_cert and self.tls_key)

    def effective_public_url(self) -> str:
        if self.public_url:
            return self.public_url.rstrip("/")
        scheme = "https" if self.tls else "http"
        host = self.host if self.host not in ("0.0.0.0", "::", "127.0.0.1", "::1") else "localhost"
        default_port = 443 if self.tls else 80
        port = "" if self.port == default_port else f":{self.port}"
        return f"{scheme}://{host}{port}"


@dataclass
class ApiConfig:
    # If non-empty only these api_keys are accepted (error 10 otherwise).
    api_keys: list[str] = field(default_factory=list)
    # api_key -> shared secret, used to verify api_sig.
    clients: dict[str, str] = field(default_factory=dict, repr=False)
    # "verify-known": verify when the secret is known, otherwise accept.
    # "strict": reject signed calls from unknown api_keys. "off": never verify.
    signature_policy: str = "verify-known"
    # Accept session keys this shim never issued (e.g. a client still holding a
    # real Last.fm session) and map them to default_user.
    accept_unknown_sessions: bool = False
    default_user: str = ""
    # Render one-element lists as an object in JSON (classic Last.fm quirk).
    json_collapse_single: bool = False
    # Reject scrobbles older than this many seconds (Last.fm: 14 days). 0 = off.
    scrobble_max_age: int = 14 * 24 * 3600
    scrobble_max_future: int = 10 * 60
    # Let auth.getSession approve pending tokens for this local user without the
    # browser step (for clients that hardcode the www.last.fm auth page).
    auto_authorize_user: str = ""
    # Write methods must be POSTed (Last.fm requirement; also CSRF protection).
    require_post_for_writes: bool = True
    # Cross-site Origin values allowed to call write methods (normally none).
    allowed_origins: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # never print client secrets
        return (
            f"ApiConfig(api_keys={len(self.api_keys)} keys, clients={len(self.clients)} secrets, "
            f"signature_policy={self.signature_policy!r}, accept_unknown_sessions={self.accept_unknown_sessions}, "
            f"default_user={self.default_user!r})"
        )


@dataclass
class UpstreamConfig:
    # Contact info appended to the User-Agent, as MusicBrainz asks.
    contact: str = ""
    offline: bool = False
    timeout: float = 15.0
    # Enrichment (popularity, similarity, images, wikis, covers): short timeout,
    # failures cached for failure_cache_ttl seconds.
    optional_timeout: float = 3.0
    failure_cache_ttl: int = 300
    musicbrainz_url: str = "https://musicbrainz.org/ws/2"
    # MusicBrainz allows 1 request/s on average; keep a margin (503s seen at exactly 1/s).
    musicbrainz_rate: float = 0.9
    coverart_url: str = "https://coverartarchive.org"
    wikipedia_lang: str = "en"
    wikipedia_url: str = ""  # default https://{lang}.wikipedia.org
    wikidata_url: str = "https://www.wikidata.org"
    theaudiodb_url: str = "https://www.theaudiodb.com/api/v1/json"
    theaudiodb_key: str = field(default="123", repr=False)
    theaudiodb_enabled: bool = True
    cache_ttl: int = 7 * 24 * 3600
    negative_cache_ttl: int = 24 * 3600
    popularity_cache_ttl: int = 24 * 3600
    # Resolve MBIDs/cover art for new scrobbles in the background (reads only).
    enrich_scrobbles: bool = True
    # ListenBrainz sitewide stats range used for chart.* and geo.*.
    chart_range: str = "week"


@dataclass
class ListenBrainzConfig:
    api_url: str = "https://api.listenbrainz.org"
    labs_url: str = "https://labs.api.listenbrainz.org"
    token: str = field(default="", repr=False)
    token_file: str = ""
    # Opt-in: forward scrobbles / now playing to ListenBrainz.
    scrobble: bool = False
    # Opt-in: forward love/unlove as recording feedback (needs a recording MBID).
    feedback: bool = False
    similar_artists_algorithm: str = (
        "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
    )
    similar_recordings_algorithm: str = (
        "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
    )
    retry_interval: int = 60
    # Only forward this local user's activity (empty = every local user).
    local_user: str = ""

    def resolved_token(self) -> str:
        if self.token:
            return self.token.strip()
        if self.token_file:
            try:
                return Path(self.token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"cannot read listenbrainz.token_file: {exc.strerror}") from None
        return ""

    def __repr__(self) -> str:
        return (
            f"ListenBrainzConfig(api_url={self.api_url!r}, token={_secret_repr(self.token or self.token_file)}, "
            f"scrobble={self.scrobble}, feedback={self.feedback})"
        )


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)
    listenbrainz: ListenBrainzConfig = field(default_factory=ListenBrainzConfig)
    data_dir: str = ""
    log_level: str = "INFO"

    def effective_data_dir(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir)
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        return Path(base) / "lfmshim"

    def validate(self) -> None:
        s = self.server
        if not (0 <= s.port < 65536):
            raise ConfigError("server.port must be 0-65535 (0 = ephemeral)")
        if bool(s.tls_cert) != bool(s.tls_key):
            raise ConfigError("server.tls_cert and server.tls_key must be set together")
        if not s.allow_remote and s.host not in ("127.0.0.1", "::1", "localhost"):
            raise ConfigError(
                f"server.host={s.host!r} is not a loopback address; set server.allow_remote = true to permit this"
            )
        if self.api.signature_policy not in ("verify-known", "strict", "off"):
            raise ConfigError("api.signature_policy must be verify-known, strict or off")
        if self.api.accept_unknown_sessions and not self.api.default_user:
            raise ConfigError("api.accept_unknown_sessions requires api.default_user")
        if (self.listenbrainz.scrobble or self.listenbrainz.feedback) and not (
            self.listenbrainz.token or self.listenbrainz.token_file
        ):
            raise ConfigError("listenbrainz.scrobble/feedback require listenbrainz.token or token_file")


_SECTIONS = {
    "server": ServerConfig,
    "api": ApiConfig,
    "upstream": UpstreamConfig,
    "listenbrainz": ListenBrainzConfig,
}

# Environment overrides: LFMSHIM_<SECTION>_<FIELD>, e.g. LFMSHIM_SERVER_PORT.
_TOP_LEVEL = {"data_dir", "log_level"}


def _coerce(value: str, current: Any) -> Any:
    if isinstance(current, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, list):
        return [v.strip() for v in value.split(",") if v.strip()]
    return value


def _apply(section_obj: Any, data: dict[str, Any], section: str) -> None:
    names = {f.name for f in dataclasses.fields(section_obj)}
    for key, value in data.items():
        if key not in names:
            raise ConfigError(f"unknown config key {section}.{key}")
        setattr(section_obj, key, value)


def load_config(path: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    cfg = Config()
    path = path or env.get("LFMSHIM_CONFIG")
    if path:
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from None
        for key, value in data.items():
            if key in _SECTIONS:
                if not isinstance(value, dict):
                    raise ConfigError(f"[{key}] must be a table")
                _apply(getattr(cfg, key), value, key)
            elif key in _TOP_LEVEL:
                setattr(cfg, key, value)
            else:
                raise ConfigError(f"unknown config key {key}")
    for name, value in env.items():
        if not name.startswith("LFMSHIM_") or name == "LFMSHIM_CONFIG":
            continue
        rest = name[len("LFMSHIM_"):].lower()
        if rest in _TOP_LEVEL:
            setattr(cfg, rest, value)
            continue
        section, _, key = rest.partition("_")
        if section in _SECTIONS and key:
            obj = getattr(cfg, section)
            if hasattr(obj, key) and key != "clients":
                setattr(obj, key, _coerce(value, getattr(obj, key)))
    cfg.validate()
    return cfg
