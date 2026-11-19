"""Persistent local state in SQLite: users, sessions, auth tokens, scrobbles,
now playing, loves, personal tags, the ListenBrainz outbox and the HTTP cache.

Secrets are stored only as salted hashes (passwords) or SHA-256 digests
(session keys, tokens), so a leaked database does not leak usable credentials.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    password_hash TEXT,
    realname TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    registered INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    key_hash TEXT PRIMARY KEY,
    user TEXT NOT NULL COLLATE NOCASE,
    api_key TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    adopted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash TEXT PRIMARY KEY,
    api_key TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL,
    user TEXT COLLATE NOCASE,
    csrf TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scrobbles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL COLLATE NOCASE,
    ts INTEGER NOT NULL,
    artist TEXT NOT NULL,
    track TEXT NOT NULL,
    album TEXT NOT NULL DEFAULT '',
    album_artist TEXT NOT NULL DEFAULT '',
    track_number TEXT NOT NULL DEFAULT '',
    duration INTEGER,
    track_mbid TEXT NOT NULL DEFAULT '',
    artist_mbid TEXT NOT NULL DEFAULT '',
    album_mbid TEXT NOT NULL DEFAULT '',
    UNIQUE(user, ts, artist, track)
);
CREATE INDEX IF NOT EXISTS scrobbles_user_ts ON scrobbles(user, ts DESC);
CREATE TABLE IF NOT EXISTS nowplaying (
    user TEXT PRIMARY KEY COLLATE NOCASE,
    started INTEGER NOT NULL,
    expires INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loves (
    user TEXT NOT NULL COLLATE NOCASE,
    artist TEXT NOT NULL COLLATE NOCASE,
    track TEXT NOT NULL COLLATE NOCASE,
    ts INTEGER NOT NULL,
    track_mbid TEXT NOT NULL DEFAULT '',
    artist_mbid TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(user, artist, track)
);
CREATE TABLE IF NOT EXISTS user_tags (
    user TEXT NOT NULL COLLATE NOCASE,
    kind TEXT NOT NULL,
    artist TEXT NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL COLLATE NOCASE,
    tag TEXT NOT NULL COLLATE NOCASE,
    ts INTEGER NOT NULL,
    PRIMARY KEY(user, kind, artist, name, tag)
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    expires INTEGER NOT NULL,
    value TEXT NOT NULL
);
"""

