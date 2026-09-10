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
import hashlib
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import math
from typing import Any, Dict, List, Optional

from flask import Flask, Response, abort, jsonify, render_template, request
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
    APPLICATION_PREP_DEFAULT_MIN_MATCH,
    ApplicationPrepSettings,
    ResponsibilitiesInventory,
    Base,
    JobChange,
    JobFitBrief,
    JobApplicationPrep,
    SessionLocal,
    IntegrationRun,
    Job,
    engine,
    ensure_job_reference_fields_column,
    ensure_job_compensation_columns,
    ensure_job_fit_briefs_table,
    ensure_job_application_preps_table,
    ensure_application_prep_settings_table,
    ensure_responsibilities_inventory_table,
)
from app.db import resolve_display_timezone, utc_now_naive

OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DEFAULT_EVENTS_PATH = os.path.join(OUTPUT_DIR, "job_board_discovery_events.jsonl")
DEFAULT_STEPS_PATH = os.path.join(BASE_DIR, "steps.json")
DEFAULT_SUGGESTIONS_PATH = os.path.join(OUTPUT_DIR, "steps_suggestions.json")
RESUME_PATH = os.getenv("RESUME_PATH", os.path.join(BASE_DIR, "resume.txt"))
DASH_FIT_BRIEF_MIN_MATCH = int(os.getenv("AI_FIT_BRIEF_MIN_MATCH", "75"))
APPLICATION_PREP_SCHEMA_VERSION = 2
APPLICATION_PREP_INVENTORY_MAX_TOKENS = max(
    0, int(os.getenv("APPLICATION_PREP_INVENTORY_MAX_TOKENS", "1200"))
)


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
            DateTime(timezone=False), server_default=func.now(), nullable=False
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
    created_at = Column(DateTime(timezone=False), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=False),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
DEFAULT_LOCAL_TZ = "America/Chicago"


def _get_local_tz_name() -> str:
    configured = (resolve_display_timezone(BASE_DIR, default=DEFAULT_LOCAL_TZ) or "").strip()
    if not configured:
        return DEFAULT_LOCAL_TZ
    if not ZoneInfo:
        return DEFAULT_LOCAL_TZ
    try:
        ZoneInfo(configured)
    except Exception:
        return DEFAULT_LOCAL_TZ
    return configured


def _now_local() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(_get_local_tz_name()))
    return datetime.now()


def _as_utc(dt: datetime) -> datetime:
    """Interpret naive database DATETIME values as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _as_local(dt: datetime) -> datetime:
    """Convert a stored UTC value to the dashboard's display timezone."""
    target = ZoneInfo(_get_local_tz_name()) if ZoneInfo else timezone.utc
    return _as_utc(dt).astimezone(target)


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

    age = utc_now_naive() - _as_utc(started_at).replace(tzinfo=None)
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


def _change_fields(change: JobChange) -> List[str]:
    raw = getattr(change, "changed_fields", None)
    if not raw:
        return []
    parsed = _safe_json_loads(raw)
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _diff_lines(before: Any, after: Any) -> List[Dict[str, str]]:
    before_lines = str(before or "").splitlines()
    after_lines = str(after or "").splitlines()
    out: List[Dict[str, str]] = []
    for line in difflib.unified_diff(before_lines, after_lines, lineterm=""):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            out.append({"type": "hunk", "text": line})
        elif line.startswith("-"):
            out.append({"type": "remove", "text": line[1:]})
        elif line.startswith("+"):
            out.append({"type": "add", "text": line[1:]})
        else:
            out.append({"type": "context", "text": line[1:] if line.startswith(" ") else line})
    return out


def _serialize_change(change: JobChange) -> Dict[str, Any]:
    fields = _change_fields(change)
    details = _safe_json_loads(getattr(change, "change_details", None))
    diffs: List[Dict[str, Any]] = []
    if isinstance(details, dict):
        before = details.get("before") if isinstance(details.get("before"), dict) else {}
        after = details.get("after") if isinstance(details.get("after"), dict) else {}
        detail_fields = details.get("fields") if isinstance(details.get("fields"), list) else fields
        fields = [str(item).strip() for item in detail_fields if str(item).strip()] or fields
        for field in fields:
            lines = _diff_lines(before.get(field, ""), after.get(field, ""))
            if lines:
                diffs.append({"field": field, "lines": lines})
    return {
        "id": change.id,
        "change_type": getattr(change, "change_type", None),
        "change_source": getattr(change, "change_source", None) or "site",
        "created_at": _fmt_dt(getattr(change, "created_at", None)),
        "changed_fields": getattr(change, "changed_fields", None),
        "fields": fields,
        "has_detail": bool(diffs),
        "diffs": diffs,
    }


_REFERENCE_FIELDS_COLUMN_READY = False


def _ensure_reference_fields_column() -> None:
    global _REFERENCE_FIELDS_COLUMN_READY
    if _REFERENCE_FIELDS_COLUMN_READY:
        return
    # Dashboard reads can happen before run.py initdb, so apply this one
    # additive column migration against whichever SessionLocal the app uses.
    with SessionLocal() as session:
        bind = session.get_bind()
        ensure_job_reference_fields_column(bind)
        ensure_job_compensation_columns(bind)
        ensure_job_fit_briefs_table(bind)
        ensure_job_application_preps_table(bind)
        ensure_application_prep_settings_table(bind)
        ensure_responsibilities_inventory_table(bind)
    _REFERENCE_FIELDS_COLUMN_READY = True


_APP_PREP_WORKER_THREAD: Optional[threading.Thread] = None
_APP_PREP_WORKER_LOCK = threading.Lock()


def _start_application_prep_worker() -> None:
    """Start one best-effort dashboard worker for newly queued prep jobs."""
    global _APP_PREP_WORKER_THREAD
    with _APP_PREP_WORKER_LOCK:
        if _APP_PREP_WORKER_THREAD and _APP_PREP_WORKER_THREAD.is_alive():
            return

        def _run() -> None:
            try:
                import analyze_jobs_ollama as analyzer

                analyzer.run_application_prep_generation(preflight=True)
            except SystemExit:
                return
            except Exception:
                # The analyzer writes its own detailed log; keep request handling isolated.
                return

        _APP_PREP_WORKER_THREAD = threading.Thread(
            target=_run,
            name="application-prep-worker",
            daemon=True,
        )
        _APP_PREP_WORKER_THREAD.start()


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
        rf"\bAs\s+an?\b[^.!?]*?{end}",
        rf"\bThe\s+ideal\s+candidate\b[^.!?]*?{end}",
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


def _simple_date(dt: Any) -> str:
    if not dt:
        return ""
    try:
        return _as_local(dt).strftime("%b %d, %Y").replace(" 0", " ")
    except Exception:
        return str(dt)


def _swipe_company_name(job: Job) -> str:
    refs = _job_reference_fields(job)
    preferred = {
        "company",
        "companyname",
        "employer",
        "employername",
        "organization",
        "organizationname",
    }
    for item in refs:
        label = re.sub(r"[^a-z0-9]", "", str(item.get("label") or "").lower())
        value = str(item.get("value") or "").strip()
        if label in preferred and value:
            return value
    return job.site or ""


