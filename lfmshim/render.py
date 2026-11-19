"""Render handler results as Last.fm XML or JSON.

Handlers return a "JSON-shaped" tree that mirrors Last.fm's JSON output,
which itself is a mechanical transform of the XML:

* a ``dict`` is an element; its keys are child elements, except
  - ``"@attr"``: a dict of attributes for that element (container form), and
  - ``"#text"``: the element's text; when present, the dict's other scalar
    keys are attributes (leaf form, e.g. ``{"#text": url, "size": "small"}``).
* a ``list`` is a repeated element (``<image/><image/>``).
* scalars are text. ``None`` is an empty element.

JSON output stringifies every scalar, as Last.fm does.
"""

from __future__ import annotations

import json
import re
from typing import Any
from xml.sax.saxutils import escape, quoteattr

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _text(value: Any) -> str:
    return escape(_XML_INVALID.sub("", scalar(value)))


def _attrs(attrs: dict[str, Any]) -> str:
    return "".join(f" {k}={quoteattr(_XML_INVALID.sub('', scalar(v)))}" for k, v in attrs.items())


def _element(tag: str, value: Any, out: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _element(tag, item, out)
        return
    if isinstance(value, dict):
        if "#text" in value:
            attrs = {k: v for k, v in value.items() if k != "#text"}
            out.append(f"<{tag}{_attrs(attrs)}>{_text(value['#text'])}</{tag}>")
            return
        attrs = value.get("@attr") or {}
        children = [(k, v) for k, v in value.items() if k != "@attr"]
        if not children:
            out.append(f"<{tag}{_attrs(attrs)}/>")
            return
        out.append(f"<{tag}{_attrs(attrs)}>")
        for k, v in children:
            _element(k, v, out)
        out.append(f"</{tag}>")
        return
    if value is None or scalar(value) == "":
        out.append(f"<{tag}/>")
        return
    out.append(f"<{tag}>{_text(value)}</{tag}>")


def to_xml(result: dict[str, Any] | None) -> bytes:
    out = ['<?xml version="1.0" encoding="UTF-8"?>\n<lfm status="ok">']
    for k, v in (result or {}).items():
        _element(k, v, out)
    out.append("</lfm>\n")
    return "".join(out).encode("utf-8")


def error_xml(code: int, message: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<lfm status="failed">'
        f"<error code={quoteattr(str(code))}>{_text(message)}</error></lfm>\n"
    ).encode("utf-8")


def _jsonify(value: Any, collapse_single: bool) -> Any:
    if isinstance(value, dict):
        return {k: _jsonify(v, collapse_single) for k, v in value.items()}
    if isinstance(value, list):
        items = [_jsonify(v, collapse_single) for v in value]
        if collapse_single and len(items) == 1:
            return items[0]
        return items
    if value is None:
        return ""
    return scalar(value)


def to_json_obj(result: dict[str, Any] | None, collapse_single: bool = False) -> Any:
    return _jsonify(result or {}, collapse_single)


def to_json(result: dict[str, Any] | None, collapse_single: bool = False) -> bytes:
    return json.dumps(to_json_obj(result, collapse_single), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def error_json(code: int, message: str) -> bytes:
    return json.dumps({"message": message, "error": code, "links": []}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


_CALLBACK = re.compile(r"^[A-Za-z_$][\w$]*(\.[A-Za-z_$][\w$]*)*$")


def valid_callback(name: str) -> bool:
    return bool(_CALLBACK.match(name)) and len(name) <= 128
