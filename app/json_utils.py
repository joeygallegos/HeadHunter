from __future__ import annotations

import json
from typing import Any, List, Optional


def safe_json_loads(value: Any) -> Optional[Any]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if not (text.startswith("{") or text.startswith("[")):
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def safe_json_list(
    value: Any,
    *,
    strip_items: bool = False,
    drop_empty: bool = False,
) -> List[str]:
    parsed = safe_json_loads(value)
    if not isinstance(parsed, list):
        return []

    items: List[str] = []
    for item in parsed:
        if item is None:
            continue
        text = str(item)
        if strip_items:
            text = text.strip()
        if drop_empty and not text:
            continue
        items.append(text)
    return items