def _swipe_change_summary(changes: List[JobChange], limit: int = 4) -> str:
    if not changes:
        return "No changes recorded."
    parts: List[str] = []
    for change in changes[:limit]:
        fields = _change_fields(change)
        fields_label = ", ".join(fields[:3]) if fields else "job details"
        if len(fields) > 3:
            fields_label += f", +{len(fields) - 3} more"
        change_type = str(getattr(change, "change_type", None) or "changed")
        source = str(getattr(change, "change_source", None) or "site")
        when = _simple_date(getattr(change, "created_at", None)) or "unknown date"
        parts.append(f"{when}: {source} {change_type} changed {fields_label}")
    if len(changes) > limit:
        parts.append(f"+{len(changes) - limit} older change(s)")
    return "; ".join(parts)


def _swipe_change_items(changes: List[JobChange], limit: int = 5) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for change in changes[:limit]:
        fields = _change_fields(change)
        fields_label = ", ".join(fields[:4]) if fields else "job details"
        if len(fields) > 4:
            fields_label += f", +{len(fields) - 4} more"
        items.append(
            {
                "date": _simple_date(getattr(change, "created_at", None)) or "Unknown date",
                "source": str(getattr(change, "change_source", None) or "site"),
                "type": str(getattr(change, "change_type", None) or "changed"),
                "summary": fields_label,
            }
        )
    return items


def _serialize_swipe_job(job: Job, changes: Optional[List[JobChange]] = None) -> Dict[str, Any]:
    changes = changes or []
    swipe = getattr(job, "swipe", None)
    return {
        "id": job.id,
        "JobID": job.job_id or "",
        "Site": job.site or "",
        "Company": _swipe_company_name(job),
        "JobTitle": job.title or "",
        "JobUrl": job.url or "",
        "JobDesc": job.desc or "",
        "JobDescHighlighted": highlight_as_you_will(job.title or "", job.desc or ""),
        "AIKeywordsOverlap": _safe_json_list(job.ai_keywords_overlap),
        "AIMissingKeywords": _safe_json_list(job.ai_missing_keywords),
        "JobLevel": job.level or "Unknown",
        "JobPay": job.pay or "",
        "DiscoveryDate": _fmt_dt(job.discovery_date) or "",
        "DiscoveryDateSimple": _simple_date(job.discovery_date),
        "ChangeSummary": _swipe_change_summary(changes),
        "Changes": _swipe_change_items(changes),
        "ChangeCount": len(changes),
        "AIMatchPercentage": job.ai_match_percentage,
        "AILocationPolicyMatch": job.ai_location_policy_match or "",
        "SwipeAction": getattr(swipe, "action", None) or "",
    }


def fetch_swipe_jobs(search_query: str = "") -> List[Dict[str, Any]]:
    _ensure_reference_fields_column()
    _ensure_swipe_table()
    terms = [part.lower() for part in re.split(r"\s+", (search_query or "").strip()) if part.strip()]
    with SessionLocal() as session:
        stmt = (
            select(Job)
            .outerjoin(JobSwipe, JobSwipe.job_pk == Job.id)
            .where(Job.is_active.is_(True))
        )
        if terms:
            # Search treats "interesting" as saved-for-later, so those jobs stay findable.
            stmt = stmt.where(or_(JobSwipe.id.is_(None), JobSwipe.action == "interesting"))
        else:
            stmt = stmt.where(JobSwipe.id.is_(None))
        for term in terms[:6]:
            like_q = f"%{term}%"
            stmt = stmt.where(
                or_(
                    func.lower(Job.job_id).like(like_q),
                    func.lower(Job.site).like(like_q),
                    func.lower(Job.title).like(like_q),
                    func.lower(Job.url).like(like_q),
                    func.lower(Job.desc).like(like_q),
                    func.lower(Job.keywords).like(like_q),
                    func.lower(Job.pay).like(like_q),
                    func.lower(Job.level).like(like_q),
                    func.lower(Job.reference_fields).like(like_q),
                    func.lower(Job.ai_fit_summary).like(like_q),
                    func.lower(Job.ai_keywords_overlap).like(like_q),
                    func.lower(Job.ai_missing_keywords).like(like_q),
                    func.lower(Job.ai_location_policy_match).like(like_q),
                )
            )
        stmt = stmt.order_by(Job.discovery_date.desc(), Job.id.desc()).limit(500)
        jobs = (
            session.execute(stmt)
            .scalars()
            .all()
        )
        changes_by_job = _changes_by_job(session, jobs, per_job_limit=20)
        return [
            _serialize_swipe_job(
                job,
                changes_by_job.get(((job.site or ""), (job.job_id or "")), []),
            )
            for job in jobs
        ]


def fetch_interesting_jobs() -> Dict[str, Any]:
    _ensure_reference_fields_column()
    _ensure_swipe_table()
    with SessionLocal() as session:
        jobs = (
            session.execute(
                select(Job)
                .join(JobSwipe, JobSwipe.job_pk == Job.id)
                .where(JobSwipe.action == "interesting")
                .order_by(JobSwipe.created_at.desc(), Job.discovery_date.desc(), Job.id.desc())
            )
            .scalars()
            .all()
        )
        changes_by_job = _changes_by_job(session, jobs, per_job_limit=20)
        rows = [
            _serialize_swipe_job(
                job,
                changes_by_job.get(((job.site or ""), (job.job_id or "")), []),
            )
            for job in jobs
        ]
    return {"returned": len(rows), "jobs": rows}


def record_swipe(job: Dict[str, Any], action: str) -> Dict[str, Any]:
    _ensure_swipe_table()
    job_pk = _to_int(job.get("id"))
    saved_job_pk = 0
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
            return {"success": False}
        saved_job_pk = int(db_job.id or 0)

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
    prep = None
    queued = False
    eligible = None
    if action == "like" and saved_job_pk:
        prep_result = queue_application_prep_for_job(saved_job_pk)
        prep = prep_result.get("application_prep")
        queued = bool(prep_result.get("queued"))
        eligible = prep_result.get("eligible")
        if queued:
            _start_application_prep_worker()
    return {
        "success": True,
        "application_prep": prep,
        "application_prep_eligible": eligible,
        "application_prep_queued": queued,
    }


def queue_application_prep_for_job(job_pk: int, *, force: bool = False) -> Dict[str, Any]:
    _ensure_reference_fields_column()
    job_pk = _to_int(job_pk)
    if not job_pk:
        return {"found": False, "application_prep": None}

    with SessionLocal() as session:
        job = session.get(Job, job_pk)
        if job is None:
            return {"found": False, "application_prep": None}

        context = _application_prep_context(session)
        eligibility = _application_prep_eligibility(job, context)
        prep = (
            session.execute(
                select(JobApplicationPrep).where(JobApplicationPrep.job_pk == job.id)
            )
            .scalars()
            .one_or_none()
        )
        if not eligibility["eligible"]:
            return {
                "found": True,
                "eligible": False,
                "queued": False,
                "reason": eligibility["reason"],
                "threshold": eligibility["minimum_match_percentage"],
                "application_prep": _serialize_application_prep(job, context),
            }

        now = utc_now_naive()
        queued = False
        if prep is None:
            prep = JobApplicationPrep(job_pk=job.id)
            session.add(prep)
            queued = True
        current_status = _application_prep_status(job, prep, context)
        if force or current_status in {"none", "queued", "failed", "stale"}:
            prep.status = "queued"
            prep.queued_at = now
            prep.started_at = None
            prep.error_text = None
            if force:
                prep.generated_at = None
            queued = True
        session.commit()
        return {
            "found": True,
            "eligible": True,
            "queued": queued,
            "reason": "",
            "threshold": eligibility["minimum_match_percentage"],
            "application_prep": _serialize_application_prep(job, context),
        }