PBKDF2_ITERATIONS = 600_000


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_password(password: str, *, iterations: int | None = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


_DUMMY: dict[int, str] = {}


def _dummy_hash() -> str:
    if PBKDF2_ITERATIONS not in _DUMMY:
        _DUMMY[PBKDF2_ITERATIONS] = hash_password(secrets.token_hex(8))
    return _DUMMY[PBKDF2_ITERATIONS]


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iterations, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(dk.hex(), dk_hex)


class Store:
    def __init__(self, path: str | os.PathLike, clock: Callable[[], float] = time.time):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    def now(self) -> int:
        return int(self.clock())

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __del__(self) -> None:
        try:
            self._db.close()
        except Exception:
            pass

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, tuple(args))

    def _all(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchall()

    def _one(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, tuple(args)).fetchone()

    # ---- users -----------------------------------------------------------
    def add_user(self, name: str, password: str | None = None, realname: str = "", country: str = "") -> None:
        pw = hash_password(password) if password else None
        self._exec(
            "INSERT INTO users(name, password_hash, realname, country, registered) VALUES(?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET password_hash=COALESCE(excluded.password_hash, users.password_hash)",
            (name, pw, realname, country, self.now()),
        )

    def set_password(self, name: str, password: str) -> None:
        self._exec("UPDATE users SET password_hash=? WHERE name=?", (hash_password(password), name))

    def get_user(self, name: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM users WHERE name=?", (name,))

    def list_users(self) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM users ORDER BY name")

    def check_password(self, name: str, password: str) -> str | None:
        """Return the canonical username if the password matches."""
        row = self.get_user(name)
        if row is None or not row["password_hash"]:
            # Spend the same PBKDF2 work as a real check so response timing
            # does not reveal which usernames exist.
            verify_password(password, _dummy_hash())
            return None
        return row["name"] if verify_password(password, row["password_hash"]) else None

    # ---- sessions --------------------------------------------------------
    def create_session(self, user: str, api_key: str = "") -> str:
        key = secrets.token_hex(16)  # 32 hex chars, like Last.fm session keys
        self._exec(
            "INSERT INTO sessions(key_hash, user, api_key, created) VALUES(?,?,?,?)",
            (digest(key), user, api_key, self.now()),
        )
        return key

    def adopt_session(self, key: str, user: str, api_key: str = "") -> None:
        self._exec(
            "INSERT OR IGNORE INTO sessions(key_hash, user, api_key, created, adopted) VALUES(?,?,?,?,1)",
            (digest(key), user, api_key, self.now()),
        )

    def session_user(self, key: str) -> str | None:
        row = self._one("SELECT user FROM sessions WHERE key_hash=?", (digest(key),))
        return row["user"] if row else None

    def revoke_sessions(self, user: str) -> int:
        return self._exec("DELETE FROM sessions WHERE user=?", (user,)).rowcount

    # ---- auth tokens (web auth flow) --------------------------------------
    TOKEN_TTL = 60 * 60

    def create_token(self, api_key: str = "") -> str:
        token = secrets.token_hex(16)
        self._exec(
            "INSERT INTO auth_tokens(token_hash, api_key, created, csrf) VALUES(?,?,?,?)",
            (digest(token), api_key, self.now(), secrets.token_urlsafe(24)),
        )
        self._exec("DELETE FROM auth_tokens WHERE created < ?", (self.now() - 24 * 3600,))
        return token

    def get_token(self, token: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM auth_tokens WHERE token_hash=?", (digest(token),))

    def token_expired(self, row: sqlite3.Row) -> bool:
        return self.now() - row["created"] > self.TOKEN_TTL

    def authorize_token(self, token: str, user: str) -> None:
        self._exec("UPDATE auth_tokens SET user=? WHERE token_hash=?", (user, digest(token)))

    def consume_token(self, token: str) -> None:
        self._exec("DELETE FROM auth_tokens WHERE token_hash=?", (digest(token),))

    # ---- scrobbles -------------------------------------------------------
    def add_scrobble(self, user: str, s: dict[str, Any]) -> bool:
        cur = self._exec(
            "INSERT OR IGNORE INTO scrobbles(user, ts, artist, track, album, album_artist, track_number, duration, "
            "track_mbid, artist_mbid, album_mbid) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                user, int(s["timestamp"]), s["artist"], s["track"], s.get("album", ""), s.get("albumArtist", ""),
                s.get("trackNumber", ""), s.get("duration"), s.get("mbid", ""), s.get("artist_mbid", ""),
                s.get("album_mbid", ""),
            ),
        )
        return cur.rowcount > 0

    def fill_scrobble_mbids(self, user: str, artist: str, track: str, album: str, track_mbid: str,
                            artist_mbid: str, album_mbid: str) -> None:
        """Fill MBIDs the client did not send; never overwrite client values."""
        self._exec(
            "UPDATE scrobbles SET track_mbid=CASE WHEN track_mbid='' THEN ? ELSE track_mbid END, "
            "artist_mbid=CASE WHEN artist_mbid='' THEN ? ELSE artist_mbid END, "
            "album_mbid=CASE WHEN album_mbid='' AND album=? COLLATE NOCASE THEN ? ELSE album_mbid END "
            "WHERE user=? AND artist=? COLLATE NOCASE AND track=? COLLATE NOCASE",
            (track_mbid, artist_mbid, album, album_mbid, user, artist, track),
        )

    def recent_scrobbles(self, user: str, limit: int, offset: int = 0, since: int | None = None,
                         until: int | None = None) -> tuple[list[sqlite3.Row], int]:
        where, args = ["user=?"], [user]
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        cond = " AND ".join(where)
        total = self._one(f"SELECT COUNT(*) AS n FROM scrobbles WHERE {cond}", args)["n"]
        rows = self._all(f"SELECT * FROM scrobbles WHERE {cond} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                         args + [limit, offset])
        return rows, total

    def scrobble_count(self, user: str) -> int:
        return self._one("SELECT COUNT(*) AS n FROM scrobbles WHERE user=?", (user,))["n"]

    def first_scrobble_ts(self, user: str) -> int | None:
        row = self._one("SELECT MIN(ts) AS t FROM scrobbles WHERE user=?", (user,))
        return row["t"] if row else None

    def top(self, user: str, kind: str, limit: int, offset: int = 0, since: int | None = None,
            until: int | None = None) -> tuple[list[sqlite3.Row], int]:
        """Aggregate a user's scrobbles. kind: artist | album | track."""
        group = {
            "artist": "artist COLLATE NOCASE",
            "album": "COALESCE(NULLIF(album_artist,''), artist) COLLATE NOCASE, album COLLATE NOCASE",
            "track": "artist COLLATE NOCASE, track COLLATE NOCASE",
        }[kind]
        where, args = ["user=?"], [user]
        if kind == "album":
            where.append("album != ''")
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        cond = " AND ".join(where)
        cols = (
            "artist, MAX(artist_mbid) AS artist_mbid, COUNT(*) AS playcount, MAX(ts) AS last_ts, "
            "COALESCE(NULLIF(album_artist,''), artist) AS album_artist_eff, album, MAX(album_mbid) AS album_mbid, "
            "track, MAX(track_mbid) AS track_mbid, MAX(duration) AS duration"
        )
        total = self._one(f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM scrobbles WHERE {cond} GROUP BY {group})",
                          args)["n"]
        rows = self._all(
            f"SELECT {cols} FROM scrobbles WHERE {cond} GROUP BY {group} "
            f"ORDER BY playcount DESC, last_ts DESC LIMIT ? OFFSET ?",
            args + [limit, offset],
        )
        return rows, total

    def playcount(self, user: str, artist: str, track: str | None = None, album: str | None = None) -> int:
        sql, args = "SELECT COUNT(*) AS n FROM scrobbles WHERE user=? AND artist=? COLLATE NOCASE", [user, artist]
        if track is not None:
            sql += " AND track=? COLLATE NOCASE"
            args.append(track)
        if album is not None:
            sql += " AND album=? COLLATE NOCASE"
            args.append(album)
        return self._one(sql, args)["n"]

    def library_artists(self, user: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
        return self.top(user, "artist", limit, offset)

    # ---- now playing -----------------------------------------------------
    def set_now_playing(self, user: str, data: dict[str, Any], duration: int | None) -> None:
        now = self.now()
        ttl = duration if duration and duration > 0 else 600
        self._exec(
            "INSERT INTO nowplaying(user, started, expires, data) VALUES(?,?,?,?) "
            "ON CONFLICT(user) DO UPDATE SET started=excluded.started, expires=excluded.expires, data=excluded.data",
            (user, now, now + ttl, json.dumps(data)),
        )

    def get_now_playing(self, user: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM nowplaying WHERE user=?", (user,))
        if row is None or row["expires"] < self.now():
            return None
        return json.loads(row["data"])

    def clear_now_playing(self, user: str, artist: str | None = None, track: str | None = None) -> None:
        np = self.get_now_playing(user)
        if np is None:
            return
        if artist is None or (np.get("artist", "").lower() == artist.lower()
                              and np.get("track", "").lower() == (track or "").lower()):
            self._exec("DELETE FROM nowplaying WHERE user=?", (user,))

    # ---- loves -----------------------------------------------------------
    def love(self, user: str, artist: str, track: str, track_mbid: str = "", artist_mbid: str = "") -> None:
        self._exec(
            "INSERT INTO loves(user, artist, track, ts, track_mbid, artist_mbid) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(user, artist, track) DO NOTHING",
            (user, artist, track, self.now(), track_mbid, artist_mbid),
        )

    def unlove(self, user: str, artist: str, track: str) -> None:
        self._exec("DELETE FROM loves WHERE user=? AND artist=? AND track=?", (user, artist, track))

    def is_loved(self, user: str, artist: str, track: str) -> bool:
        return self._one("SELECT 1 FROM loves WHERE user=? AND artist=? AND track=?", (user, artist, track)) is not None

    def loved(self, user: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
        total = self._one("SELECT COUNT(*) AS n FROM loves WHERE user=?", (user,))["n"]
        rows = self._all("SELECT * FROM loves WHERE user=? ORDER BY ts DESC LIMIT ? OFFSET ?", (user, limit, offset))
        return rows, total

    # ---- personal tags ---------------------------------------------------
    def add_tags(self, user: str, kind: str, artist: str, name: str, tags: list[str]) -> None:
        for tag in tags:
            self._exec(
                "INSERT OR IGNORE INTO user_tags(user, kind, artist, name, tag, ts) VALUES(?,?,?,?,?,?)",
                (user, kind, artist, name, tag, self.now()),
            )

    def remove_tag(self, user: str, kind: str, artist: str, name: str, tag: str) -> None:
        self._exec("DELETE FROM user_tags WHERE user=? AND kind=? AND artist=? AND name=? AND tag=?",
                   (user, kind, artist, name, tag))

    def get_tags(self, user: str, kind: str, artist: str, name: str) -> list[str]:
        rows = self._all("SELECT tag FROM user_tags WHERE user=? AND kind=? AND artist=? AND name=? ORDER BY ts, rowid",
                         (user, kind, artist, name))
        return [r["tag"] for r in rows]

    def top_user_tags(self, user: str, limit: int) -> list[sqlite3.Row]:
        return self._all("SELECT tag, COUNT(*) AS n FROM user_tags WHERE user=? GROUP BY tag COLLATE NOCASE "
                         "ORDER BY n DESC, tag LIMIT ?", (user, limit))

    def tagged_items(self, user: str, tag: str, kind: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], int]:
        total = self._one("SELECT COUNT(*) AS n FROM user_tags WHERE user=? AND tag=? AND kind=?",
                          (user, tag, kind))["n"]
        rows = self._all("SELECT * FROM user_tags WHERE user=? AND tag=? AND kind=? ORDER BY ts DESC LIMIT ? OFFSET ?",
                         (user, tag, kind, limit, offset))
        return rows, total

    def global_tags(self, limit: int, offset: int) -> list[sqlite3.Row]:
        return self._all("SELECT tag, COUNT(*) AS n, COUNT(DISTINCT user) AS reach FROM user_tags "
                         "GROUP BY tag COLLATE NOCASE ORDER BY n DESC, tag LIMIT ? OFFSET ?", (limit, offset))

    # ---- outbox ----------------------------------------------------------
    def enqueue(self, kind: str, payload: dict[str, Any]) -> int:
        cur = self._exec("INSERT INTO outbox(kind, payload, created) VALUES(?,?,?)",
                         (kind, json.dumps(payload), self.now()))
        return int(cur.lastrowid)

    def due_outbox(self, limit: int = 50) -> list[sqlite3.Row]:
        return self._all("SELECT * FROM outbox WHERE next_attempt <= ? ORDER BY id LIMIT ?", (self.now(), limit))

    def outbox_done(self, item_id: int) -> None:
        self._exec("DELETE FROM outbox WHERE id=?", (item_id,))

    def outbox_failed(self, item_id: int, error: str, retry_in: int) -> None:
        self._exec("UPDATE outbox SET attempts=attempts+1, next_attempt=?, last_error=? WHERE id=?",
                   (self.now() + retry_in, error[:500], item_id))

    def outbox_size(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM outbox")["n"]

    # ---- cache -----------------------------------------------------------
    def cache_get(self, key: str) -> Any | None:
        row = self._one("SELECT value, expires FROM http_cache WHERE key=?", (key,))
        if row is None or row["expires"] < self.now():
            return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any, ttl: int) -> None:
        self._exec("INSERT OR REPLACE INTO http_cache(key, expires, value) VALUES(?,?,?)",
                   (key, self.now() + ttl, json.dumps(value)))

    def cache_purge(self) -> int:
        return self._exec("DELETE FROM http_cache WHERE expires < ?", (self.now(),)).rowcount
