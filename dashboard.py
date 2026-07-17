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
import csv
import io
import threading
import time
import uuid
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, Response, jsonify, render_template, request
from sqlalchemy import (
    and_,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    or_,
    select,
    text as sa_text,
    tuple_,
)
from sqlalchemy.exc import IntegrityError

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


class DashboardQueryReport(Base):  # type: ignore
    __tablename__ = "dashboard_query_reports"
    __table_args__ = (
        UniqueConstraint("title", name="uq_dashboard_query_report_title"),
        {
            "extend_existing": True,
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(120), nullable=False)
    config_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
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


_QUERY_REPORT_TABLE_READY = False


def _ensure_query_report_table() -> None:
    global _QUERY_REPORT_TABLE_READY
    if _QUERY_REPORT_TABLE_READY:
        return
    DashboardQueryReport.__table__.create(bind=engine, checkfirst=True)
    _QUERY_REPORT_TABLE_READY = True


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
    return fetch_jobs_query(
        {
            "columns": [
                "id",
                "site",
                "job_id",
                "title",
                "level",
                "pay",
                "url",
                "discovery_date",
                "age_hours",
                "is_active",
                "ai_match_percentage",
                "ai_location_policy_match",
                "ai_salary",
                "latest_change_type",
                "latest_change_at",
            ],
            "limit": limit,
            "quick": {
                "hours": "all" if hours <= 0 else hours,
                "min_match": min_match,
                "location_policy": location_policy or "any",
                "text": "",
            },
            "filter": {"op": "and", "items": []},
        }
    )


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


DEFAULT_QUERY_COLUMNS = [
    "id",
    "site",
    "title",
    "level",
    "pay",
    "discovery_date",
    "is_active",
    "ai_match_percentage",
    "ai_location_policy_match",
    "ai_salary",
]

RAW_QUERY_COLUMNS = ["url", "desc", "keywords", "ai_raw", "ai_json", "changes_json"]
MAX_QUERY_LIMIT = 500
MAX_EXPORT_LIMIT = 5000
QUERY_JOB_DIR = os.getenv("DASH_QUERY_JOB_DIR", os.path.join(OUTPUT_DIR, "dashboard_query_jobs"))
QUERY_JOB_TTL_SEC = max(60, int(os.getenv("DASH_QUERY_JOB_TTL_SEC", "3600")))
QUERY_JOB_POLL_MS = max(250, int(os.getenv("DASH_QUERY_JOB_POLL_MS", "1000")))
_QUERY_JOB_THREADS: Dict[str, threading.Thread] = {}
_QUERY_JOB_THREADS_LOCK = threading.Lock()

STATIC_QUERY_COLUMNS: List[Dict[str, str]] = [
    {"key": "id", "label": "ID", "group": "Core"},
    {"key": "site", "label": "Site", "group": "Core"},
    {"key": "job_id", "label": "Job ID", "group": "Core"},
    {"key": "title", "label": "Title", "group": "Core"},
    {"key": "url", "label": "URL", "group": "Core"},
    {"key": "level", "label": "Level", "group": "Core"},
    {"key": "pay", "label": "Pay", "group": "Core"},
    {"key": "discovery_date", "label": "Discovery", "group": "Core"},
    {"key": "age_hours", "label": "Age Hours", "group": "Core"},
    {"key": "is_active", "label": "Active", "group": "Core"},
    {"key": "updated_at", "label": "Updated", "group": "Core"},
    {"key": "content_hash", "label": "Content Hash", "group": "Core"},
    {"key": "run_id", "label": "Run ID", "group": "Core"},
    {"key": "first_seen_run_id", "label": "First Seen Run", "group": "Core"},
    {"key": "last_seen_run_id", "label": "Last Seen Run", "group": "Core"},
    {"key": "ai_match_percentage", "label": "AI Match %", "group": "AI"},
    {"key": "ai_salary", "label": "AI Salary", "group": "AI"},
    {"key": "ai_fit_summary", "label": "AI Fit Summary", "group": "AI"},
    {"key": "ai_keywords_overlap", "label": "AI Keywords Overlap", "group": "AI"},
    {"key": "ai_missing_keywords", "label": "AI Missing Keywords", "group": "AI"},
    {"key": "ai_experience_match", "label": "AI Experience", "group": "AI"},
    {"key": "ai_location_policy_match", "label": "AI Location", "group": "AI"},
    {"key": "ai_analyzed_at", "label": "AI Analyzed", "group": "AI"},
    {"key": "latest_change_type", "label": "Latest Change", "group": "Changes"},
    {"key": "latest_change_source", "label": "Latest Change Source", "group": "Changes"},
    {"key": "latest_change_at", "label": "Latest Change At", "group": "Changes"},
    {"key": "latest_changed_fields", "label": "Latest Changed Fields", "group": "Changes"},
    {"key": "desc", "label": "Description", "group": "Raw/Long Text"},
    {"key": "keywords", "label": "Keywords", "group": "Raw/Long Text"},
    {"key": "ai_raw", "label": "Raw AI Text", "group": "Raw/Long Text"},
    {"key": "ai_json", "label": "AI JSON", "group": "Raw/Long Text"},
    {"key": "changes_json", "label": "Changes JSON", "group": "Raw/Long Text"},
]

STATIC_QUERY_COLUMN_MAP = {item["key"]: item for item in STATIC_QUERY_COLUMNS}
QUERY_OPERATORS = {
    "equals",
    "not_equals",
    "contains",
    "not_contains",
    "is_empty",
    "is_not_empty",
    "gte",
    "lte",
    "between",
}


def _query_column_label(key: str) -> str:
    if key.startswith("reference."):
        return key.split(".", 1)[1]
    return STATIC_QUERY_COLUMN_MAP.get(key, {}).get("label", key)


def _query_column_group(key: str) -> str:
    if key.startswith("reference."):
        return "Reference Fields"
    return STATIC_QUERY_COLUMN_MAP.get(key, {}).get("group", "Core")


def _normalize_selected_columns(columns: Any) -> List[str]:
    selected: List[str] = []
    for key in columns if isinstance(columns, list) else DEFAULT_QUERY_COLUMNS:
        key = str(key or "").strip()
        if not key:
            continue
        if key in STATIC_QUERY_COLUMN_MAP or key.startswith("reference."):
            if key not in selected:
                selected.append(key)
    return selected or list(DEFAULT_QUERY_COLUMNS)


def _query_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _query_to_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _query_to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _normalize_min_match(value: Any) -> Optional[int]:
    parsed = _query_to_int(value, 0)
    if parsed <= 0:
        return None
    return min(parsed, 100)


def _query_rule_matches(row: Dict[str, Any], rule: Dict[str, Any]) -> bool:
    field = str(rule.get("field") or "").strip()
    operator = str(rule.get("operator") or "").strip()
    if not _is_allowed_query_field(field):
        raise ValueError(f"unsupported query field: {field}")
    if operator not in QUERY_OPERATORS:
        raise ValueError(f"unsupported query operator: {operator}")

    actual = row.get(field)
    actual_text = _query_value_text(actual).lower()
    expected = rule.get("value")
    expected_text = _query_value_text(expected).lower()

    if operator == "is_empty":
        return actual_text.strip() == ""
    if operator == "is_not_empty":
        return actual_text.strip() != ""
    if operator == "contains":
        return expected_text in actual_text
    if operator == "not_contains":
        return expected_text not in actual_text
    if operator == "equals":
        return actual_text == expected_text
    if operator == "not_equals":
        return actual_text != expected_text

    actual_num = _query_to_number(actual)
    if actual_num is None:
        return False
    if operator == "gte":
        expected_num = _query_to_number(expected)
        return expected_num is not None and actual_num >= expected_num
    if operator == "lte":
        expected_num = _query_to_number(expected)
        return expected_num is not None and actual_num <= expected_num
    if operator == "between":
        bounds = expected if isinstance(expected, list) else []
        if len(bounds) != 2:
            return False
        low = _query_to_number(bounds[0])
        high = _query_to_number(bounds[1])
        return low is not None and high is not None and low <= actual_num <= high
    return False


def _query_group_matches(row: Dict[str, Any], group: Any) -> bool:
    if not isinstance(group, dict):
        return True
    items = group.get("items")
    if not isinstance(items, list) or not items:
        return True
    op = str(group.get("op") or "and").lower()
    if op not in {"and", "or"}:
        raise ValueError("query group op must be and or or")

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "items" in item:
            results.append(_query_group_matches(row, item))
        else:
            results.append(_query_rule_matches(row, item))
    if not results:
        return True
    return any(results) if op == "or" else all(results)


def _query_group_has_rules(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    items = group.get("items")
    if not isinstance(items, list):
        return False
    for item in items:
        if not isinstance(item, dict):
            continue
        if "items" in item:
            if _query_group_has_rules(item):
                return True
            continue
        if str(item.get("field") or "").strip():
            return True
    return False


def _query_group_fields(group: Any) -> set[str]:
    fields: set[str] = set()
    if not isinstance(group, dict):
        return fields
    items = group.get("items")
    if not isinstance(items, list):
        return fields
    for item in items:
        if not isinstance(item, dict):
            continue
        if "items" in item:
            fields.update(_query_group_fields(item))
            continue
        field = str(item.get("field") or "").strip()
        if field:
            fields.add(field)
    return fields


def _validate_query_group(group: Any) -> None:
    if not isinstance(group, dict):
        return
    op = str(group.get("op") or "and").lower()
    if op not in {"and", "or"}:
        raise ValueError("query group op must be and or or")
    items = group.get("items")
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if "items" in item:
            _validate_query_group(item)
            continue
        field = str(item.get("field") or "").strip()
        operator = str(item.get("operator") or "").strip()
        if not _is_allowed_query_field(field):
            raise ValueError(f"unsupported query field: {field}")
        if operator not in QUERY_OPERATORS:
            raise ValueError(f"unsupported query operator: {operator}")


def _is_allowed_query_field(field: str) -> bool:
    return field in STATIC_QUERY_COLUMN_MAP or field.startswith("reference.")


def _job_reference_map(j: Job) -> Dict[str, str]:
    return {item["label"]: item["value"] for item in _job_reference_fields(j)}


def _changes_by_job(session: Any, jobs: List[Job], per_job_limit: int = 100) -> Dict[tuple[str, str], List[JobChange]]:
    pairs = [((job.site or ""), (job.job_id or "")) for job in jobs]
    pairs = [(site, job_id) for site, job_id in pairs if site and job_id]
    if not pairs:
        return {}

    job_pks = [job.id for job in jobs if job.id is not None]
    valid_pairs = set(pairs)
    conditions = [tuple_(JobChange.site, JobChange.job_id_text).in_(pairs)]
    if job_pks:
        conditions.append(JobChange.job_pk.in_(job_pks))

    changes = (
        session.execute(
            select(JobChange)
            .where(or_(*conditions))
            .order_by(JobChange.created_at.desc(), JobChange.id.desc())
        )
        .scalars()
        .all()
    )

    grouped: Dict[tuple[str, str], List[JobChange]] = defaultdict(list)
    for change in changes:
        key = ((change.site or ""), (change.job_id_text or ""))
        if key not in valid_pairs:
            continue
        bucket = grouped[key]
        if len(bucket) < per_job_limit:
            bucket.append(change)
    return grouped


def _query_needs_changes(columns: List[str], filter_group: Any) -> bool:
    fields = set(columns)
    fields.update(_query_group_fields(filter_group))
    return any(field.startswith("latest_") or field == "changes_json" for field in fields)


def _serialize_query_row(
    j: Job,
    changes: List[JobChange],
    now_q: datetime,
) -> Dict[str, Any]:
    dt = j.discovery_date
    dt_q = dt.replace(tzinfo=None) if dt else None
    ai_raw = (getattr(j, "ai_analysis", None) or "").strip()
    ai_obj = _safe_json_loads(ai_raw)
    refs = _job_reference_map(j)
    latest = changes[0] if changes else None

    row: Dict[str, Any] = {
        "id": j.id,
        "site": (j.site or "").strip(),
        "job_id": (j.job_id or "").strip(),
        "title": (j.title or "").strip(),
        "url": (j.url or "").strip(),
        "desc": (j.desc or "").strip(),
        "keywords": (j.keywords or "").strip(),
        "level": (j.level or "").strip(),
        "pay": (j.pay or "").strip(),
        "discovery_date": dt_q.isoformat(sep=" ") if dt_q else "",
        "age_hours": round((now_q - dt_q).total_seconds() / 3600.0, 2) if dt_q else "",
        "is_active": bool(j.is_active),
        "updated_at": _fmt_dt(j.updated_at) or "",
        "content_hash": j.content_hash or "",
        "run_id": j.run_id,
        "first_seen_run_id": j.first_seen_run_id,
        "last_seen_run_id": j.last_seen_run_id,
        "ai_match_percentage": j.ai_match_percentage,
        "ai_salary": j.ai_salary or "",
        "ai_fit_summary": j.ai_fit_summary or "",
        "ai_keywords_overlap": _safe_json_list(j.ai_keywords_overlap),
        "ai_missing_keywords": _safe_json_list(j.ai_missing_keywords),
        "ai_experience_match": j.ai_experience_match or "",
        "ai_location_policy_match": j.ai_location_policy_match or "",
        "ai_analyzed_at": _fmt_dt(j.ai_analyzed_at) or "",
        "ai_raw": ai_raw if not ai_obj else "",
        "ai_json": ai_obj or "",
        "latest_change_type": getattr(latest, "change_type", "") if latest else "",
        "latest_change_source": (getattr(latest, "change_source", None) or "site") if latest else "",
        "latest_change_at": _fmt_dt(getattr(latest, "created_at", None)) if latest else "",
        "latest_changed_fields": getattr(latest, "changed_fields", "") if latest else "",
        "changes_json": [
            {
                "id": c.id,
                "change_type": getattr(c, "change_type", None),
                "change_source": getattr(c, "change_source", None) or "site",
                "created_at": _fmt_dt(getattr(c, "created_at", None)),
                "changed_fields": getattr(c, "changed_fields", None),
            }
            for c in changes
        ],
    }
    for label, value in refs.items():
        row[f"reference.{label}"] = value
    return row


def _extract_query_payload(data: Optional[Dict[str, Any]], export: bool = False) -> Dict[str, Any]:
    payload = data or {}
    quick = payload.get("quick") if isinstance(payload.get("quick"), dict) else {}
    hours_raw = quick.get("hours", payload.get("hours", "48"))
    hours = 0 if str(hours_raw).strip().lower() == "all" else _query_to_int(hours_raw, 48)
    limit_default = MAX_EXPORT_LIMIT if export else MAX_QUERY_LIMIT
    limit_max = MAX_EXPORT_LIMIT if export else MAX_QUERY_LIMIT
    limit = max(1, min(_query_to_int(payload.get("limit", quick.get("limit", limit_default)), limit_default), limit_max))
    min_match_raw = quick.get("min_match", payload.get("min_match"))
    min_match = None if min_match_raw in {None, ""} else _normalize_min_match(min_match_raw)
    location_policy = str(quick.get("location_policy", payload.get("location_policy", "any")) or "any").strip().lower()
    if location_policy not in {"any", "remote", "hybrid", "onsite", "unknown", "non_hybrid"}:
        location_policy = "any"
    text_query = str(quick.get("text", payload.get("text", "")) or "").strip()
    return {
        "hours": hours,
        "limit": limit,
        "min_match": min_match,
        "location_policy": location_policy,
        "text": text_query,
        "columns": _normalize_selected_columns(payload.get("columns")),
        "filter": payload.get("filter") if isinstance(payload.get("filter"), dict) else {"op": "and", "items": []},
    }


def _query_jobs(payload: Optional[Dict[str, Any]], export: bool = False) -> Dict[str, Any]:
    query = _extract_query_payload(payload, export=export)
    columns = _normalize_selected_columns(query["columns"])
    has_row_filter = _query_group_has_rules(query["filter"])
    needs_changes = _query_needs_changes(columns, query["filter"])
    db_limit = MAX_EXPORT_LIMIT if has_row_filter else query["limit"]
    all_time = query["hours"] <= 0
    hours = 0 if all_time else max(1, min(query["hours"], 24 * 30))
    now = _now_local()
    now_q = now.replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours) if not all_time else None
    cutoff_q = cutoff.replace(tzinfo=None) if cutoff else None

    _ensure_reference_fields_column()
    _validate_query_group(query["filter"])
    with SessionLocal() as session:
        stmt = select(Job).where(Job.discovery_date.is_not(None))
        if cutoff_q is not None:
            stmt = stmt.where(Job.discovery_date >= cutoff_q)
        if query["min_match"] is not None:
            stmt = stmt.where(Job.ai_match_percentage.is_not(None)).where(
                Job.ai_match_percentage >= query["min_match"]
            )
        if query["location_policy"] == "non_hybrid":
            stmt = stmt.where(
                or_(
                    Job.ai_location_policy_match.is_(None),
                    func.lower(Job.ai_location_policy_match) != "hybrid",
                )
            )
        elif query["location_policy"] != "any":
            stmt = stmt.where(func.lower(Job.ai_location_policy_match) == query["location_policy"])
        if query["text"]:
            like_q = f"%{query['text'].lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Job.site).like(like_q),
                    func.lower(Job.job_id).like(like_q),
                    func.lower(Job.title).like(like_q),
                    func.lower(Job.level).like(like_q),
                    func.lower(Job.pay).like(like_q),
                )
            )

        jobs = (
            session.execute(
                stmt.order_by(Job.discovery_date.desc(), Job.id.desc()).limit(db_limit)
            )
            .scalars()
            .all()
        )

        rows: List[Dict[str, Any]] = []
        reference_keys = set()
        changes_by_job = _changes_by_job(session, jobs) if needs_changes else {}
        for job in jobs:
            changes = changes_by_job.get(((job.site or ""), (job.job_id or "")), [])
            row = _serialize_query_row(job, changes, now_q)
            reference_keys.update(key for key in row if key.startswith("reference."))
            if not _query_group_matches(row, query["filter"]):
                continue
            rows.append(row)
            if len(rows) >= query["limit"]:
                break

    selected_rows = [
        {key: row.get(key, "") for key in columns}
        for row in rows
    ]
    available_columns = get_query_columns(sorted(reference_keys))
    return {
        "hours": hours,
        "limit": query["limit"],
        "min_match": query["min_match"],
        "location_policy": query["location_policy"],
        "text": query["text"],
        "cutoff_iso": cutoff_q.isoformat() if cutoff_q else "all",
        "returned": len(selected_rows),
        "columns": columns,
        "available_columns": available_columns,
        "jobs": selected_rows,
    }