def fetch_application_prep_deck() -> Dict[str, Any]:
    _ensure_reference_fields_column()
    with SessionLocal() as session:
        context = _application_prep_context(session)
        rows = (
            session.execute(
                select(Job)
                .join(JobApplicationPrep, JobApplicationPrep.job_pk == Job.id)
                .join(JobSwipe, JobSwipe.job_pk == Job.id)
                .where(JobSwipe.action == "like")
                .order_by(JobApplicationPrep.queued_at.desc(), Job.id.desc())
            )
            .scalars()
            .all()
        )
        jobs = []
        for job in rows:
            prep = _serialize_application_prep(job, context)
            jobs.append(
                {
                    "id": job.id,
                    "site": job.site or "",
                    "job_id": job.job_id or "",
                    "title": job.title or "",
                    "url": job.url or "",
                    "is_active": bool(job.is_active),
                    "ai_match_percentage": job.ai_match_percentage,
                    "ai_location_policy_match": job.ai_location_policy_match or "",
                    "queued_at": prep.get("queued_at", ""),
                    "generated_at": prep.get("generated_at", ""),
                    "status": prep.get("status", "none"),
                    "error_text": prep.get("error_text", ""),
                    "application_prep": prep,
                }
            )
    return {
        "returned": len(jobs),
        "jobs": jobs,
        "minimum_match_percentage": context["minimum_match_percentage"],
    }


def fetch_application_prep_settings() -> Dict[str, Any]:
    """Return the editable threshold and active Markdown evidence inventory."""
    _ensure_reference_fields_column()
    with SessionLocal() as session:
        context = _application_prep_context(session)
    return {
        "minimum_match_percentage": context["minimum_match_percentage"],
        "inventory_markdown": context["inventory_markdown"],
        "inventory_updated_at": _fmt_dt(context["inventory_updated_at"]) or "",
        "inventory_max_tokens": APPLICATION_PREP_INVENTORY_MAX_TOKENS,
    }


