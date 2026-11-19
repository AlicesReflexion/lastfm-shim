"""Builders for Last.fm-shaped response fragments."""

from __future__ import annotations

import html
import time
from email.utils import format_datetime
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import quote_plus

from ..errors import INVALID_PARAMS, LastFmError

IMAGE_SIZES = ("small", "medium", "large", "extralarge", "mega", "")
LICENSE = ("User-contributed text is available under the Creative Commons By-SA License; "
           "additional terms may apply.")
OPENSEARCH_NS = "http://a9.com/-/spec/opensearch/1.1/"

PERIODS = {"overall": None, "7day": 7, "1month": 30, "3month": 90, "6month": 180, "12month": 365}


def lfm_url(*parts: str) -> str:
    return "https://www.last.fm/music/" + "/".join(quote_plus(p or "") for p in parts)


def user_url(name: str) -> str:
    return "https://www.last.fm/user/" + quote_plus(name)


def tag_url(name: str) -> str:
    return "https://www.last.fm/tag/" + quote_plus(name)


def images(small: str = "", large: str = "", mega: str = "") -> list[dict[str, str]]:
    """Six image entries, as Last.fm emits (small..mega plus an unsized one)."""
    small = small or large or mega
    large = large or small or mega
    mega = mega or large
    urls = {"small": small, "medium": small, "large": small, "extralarge": large, "mega": mega, "": mega}
    return [{"#text": urls[s], "size": s} for s in IMAGE_SIZES]


def cover_images(cover: dict[str, str]) -> list[dict[str, str]]:
    return images(cover.get("250", ""), cover.get("500", ""), cover.get("1200") or cover.get("image", ""))


def artist_images(tadb: dict[str, str]) -> list[dict[str, str]]:
    thumb = tadb.get("strArtistThumb", "")
    if not thumb:
        return images()
    # TheAudioDB's size-suffix variants are undocumented; use the original.
    return images(thumb, thumb, thumb)


def toptags(tags: Iterable[tuple[str, int]], limit: int = 5, with_url: bool = True,
            with_count: bool = False) -> list[dict[str, Any]]:
    tags = list(tags)[:limit]
    top = max((c for _, c in tags), default=0) or 1
    out = []
    for name, count in tags:
        t: dict[str, Any] = {}
        if with_count:
            t["count"] = round(100 * count / top) if count else 0
        t["name"] = name.lower()
        if with_url:
            t["url"] = tag_url(name.lower())
        out.append(t)
    return out


def wiki(summary: dict[str, Any] | None, lfm_page: str, now: float | None = None) -> dict[str, Any] | None:
    if not summary:
        return None
    text = summary["extract"]
    href = summary["url"]
    published = summary.get("timestamp") or ""
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00")) if published else None
    except ValueError:
        dt = None
    dt = dt or datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    link = f' <a href="{html.escape(href, quote=True)}">Read more on Wikipedia</a>'
    return {
        "links": {"link": {"#text": "", "rel": "original", "href": lfm_page + "/+wiki"}},
        "published": dt.strftime("%d %b %Y, %H:%M"),
        "summary": html.escape(text, quote=False) + link,
        "content": html.escape(text, quote=False) + link + "\n\n" + LICENSE,
    }


def opensearch(query: dict[str, str], total: int, page: int, limit: int, matches_key: str,
               matches: dict[str, Any], for_attr: str) -> dict[str, Any]:
    return {
        "opensearch:Query": {"#text": "", "role": "request", **query, "startPage": str(page)},
        "opensearch:totalResults": total,
        "opensearch:startIndex": (page - 1) * limit,
        "opensearch:itemsPerPage": limit,
        matches_key: matches,
        "@attr": {"for": for_attr},
    }


def paging_attr(page: int, limit: int, total: int, **extra: Any) -> dict[str, Any]:
    pages = (total + limit - 1) // limit if limit else 0
    attr = dict(extra)
    attr.update({"page": page, "perPage": limit, "totalPages": pages, "total": total})
    return attr


def period_since(period: str | None, now: int) -> int | None:
    period = (period or "overall").lower()
    if period not in PERIODS:
        raise LastFmError(INVALID_PARAMS, "Invalid parameters - period must be one of " + ", ".join(PERIODS))
    days = PERIODS[period]
    return None if days is None else now - days * 86400


def uts_date(ts: int) -> dict[str, Any]:
    dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
    return {"uts": int(ts), "#text": dt.strftime("%d %b %Y, %H:%M")}


def http_date(ts: int) -> str:
    return format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc), usegmt=True)


def ms_to_s(ms: Any) -> int:
    try:
        return int(int(ms) / 1000)
    except (TypeError, ValueError):
        return 0