def get_query_columns(reference_keys: Optional[List[str]] = None) -> List[Dict[str, str]]:
    out = [dict(item) for item in STATIC_QUERY_COLUMNS]
    for key in reference_keys or []:
        if key.startswith("reference."):
            out.append({"key": key, "label": _query_column_label(key), "group": "Reference Fields"})
    return out


def fetch_query_columns() -> Dict[str, Any]:
    _ensure_reference_fields_column()
    reference_keys = set()
    with SessionLocal() as session:
        jobs = (
            session.execute(
                select(Job.reference_fields)
                .where(Job.reference_fields.is_not(None))
                .order_by(Job.discovery_date.desc(), Job.id.desc())
                .limit(1000)
            )
            .scalars()
            .all()
        )
    for raw in jobs:
        refs = _safe_json_loads(raw)
        if isinstance(refs, dict):
            for key, value in refs.items():
                if value not in {None, ""}:
                    reference_keys.add(f"reference.{key}")
    return {
        "columns": get_query_columns(sorted(reference_keys)),
        "default_columns": DEFAULT_QUERY_COLUMNS,
        "raw_columns": RAW_QUERY_COLUMNS,
        "operators": sorted(QUERY_OPERATORS),
    }


def _format_export_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def export_query_jobs(payload: Optional[Dict[str, Any]], fmt: str) -> Response:
    data = _query_jobs(payload, export=True)
    rows = data["jobs"]
    columns = data["columns"]
    if fmt == "json":
        body = json.dumps({"columns": columns, "rows": rows, "returned": data["returned"]}, ensure_ascii=False, indent=2)
        return Response(body + "\n", mimetype="application/json")
    if fmt != "csv":
        raise ValueError("format must be csv or json")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_query_column_label(key) for key in columns])
    for row in rows:
        writer.writerow([_format_export_value(row.get(key, "")) for key in columns])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=query-builder-export.csv"},
    )


