from __future__ import annotations

import json
import os
from datetime import datetime
from os import PathLike
from typing import Any, Dict, List, Optional


PathValue = str | PathLike[str]


def reset_event_log(path: Optional[PathValue]) -> None:
    if not path:
        return
    directory = os.path.dirname(os.fspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("")


def append_event(
    path: Optional[PathValue],
    event_type: str,
    message: str,
    **fields: Any,
) -> None:
    if not path:
        return
    event = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "type": event_type,
        "message": message,
        **fields,
    }
    try:
        directory = os.path.dirname(os.fspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    except Exception:
        pass


def read_event_log(path: PathValue, limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    limit = max(1, min(int(limit if limit is not None else 200), 1000))
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        lines = handle.readlines()[-limit:]
    events: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
