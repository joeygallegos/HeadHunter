from __future__ import annotations

import os
import shutil
import urllib.parse
from datetime import datetime
from os import PathLike
from typing import Any, Dict, Iterable, List, Optional, Set

from .file_utils import read_json_file, write_json


PathValue = str | PathLike[str]


def normalize_url(base_url: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return urllib.parse.urljoin(base_url, url)


def load_steps_urls(steps_data: Dict[str, Any]) -> Set[str]:
    urls: Set[str] = set()
    for steps in steps_data.values():
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and step.get("action") == "load_url":
                url = normalize_url("", str(step.get("url") or ""))
                if url:
                    urls.add(url)
    return urls


def merge_steps_suggestions(
    *,
    steps_path: PathValue,
    suggestions_path: PathValue,
    selected_ids: Optional[Iterable[str]] = None,
    backup: bool = True,
) -> Dict[str, Any]:
    steps_data = read_json_file(steps_path, {})
    if not isinstance(steps_data, dict):
        raise ValueError(f"{steps_path} must contain a JSON object")

    suggestions_doc = read_json_file(suggestions_path, {"suggestions": []})
    suggestions = suggestions_doc.get("suggestions", [])
    if not isinstance(suggestions, list):
        raise ValueError(f"{suggestions_path} must contain suggestions[]")

    selected = {str(item) for item in selected_ids or []}
    existing_urls = load_steps_urls(steps_data)
    applied: List[str] = []
    skipped: List[Dict[str, str]] = []

    if backup and suggestions:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{steps_path}.{ts}.bak"
        if os.path.exists(steps_path):
            shutil.copy2(steps_path, backup_path)
    else:
        backup_path = ""

    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        sid = str(suggestion.get("id") or "")
        if selected and sid not in selected:
            continue
        if suggestion.get("state") not in {"pending", "approved"}:
            continue
        site_key = str(suggestion.get("site_key") or "").strip()
        steps = suggestion.get("steps")
        if not site_key or not isinstance(steps, list):
            skipped.append({"id": sid, "reason": "missing site_key or steps"})
            continue
        load_url = ""
        for step in steps:
            if isinstance(step, dict) and step.get("action") == "load_url":
                load_url = normalize_url("", str(step.get("url") or "")) or ""
                break
        if site_key in steps_data:
            skipped.append({"id": sid, "reason": f"site_key exists: {site_key}"})
            continue
        if load_url and load_url in existing_urls:
            skipped.append({"id": sid, "reason": f"load_url exists: {load_url}"})
            continue
        steps_data[site_key] = steps
        if load_url:
            existing_urls.add(load_url)
        suggestion["state"] = "applied"
        suggestion["applied_at"] = datetime.now().isoformat(timespec="seconds")
        applied.append(sid)

    write_json(steps_path, steps_data)
    write_json(suggestions_path, suggestions_doc)
    return {"applied": applied, "skipped": skipped, "backup_path": backup_path}
