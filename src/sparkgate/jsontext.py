"""Recovering the JSON value a model meant to return."""

from __future__ import annotations

import json
from typing import Any

_DECODER = json.JSONDecoder()


def extract_json(text: str) -> Any | None:
    """The JSON value a model meant to return, tolerating fences and surrounding prose.

    Tries, in order: the whole string; the first ```json fenced block; the first
    position where a JSON object or array decodes cleanly.
    """
    s = text.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    if "```" in s:
        for block in s.split("```")[1::2]:
            body = block.split("\n", 1)[1] if block.lower().startswith("json") else block
            try:
                return json.loads(body.strip())
            except (json.JSONDecodeError, IndexError):
                continue
    for i, ch in enumerate(s):
        if ch in "{[":
            try:
                value, _ = _DECODER.raw_decode(s, i)
                return value
            except json.JSONDecodeError:
                continue
    return None
