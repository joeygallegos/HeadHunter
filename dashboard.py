# dashboard.py
from __future__ import annotations

import json
import os
import html
import re
import difflib
import shutil
import sys
import urllib.parse
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request
from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    or_,
    select,
    text as sa_text,
)

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# ----------------------------------------------------------------------
# Import your existing models / DB session (from app/models.py)
# ----------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
app_dir = os.path.join(BASE_DIR, "app")
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

from dotenv import load_dotenv

load_dotenv()

# NOTE: app/models.py currently prints DATABASE_URL on import. Remove that print.
from app.models import (  # type: ignore
    Base,
    JobChange,
    SessionLocal,
    IntegrationRun,
    Job,
    engine,
    ensure_job_reference_fields_column,
)

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DEFAULT_EVENTS_PATH = os.path.join(OUTPUT_DIR, "job_board_discovery_events.jsonl")
DEFAULT_STEPS_PATH = os.path.join(BASE_DIR, "steps.json")
DEFAULT_SUGGESTIONS_PATH = os.path.join(OUTPUT_DIR, "steps_suggestions.json")


def read_json_file(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")


def read_event_log(path: str = DEFAULT_EVENTS_PATH, limit: int = 200) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    limit = max(1, min(int(limit or 200), 1000))
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()[-limit:]
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


def normalize_url(base_url: str, url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return urllib.parse.urljoin(base_url, url)


def _load_steps_urls(steps_data: Dict[str, Any]) -> set[str]:
    urls: set[str] = set()
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
    steps_path: str = DEFAULT_STEPS_PATH,
    suggestions_path: str = DEFAULT_SUGGESTIONS_PATH,
    selected_ids: Optional[List[str]] = None,
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
    existing_urls = _load_steps_urls(steps_data)
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

try:
    from app.models import JobSwipe  # type: ignore
except ImportError:
    # Older deployments may have dashboard.py before app/models.py is updated.
    # Keep the dashboard importable and create the same table shape on first use.
    class JobSwipe(Base):  # type: ignore
        __tablename__ = "job_swipes"
        __table_args__ = (
            UniqueConstraint("job_pk", name="uq_job_swipe_job_pk"),
            {
                "extend_existing": True,
                "mysql_charset": "utf8mb4",
                "mysql_collate": "utf8mb4_unicode_ci",
            },
        )

        id = Column(Integer, primary_key=True, autoincrement=True)
        job_pk = Column(
            Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        )
        action = Column(String(16), nullable=False)
        created_at = Column(
            DateTime(timezone=True), server_default=func.now(), nullable=False
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
LOCAL_TZ = "America/Chicago"


def _now_local() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(LOCAL_TZ))
    return datetime.now()


def _to_int(x: Any) -> int:
    if x is None:
        return 0
    try:
        return int(x)
    except Exception:
        return 0


def _run_status(started_at: Optional[datetime], finished_at: Optional[datetime]) -> str:
    if finished_at:
        return "Finished"
    if not started_at:
        return "Running"

    age = _now_local().replace(tzinfo=None) - started_at.replace(tzinfo=None)
    if age > timedelta(hours=24):
        return "Incomplete"
    return "Running"


def _rolling_mean(values: List[float], window: int) -> List[float]:
    if not values:
        return []
    w = max(1, window)
    out: List[float] = []
    for i in range(len(values)):
        start = max(0, i - w + 1)
        chunk = values[start : i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _safe_json_loads(s: Any) -> Optional[Any]:
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    if not isinstance(s, str):
        return None
    txt = s.strip()
    if not txt:
        return None
    if not (txt.startswith("{") or txt.startswith("[")):
        return None
    try:
        return json.loads(txt)
    except Exception:
        return None


def _safe_json_list(s: Any) -> List[str]:
    value = _safe_json_loads(s)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None]


_REFERENCE_FIELDS_COLUMN_READY = False


def _ensure_reference_fields_column() -> None:
    global _REFERENCE_FIELDS_COLUMN_READY
    if _REFERENCE_FIELDS_COLUMN_READY:
        return
    # Dashboard reads can happen before run.py initdb, so apply this one
    # additive column migration against whichever SessionLocal the app uses.
    with SessionLocal() as session:
        ensure_job_reference_fields_column(session.get_bind())
    _REFERENCE_FIELDS_COLUMN_READY = True


def _job_reference_fields(j: Job) -> List[Dict[str, str]]:
    raw_refs = _safe_json_loads(getattr(j, "reference_fields", None))
    if not isinstance(raw_refs, dict):
        return []

    fields: List[Dict[str, str]] = []
    for label, value in raw_refs.items():
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            fields.append({"label": str(label), "value": text_value})
    return fields


def _job_ai_columns(j: Job) -> Dict[str, Any]:
    return {
        "match_percentage": getattr(j, "ai_match_percentage", None),
        "salary": getattr(j, "ai_salary", None),
        "fit_summary": getattr(j, "ai_fit_summary", None),
        "keywords_overlap": _safe_json_list(getattr(j, "ai_keywords_overlap", None)),
        "missing_keywords": _safe_json_list(getattr(j, "ai_missing_keywords", None)),
        "experience_match": getattr(j, "ai_experience_match", None),
        "location_policy_match": getattr(j, "ai_location_policy_match", None),
        "analyzed_at": _fmt_dt(getattr(j, "ai_analyzed_at", None)),
    }


def _normalize_highlight_text(s: str) -> str:
    return (
        s.replace("â€™", "'")
        .replace("â€˜", "'")
        .replace("â€œ", '"')
        .replace("â€", '"')
    )


def highlight_as_you_will(job_title: str, job_desc: str) -> str:
    if not job_title or not job_desc:
        return html.escape(job_desc or "")

    text = job_desc
    norm = _normalize_highlight_text(text)
    end = r'[.!?](?:["\')\]]+)?(?:\s|$)'
    patterns = [
        rf"\bAs\s+a\b[^.!?]{{0,300}}?\byou\s+will\b[^.!?]*?{end}",
        r"\bAbout the Role:\s*[^.!?]*?(?<!S)(?<!J)\.{1}(?:\s|$)",
        r"\bWhat You(?:'|â€™)ll Do:\s*[^.!?]*?(?<!S)(?<!J)\.{1}(?:\s|$)",
        rf"\bIn this role\b[^.!?]*?{end}",
        rf"\bYou will own\b[^.!?]*?{end}",
        rf"\bYou will be responsible for\b[^.!?]*?{end}",
        rf"\bYou will lead\b[^.!?]*?{end}",
        rf"\bYou will manage\b[^.!?]*?{end}",
        rf"\bAs part of this\b[^.!?]*?{end}",
        rf"\bThis role will be responsible for\b[^.!?]*?{end}",
        rf"\bWe seek a\b[^.!?]*?{end}",
    ]

    spans = []
    for pattern in patterns:
        match = re.search(pattern, norm, re.IGNORECASE)
        if match:
            spans.append(match.span())

    if not spans:
        return html.escape(text)

    spans.sort()
    dedup = []
    last_end = -1
    for start, stop in spans:
        if start >= last_end:
            dedup.append((start, stop))
            last_end = stop
            continue
        prev_start, prev_stop = dedup[-1]
        if (stop - start) > (prev_stop - prev_start):
            dedup[-1] = (start, stop)
            last_end = stop

    out = []
    cursor = 0
    for start, stop in dedup:
        if cursor < start:
            out.append(html.escape(text[cursor:start]))
        out.append(
            f"<mark class='bg-yellow-200'>{html.escape(text[start:stop])}</mark>"
        )
        cursor = stop
    if cursor < len(text):
        out.append(html.escape(text[cursor:]))
    return "".join(out)


_SWIPE_TABLE_READY = False


def _ensure_swipe_table() -> None:
    global _SWIPE_TABLE_READY
    if _SWIPE_TABLE_READY:
        return
    # Create only the swipe table here so the dashboard can adopt the new DB-backed
    # review queue without requiring the scraper init path to run first.
    JobSwipe.__table__.create(bind=engine, checkfirst=True)
    _SWIPE_TABLE_READY = True


def _serialize_swipe_job(job: Job) -> Dict[str, Any]:
    return {
        "id": job.id,
        "JobID": job.job_id or "",
        "Site": job.site or "",
        "JobTitle": job.title or "",
        "JobUrl": job.url or "",
        "JobDesc": job.desc or "",
        "JobDescHighlighted": highlight_as_you_will(job.title or "", job.desc or ""),
        "Keywords": job.keywords or "",
        "JobLevel": job.level or "Unknown",
        "JobPay": job.pay or "",
        "DiscoveryDate": _fmt_dt(job.discovery_date) or "",
        "AIMatchPercentage": job.ai_match_percentage,
        "AILocationPolicyMatch": job.ai_location_policy_match or "",
    }


def fetch_swipe_jobs() -> List[Dict[str, Any]]:
    _ensure_swipe_table()
    with SessionLocal() as session:
        jobs = (
            session.execute(
                select(Job)
                .outerjoin(JobSwipe, JobSwipe.job_pk == Job.id)
                .where(JobSwipe.id.is_(None))
                .where(Job.is_active.is_(True))
                .order_by(Job.discovery_date.desc(), Job.id.desc())
                .limit(500)
            )
            .scalars()
            .all()
        )
        return [_serialize_swipe_job(job) for job in jobs]


def record_swipe(job: Dict[str, Any], action: str) -> bool:
    _ensure_swipe_table()
    job_pk = _to_int(job.get("id"))
    with SessionLocal() as session:
        db_job = session.get(Job, job_pk) if job_pk else None
        if db_job is None and job.get("JobID") and job.get("Site"):
            db_job = (
                session.execute(
                    select(Job)
                    .where(Job.job_id == str(job.get("JobID")))
                    .where(Job.site == str(job.get("Site")))
                )
                .scalars()
                .first()
            )
        if db_job is None:
            return False

        existing = (
            session.execute(select(JobSwipe).where(JobSwipe.job_pk == db_job.id))
            .scalars()
            .first()
        )
        if existing:
            existing.action = action
        else:
            session.add(JobSwipe(job_pk=db_job.id, action=action))
        session.commit()
        return True


# ----------------------------------------------------------------------
# Discovery suggestion review
# ----------------------------------------------------------------------
def fetch_discovery_suggestions() -> Dict[str, Any]:
    doc = read_json_file(DEFAULT_SUGGESTIONS_PATH, {"suggestions": []})
    if not isinstance(doc, dict):
        doc = {"suggestions": []}
    suggestions = doc.get("suggestions")
    if not isinstance(suggestions, list):
        suggestions = []
    doc["suggestions"] = [
        item for item in suggestions if isinstance(item, dict)
    ]
    doc["returned"] = len(doc["suggestions"])
    return doc


def update_discovery_suggestion_state(ids: List[str], state: str) -> Dict[str, Any]:
    if state not in {"pending", "approved", "rejected", "applied"}:
        raise ValueError("invalid discovery suggestion state")
    selected = {str(item) for item in ids if str(item).strip()}
    doc = fetch_discovery_suggestions()
    changed = []
    for suggestion in doc["suggestions"]:
        sid = str(suggestion.get("id") or "")
        if sid in selected:
            suggestion["state"] = state
            suggestion["updated_at"] = datetime.now().isoformat(timespec="seconds")
            changed.append(sid)
    write_json(DEFAULT_SUGGESTIONS_PATH, doc)
    return {"changed": changed, "state": state}


def fetch_discovery_events(limit: int = 200) -> Dict[str, Any]:
    events = read_event_log(DEFAULT_EVENTS_PATH, limit=limit)
    return {
        "path": DEFAULT_EVENTS_PATH,
        "returned": len(events),
        "events": events,
    }


# ----------------------------------------------------------------------
# steps.json editor
# ----------------------------------------------------------------------
def _format_steps_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _steps_modified_at(path: str) -> str:
    if not os.path.exists(path):
        return ""
    return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")


TOP_LEVEL_STEP_ACTIONS = {
    "load_url",
    "debug_print_dom_by_css",
    "sleep",
    "scroll_to",
    "click_button",
    "select_checkbox",
    "type_text",
    "data_extract",
    "json_set_payload",
    "json_replace_text",
    "json_data_extract",
    "json_html_data_extract",
}
DOM_EXTRACT_ACTIONS = {"extract", "redirect", "sleep", "replace_text", "regex_extract", "next"}
JSON_EXTRACT_ACTIONS = {"extract", "next"}
JSON_HTML_EXTRACT_ACTIONS = {
    "extract",
    "redirect",
    "extract_detail",
    "replace_text",
    "regex_extract",
    "next",
}


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _has_selector(step: Dict[str, Any]) -> bool:
    return _is_nonempty_str(step.get("xpath")) or _is_nonempty_str(step.get("selector"))


def _require_nonempty_str(
    errors: List[str], step: Dict[str, Any], path: str, field: str
) -> None:
    if not _is_nonempty_str(step.get(field)):
        errors.append(f"{path}.{field} is required")


def _require_str_field(
    errors: List[str], step: Dict[str, Any], path: str, field: str
) -> None:
    if field not in step or not isinstance(step.get(field), str):
        errors.append(f"{path}.{field} must be a string")


def _validate_pagination(errors: List[str], pagination: Any, path: str) -> None:
    if not isinstance(pagination, dict):
        errors.append(f"{path}.pagination must be an object")
        return
    mode = pagination.get("mode")
    if mode != "click_next":
        errors.append(f'{path}.pagination.mode must be "click_next"')
    if "max_pages" in pagination and not isinstance(pagination.get("max_pages"), int):
        errors.append(f"{path}.pagination.max_pages must be an integer")
    if "page_wait_ms" in pagination and not isinstance(
        pagination.get("page_wait_ms"), (int, float)
    ):
        errors.append(f"{path}.pagination.page_wait_ms must be a number")
    for field in (
        "current_page_css",
        "next_page_css",
        "next_disabled_css",
        "page_as_column",
    ):
        if field in pagination and not isinstance(pagination.get(field), str):
            errors.append(f"{path}.pagination.{field} must be a string")


def _validate_extract_step(
    errors: List[str],
    step: Any,
    path: str,
    *,
    context: str,
) -> None:
    if not isinstance(step, dict):
        errors.append(f"{path} must be an object")
        return

    action = step.get("action")
    allowed = {
        "dom": DOM_EXTRACT_ACTIONS,
        "json": JSON_EXTRACT_ACTIONS,
        "json_html": JSON_HTML_EXTRACT_ACTIONS,
    }[context]
    if not _is_nonempty_str(action):
        errors.append(f"{path}.action is required")
        return
    if action not in allowed:
        errors.append(f'{path}.action "{action}" is not supported for {context} extraction')
        return

    if action == "extract":
        _require_nonempty_str(errors, step, path, "as_column")
        if context == "json":
            _require_nonempty_str(errors, step, path, "key")
        elif (step.get("data_type") or "").lower() != "current_url" and not _has_selector(step):
            errors.append(f"{path}.xpath or {path}.selector is required")
    elif action == "extract_detail":
        _require_nonempty_str(errors, step, path, "as_column")
        if not _has_selector(step):
            errors.append(f"{path}.xpath or {path}.selector is required")
    elif action == "redirect":
        if context == "dom":
            if not _is_nonempty_str(step.get("using_column")) and not _is_nonempty_str(
                step.get("link_css")
            ):
                errors.append(f"{path}.using_column or {path}.link_css is required")
        else:
            _require_nonempty_str(errors, step, path, "using_column")
    elif action == "replace_text":
        _require_nonempty_str(errors, step, path, "using_column")
        _require_str_field(errors, step, path, "text_find")
        _require_str_field(errors, step, path, "text_replace")
    elif action == "regex_extract":
        _require_nonempty_str(errors, step, path, "using_column")
        _require_nonempty_str(errors, step, path, "as_column")
        _require_nonempty_str(errors, step, path, "regex_pattern")
    elif action == "sleep" and "seconds" in step and not isinstance(
        step.get("seconds"), (int, float)
    ):
        errors.append(f"{path}.seconds must be a number")


def _validate_extract_steps(
    errors: List[str],
    step: Dict[str, Any],
    path: str,
    *,
    context: str,
) -> None:
    extract_steps = step.get("extract_steps")
    if not isinstance(extract_steps, list) or not extract_steps:
        errors.append(f"{path}.extract_steps must be a non-empty array")
        return
    for idx, extract_step in enumerate(extract_steps):
        _validate_extract_step(
            errors,
            extract_step,
            f"{path}.extract_steps[{idx}]",
            context=context,
        )


def _validate_top_level_step(errors: List[str], step: Any, path: str) -> None:
    if not isinstance(step, dict):
        errors.append(f"{path} must be an object")
        return

    action = step.get("action")
    if not _is_nonempty_str(action):
        errors.append(f"{path}.action is required")
        return
    if action not in TOP_LEVEL_STEP_ACTIONS:
        errors.append(f'{path}.action "{action}" is not supported by StepScraper')
        return

    if action == "load_url":
        _require_nonempty_str(errors, step, path, "url")
    elif action == "debug_print_dom_by_css":
        _require_nonempty_str(errors, step, path, "find_css")
    elif action in {"scroll_to", "click_button"}:
        if not _has_selector(step):
            errors.append(f"{path}.xpath or {path}.selector is required")
    elif action in {"select_checkbox", "type_text"}:
        _require_nonempty_str(errors, step, path, "selector")
    elif action == "sleep" and "seconds" in step and not isinstance(
        step.get("seconds"), (int, float)
    ):
        errors.append(f"{path}.seconds must be a number")
    elif action == "data_extract":
        _require_nonempty_str(errors, step, path, "focus_scope")
        _validate_extract_steps(errors, step, path, context="dom")
        if "pagination" in step:
            _validate_pagination(errors, step["pagination"], path)
    elif action == "json_replace_text":
        _require_str_field(errors, step, path, "text_find")
        _require_str_field(errors, step, path, "text_replace")
    elif action == "json_data_extract":
        _require_nonempty_str(errors, step, path, "focus_scope")
        _validate_extract_steps(errors, step, path, context="json")
    elif action == "json_html_data_extract":
        if not _is_nonempty_str(step.get("html_key")) and not _is_nonempty_str(
            step.get("focus_html_key")
        ):
            errors.append(f"{path}.html_key or {path}.focus_html_key is required")
        _require_nonempty_str(errors, step, path, "focus_scope")
        _validate_extract_steps(errors, step, path, context="json_html")


def _validate_steps_editor_schema(data: Dict[str, Any]) -> None:
    errors: List[str] = []
    for site, steps in data.items():
        if not isinstance(site, str) or not site.strip():
            errors.append("site keys must be non-empty strings")
            continue
        if not isinstance(steps, list):
            errors.append(f"{site} must be an array of step objects")
            continue
        for idx, step in enumerate(steps):
            _validate_top_level_step(errors, step, f"{site}[{idx}]")

    if errors:
        preview = "; ".join(errors[:8])
        suffix = f"; and {len(errors) - 8} more" if len(errors) > 8 else ""
        raise ValueError(f"steps.json schema validation failed: {preview}{suffix}")


def _parse_steps_editor_content(content: Any) -> tuple[Dict[str, Any], str]:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("steps.json content is required")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError("steps.json must contain a top-level JSON object")
    _validate_steps_editor_schema(data)
    return data, _format_steps_json(data)


def _load_steps_editor_state() -> Dict[str, Any]:
    data = read_json_file(DEFAULT_STEPS_PATH, {})
    if not isinstance(data, dict):
        raise ValueError("steps.json must contain a top-level JSON object")
    content = _format_steps_json(data)
    return {
        "path": DEFAULT_STEPS_PATH,
        "content": content,
        "site_count": len(data),
        "modified_at": _steps_modified_at(DEFAULT_STEPS_PATH),
    }


def _steps_unified_diff(current_content: str, edited_content: str) -> str:
    lines = difflib.unified_diff(
        current_content.splitlines(),
        edited_content.splitlines(),
        fromfile="steps.json (current)",
        tofile="steps.json (edited)",
        lineterm="",
    )
    diff = "\n".join(lines)
    return f"{diff}\n" if diff else ""


def preview_steps_editor_content(content: Any) -> Dict[str, Any]:
    data, formatted = _parse_steps_editor_content(content)
    current = _load_steps_editor_state()["content"]
    diff = _steps_unified_diff(current, formatted)
    return {
        "content": formatted,
        "diff": diff,
        "has_changes": bool(diff),
        "site_count": len(data),
        "message": "Changes ready to review." if diff else "No changes.",
    }


def save_steps_editor_content(content: Any) -> Dict[str, Any]:
    data, formatted = _parse_steps_editor_content(content)
    current = _load_steps_editor_state()["content"]
    if formatted == current:
        return {
            "saved": False,
            "backup_path": "",
            "path": DEFAULT_STEPS_PATH,
            "site_count": len(data),
            "modified_at": _steps_modified_at(DEFAULT_STEPS_PATH),
            "message": "No changes to save.",
        }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{DEFAULT_STEPS_PATH}.{ts}.bak"
    if os.path.exists(DEFAULT_STEPS_PATH):
        shutil.copy2(DEFAULT_STEPS_PATH, backup_path)
    write_json(DEFAULT_STEPS_PATH, data)
    return {
        "saved": True,
        "backup_path": backup_path,
        "path": DEFAULT_STEPS_PATH,
        "site_count": len(data),
        "modified_at": _steps_modified_at(DEFAULT_STEPS_PATH),
        "content": formatted,
        "message": "Saved steps.json.",
    }


# ----------------------------------------------------------------------
# Data access: Integration Runs (existing)
# ----------------------------------------------------------------------
def fetch_runs(days: int) -> Dict[str, List]:
    today = _now_local().date()
    since_date = today - timedelta(days=max(0, days - 1))
    since_dt = datetime.combine(since_date, datetime.min.time())
    if ZoneInfo:
        since_dt = since_dt.replace(tzinfo=ZoneInfo(LOCAL_TZ))

    with SessionLocal() as session:
        runs = (
            session.execute(
                select(IntegrationRun)
                .where(IntegrationRun.started_at >= since_dt)
                .order_by(IntegrationRun.started_at.asc())
            )
            .scalars()
            .all()
        )

    per_day: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in runs:
        started_at = r.started_at
        if started_at is None:
            continue
        try:
            day_str = started_at.date().isoformat()
        except Exception:
            continue

        per_day[day_str]["inserted"] += _to_int(getattr(r, "inserted_count", 0))
        per_day[day_str]["updated"] += _to_int(getattr(r, "updated_count", 0))
        per_day[day_str]["missing"] += _to_int(getattr(r, "missing_count", 0))
        per_day[day_str]["unchanged"] += _to_int(getattr(r, "unchanged_count", 0))
        per_day[day_str]["error"] += _to_int(getattr(r, "error_count", 0))
        per_day[day_str]["total_seen"] += _to_int(getattr(r, "total_seen", 0))

    ordered = OrderedDict(sorted(per_day.items(), key=lambda kv: kv[0]))
    labels = list(ordered.keys())

    inserted: List[int] = []
    updated: List[int] = []
    missing: List[int] = []
    unchanged: List[int] = []
    error: List[int] = []
    total_seen: List[int] = []
    net_change: List[int] = []
    change_rate: List[float] = []
    net_rate: List[float] = []

    for d in labels:
        day = ordered[d]
        ins = _to_int(day.get("inserted"))
        upd = _to_int(day.get("updated"))
        miss = _to_int(day.get("missing"))
        unch = _to_int(day.get("unchanged"))
        err = _to_int(day.get("error"))
        tot = _to_int(day.get("total_seen"))

        inserted.append(ins)
        updated.append(upd)
        missing.append(miss)
        unchanged.append(unch)
        error.append(err)
        total_seen.append(tot)

        net = ins - miss
        net_change.append(net)

        if tot > 0:
            ch = (ins + upd + miss) / tot
            nr = net / tot
        else:
            ch = 0.0
            nr = 0.0

        change_rate.append(ch)
        net_rate.append(nr)

    change_rate_ma7 = _rolling_mean(change_rate, 7)
    net_rate_ma7 = _rolling_mean(net_rate, 7)

    def to_pct(xs: List[float]) -> List[float]:
        return [round(x * 100.0, 2) for x in xs]

    recent_runs: List[Dict[str, Any]] = []
    for r in sorted(
        runs,
        key=lambda run: (run.started_at is not None, run.started_at or datetime.min),
        reverse=True,
    ):
        started_at = getattr(r, "started_at", None)
        finished_at = getattr(r, "finished_at", None)
        recent_runs.append(
            {
                "id": r.id,
                "started_at": started_at.isoformat(sep=" ") if started_at else "",
                "finished_at": finished_at.isoformat(sep=" ") if finished_at else "",
                "user": getattr(r, "user", "") or "",
                "mode": getattr(r, "mode", "") or "",
                "total_seen": _to_int(getattr(r, "total_seen", 0)),
                "inserted": _to_int(getattr(r, "inserted_count", 0)),
                "updated": _to_int(getattr(r, "updated_count", 0)),
                "missing": _to_int(getattr(r, "missing_count", 0)),
                "unchanged": _to_int(getattr(r, "unchanged_count", 0)),
                "error": _to_int(getattr(r, "error_count", 0)),
                "status": _run_status(started_at, finished_at),
            }
        )

    return {
        "labels": labels,
        "inserted": inserted,
        "updated": updated,
        "missing": missing,
        "unchanged": unchanged,
        "error": error,
        "total_seen": total_seen,
        "net_change": net_change,
        "change_rate_pct": to_pct(change_rate),
        "net_rate_pct": to_pct(net_rate),
        "change_rate_ma7_pct": to_pct(change_rate_ma7),
        "net_rate_ma7_pct": to_pct(net_rate_ma7),
        "recent_runs": recent_runs,
    }


# ----------------------------------------------------------------------
# Data access: job index rows, optionally limited to recent discoveries.
# ----------------------------------------------------------------------
def fetch_jobs_last_hours(
    hours: int = 48,
    limit: int = 500,
    min_match: Optional[int] = None,
    location_policy: Optional[str] = None,
) -> Dict[str, Any]:
    all_time = hours <= 0
    hours = 0 if all_time else max(1, min(hours, 24 * 30))
    limit = max(1, min(limit, 5000))
    if min_match is not None:
        min_match = max(0, min(int(min_match), 100))
    location_policy = (location_policy or "any").strip().lower()
    if location_policy not in {
        "any",
        "remote",
        "hybrid",
        "onsite",
        "unknown",
        "non_hybrid",
    }:
        location_policy = "any"

    now = _now_local()
    cutoff = now - timedelta(hours=hours) if not all_time else None

    # MySQL DATETIME is typically stored/returned naive.
    # Compare using naive values to avoid tz mismatch issues.
    cutoff_q = cutoff.replace(tzinfo=None) if cutoff else None
    now_q = now.replace(tzinfo=None)

    # Pull a bit extra; still cheap with an index on discovery_date.
    prefetch = min(5000, max(limit * 5, 1000))

    with SessionLocal() as session:
        stmt = select(Job).where(Job.discovery_date.is_not(None))
        if cutoff_q is not None:
            stmt = stmt.where(Job.discovery_date >= cutoff_q)
        if min_match is not None:
            stmt = stmt.where(Job.ai_match_percentage.is_not(None)).where(
                Job.ai_match_percentage >= min_match
            )
        if location_policy == "non_hybrid":
            stmt = stmt.where(
                or_(
                    Job.ai_location_policy_match.is_(None),
                    func.lower(Job.ai_location_policy_match) != "hybrid",
                )
            )
        elif location_policy != "any":
            stmt = stmt.where(
                func.lower(Job.ai_location_policy_match) == location_policy
            )
        jobs = (
            session.execute(
                stmt.order_by(Job.discovery_date.desc(), Job.id.desc()).limit(prefetch)
            )
            .scalars()
            .all()
        )

    out: List[Dict[str, Any]] = []
    for j in jobs:
        dt = j.discovery_date
        if not dt:
            continue

        # Ensure naive math for age_hours if dt is naive (typical MySQL behavior)
        dt_q = dt.replace(tzinfo=None)

        ai_raw = (getattr(j, "ai_analysis", None) or "").strip()
        ai_obj = _safe_json_loads(ai_raw)
        ai_row = _job_ai_columns(j)

        job_changes = (
            session.execute(
                select(JobChange)
                .where(JobChange.job_id_text == j.job_id)
                .order_by(JobChange.created_at.desc())
            )
            .scalars()
            .all()
        )

        # Serialize job_changes to a list of dicts
        job_changes_list = [
            {
                "id": c.id,
                "change_type": getattr(c, "change_type", None),
                "change_source": getattr(c, "change_source", None) or "site",
                "created_at": (
                    c.created_at.isoformat(sep=" ")
                    if getattr(c, "created_at", None)
                    else None
                ),
                "changed_fields": getattr(c, "changed_fields", None),
            }
            for c in job_changes
        ]

        out.append(
            {
                "id": j.id,
                "site": (j.site or "").strip(),
                "job_id": (j.job_id or "").strip(),
                "title": (j.title or "").strip(),
                "level": (j.level or "").strip(),
                "is_active": bool(j.is_active),
                "pay": (j.pay or "").strip(),
                "url": (j.url or "").strip(),
                "discovery_date": dt_q.isoformat(sep=" "),
                "age_hours": round((now_q - dt_q).total_seconds() / 3600.0, 2),
                "ai": ai_obj,
                "ai_row": ai_row,
                "ai_raw": ai_raw if not ai_obj else "",
                "changes": job_changes_list,
            }
        )

        if len(out) >= limit:
            break

    return {
        "hours": hours,
        "min_match": min_match,
        "location_policy": location_policy,
        "cutoff_iso": cutoff_q.isoformat() if cutoff_q else "all",
        "returned": len(out),
        "jobs": out,
    }


def _fmt_dt(dt: Any) -> Optional[str]:
    if not dt:
        return None
    try:
        return dt.replace(tzinfo=None).isoformat(sep=" ")
    except Exception:
        try:
            return dt.isoformat(sep=" ")
        except Exception:
            return str(dt)


def _serialize_job_detail(j: Job, changes: List[JobChange]) -> Dict[str, Any]:
    ai_raw = (getattr(j, "ai_analysis", None) or "").strip()
    ai_obj = _safe_json_loads(ai_raw)
    ai_row = _job_ai_columns(j)
    return {
        "id": j.id,
        "site": (j.site or "").strip(),
        "job_id": (j.job_id or "").strip(),
        "title": (j.title or "").strip(),
        "url": (j.url or "").strip(),
        "desc": (j.desc or "").strip(),
        "keywords": (j.keywords or "").strip(),
        "level": (j.level or "").strip(),
        "pay": (j.pay or "").strip(),
        "reference_fields": _job_reference_fields(j),
        "discovery_date": _fmt_dt(j.discovery_date),
        "updated_at": _fmt_dt(j.updated_at),
        "is_active": bool(j.is_active),
        "content_hash": j.content_hash,
        "first_seen_run_id": j.first_seen_run_id,
        "last_seen_run_id": j.last_seen_run_id,
        "run_id": j.run_id,
        "ai": ai_obj,
        "ai_row": ai_row,
        "ai_raw": ai_raw if not ai_obj else "",
        "changes": [
            {
                "id": c.id,
                "site": getattr(c, "site", None),
                "change_type": getattr(c, "change_type", None),
                "change_source": getattr(c, "change_source", None) or "site",
                "created_at": _fmt_dt(getattr(c, "created_at", None)),
                "changed_fields": getattr(c, "changed_fields", None),
                "old_hash": getattr(c, "old_hash", None),
                "new_hash": getattr(c, "new_hash", None),
            }
            for c in changes
        ],
    }


def fetch_job_lookup(query: str, limit: int = 25) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"query": q, "returned": 0, "jobs": []}

    limit = max(1, min(limit, 50))
    q_norm = q.lower()
    like_q = f"%{q_norm}%"

    _ensure_reference_fields_column()
    with SessionLocal() as session:
        exact_jobs = (
            session.execute(
                select(Job)
                .where(func.lower(func.trim(Job.job_id)) == q_norm)
                .order_by(Job.discovery_date.desc(), Job.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        jobs = exact_jobs
        if not jobs:
            jobs = (
                session.execute(
                    select(Job)
                    .where(
                        or_(
                            func.lower(Job.job_id).like(like_q),
                            func.lower(Job.title).like(like_q),
                            func.lower(Job.site).like(like_q),
                        )
                    )
                    .order_by(Job.discovery_date.desc(), Job.id.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )

        out = []
        for job in jobs:
            changes = (
                session.execute(
                    select(JobChange)
                    .where(JobChange.job_id_text == job.job_id)
                    .where(JobChange.site == job.site)
                    .order_by(JobChange.created_at.desc(), JobChange.id.desc())
                    .limit(100)
                )
                .scalars()
                .all()
            )
            out.append(_serialize_job_detail(job, changes))

    return {"query": q, "returned": len(out), "exact": bool(exact_jobs), "jobs": out}


def fetch_job_detail_by_id(job_pk: int) -> Dict[str, Any]:
    job_pk = max(0, int(job_pk or 0))
    if not job_pk:
        return {"id": job_pk, "found": False, "job": None}

    _ensure_reference_fields_column()
    with SessionLocal() as session:
        job = session.get(Job, job_pk)
        if job is None:
            return {"id": job_pk, "found": False, "job": None}

        changes = (
            session.execute(
                select(JobChange)
                .where(JobChange.job_id_text == job.job_id)
                .where(JobChange.site == job.site)
                .order_by(JobChange.created_at.desc(), JobChange.id.desc())
                .limit(100)
            )
            .scalars()
            .all()
        )
        return {"id": job_pk, "found": True, "job": _serialize_job_detail(job, changes)}


# ----------------------------------------------------------------------
# Flask app + templates
# ----------------------------------------------------------------------
# Page markup lives in templates/ so dashboard.py stays focused on data and routes.
app = Flask(__name__)





# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/")
def index():
    # Keep route behavior unchanged while serving the extracted dashboard markup.
    return render_template("index.html")


@app.route("/swipe")
def swipe_page():
    # Swipe UI was moved out of the Python module for easier frontend edits.
    return render_template("swipe.html")


@app.route("/steps")
def steps_page():
    # Steps editor remains a normal Flask template backed by the same API routes.
    return render_template("steps.html")


@app.route("/api/swipe/jobs")
def swipe_jobs_api():
    try:
        return jsonify(fetch_swipe_jobs())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/swipe", methods=["POST"])
def swipe_api():
    data = request.get_json(silent=True) or {}
    job = data.get("job") or {}
    action = str(data.get("action") or "").strip().lower()
    if action not in {"like", "dislike"}:
        return jsonify(error="action must be like or dislike"), 400
    try:
        if not record_swipe(job, action):
            return jsonify(error="job not found in database"), 404
        return jsonify(success=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/steps")
def api_steps():
    try:
        return jsonify(_load_steps_editor_state())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/steps/preview", methods=["POST"])
def api_steps_preview():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(preview_steps_editor_content(data.get("content")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/steps/save", methods=["POST"])
def api_steps_save():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(save_steps_editor_content(data.get("content")))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/data")
def api_data():
    try:
        days = int(request.args.get("days", "30"))
        days = max(1, min(days, 365))
    except Exception:
        days = 30
    return jsonify(fetch_runs(days))


@app.route("/api/jobs")
def api_jobs():
    hours_raw = request.args.get("hours", "48").strip().lower()
    if hours_raw == "all":
        hours = 0
    else:
        try:
            hours = int(hours_raw)
        except Exception:
            hours = 48
    try:
        limit = int(request.args.get("limit", "500"))
    except Exception:
        limit = 500
    min_match_raw = request.args.get("min_match", "").strip()
    min_match: Optional[int] = None
    if min_match_raw:
        try:
            min_match = int(float(min_match_raw))
        except Exception:
            min_match = None
    location_policy = request.args.get("location_policy", "any")
    return jsonify(
        fetch_jobs_last_hours(
            hours=hours,
            limit=limit,
            min_match=min_match,
            location_policy=location_policy,
        )
    )


@app.route("/api/job-lookup")
def api_job_lookup():
    q = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "25"))
    except Exception:
        limit = 25
    return jsonify(fetch_job_lookup(q, limit=limit))


@app.route("/api/job-detail")
def api_job_detail():
    try:
        job_pk = int(request.args.get("id", "0"))
    except Exception:
        job_pk = 0
    return jsonify(fetch_job_detail_by_id(job_pk))


@app.route("/api/discovery/suggestions")
def api_discovery_suggestions():
    try:
        return jsonify(fetch_discovery_suggestions())
    except Exception as exc:
        return jsonify(error=str(exc), suggestions=[]), 500


@app.route("/api/discovery/events")
def api_discovery_events():
    try:
        limit = int(request.args.get("limit", "200"))
    except Exception:
        limit = 200
    try:
        return jsonify(fetch_discovery_events(limit=limit))
    except Exception as exc:
        return jsonify(error=str(exc), events=[]), 500


@app.route("/api/discovery/apply", methods=["POST"])
def api_discovery_apply():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify(error="ids must be a non-empty list"), 400
    try:
        result = merge_steps_suggestions(
            steps_path=DEFAULT_STEPS_PATH,
            suggestions_path=DEFAULT_SUGGESTIONS_PATH,
            selected_ids=[str(item) for item in ids],
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/discovery/reject", methods=["POST"])
def api_discovery_reject():
    data = request.get_json(silent=True) or {}
    ids = data.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify(error="ids must be a non-empty list"), 400
    try:
        return jsonify(update_discovery_suggestion_state([str(item) for item in ids], "rejected"))
    except Exception as exc:
        return jsonify(error=str(exc)), 500


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------
def main():
    host = os.getenv("DASH_HOST", "127.0.0.1")
    port = int(os.getenv("DASH_PORT", "5000"))
    # Do NOT print DATABASE_URL here (and remove it from app/models.py too).
    print(f"[dash] Serving on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    main()