def fetch_jobs_query(payload: Optional[Dict[str, Any]], export: bool = False) -> Dict[str, Any]:
    return _query_jobs(payload, export=export)


def _query_job_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _query_job_path(query_id: str) -> str:
    safe_id = re.sub(r"[^a-f0-9]", "", str(query_id or "").lower())
    if not safe_id or safe_id != query_id:
        raise ValueError("invalid query job id")
    return os.path.join(QUERY_JOB_DIR, f"{safe_id}.json")


def _write_query_job_state(query_id: str, state: Dict[str, Any]) -> None:
    path = _query_job_path(query_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_json(path, state)


def _read_query_job_state(query_id: str) -> Optional[Dict[str, Any]]:
    try:
        data = read_json_file(_query_job_path(query_id), None)
    except ValueError:
        raise
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _cleanup_query_jobs() -> None:
    try:
        os.makedirs(QUERY_JOB_DIR, exist_ok=True)
        now = time.time()
        for name in os.listdir(QUERY_JOB_DIR):
            if not name.endswith(".json"):
                continue
            path = os.path.join(QUERY_JOB_DIR, name)
            try:
                if now - os.path.getmtime(path) > QUERY_JOB_TTL_SEC:
                    os.remove(path)
            except OSError:
                continue
    except OSError:
        return


def _run_query_job(query_id: str, payload: Dict[str, Any]) -> None:
    started_state = _read_query_job_state(query_id) or {}
    created_at = started_state.get("created_at") or _query_job_now_iso()
    try:
        result = fetch_jobs_query(payload)
        _write_query_job_state(
            query_id,
            {
                "query_id": query_id,
                "status": "complete",
                "created_at": created_at,
                "completed_at": _query_job_now_iso(),
                "message": "Query complete.",
                "result": result,
            },
        )
    except ValueError as exc:
        _write_query_job_state(
            query_id,
            {
                "query_id": query_id,
                "status": "failed",
                "http_status": 400,
                "created_at": created_at,
                "error": str(exc),
                "completed_at": _query_job_now_iso(),
                "message": "Query failed.",
            },
        )
    except Exception as exc:
        _write_query_job_state(
            query_id,
            {
                "query_id": query_id,
                "status": "failed",
                "http_status": 500,
                "created_at": created_at,
                "error": str(exc),
                "completed_at": _query_job_now_iso(),
                "message": "Query failed.",
            },
        )
    finally:
        with _QUERY_JOB_THREADS_LOCK:
            _QUERY_JOB_THREADS.pop(query_id, None)


def start_query_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    _cleanup_query_jobs()
    query_id = uuid.uuid4().hex
    state = {
        "query_id": query_id,
        "status": "running",
        "created_at": _query_job_now_iso(),
        "created_at_epoch": time.time(),
        "poll_after_ms": QUERY_JOB_POLL_MS,
        "ttl_seconds": QUERY_JOB_TTL_SEC,
        "message": "Query queued.",
    }
    _write_query_job_state(query_id, state)
    thread = threading.Thread(target=_run_query_job, args=(query_id, payload), daemon=True)
    with _QUERY_JOB_THREADS_LOCK:
        _QUERY_JOB_THREADS[query_id] = thread
    thread.start()
    return state


def fetch_query_job_state(query_id: str) -> tuple[Dict[str, Any], int]:
    state = _read_query_job_state(query_id)
    if state is None:
        return {
            "query_id": query_id,
            "status": "not_found",
            "error": "query job not found",
            "message": "Query job was not found.",
        }, 404
    if state.get("status") == "running":
        created = float(state.get("created_at_epoch") or time.time())
        age_seconds = max(0, int(time.time() - created))
        if age_seconds > QUERY_JOB_TTL_SEC:
            failed = {
                "query_id": query_id,
                "status": "failed",
                "http_status": 504,
                "created_at": state.get("created_at") or "",
                "error": f"query job expired after {QUERY_JOB_TTL_SEC} seconds",
                "completed_at": _query_job_now_iso(),
                "message": "Query expired.",
            }
            _write_query_job_state(query_id, failed)
            return failed, 504
        return {
            "query_id": query_id,
            "status": "running",
            "created_at": state.get("created_at") or "",
            "age_seconds": age_seconds,
            "poll_after_ms": QUERY_JOB_POLL_MS,
            "ttl_seconds": QUERY_JOB_TTL_SEC,
            "message": f"Query running for {age_seconds}s.",
        }, 202
    if state.get("status") == "failed":
        return {
            "query_id": query_id,
            "status": "failed",
            "created_at": state.get("created_at") or "",
            "completed_at": state.get("completed_at") or "",
            "error": state.get("error") or "query failed",
            "message": state.get("message") or "Query failed.",
        }, int(state.get("http_status") or 500)
    if state.get("status") == "complete":
        result = state.get("result") if isinstance(state.get("result"), dict) else {}
        result = dict(result)
        result["query_id"] = query_id
        result["status"] = "complete"
        result["created_at"] = state.get("created_at") or ""
        result["completed_at"] = state.get("completed_at") or ""
        result["message"] = state.get("message") or "Query complete."
        return result, 200
    return {"error": "query job state is invalid"}, 500


def fetch_query_reports() -> Dict[str, Any]:
    _ensure_query_report_table()
    with SessionLocal() as session:
        reports = (
            session.execute(select(DashboardQueryReport).order_by(DashboardQueryReport.title.asc()))
            .scalars()
            .all()
        )
        return {
            "reports": [
                {
                    "id": report.id,
                    "title": report.title,
                    "config": _safe_json_loads(report.config_json) or {},
                    "created_at": _fmt_dt(report.created_at),
                    "updated_at": _fmt_dt(report.updated_at),
                }
                for report in reports
            ]
        }


def save_query_report(data: Dict[str, Any], report_id: Optional[int] = None) -> Dict[str, Any]:
    """Create, update, or rename a saved Query Builder configuration."""
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    if len(title) > 120:
        raise ValueError("title must be 120 characters or fewer")
    config = data.get("config")
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    # Save-as uses create_only so a duplicate title cannot silently overwrite
    # another report. Existing POST callers retain the original upsert behavior.
    create_only = data.get("create_only") is True

    _ensure_query_report_table()
    with SessionLocal() as session:
        if report_id:
            # PUT targets one known report and must never steal another title.
            report = session.get(DashboardQueryReport, report_id)
            if report is None:
                raise ValueError("report not found")
            duplicate = (
                session.execute(
                    select(DashboardQueryReport)
                    .where(DashboardQueryReport.title == title)
                    .where(DashboardQueryReport.id != report_id)
                    .limit(1)
                )
                .scalars()
                .first()
            )
            if duplicate is not None:
                raise ValueError("report title already exists")
        else:
            # Treat "Save" on an existing title as a replace/update so changing
            # filters or column order does not hit the unique title constraint.
            report = (
                session.execute(
                    select(DashboardQueryReport)
                    .where(DashboardQueryReport.title == title)
                    .limit(1)
                )
                .scalars()
                .first()
            )
            # The UI uses this stricter mode for "Save as new". Legacy POST
            # callers omit the flag and keep the original upsert behavior.
            if report is not None and create_only:
                raise ValueError("report title already exists")
        if report is None:
            # New rows start with valid JSON before receiving the submitted config.
            report = DashboardQueryReport(title=title, config_json="{}")
            session.add(report)
        report.title = title
        report.config_json = json.dumps(config, ensure_ascii=False, indent=2)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise ValueError("report title already exists") from exc
        session.refresh(report)
        return {
            "id": report.id,
            "title": report.title,
            "config": _safe_json_loads(report.config_json) or {},
            "created_at": _fmt_dt(report.created_at),
            "updated_at": _fmt_dt(report.updated_at),
        }


def delete_query_report(report_id: int) -> Dict[str, Any]:
    _ensure_query_report_table()
    with SessionLocal() as session:
        report = session.get(DashboardQueryReport, max(0, int(report_id or 0)))
        if report is None:
            return {"deleted": False}
        session.delete(report)
        session.commit()
        return {"deleted": True}


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


@app.route("/api/jobs/query", methods=["POST"])
def api_jobs_query():
    data = request.get_json(silent=True) or {}
    background = str(request.args.get("background", "")).strip().lower() in {"1", "true", "yes"}
    if background:
        try:
            state = start_query_job(data)
            return jsonify(state), 202
        except Exception as exc:
            return jsonify(error=str(exc)), 500
    try:
        return jsonify(fetch_jobs_query(data))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/jobs/query/<query_id>")
def api_jobs_query_status(query_id: str):
    try:
        data, status = fetch_query_job_state(query_id)
        return jsonify(data), status
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/jobs/columns")
def api_jobs_columns():
    try:
        return jsonify(fetch_query_columns())
    except Exception as exc:
        return jsonify(error=str(exc), columns=[]), 500


@app.route("/api/jobs/export", methods=["POST"])
def api_jobs_export():
    fmt = (request.args.get("format", "csv") or "csv").strip().lower()
    data = request.get_json(silent=True) or {}
    try:
        return export_query_jobs(data, fmt)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/query-reports", methods=["GET", "POST"])
def api_query_reports():
    if request.method == "GET":
        try:
            return jsonify(fetch_query_reports())
        except Exception as exc:
            return jsonify(error=str(exc), reports=[]), 500

    data = request.get_json(silent=True) or {}
    try:
        return jsonify(save_query_report(data))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/query-reports/<int:report_id>", methods=["PUT", "DELETE"])
def api_query_report_detail(report_id: int):
    if request.method == "DELETE":
        try:
            return jsonify(delete_query_report(report_id))
        except Exception as exc:
            return jsonify(error=str(exc)), 500

    data = request.get_json(silent=True) or {}
    try:
        return jsonify(save_query_report(data, report_id=report_id))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


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