def save_application_prep_settings(data: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the only Application Prep setting and its grounded evidence source."""
    raw_minimum = data.get("minimum_match_percentage")
    if isinstance(raw_minimum, bool):
        raise ValueError("minimum_match_percentage must be an integer from 0 to 100")
    try:
        minimum_match_percentage = int(raw_minimum)
    except (TypeError, ValueError) as exc:
        raise ValueError("minimum_match_percentage must be an integer from 0 to 100") from exc
    if not 0 <= minimum_match_percentage <= 100:
        raise ValueError("minimum_match_percentage must be an integer from 0 to 100")

    markdown = data.get("inventory_markdown")
    if not isinstance(markdown, str):
        raise ValueError("inventory_markdown must be text")
    if len(markdown) > 120000:
        raise ValueError("inventory_markdown must be 120,000 characters or fewer")

    _ensure_reference_fields_column()
    with SessionLocal() as session:
        settings = session.get(ApplicationPrepSettings, 1)
        if settings is None:
            settings = ApplicationPrepSettings(id=1)
            session.add(settings)
        settings.minimum_match_percentage = minimum_match_percentage

        inventory = session.get(ResponsibilitiesInventory, 1)
        if inventory is None:
            inventory = ResponsibilitiesInventory(id=1)
            session.add(inventory)
        inventory.markdown = markdown
        inventory.content_hash = _stable_text_hash(markdown)
        inventory.updated_at = utc_now_naive()
        session.commit()
    return fetch_application_prep_settings()


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
# Data access: Integration Runs
# ----------------------------------------------------------------------
def _runs_since_dt(days: int) -> datetime:
    today = _now_local().date()
    since_date = today - timedelta(days=max(0, days - 1))
    since_local = datetime.combine(since_date, datetime.min.time())
    if ZoneInfo:
        since_local = since_local.replace(tzinfo=ZoneInfo(_get_local_tz_name()))
        return since_local.astimezone(timezone.utc).replace(tzinfo=None)
    return since_local


def _serialize_recent_runs(runs: List[IntegrationRun]) -> List[Dict[str, Any]]:
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
                "started_at": _fmt_dt(started_at) or "",
                "finished_at": _fmt_dt(finished_at) or "",
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
    return recent_runs


def _classify_daily_job_changes(changes: List[JobChange]) -> Dict[str, Dict[Any, str]]:
    per_day_job: Dict[str, Dict[Any, Dict[str, Any]]] = defaultdict(dict)
    for change in changes:
        created_at = getattr(change, "created_at", None)
        if created_at is None:
            continue
        try:
            day_str = _as_local(created_at).date().isoformat()
        except Exception:
            continue

        job_key = (
            str(getattr(change, "site", "") or ""),
            str(getattr(change, "job_id_text", "") or ""),
        )
        if not job_key[0] or not job_key[1]:
            continue

        record = per_day_job[day_str].setdefault(
            job_key,
            {"has_insert": False, "last_type": "", "last_sort": (datetime.min, 0)},
        )
        change_type = str(getattr(change, "change_type", "") or "").lower()
        if change_type == "insert":
            record["has_insert"] = True

        sort_key = (created_at, _to_int(getattr(change, "id", 0)))
        if sort_key >= record["last_sort"]:
            record["last_type"] = change_type
            record["last_sort"] = sort_key

    classified: Dict[str, Dict[Any, str]] = defaultdict(dict)
    for day_str, jobs in per_day_job.items():
        for job_key, record in jobs.items():
            if record["has_insert"]:
                category = "inserted"
            elif record["last_type"] == "missing":
                category = "missing"
            else:
                category = "updated"
            classified[day_str][job_key] = category
    return classified


def fetch_runs_summary(days: int) -> Dict[str, Any]:
    days = max(1, min(int(days or 30), 365))
    since_dt = _runs_since_dt(days)

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
        changes = (
            session.execute(
                select(JobChange)
                .where(JobChange.created_at >= since_dt)
                .order_by(JobChange.created_at.asc(), JobChange.id.asc())
            )
            .scalars()
            .all()
        )

    per_day: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in runs:
        started_at = r.started_at
        if started_at is None:
            continue
        try:
            day_str = _as_local(started_at).date().isoformat()
        except Exception:
            continue

        per_day[day_str]["baseline_total_seen"] = max(
            _to_int(per_day[day_str].get("baseline_total_seen")),
            _to_int(getattr(r, "total_seen", 0)),
        )
        per_day[day_str]["error"] += _to_int(getattr(r, "error_count", 0))

    for day_str, jobs in _classify_daily_job_changes(changes).items():
        for category in jobs.values():
            per_day[day_str][category] += 1

    change_rate: List[float] = []
    net_rate: List[float] = []
    daily: List[Dict[str, Any]] = []

    for d, day in OrderedDict(sorted(per_day.items(), key=lambda kv: kv[0])).items():
        ins = _to_int(day.get("inserted"))
        upd = _to_int(day.get("updated"))
        miss = _to_int(day.get("missing"))
        err = _to_int(day.get("error"))
        baseline = _to_int(day.get("baseline_total_seen"))

        net = ins - miss
        if baseline > 0:
            ch = (ins + upd + miss) / baseline
            nr = net / baseline
        else:
            ch = 0.0
            nr = 0.0

        change_rate.append(ch)
        net_rate.append(nr)
        daily.append(
            {
                "date": d,
                "baseline_total_seen": baseline,
                "inserted": ins,
                "updated": upd,
                "missing": miss,
                "unchanged": max(0, baseline - ins - upd - miss),
                "error": err,
                "net_change": net,
                "change_rate_pct": round(ch * 100.0, 2),
                "net_rate_pct": round(nr * 100.0, 2),
            }
        )

    change_rate_ma7 = _rolling_mean(change_rate, 7)
    net_rate_ma7 = _rolling_mean(net_rate, 7)
    for idx, day in enumerate(daily):
        day["change_rate_ma7_pct"] = round(change_rate_ma7[idx] * 100.0, 2)
        day["net_rate_ma7_pct"] = round(net_rate_ma7[idx] * 100.0, 2)

    return {
        "days": days,
        "timezone": _get_local_tz_name(),
        "metrics_definition": {
            "daily_counts": "unique daily job impact",
            "baseline": "max run total_seen for the day",
            "change_rate": "(inserted + updated + missing) / baseline",
            "net_rate": "(inserted - missing) / baseline",
        },
        "daily": daily,
        "recent_runs": _serialize_recent_runs(runs),
    }


def fetch_runs(days: int) -> Dict[str, List]:
    summary = fetch_runs_summary(days)
    daily = summary.get("daily") or []

    return {
        "labels": [row.get("date") for row in daily],
        "inserted": [_to_int(row.get("inserted")) for row in daily],
        "updated": [_to_int(row.get("updated")) for row in daily],
        "missing": [_to_int(row.get("missing")) for row in daily],
        "unchanged": [_to_int(row.get("unchanged")) for row in daily],
        "error": [_to_int(row.get("error")) for row in daily],
        "total_seen": [_to_int(row.get("baseline_total_seen")) for row in daily],
        "net_change": [_to_int(row.get("net_change")) for row in daily],
        "change_rate_pct": [row.get("change_rate_pct", 0.0) for row in daily],
        "net_rate_pct": [row.get("net_rate_pct", 0.0) for row in daily],
        "change_rate_ma7_pct": [row.get("change_rate_ma7_pct", 0.0) for row in daily],
        "net_rate_ma7_pct": [row.get("net_rate_ma7_pct", 0.0) for row in daily],
        "recent_runs": summary.get("recent_runs") or [],
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
        local_dt = _as_local(dt)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        try:
            return dt.isoformat(sep=" ")
        except Exception:
            return str(dt)


def _stable_text_hash(text_value: str) -> str:
    return hashlib.sha256((text_value or "").encode("utf-8", "ignore")).hexdigest()


def _current_resume_hash() -> str:
    try:
        with open(RESUME_PATH, "r", encoding="utf-8", errors="ignore") as handle:
            return _stable_text_hash(handle.read())
    except Exception:
        return ""


def _application_prep_context(session) -> Dict[str, Any]:
    """Read the shared eligibility setting and active evidence inventory once."""
    settings = session.get(ApplicationPrepSettings, 1)
    raw_minimum = getattr(
        settings, "minimum_match_percentage", APPLICATION_PREP_DEFAULT_MIN_MATCH
    )
    try:
        minimum_match_percentage = int(raw_minimum)
    except (TypeError, ValueError):
        minimum_match_percentage = APPLICATION_PREP_DEFAULT_MIN_MATCH
    minimum_match_percentage = max(0, min(100, minimum_match_percentage))

    inventory = session.get(ResponsibilitiesInventory, 1)
    markdown = str(getattr(inventory, "markdown", "") or "")
    return {
        "minimum_match_percentage": minimum_match_percentage,
        "inventory_markdown": markdown,
        "inventory_hash": str(getattr(inventory, "content_hash", "") or ""),
        "inventory_updated_at": getattr(inventory, "updated_at", None),
    }


def _application_prep_eligibility(j: Job, context: Dict[str, Any]) -> Dict[str, Any]:
    """Keep all dashboard Application Prep entry points on one score rule."""
    minimum = int(context["minimum_match_percentage"])
    score = getattr(j, "ai_match_percentage", None)
    if not bool(j.is_active):
        reason = "This job is no longer active."
    elif score is None:
        reason = "This job has no final AI match score yet."
    elif score < minimum:
        reason = f"Application Prep requires a match score of at least {minimum}% (this job is {score}%)."
    else:
        reason = ""
    return {
        "eligible": not reason,
        "minimum_match_percentage": minimum,
        "match_percentage": score,
        "reason": reason,
    }


def _fit_brief_job_hash(j: Job) -> str:
    return j.content_hash or _stable_text_hash(
        "|".join([j.title or "", j.desc or "", j.ai_analysis or ""])
    )


def _application_prep_job_hash(j: Job) -> str:
    return j.content_hash or _stable_text_hash(
        "|".join([j.title or "", j.desc or "", j.ai_analysis or ""])
    )


def _serialize_fit_brief(j: Job) -> Dict[str, Any]:
    brief = getattr(j, "fit_brief", None)
    if brief is None:
        eligible = bool(j.is_active) and (j.ai_match_percentage or 0) >= DASH_FIT_BRIEF_MIN_MATCH and bool((j.title or j.desc or "").strip())
        return {
            "available": False,
            "eligible": eligible,
            "status": "missing" if eligible else "not_eligible",
            "generated_at": "",
            "schema_version": None,
            "resume_bullet_count": 0,
            "brief": None,
        }

    payload = _safe_json_loads(getattr(brief, "brief_json", None)) or {}
    current_resume_hash = _current_resume_hash()
    stale_resume = bool(current_resume_hash) and brief.resume_hash != current_resume_hash
    stale_job = brief.job_content_hash != _fit_brief_job_hash(j)
    matches = payload.get("resume_bullet_matches") if isinstance(payload, dict) else []
    return {
        "available": True,
        "eligible": True,
        "status": "stale" if (stale_resume or stale_job) else "current",
        "generated_at": _fmt_dt(getattr(brief, "generated_at", None)) or "",
        "schema_version": getattr(brief, "schema_version", None),
        "resume_bullet_count": len(matches) if isinstance(matches, list) else 0,
        "brief": payload if isinstance(payload, dict) else None,
    }


def _application_prep_status(
    j: Job, prep: Optional[JobApplicationPrep], context: Dict[str, Any]
) -> str:
    if prep is None:
        return "none"
    if not _application_prep_eligibility(j, context)["eligible"]:
        return "ineligible"
    status = (prep.status or "queued").strip().lower()
    if status in {"queued", "running", "failed"}:
        return status
    current_resume_hash = _current_resume_hash()
    if (
        (current_resume_hash and prep.resume_hash != current_resume_hash)
        or (prep.responsibilities_hash or "") != context["inventory_hash"]
        or prep.job_content_hash != _application_prep_job_hash(j)
        or prep.schema_version != APPLICATION_PREP_SCHEMA_VERSION
    ):
        return "stale"
    return "done" if status == "done" else status


def _serialize_application_prep(
    j: Job, context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    context = context or {
        "minimum_match_percentage": APPLICATION_PREP_DEFAULT_MIN_MATCH,
        "inventory_hash": "",
    }
    prep = getattr(j, "application_prep", None)
    if prep is None:
        return {
            "available": False,
            "status": "none",
            "queued_at": "",
            "started_at": "",
            "generated_at": "",
            "schema_version": None,
            "error_text": "",
            "prep": None,
        }
    payload = _safe_json_loads(getattr(prep, "prep_json", None)) or {}
    return {
        "available": bool(isinstance(payload, dict) and payload),
        "status": _application_prep_status(j, prep, context),
        "queued_at": _fmt_dt(getattr(prep, "queued_at", None)) or "",
        "started_at": _fmt_dt(getattr(prep, "started_at", None)) or "",
        "generated_at": _fmt_dt(getattr(prep, "generated_at", None)) or "",
        "schema_version": getattr(prep, "schema_version", None),
        "error_text": getattr(prep, "error_text", None) or "",
        "prep": payload if isinstance(payload, dict) else None,
    }


def _serialize_job_detail(
    j: Job, changes: List[JobChange], context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    context = context or {
        "minimum_match_percentage": APPLICATION_PREP_DEFAULT_MIN_MATCH,
        "inventory_hash": "",
    }
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
        "fit_brief": _serialize_fit_brief(j),
        "swipe_action": getattr(getattr(j, "swipe", None), "action", None) or "",
        "application_prep_eligibility": _application_prep_eligibility(j, context),
        "application_prep": _serialize_application_prep(j, context),
        "changes": [_serialize_change(c) for c in changes],
    }


DEFAULT_QUERY_COLUMNS = [
    "id",
    "site",
    "title",
    "level",
    "pay",
    "base_pay_low",
    "base_pay_high",
    "pay_currency",
    "pay_period",
    "ote_low",
    "ote_high",
    "bonus_offered",
    "equity_offered",
    "commission_offered",
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
    {"key": "id", "label": "ID", "group": "Core", "type": "number"},
    {"key": "site", "label": "Site", "group": "Core", "type": "text"},
    {"key": "job_id", "label": "Job ID", "group": "Core", "type": "text"},
    {"key": "title", "label": "Title", "group": "Core", "type": "text"},
    {"key": "url", "label": "URL", "group": "Core", "type": "text"},
    {"key": "level", "label": "Level", "group": "Core", "type": "text"},
    {"key": "pay", "label": "Pay", "group": "Core", "type": "text"},
    {"key": "base_pay_low", "label": "Base Pay Low", "group": "Compensation", "type": "number"},
    {"key": "base_pay_high", "label": "Base Pay High", "group": "Compensation", "type": "number"},
    {"key": "pay_currency", "label": "Pay Currency", "group": "Compensation", "type": "text"},
    {"key": "pay_period", "label": "Pay Period", "group": "Compensation", "type": "text"},
    {"key": "ote_low", "label": "OTE Low", "group": "Compensation", "type": "number"},
    {"key": "ote_high", "label": "OTE High", "group": "Compensation", "type": "number"},
    {"key": "bonus_offered", "label": "Bonus", "group": "Compensation", "type": "boolean"},
    {"key": "equity_offered", "label": "Equity", "group": "Compensation", "type": "boolean"},
    {"key": "commission_offered", "label": "Commission", "group": "Compensation", "type": "boolean"},
    {"key": "multiple_pay_ranges", "label": "Multiple Pay Ranges", "group": "Compensation", "type": "boolean"},
    {"key": "compensation_text", "label": "Compensation Evidence", "group": "Compensation", "type": "text"},
    {"key": "compensation_notes", "label": "Compensation Notes", "group": "Compensation", "type": "text"},
    {"key": "compensation_source", "label": "Compensation Source", "group": "Compensation", "type": "text"},
    {"key": "compensation_analyzed_at", "label": "Compensation Analyzed", "group": "Compensation", "type": "datetime"},
    {"key": "compensation_schema_version", "label": "Compensation Version", "group": "Compensation", "type": "number"},
    {"key": "discovery_date", "label": "Discovery", "group": "Core", "type": "datetime"},
    {"key": "age_hours", "label": "Age Hours", "group": "Core", "type": "number"},
    {"key": "is_active", "label": "Active", "group": "Core", "type": "boolean"},
    {"key": "updated_at", "label": "Updated", "group": "Core", "type": "datetime"},
    {"key": "content_hash", "label": "Content Hash", "group": "Core", "type": "text"},
    {"key": "run_id", "label": "Run ID", "group": "Core", "type": "number"},
    {"key": "first_seen_run_id", "label": "First Seen Run", "group": "Core", "type": "number"},
    {"key": "last_seen_run_id", "label": "Last Seen Run", "group": "Core", "type": "number"},
    {"key": "ai_match_percentage", "label": "AI Match %", "group": "AI", "type": "number"},
    {"key": "ai_salary", "label": "AI Salary", "group": "AI", "type": "text"},
    {"key": "ai_fit_summary", "label": "AI Fit Summary", "group": "AI", "type": "text"},
    {"key": "ai_keywords_overlap", "label": "AI Keywords Overlap", "group": "AI", "type": "text"},
    {"key": "ai_missing_keywords", "label": "AI Missing Keywords", "group": "AI", "type": "text"},
    {"key": "ai_experience_match", "label": "AI Experience", "group": "AI", "type": "text"},
    {"key": "ai_location_policy_match", "label": "AI Location", "group": "AI", "type": "text"},
    {"key": "ai_analyzed_at", "label": "AI Analyzed", "group": "AI", "type": "datetime"},
    {"key": "fit_brief_generated_at", "label": "Fit Brief Generated", "group": "Fit Brief", "type": "datetime"},
    {"key": "fit_brief_status", "label": "Fit Brief Status", "group": "Fit Brief", "type": "text"},
    {"key": "fit_brief_resume_bullet_count", "label": "Mapped Resume Bullets", "group": "Fit Brief", "type": "number"},
    {"key": "application_prep_status", "label": "Application Prep Status", "group": "Application Prep", "type": "text"},
    {"key": "application_prep_generated_at", "label": "Application Prep Generated", "group": "Application Prep", "type": "datetime"},
    {"key": "latest_change_type", "label": "Latest Change", "group": "Changes", "type": "text"},
    {"key": "latest_change_source", "label": "Latest Change Source", "group": "Changes", "type": "text"},
    {"key": "latest_change_at", "label": "Latest Change At", "group": "Changes", "type": "datetime"},
    {"key": "latest_changed_fields", "label": "Latest Changed Fields", "group": "Changes", "type": "text"},
    {"key": "desc", "label": "Description", "group": "Raw/Long Text", "type": "text"},
    {"key": "keywords", "label": "Keywords", "group": "Raw/Long Text", "type": "text"},
    {"key": "ai_raw", "label": "Raw AI Text", "group": "Raw/Long Text", "type": "text"},
    {"key": "ai_json", "label": "AI JSON", "group": "Raw/Long Text", "type": "text"},
    {"key": "changes_json", "label": "Changes JSON", "group": "Raw/Long Text", "type": "text"},
]

STATIC_QUERY_COLUMN_MAP = {item["key"]: item for item in STATIC_QUERY_COLUMNS}
STATIC_QUERY_COLUMN_TYPES = {item["key"]: item.get("type", "text") for item in STATIC_QUERY_COLUMNS}
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


def _query_column_type(key: str) -> str:
    if key.startswith("reference."):
        return "text"
    return STATIC_QUERY_COLUMN_TYPES.get(key, "text")


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
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    try:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        return float(text)
    except Exception:
        return None


def _query_to_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _query_parse_relative_duration(value: Any) -> Optional[timedelta]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hours = float(value)
        if hours >= 0:
            return timedelta(hours=hours)
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text == "all":
        return None
    compact = text.replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhdwy]?)", compact)
    if match:
        amount = float(match.group(1))
        unit = match.group(2) or "h"
        if amount < 0:
            return None
        if unit == "s":
            return timedelta(seconds=amount)
        if unit == "m":
            return timedelta(minutes=amount)
        if unit == "h":
            return timedelta(hours=amount)
        if unit == "d":
            return timedelta(days=amount)
        if unit == "w":
            return timedelta(weeks=amount)
        if unit == "y":
            return timedelta(days=amount * 365)
    named = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|year|years)", text)
    if not named:
        return None
    amount = float(named.group(1))
    if amount < 0:
        return None
    unit = named.group(2)
    if unit.startswith("second"):
        return timedelta(seconds=amount)
    if unit.startswith("minute"):
        return timedelta(minutes=amount)
    if unit.startswith("hour"):
        return timedelta(hours=amount)
    if unit.startswith("day"):
        return timedelta(days=amount)
    if unit.startswith("week"):
        return timedelta(weeks=amount)
    return timedelta(days=amount * 365)


def _query_to_datetime(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() == "all":
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        parsed = None
    if parsed is None:
        for fmt in (
            "%Y-%m-%d",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
            "%m/%d/%Y",
            "%m/%d/%Y %H:%M",
            "%m/%d/%Y %H:%M:%S",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except Exception:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    elif ZoneInfo:
        parsed = parsed.replace(tzinfo=ZoneInfo(_get_local_tz_name())).astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _query_value_is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _query_value_for_field(row: Dict[str, Any], field: str) -> Any:
    raw = row.get("__raw")
    if isinstance(raw, dict) and field in raw:
        return raw.get(field)
    return row.get(field)


def _query_sort_key(row: Dict[str, Any], field: str) -> tuple[int, Any, Any]:
    value = _query_value_for_field(row, field)
    if _query_value_is_empty(value):
        return (1, None, row.get("id", 0))
    field_type = _query_column_type(field)
    if field_type == "number":
        parsed = _query_to_number(value)
        if parsed is not None:
            return (0, parsed, row.get("id", 0))
        return (0, _query_value_text(value).lower(), row.get("id", 0))
    if field_type == "boolean":
        parsed = _query_to_bool(value)
        if parsed is not None:
            return (0, int(parsed), row.get("id", 0))
        return (0, _query_value_text(value).lower(), row.get("id", 0))
    if field_type == "datetime":
        parsed = _query_to_datetime(value)
        if parsed is not None:
            return (0, parsed, row.get("id", 0))
        return (0, _query_value_text(value).lower(), row.get("id", 0))
    text = _query_value_text(value).lower()
    return (0, text, row.get("id", 0))


def _query_reference_type(values: List[Any]) -> str:
    non_empty = [value for value in values if not _query_value_is_empty(value)]
    if not non_empty:
        return "text"
    if all(_query_to_bool(value) is not None for value in non_empty):
        return "boolean"
    if all(_query_to_number(value) is not None for value in non_empty):
        return "number"
    if all(_query_to_datetime(value) is not None for value in non_empty):
        return "datetime"
    return "text"


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

    field_type = _query_column_type(field)
    actual = _query_value_for_field(row, field)
    expected = rule.get("value")
    value_end = rule.get("value_end")
    if value_end in {None, ""}:
        value_end = rule.get("end")
    actual_text = _query_value_text(actual).lower()
    expected_text = _query_value_text(expected).lower()

    if operator == "is_empty":
        return _query_value_is_empty(actual)
    if operator == "is_not_empty":
        return not _query_value_is_empty(actual)
    if operator == "contains":
        return expected_text in actual_text
    if operator == "not_contains":
        return expected_text not in actual_text
    if operator == "equals":
        if field_type == "boolean":
            actual_bool = _query_to_bool(actual)
            expected_bool = _query_to_bool(expected)
            return actual_bool is not None and expected_bool is not None and actual_bool == expected_bool
        if field_type == "datetime":
            actual_dt = _query_to_datetime(actual)
            expected_dt = _query_to_datetime(expected)
            if actual_dt is None or expected_dt is None:
                return False
            return actual_dt == expected_dt
        return actual_text == expected_text
    if operator == "not_equals":
        if field_type == "boolean":
            actual_bool = _query_to_bool(actual)
            expected_bool = _query_to_bool(expected)
            return actual_bool is not None and expected_bool is not None and actual_bool != expected_bool
        if field_type == "datetime":
            actual_dt = _query_to_datetime(actual)
            expected_dt = _query_to_datetime(expected)
            if actual_dt is None or expected_dt is None:
                return False
            return actual_dt != expected_dt
        return actual_text != expected_text

    if field_type == "datetime":
        actual_dt = _query_to_datetime(actual)
        if actual_dt is None:
            return False
        if operator in {"gte", "lte"}:
            expected_dt = _query_to_datetime(expected)
            if expected_dt is None:
                duration = _query_parse_relative_duration(expected)
                if duration is None:
                    return False
                expected_dt = utc_now_naive() - duration
            return actual_dt >= expected_dt if operator == "gte" else actual_dt <= expected_dt
        if operator == "between":
            bounds: Any = expected
            if isinstance(bounds, dict):
                bounds = [bounds.get("start"), bounds.get("end")]
            elif isinstance(bounds, str) and value_end not in {None, ""}:
                bounds = [bounds, value_end]
            elif isinstance(bounds, str):
                parts = [part.strip() for part in re.split(r"\s*(?:\.\.|,|to)\s*", bounds) if part.strip()]
                bounds = parts
            if not isinstance(bounds, list) or len(bounds) != 2:
                return False
            low = _query_to_datetime(bounds[0])
            high = _query_to_datetime(bounds[1])
            if low is None:
                duration = _query_parse_relative_duration(bounds[0])
                if duration is not None:
                    low = utc_now_naive() - duration
            if high is None:
                duration = _query_parse_relative_duration(bounds[1])
                if duration is not None:
                    high = utc_now_naive() - duration
            if low is not None and high is not None and low > high:
                low, high = high, low
            return low is not None and high is not None and low <= actual_dt <= high
    if field_type == "number":
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
            if low is not None and high is not None and low > high:
                low, high = high, low
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

    # Numeric compensation values are meaningful only inside one currency and
    # period. Require those equality filters in the same AND group.
    rules = [item for item in items if isinstance(item, dict) and "items" not in item]
    numeric_fields = {"base_pay_low", "base_pay_high", "ote_low", "ote_high"}
    has_numeric_rule = any(
        str(rule.get("field") or "") in numeric_fields
        and str(rule.get("operator") or "") in {"gte", "lte", "between"}
        for rule in rules
    )
    if has_numeric_rule and op != "and":
        raise ValueError("numeric compensation filters must be placed in an AND group")
    if has_numeric_rule:
        equality_fields = {
            str(rule.get("field") or "")
            for rule in rules
            if str(rule.get("operator") or "") == "equals"
            and str(rule.get("value") or "").strip()
        }
        if has_numeric_rule and not {"pay_currency", "pay_period"}.issubset(equality_fields):
            raise ValueError(
                "numeric compensation filters require pay_currency and pay_period equals filters in the same AND group"
            )


def _normalize_query_sort(payload: Any) -> Dict[str, str]:
    sort = payload if isinstance(payload, dict) else {}
    field = str(sort.get("field") or "").strip()
    direction = str(sort.get("direction") or "asc").strip().lower()
    if field and not _is_allowed_query_field(field):
        field = ""
    if direction not in {"asc", "desc"}:
        direction = "asc"
    return {"field": field, "direction": direction}


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
    fit_brief = _serialize_fit_brief(j)
    application_prep = _serialize_application_prep(j)
    raw: Dict[str, Any] = {
        "id": j.id,
        "site": j.site or "",
        "job_id": j.job_id or "",
        "title": j.title or "",
        "url": j.url or "",
        "desc": j.desc or "",
        "keywords": j.keywords or "",
        "level": j.level or "",
        "pay": j.pay or "",
        "base_pay_low": float(j.base_pay_low) if j.base_pay_low is not None else "",
        "base_pay_high": float(j.base_pay_high) if j.base_pay_high is not None else "",
        "pay_currency": j.pay_currency or "",
        "pay_period": j.pay_period or "",
        "ote_low": float(j.ote_low) if j.ote_low is not None else "",
        "ote_high": float(j.ote_high) if j.ote_high is not None else "",
        "bonus_offered": j.bonus_offered,
        "equity_offered": j.equity_offered,
        "commission_offered": j.commission_offered,
        "multiple_pay_ranges": bool(j.multiple_pay_ranges),
        "compensation_text": j.compensation_text or "",
        "compensation_notes": j.compensation_notes or "",
        "compensation_source": j.compensation_source or "",
        "compensation_analyzed_at": j.compensation_analyzed_at,
        "compensation_schema_version": j.compensation_schema_version,
        "discovery_date": dt_q,
        "age_hours": round((now_q - dt_q).total_seconds() / 3600.0, 2) if dt_q else "",
        "is_active": bool(j.is_active),
        "updated_at": j.updated_at,
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
        "ai_analyzed_at": j.ai_analyzed_at,
        "fit_brief_generated_at": getattr(getattr(j, "fit_brief", None), "generated_at", None),
        "fit_brief_status": fit_brief.get("status", ""),
        "fit_brief_resume_bullet_count": fit_brief.get("resume_bullet_count", 0),
        "application_prep_status": application_prep.get("status", ""),
        "application_prep_generated_at": getattr(getattr(j, "application_prep", None), "generated_at", None),
        "latest_change_type": getattr(latest, "change_type", "") if latest else "",
        "latest_change_source": (getattr(latest, "change_source", None) or "site") if latest else "",
        "latest_change_at": getattr(latest, "created_at", None) if latest else None,
        "latest_changed_fields": getattr(latest, "changed_fields", "") if latest else "",
    }
    raw.update({f"reference.{label}": value for label, value in refs.items()})

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
        "base_pay_low": float(j.base_pay_low) if j.base_pay_low is not None else "",
        "base_pay_high": float(j.base_pay_high) if j.base_pay_high is not None else "",
        "pay_currency": j.pay_currency or "",
        "pay_period": j.pay_period or "",
        "ote_low": float(j.ote_low) if j.ote_low is not None else "",
        "ote_high": float(j.ote_high) if j.ote_high is not None else "",
        "bonus_offered": j.bonus_offered,
        "equity_offered": j.equity_offered,
        "commission_offered": j.commission_offered,
        "multiple_pay_ranges": bool(j.multiple_pay_ranges),
        "compensation_text": j.compensation_text or "",
        "compensation_notes": j.compensation_notes or "",
        "compensation_source": j.compensation_source or "",
        "compensation_analyzed_at": _fmt_dt(j.compensation_analyzed_at) or "",
        "compensation_schema_version": j.compensation_schema_version,
        "discovery_date": _fmt_dt(dt_q) or "",
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
        "fit_brief_generated_at": fit_brief.get("generated_at", ""),
        "fit_brief_status": fit_brief.get("status", ""),
        "fit_brief_resume_bullet_count": fit_brief.get("resume_bullet_count", 0),
        "application_prep_status": application_prep.get("status", ""),
        "application_prep_generated_at": application_prep.get("generated_at", ""),
        "ai_raw": ai_raw if not ai_obj else "",
        "ai_json": ai_obj or "",
        "latest_change_type": getattr(latest, "change_type", "") if latest else "",
        "latest_change_source": (getattr(latest, "change_source", None) or "site") if latest else "",
        "latest_change_at": _fmt_dt(getattr(latest, "created_at", None)) if latest else "",
        "latest_changed_fields": getattr(latest, "changed_fields", "") if latest else "",
        "changes_json": [_serialize_change(c) for c in changes],
    }
    for label, value in refs.items():
        row[f"reference.{label}"] = value
    row["__raw"] = raw
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
    sort = _normalize_query_sort(payload.get("sort"))
    return {
        "hours": hours,
        "limit": limit,
        "min_match": min_match,
        "location_policy": location_policy,
        "text": text_query,
        "columns": _normalize_selected_columns(payload.get("columns")),
        "filter": payload.get("filter") if isinstance(payload.get("filter"), dict) else {"op": "and", "items": []},
        "sort": sort,
    }


def _query_jobs(payload: Optional[Dict[str, Any]], export: bool = False) -> Dict[str, Any]:
    query = _extract_query_payload(payload, export=export)
    columns = _normalize_selected_columns(query["columns"])
    has_row_filter = _query_group_has_rules(query["filter"])
    needs_changes = _query_needs_changes(columns, query["filter"])
    sort_field = str(query.get("sort", {}).get("field") or "").strip()
    sort_active = bool(sort_field)
    db_limit = MAX_EXPORT_LIMIT if (has_row_filter or sort_active or export) else query["limit"]
    all_time = query["hours"] <= 0
    hours = 0 if all_time else max(1, min(query["hours"], 24 * 30))
    # Stored DATETIME values are naive UTC, so database comparisons and age
    # calculations must stay in UTC. Conversion happens only during display.
    now_q = utc_now_naive()
    cutoff_q = now_q - timedelta(hours=hours) if not all_time else None

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
                    func.lower(Job.ai_salary).like(like_q),
                    func.lower(Job.compensation_notes).like(like_q),
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
        reference_values: Dict[str, List[Any]] = defaultdict(list)
        changes_by_job = _changes_by_job(session, jobs) if needs_changes else {}
        for job in jobs:
            changes = changes_by_job.get(((job.site or ""), (job.job_id or "")), [])
            row = _serialize_query_row(job, changes, now_q)
            reference_keys.update(key for key in row if key.startswith("reference."))
            raw_values = row.get("__raw") if isinstance(row.get("__raw"), dict) else {}
            for key in reference_keys:
                if key.startswith("reference.") and key in raw_values and raw_values.get(key) not in {None, ""}:
                    reference_values[key].append(raw_values.get(key))
            if not _query_group_matches(row, query["filter"]):
                continue
            rows.append(row)
        if sort_active:
            rows = sorted(
                rows,
                key=lambda item: _query_sort_key(item, sort_field),
                reverse=str(query["sort"].get("direction") or "asc") == "desc",
            )
            empty_rows = [row for row in rows if _query_value_is_empty(_query_value_for_field(row, sort_field))]
            non_empty_rows = [row for row in rows if not _query_value_is_empty(_query_value_for_field(row, sort_field))]
            rows = non_empty_rows + empty_rows
        if len(rows) > query["limit"]:
            rows = rows[: query["limit"]]

    selected_rows = [
        {key: row.get(key, "") for key in columns}
        for row in rows
    ]
    available_columns = get_query_columns(sorted(reference_keys))
    for column in available_columns:
        if column["key"].startswith("reference."):
            column["type"] = _query_reference_type(reference_values.get(column["key"], []))
    return {
        "hours": hours,
        "limit": query["limit"],
        "min_match": query["min_match"],
        "location_policy": query["location_policy"],
        "text": query["text"],
        "cutoff_iso": f"{cutoff_q.isoformat()}Z" if cutoff_q else "all",
        "sort": query["sort"],
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
    reference_values: Dict[str, List[Any]] = defaultdict(list)
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
                    reference_values[f"reference.{key}"].append(value)
    columns = get_query_columns(sorted(reference_values))
    for column in columns:
        if column["key"].startswith("reference."):
            column["type"] = _query_reference_type(reference_values.get(column["key"], []))
    return {
        "columns": columns,
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
        context = _application_prep_context(session)
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
            out.append(_serialize_job_detail(job, changes, context))

    return {"query": q, "returned": len(out), "exact": bool(exact_jobs), "jobs": out}


def fetch_job_detail_by_id(job_pk: int) -> Dict[str, Any]:
    job_pk = max(0, int(job_pk or 0))
    if not job_pk:
        return {"id": job_pk, "found": False, "job": None}

    _ensure_reference_fields_column()
    with SessionLocal() as session:
        context = _application_prep_context(session)
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
        return {
            "id": job_pk,
            "found": True,
            "job": _serialize_job_detail(job, changes, context),
        }


def fetch_job_detail_by_identity(site: str, job_id: str) -> Dict[str, Any]:
    """Return one job detail using the natural ID plus site identity pair."""
    site = (site or "").strip()
    job_id = (job_id or "").strip()
    if not site or not job_id:
        return {"site": site, "job_id": job_id, "found": False, "job": None}

    _ensure_reference_fields_column()
    with SessionLocal() as session:
        context = _application_prep_context(session)
        job = session.execute(
            select(Job)
            .where(Job.site == site)
            .where(Job.job_id == job_id)
            .limit(1)
        ).scalar_one_or_none()
        if job is None:
            return {"site": site, "job_id": job_id, "found": False, "job": None}

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
        return {
            "site": site,
            "job_id": job_id,
            "found": True,
            "job": _serialize_job_detail(job, changes, context),
        }


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
    # Keep dashboard sections addressable so Application Prep can open one job directly.
    requested_view = (request.args.get("view") or "").strip()
    initial_view = requested_view if requested_view in {"lookup", "applicationPrep", "settings"} else "runs"
    try:
        initial_application_prep_job_id = max(0, int(request.args.get("job", "0")))
    except ValueError:
        initial_application_prep_job_id = 0
    return render_template(
        "index.html",
        initial_view=initial_view,
        job_detail_page=False,
        initial_job=None,
        initial_application_prep_job_id=initial_application_prep_job_id,
    )


@app.route("/report")
def report_page():
    # Query Builder has a stable URL so reports can be opened directly.
    return render_template(
        "index.html",
        initial_view="jobs",
        job_detail_page=False,
        initial_job=None,
        initial_application_prep_job_id=0,
    )


@app.route("/job/<site>/<path:job_id>")
def job_detail_page(site: str, job_id: str):
    # A natural job ID is only unique within one site, so both path values are required.
    result = fetch_job_detail_by_identity(site, job_id)
    if not result.get("found"):
        abort(404)

    job = result["job"]
    page_label = job.get("title") or job.get("job_id") or "Job detail"
    return render_template(
        "index.html",
        initial_view="lookup",
        job_detail_page=True,
        initial_job=job,
        initial_application_prep_job_id=0,
        page_title=f"{page_label} | Job Detail",
    )


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
        return jsonify(fetch_swipe_jobs(request.args.get("q", "")))
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/swipe", methods=["POST"])
def swipe_api():
    data = request.get_json(silent=True) or {}
    job = data.get("job") or {}
    action = str(data.get("action") or "").strip().lower()
    if action not in {"like", "dislike", "interesting"}:
        return jsonify(error="action must be like, dislike, or interesting"), 400
    try:
        result = record_swipe(job, action)
        if not result.get("success"):
            return jsonify(error="job not found in database"), 404
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/jobs/<int:job_pk>/move-forward", methods=["POST"])
def api_job_move_forward(job_pk: int):
    """Move a standalone detail-page job forward through the shared swipe path."""
    try:
        result = record_swipe({"id": job_pk}, "like")
        if not result.get("success"):
            return jsonify(error="job not found in database"), 404
        result["application_prep_url"] = f"/?view=applicationPrep&job={job_pk}"
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/interesting/jobs")
def api_interesting_jobs():
    try:
        return jsonify(fetch_interesting_jobs())
    except Exception as exc:
        return jsonify(error=str(exc), jobs=[]), 500


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


@app.route("/api/runs/summary")
def api_runs_summary():
    try:
        days = int(request.args.get("days", "30"))
        days = max(1, min(days, 365))
    except Exception:
        days = 30
    return jsonify(fetch_runs_summary(days))


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


@app.route("/api/application-prep/jobs")
def api_application_prep_jobs():
    try:
        return jsonify(fetch_application_prep_deck())
    except Exception as exc:
        return jsonify(error=str(exc), jobs=[]), 500


@app.route("/api/application-prep/settings", methods=["GET", "PUT"])
def api_application_prep_settings():
    try:
        if request.method == "GET":
            return jsonify(fetch_application_prep_settings())
        return jsonify(save_application_prep_settings(request.get_json(silent=True) or {}))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/application-prep/jobs/<int:job_pk>")
def api_application_prep_job(job_pk: int):
    try:
        return jsonify(fetch_job_detail_by_id(job_pk))
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/application-prep/jobs/<int:job_pk>/queue", methods=["POST"])
def api_application_prep_queue(job_pk: int):
    try:
        result = queue_application_prep_for_job(job_pk, force=True)
        if not result.get("found"):
            return jsonify(error="job not found in database"), 404
        if not result.get("eligible"):
            return jsonify(error=result.get("reason") or "job is not eligible for Application Prep"), 409
        if result.get("queued"):
            _start_application_prep_worker()
        return jsonify(result)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


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
