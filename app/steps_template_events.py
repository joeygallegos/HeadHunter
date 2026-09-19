"""Opt-in, local-only aggregate events for starter-template quality checks."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List


def metrics_enabled() -> bool:
    """Require an explicit opt-in before recording local template metrics."""
    return os.getenv("STEPS_TEMPLATE_METRICS", "").strip().lower() == "true"


def events_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, "output", "steps_template_events.jsonl")


def append_event(event: Dict[str, Any]) -> None:
    """Append a safe aggregate event without URLs, site keys, or job content."""
    if not metrics_enabled():
        return
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    os.makedirs(os.path.dirname(events_path()), exist_ok=True)
    with open(events_path(), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_recent_events(limit: int = 10) -> List[Dict[str, Any]]:
    """Read recent aggregate events only when local metrics are enabled."""
    if not metrics_enabled() or not os.path.exists(events_path()):
        return []
    with open(events_path(), encoding="utf-8") as handle:
        lines = handle.readlines()[-max(1, min(limit, 50)) :]
    events: List[Dict[str, Any]] = []
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events
