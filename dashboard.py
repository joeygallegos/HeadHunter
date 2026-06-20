# dashboard.py
from __future__ import annotations

import json
import os
import html
import re
import difflib
import shutil
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template_string, request
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

# NOTE: app/models.py currently prints DATABASE_URL on import. Remove that print.
from app.models import Base, JobChange, SessionLocal, IntegrationRun, Job, engine  # type: ignore
from discover_job_boards import (  # type: ignore
    DEFAULT_STEPS_PATH,
    DEFAULT_SUGGESTIONS_PATH,
    merge_steps_suggestions,
    read_json_file,
    write_json,
)

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


# ----------------------------------------------------------------------
# steps.json editor
# ----------------------------------------------------------------------
def _format_steps_json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def _steps_modified_at(path: str) -> str:
    if not os.path.exists(path):
        return ""
    return datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds")


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
# Flask app + HTML
# ----------------------------------------------------------------------
app = Flask(__name__)

INDEX_HTML = r"""<!doctype html>
<html lang="en" x-data="dashboard()">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Jobs Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  </head>

  <body class="bg-slate-50 text-slate-900">
    <div class="max-w-7xl mx-auto p-4 space-y-6">
      <header class="space-y-2">
        <h1 class="text-2xl font-bold tracking-tight">Impact Dashboard</h1>

        <div class="flex flex-wrap gap-2">
          <button @click="view='runs'"
                  :class="view==='runs' ? 'bg-indigo-600 text-white' : 'bg-white text-slate-700'"
                  class="px-3 py-2 rounded-lg border border-slate-200 text-sm">
            Integration Runs
          </button>
          <button @click="view='jobs'"
                  :class="view==='jobs' ? 'bg-indigo-600 text-white' : 'bg-white text-slate-700'"
                  class="px-3 py-2 rounded-lg border border-slate-200 text-sm">
            Job index
          </button>
          <button @click="view='lookup'"
                  :class="view==='lookup' ? 'bg-indigo-600 text-white' : 'bg-white text-slate-700'"
                  class="px-3 py-2 rounded-lg border border-slate-200 text-sm">
            Job lookup
          </button>
          <button @click="view='discovery'; loadDiscovery()"
                  :class="view==='discovery' ? 'bg-indigo-600 text-white' : 'bg-white text-slate-700'"
                  class="px-3 py-2 rounded-lg border border-slate-200 text-sm">
            Discovery
          </button>
          <a href="/steps"
             class="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white text-slate-700">
            Steps editor
          </a>
          <a href="/swipe"
             class="px-3 py-2 rounded-lg border border-slate-200 text-sm bg-white text-slate-700">
            Swipe
          </a>
        </div>
      </header>

      <!-- ---------------- RUNS VIEW ---------------- -->
      <template x-if="view==='runs'">
        <div class="space-y-6">

          <section class="flex flex-col md:flex-row md:items-center gap-3">
            <div class="flex items-center gap-2">
              <label for="days" class="text-sm font-medium text-slate-700">Days:</label>
              <select id="days" x-model.number="days"
                      class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                <option value="7">7</option>
                <option value="14">14</option>
                <option value="30" selected>30</option>
                <option value="60">60</option>
                <option value="90">90</option>
              </select>
              <button @click="loadRuns()"
                      class="ml-2 inline-flex items-center px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm hover:bg-indigo-700 active:bg-indigo-800">
                Refresh
              </button>
            </div>
            <div class="text-sm text-slate-600" x-show="runsLoaded">
              <span class="font-semibold text-slate-800">Summary:</span>
              <span class="ml-2">New: <span class="font-semibold" x-text="sum(inserted)"></span></span>
              <span class="ml-2">Updated: <span class="font-semibold" x-text="sum(updated)"></span></span>
              <span class="ml-2">Missing: <span class="font-semibold" x-text="sum(missing)"></span></span>
              <span class="ml-2">Errors: <span class="font-semibold" x-text="sum(error)"></span></span>
              <span class="ml-2">Total Seen: <span class="font-semibold" x-text="sum(total_seen)"></span></span>
              <span class="ml-2">Net Δ: <span class="font-semibold" x-text="sum(net_change)"></span></span>
            </div>
          </section>

          <template x-if="view==='runs'">
          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <h2 class="text-sm font-semibold text-slate-800 mb-2">Run list</h2>
            <div class="mt-3 overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead class="text-left text-slate-600 border-b">
                  <tr>
                    <th class="py-2 pr-4">ID</th>
                  </tr>
                </thead>
                <tbody x-show="runsLoaded" x-for="row in runs" :key="row.id">
                  <tr>
                    <td class="py-2 pr-4" x-text="row.id"></td>
                  </tr>
                </tbody>
              </table>
            </div>
          </section></template>

          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            
            <h2 class="text-sm font-semibold text-slate-800 mb-2">Daily counts</h2>
            <div class="h-80">
              <canvas id="runsChart"></canvas>
            </div>
          </section>

          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <h2 class="text-sm font-semibold text-slate-800 mb-2">Change rate (%) and net rate (%)</h2>
            <div class="h-64">
              <canvas id="runsRateChart"></canvas>
            </div>
            <p class="mt-2 text-xs text-slate-500">
              Change % = (Inserted + Updated + Missing) / Total Seen · 100. Net % = (Inserted − Missing) / Total Seen · 100.
              Dashed lines show 7-day rolling averages.
            </p>
          </section>

        </div>
      </template>

          <!-- ---------------- JOB INDEX VIEW ---------------- -->
      <template x-if="view==='jobs'">
        <div class="space-y-4">

          <section class="flex flex-col lg:flex-row lg:items-center gap-3">
            <div class="flex items-center gap-2">
              <label class="text-sm font-medium text-slate-700">Hours:</label>
              <select x-model.number="hours"
                      class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                <option value="0">All</option>
                <option value="24">24</option>
                <option value="48">48</option>
                <option value="72">72</option>
                <option value="168">168</option>
              </select>

              <label class="ml-3 text-sm font-medium text-slate-700">Limit:</label>
              <input type="number" x-model.number="jobsLimit" min="1" max="5000"
                     class="w-28 px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>

              <label class="ml-3 text-sm font-medium text-slate-700">Min match:</label>
              <input type="number" x-model.number="minAiMatch" min="0" max="100" step="1"
                     class="w-24 px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
              <button @click="minAiMatch=80; loadJobs()"
                      class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 hover:bg-slate-50">
                80%+
              </button>
              <button @click="minAiMatch=90; loadJobs()"
                      class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 hover:bg-slate-50">
                90%+
              </button>

              <label class="ml-3 text-sm font-medium text-slate-700">Location:</label>
              <select x-model="locationPolicy"
                      class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500">
                <option value="any">Any</option>
                <option value="non_hybrid">Exclude hybrid</option>
                <option value="remote">Remote only</option>
                <option value="hybrid">Hybrid only</option>
                <option value="onsite">Onsite only</option>
                <option value="unknown">Unknown only</option>
              </select>

              <button @click="loadJobs()"
                      class="ml-2 inline-flex items-center px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm hover:bg-indigo-700 active:bg-indigo-800">
                Refresh
              </button>
            </div>

            <div class="flex-1"></div>

            <div class="flex items-center gap-2">
              <input type="text" x-model="q" placeholder="Search site/title/level…"
                     class="w-72 px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
            </div>
          </section>

          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <div class="flex items-center justify-between">
              <h2 class="text-sm font-semibold text-slate-800">
                Job index <span x-text="hours > 0 ? `(last ${hours} hours)` : '(all time)'"></span>
              </h2>
              <div class="text-xs text-slate-500" x-show="jobsLoaded">
                returned=<span x-text="jobsMeta.returned"></span>,
                min_match=<span x-text="jobsMeta.min_match ?? '-'"></span>,
                location=<span x-text="jobsMeta.location_policy ?? 'any'"></span>,
                parse_failures=<span x-text="jobsMeta.parse_failures"></span>,
                cutoff=<span x-text="jobsMeta.cutoff_iso"></span>
              </div>
            </div>

            <div class="mt-3 overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead class="text-left text-slate-600 border-b">
                  <tr>
                    <th class="py-2 pr-4">ID</th>
                    <th class="py-2 pr-4">Site</th>
                    <th class="py-2 pr-4">Title</th>
                    <th class="py-2 pr-4">Level</th>
                    <th class="py-2 pr-4">Pay</th>
                    <th class="py-2 pr-4">Discovery</th>
                    <th class="py-2 pr-4">AI</th>
                  </tr>
                </thead>
                <tbody>
                  <template x-for="row in filteredJobs()" :key="row.id">
                    <tr class="border-b align-top">
                      <td class="py-3 pr-4">
                        <button type="button"
                                class="text-indigo-700 hover:underline"
                                @click="openJobDetail(row.id)"
                                x-text="`ID ${row.id}`"></button>
                      </td>
                      <td class="py-3 pr-4 font-medium text-slate-800" x-text="row.site"></td>

                      <td class="py-3 pr-4">
                        <a class="text-indigo-700 hover:underline"
                           :href="row.url || '#'" target="_blank" rel="noreferrer"
                           x-text="row.title || '(no title)'"></a>
                        <div class="text-xs text-slate-500 mt-1">
                          <span x-text="row.job_id"></span>
                          <span class="ml-2" x-text="row.age_hours + 'h ago'"></span>
                        </div>
                      </td>

                      <td class="py-3 pr-4 text-slate-700" x-text="row.level"></td>
                      <td class="py-3 pr-4 text-slate-700">
                        <div x-text="row.pay"></div>
                        <div class="text-green-600"
                             :class="row.ai_row?.salary ? 'text-green-600' : 'text-red-600'"
                             x-text="row.ai_row?.salary ?? 'No AI salary'"></div>
                      </td>

                      <td class="py-3 pr-4 text-slate-700">
                        <div x-text="row.discovery_date"></div>
                        <div class="text-xs" :class="row.is_active ? 'text-green-600' : 'text-red-600'" x-text="row.is_active ? 'Active' : 'Inactive'"></div>
                      </td>

                      <td class="py-3 pr-4 text-slate-700">
                        <template x-if="(row.ai && typeof row.ai === 'object') || row.ai_row">
                          <div class="space-y-1">
                            <div class="text-xs">
                              <span class="font-semibold">match</span>:
                              <span x-text="matchValue(row) ?? '-'"></span>
                              <span class="ml-2 font-semibold">experience_match</span>:
                              <span x-text="row.ai_row?.experience_match ?? row.ai?.experience_match ?? '-'"></span>
                              <span class="ml-2 font-semibold">location</span>:
                              <span x-text="locationValue(row) ?? '-'"></span>
                            </div>
                            <div class="text-xs text-slate-600 line-clamp-3" x-text="row.ai_row?.fit_summary ?? row.ai?.fit_summary ?? ''"></div>
                            <button class="text-xs text-indigo-700 hover:underline"
                                    x-show="row.ai && typeof row.ai === 'object'"
                                    @click="row._showAi = !row._showAi">
                              <span x-text="row._showAi ? 'Hide JSON' : 'View JSON'"></span>
                            </button>
                            <pre x-show="row._showAi"
                                 class="text-xs bg-slate-50 border border-slate-200 rounded-lg p-2 overflow-auto max-h-64"
                                 x-text="JSON.stringify(row.ai, null, 2)"></pre>
                          </div>
                        </template>

                        <template x-if="!((row.ai && typeof row.ai === 'object') || row.ai_row) && (row.ai_raw || '').trim().length">
                          <div class="space-y-1">
                            <div class="text-xs text-slate-500">ai_analysis (raw)</div>
                            <button class="text-xs text-indigo-700 hover:underline"
                                    @click="row._showAiRaw = !row._showAiRaw">
                              <span x-text="row._showAiRaw ? 'Hide' : 'View'"></span>
                            </button>
                            <pre x-show="row._showAiRaw"
                                 class="text-xs bg-slate-50 border border-slate-200 rounded-lg p-2 overflow-auto max-h-64"
                                 x-text="row.ai_raw"></pre>
                          </div>
                        </template>

                        <template x-if="!((row.ai && typeof row.ai === 'object') || row.ai_row || (row.ai_raw || '').trim().length)">
                          <div class="text-xs text-slate-400">—</div>
                        </template>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>

              <div class="text-sm text-slate-500 py-3" x-show="jobsLoaded && filteredJobs().length === 0">
                No rows match your filter.
              </div>
            </div>
          </section>

        </div>
      </template>

      <!-- ---------------- JOB LOOKUP VIEW ---------------- -->
      <template x-if="view==='lookup'">
        <div class="space-y-4">

          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <form class="flex flex-col md:flex-row md:items-center gap-3" @submit.prevent="lookupJob()">
              <label class="text-sm font-medium text-slate-700" for="jobLookup">Job ID or keyword:</label>
              <input id="jobLookup" type="text" x-model="lookupQ" placeholder="JR1996524"
                     class="w-full md:w-96 px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"/>
              <button type="submit"
                      class="inline-flex items-center justify-center px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm hover:bg-indigo-700 active:bg-indigo-800">
                Search
              </button>
              <div class="text-sm text-slate-500" x-show="lookupLoading">Searching...</div>
            </form>
          </section>

          <section class="grid grid-cols-1 lg:grid-cols-4 gap-4" x-show="lookupLoaded">
            <div class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4 lg:col-span-1">
              <div class="flex items-center justify-between">
                <h2 class="text-sm font-semibold text-slate-800">Matches</h2>
                <span class="text-xs text-slate-500" x-text="lookupResults.length"></span>
              </div>

              <div class="mt-3 space-y-2" x-show="lookupResults.length">
                <template x-for="job in lookupResults" :key="job.id">
                  <button type="button" @click="selectedJobId = job.id"
                          class="w-full text-left rounded-lg border px-3 py-2 text-sm"
                          :class="selectedJobId === job.id ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200 bg-white hover:bg-slate-50'">
                    <div class="font-medium text-slate-900" x-text="job.job_id"></div>
                    <div class="text-xs text-slate-500" x-text="`ID ${job.id}`"></div>
                    <div class="text-xs text-slate-500 truncate" x-text="job.site"></div>
                    <div class="text-xs text-slate-700 mt-1 line-clamp-2" x-text="job.title || '(no title)'"></div>
                  </button>
                </template>
              </div>

              <div class="mt-3 text-sm text-slate-500" x-show="lookupLoaded && lookupResults.length === 0">
                No jobs found.
              </div>
            </div>

            <template x-if="selectedJob()">
            <div class="space-y-4 lg:col-span-3">
              <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
                <div class="flex flex-col md:flex-row md:items-start gap-3 md:justify-between">
                  <div>
                    <div class="text-xs font-semibold uppercase text-slate-500" x-text="selectedJob().site"></div>
                    <h2 class="mt-1 text-xl font-semibold text-slate-900" x-text="selectedJob().title || '(no title)'"></h2>
                    <div class="mt-1 text-sm text-slate-600">
                      <span class="font-medium" x-text="selectedJob().job_id"></span>
                      <span class="ml-2 text-slate-500" x-text="`ID ${selectedJob().id}`"></span>
                      <span class="ml-2" :class="selectedJob().is_active ? 'text-green-700' : 'text-red-700'"
                            x-text="selectedJob().is_active ? 'Active' : 'Inactive'"></span>
                    </div>
                  </div>
                  <a class="inline-flex items-center justify-center px-3 py-2 rounded-lg bg-slate-900 text-white text-sm hover:bg-slate-800"
                     :href="selectedJob().url || '#'" target="_blank" rel="noreferrer"
                     x-show="selectedJob().url">
                    Open posting
                  </a>
                </div>

                <dl class="mt-4 grid grid-cols-1 md:grid-cols-3 gap-3 text-sm">
                  <div class="rounded-lg border border-slate-200 p-3">
                    <dt class="text-xs text-slate-500">Level</dt>
                    <dd class="font-medium text-slate-900" x-text="selectedJob().level || '-'"></dd>
                  </div>
                  <div class="rounded-lg border border-slate-200 p-3">
                    <dt class="text-xs text-slate-500">Pay</dt>
                    <dd class="font-medium text-slate-900" x-text="selectedJob().pay || '-'"></dd>
                  </div>
                  <div class="rounded-lg border border-slate-200 p-3">
                    <dt class="text-xs text-slate-500">Discovered</dt>
                    <dd class="font-medium text-slate-900" x-text="selectedJob().discovery_date || '-'"></dd>
                  </div>
                </dl>
              </section>

              <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
                <h3 class="text-sm font-semibold text-slate-800">Description</h3>
                <pre class="mt-3 whitespace-pre-wrap text-sm leading-6 text-slate-800 bg-slate-50 border border-slate-200 rounded-lg p-3 max-h-[34rem] overflow-auto"
                     x-text="selectedJob().desc || 'No description stored.'"></pre>
              </section>

              <section class="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
                  <h3 class="text-sm font-semibold text-slate-800">AI analysis</h3>
                  <template x-if="selectedJob().ai && typeof selectedJob().ai === 'object'">
                    <pre class="mt-3 text-xs bg-slate-50 border border-slate-200 rounded-lg p-3 overflow-auto max-h-96"
                         x-text="JSON.stringify(selectedJob().ai, null, 2)"></pre>
                  </template>
                  <template x-if="!(selectedJob().ai && typeof selectedJob().ai === 'object')">
                    <pre class="mt-3 text-xs bg-slate-50 border border-slate-200 rounded-lg p-3 overflow-auto max-h-96"
                         x-text="selectedJob().ai_raw || 'No AI analysis stored.'"></pre>
                  </template>
                </div>

                <div class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
                  <h3 class="text-sm font-semibold text-slate-800">Metadata</h3>
                  <dl class="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
                    <div>
                      <dt class="text-xs text-slate-500">Updated</dt>
                      <dd x-text="selectedJob().updated_at || '-'"></dd>
                    </div>
                    <div>
                      <dt class="text-xs text-slate-500">Run ID</dt>
                      <dd x-text="selectedJob().run_id ?? '-'"></dd>
                    </div>
                    <div>
                      <dt class="text-xs text-slate-500">First seen run</dt>
                      <dd x-text="selectedJob().first_seen_run_id ?? '-'"></dd>
                    </div>
                    <div>
                      <dt class="text-xs text-slate-500">Last seen run</dt>
                      <dd x-text="selectedJob().last_seen_run_id ?? '-'"></dd>
                    </div>
                    <div class="sm:col-span-2">
                      <dt class="text-xs text-slate-500">Content hash</dt>
                      <dd class="break-all" x-text="selectedJob().content_hash || '-'"></dd>
                    </div>
                  </dl>

                  <h3 class="mt-5 text-sm font-semibold text-slate-800">Keywords</h3>
                  <pre class="mt-3 whitespace-pre-wrap text-xs bg-slate-50 border border-slate-200 rounded-lg p-3 overflow-auto max-h-40"
                       x-text="selectedJob().keywords || 'No keywords stored.'"></pre>
                </div>
              </section>

              <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
                <h3 class="text-sm font-semibold text-slate-800">Change history</h3>
                <div class="mt-3 overflow-x-auto">
                  <table class="min-w-full text-sm">
                    <thead class="text-left text-slate-600 border-b">
                      <tr>
                        <th class="py-2 pr-4">When</th>
                        <th class="py-2 pr-4">Source</th>
                        <th class="py-2 pr-4">Type</th>
                        <th class="py-2 pr-4">Fields</th>
                      </tr>
                    </thead>
                    <tbody>
                      <template x-for="change in (selectedJob().changes || [])" :key="change.id">
                        <tr class="border-b">
                          <td class="py-2 pr-4" x-text="change.created_at || '-'"></td>
                          <td class="py-2 pr-4">
                            <span class="inline-flex rounded px-2 py-0.5 text-xs font-medium"
                                  :class="(change.change_source || 'site') === 'ai' ? 'bg-violet-100 text-violet-700' : 'bg-slate-100 text-slate-700'"
                                  x-text="change.change_source || 'site'"></span>
                          </td>
                          <td class="py-2 pr-4" x-text="change.change_type || '-'"></td>
                          <td class="py-2 pr-4" x-text="change.changed_fields || '-'"></td>
                        </tr>
                      </template>
                    </tbody>
                  </table>
                  <div class="text-sm text-slate-500 py-3" x-show="!(selectedJob().changes || []).length">
                    No change records found.
                  </div>
                </div>
              </section>
            </div>
            </template>
          </section>

        </div>
      </template>

      <!-- ---------------- DISCOVERY REVIEW VIEW ---------------- -->
      <template x-if="view==='discovery'">
        <div class="space-y-4">
          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <div class="flex flex-col md:flex-row md:items-center gap-3 md:justify-between">
              <div>
                <h2 class="text-sm font-semibold text-slate-800">Discovery suggestions</h2>
                <p class="text-xs text-slate-500 mt-1">
                  Review Ollama-guided career page suggestions before adding them to steps.json.
                </p>
              </div>
              <div class="flex flex-wrap items-center gap-2">
                <button @click="loadDiscovery()"
                        class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 hover:bg-slate-50">
                  Refresh
                </button>
                <button @click="applyDiscoverySelected()"
                        :disabled="selectedDiscoveryIds.length === 0 || discoverySaving"
                        class="px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm disabled:opacity-40">
                  Apply selected
                </button>
                <button @click="rejectDiscoverySelected()"
                        :disabled="selectedDiscoveryIds.length === 0 || discoverySaving"
                        class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 disabled:opacity-40">
                  Reject selected
                </button>
              </div>
            </div>
            <div class="mt-3 text-sm text-slate-600" x-show="discoveryMessage" x-text="discoveryMessage"></div>
            <div class="mt-3 text-sm text-slate-500" x-show="discoveryLoading">Loading suggestions...</div>
          </section>

          <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4">
            <div class="overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead class="text-left text-slate-600 border-b">
                  <tr>
                    <th class="py-2 pr-4">Use</th>
                    <th class="py-2 pr-4">Site</th>
                    <th class="py-2 pr-4">Platform</th>
                    <th class="py-2 pr-4">Confidence</th>
                    <th class="py-2 pr-4">Evidence</th>
                    <th class="py-2 pr-4">Steps</th>
                  </tr>
                </thead>
                <tbody>
                  <template x-for="item in discoverySuggestions" :key="item.id">
                    <tr class="border-b align-top">
                      <td class="py-3 pr-4">
                        <input type="checkbox"
                               :disabled="!['pending','approved'].includes(item.state || 'pending')"
                               :checked="isDiscoverySelected(item.id)"
                               @change="toggleDiscovery(item.id)" />
                        <div class="mt-1 text-xs text-slate-500" x-text="item.state || 'pending'"></div>
                      </td>
                      <td class="py-3 pr-4">
                        <div class="font-medium text-slate-900" x-text="item.site_key || '-'"></div>
                        <a class="text-xs text-indigo-700 hover:underline break-all"
                           :href="item.url || '#'" target="_blank" rel="noreferrer"
                           x-text="item.url || '-'"></a>
                        <div class="mt-1 text-xs text-amber-700" x-show="item.manual_review">Manual selector review</div>
                      </td>
                      <td class="py-3 pr-4" x-text="item.platform || 'unknown'"></td>
                      <td class="py-3 pr-4" x-text="Math.round(Number(item.confidence || 0) * 100) + '%'"></td>
                      <td class="py-3 pr-4 max-w-md">
                        <div class="text-slate-800" x-text="item.reason || '-'"></div>
                        <div class="mt-1 text-xs text-slate-500" x-show="item.remote_evidence">
                          Remote: <span x-text="item.remote_evidence"></span>
                        </div>
                        <div class="mt-1 text-xs text-slate-500" x-show="item.location_evidence">
                          Location: <span x-text="item.location_evidence"></span>
                        </div>
                        <div class="mt-1 text-xs text-slate-500" x-show="item.notes" x-text="item.notes"></div>
                      </td>
                      <td class="py-3 pr-4">
                        <details>
                          <summary class="cursor-pointer text-indigo-700">View steps</summary>
                          <pre class="mt-2 text-xs bg-slate-50 border border-slate-200 rounded-lg p-2 overflow-auto max-h-80 w-[32rem]"
                               x-text="JSON.stringify(item.steps || [], null, 2)"></pre>
                        </details>
                      </td>
                    </tr>
                  </template>
                </tbody>
              </table>
              <div class="text-sm text-slate-500 py-3" x-show="discoveryLoaded && discoverySuggestions.length === 0">
                No discovery suggestions found. Run python discover_job_boards.py first.
              </div>
            </div>
          </section>
        </div>
      </template>

    </div>

    <script>
      function dashboard() {
        return {
          view: 'runs',

          // Runs state
          days: 30,
          runsLoaded: false,
          labels: [],
          inserted: [],
          updated: [],
          missing: [],
          unchanged: [],
          error: [],
          total_seen: [],
          net_change: [],
          change_rate_pct: [],
          net_rate_pct: [],
          change_rate_ma7_pct: [],
          net_rate_ma7_pct: [],
          chartCounts: null,
          chartRates: null,

          // Jobs state
          jobsLoaded: false,
          hours: 48,
          jobsLimit: 500,
          minAiMatch: 80,
          locationPolicy: 'any',
          jobs: [],
          jobsMeta: { returned: 0, parse_failures: 0, cutoff_iso: '', min_match: null, location_policy: 'any' },
          q: '',

          // Lookup state
          lookupQ: '',
          lookupLoaded: false,
          lookupLoading: false,
          lookupResults: [],
          selectedJobId: null,

          // Discovery state
          discoveryLoaded: false,
          discoveryLoading: false,
          discoverySaving: false,
          discoverySuggestions: [],
          selectedDiscoveryIds: [],
          discoveryMessage: '',

          sum(arr) { return (arr || []).reduce((a,b)=>a+(b||0), 0); },

          // ---------------- RUNS ----------------
          async loadRuns() {
            this.runsLoaded = false;
            const res = await fetch(`/api/data?days=${this.days}`);
            const data = await res.json();

            this.labels = data.labels || [];
            this.inserted = data.inserted || [];
            this.updated = data.updated || [];
            this.missing = data.missing || [];
            this.unchanged = data.unchanged || [];
            this.error = data.error || [];
            this.total_seen = data.total_seen || [];
            this.net_change = data.net_change || [];
            this.change_rate_pct = data.change_rate_pct || [];
            this.net_rate_pct = data.net_rate_pct || [];
            this.change_rate_ma7_pct = data.change_rate_ma7_pct || [];
            this.net_rate_ma7_pct = data.net_rate_ma7_pct || [];

            this.renderCountsChart();
            this.renderRatesChart();
            this.runsLoaded = true;
          },

          renderCountsChart() {
            const ctx = document.getElementById('runsChart').getContext('2d');

            const barDatasets = [
              { label: 'Inserted', data: this.inserted, stack: 'impact', borderWidth: 1,
                backgroundColor: 'rgba(16, 185, 129, 0.85)', borderColor: 'rgb(16, 185, 129)' },
              { label: 'Updated', data: this.updated, stack: 'impact', borderWidth: 1,
                backgroundColor: 'rgba(59, 130, 246, 0.85)', borderColor: 'rgb(59, 130, 246)' },
              { label: 'Missing', data: this.missing, stack: 'impact', borderWidth: 1,
                backgroundColor: 'rgba(249, 115, 22, 0.9)', borderColor: 'rgb(249, 115, 22)' }
            ];

            const totalDataset = {
              label: 'Total Seen',
              data: this.total_seen,
              type: 'line',
              yAxisID: 'y',
              borderColor: 'rgb(15, 23, 42)',
              backgroundColor: 'rgba(15, 23, 42, 0.08)',
              tension: 0.25,
              pointRadius: 3,
              borderWidth: 2,
            };

            const netChangeDataset = {
              label: 'Net Change (Inserted - Missing)',
              data: this.net_change,
              type: 'line',
              yAxisID: 'y',
              borderColor: 'rgb(34, 197, 94)',
              backgroundColor: 'rgba(34, 197, 94, 0.1)',
              tension: 0.25,
              pointRadius: 3,
              borderWidth: 2,
            };

            const config = {
              type: 'bar',
              data: { labels: this.labels, datasets: [...barDatasets, totalDataset, netChangeDataset] },
              options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: { x: { stacked: true, ticks: { autoSkip: true, maxRotation: 0 } },
                          y: { stacked: true, beginAtZero: true } },
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'bottom' } }
              },
            };

            if (this.chartCounts) this.chartCounts.destroy();
            this.chartCounts = new Chart(ctx, config);
          },

          renderRatesChart() {
            const ctx = document.getElementById('runsRateChart').getContext('2d');
            const datasets = [
              { label: 'Change %', data: this.change_rate_pct,
                borderColor: 'rgb(59, 130, 246)', backgroundColor: 'rgba(59, 130, 246, 0.1)',
                tension: 0.25, pointRadius: 2, borderWidth: 2 },
              { label: 'Change % (7d avg)', data: this.change_rate_ma7_pct,
                borderColor: 'rgb(59, 130, 246)', borderDash: [4,4],
                backgroundColor: 'rgba(59, 130, 246, 0.05)', tension: 0.25, pointRadius: 0, borderWidth: 2 },
              { label: 'Net %', data: this.net_rate_pct,
                borderColor: 'rgb(34, 197, 94)', backgroundColor: 'rgba(34, 197, 94, 0.1)',
                tension: 0.25, pointRadius: 2, borderWidth: 2 },
              { label: 'Net % (7d avg)', data: this.net_rate_ma7_pct,
                borderColor: 'rgb(34, 197, 94)', borderDash: [4,4],
                backgroundColor: 'rgba(34, 197, 94, 0.05)', tension: 0.25, pointRadius: 0, borderWidth: 2 },
            ];

            const config = {
              type: 'line',
              data: { labels: this.labels, datasets },
              options: {
                responsive: true,
                maintainAspectRatio: false,
                scales: { x: { ticks: { autoSkip: true, maxRotation: 0 } },
                          y: { beginAtZero: true, ticks: { callback: (v) => `${v}%` } } },
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'bottom' } }
              }
            };

            if (this.chartRates) this.chartRates.destroy();
            this.chartRates = new Chart(ctx, config);
          },

          // ---------------- JOBS ----------------
          async loadJobs() {
            this.jobsLoaded = false;
            const params = new URLSearchParams({
              hours: String(this.hours || 0),
              limit: String(this.jobsLimit || 500)
            });
            if (this.minAiMatch !== null && this.minAiMatch !== '' && !Number.isNaN(Number(this.minAiMatch))) {
              params.set('min_match', String(this.minAiMatch));
            }
            if (this.locationPolicy) {
              params.set('location_policy', this.locationPolicy);
            }
            const res = await fetch(`/api/jobs?${params.toString()}`);
            const data = await res.json();
            this.jobsMeta = {
              returned: data.returned || 0,
              min_match: data.min_match ?? null,
              location_policy: data.location_policy || 'any',
              parse_failures: data.parse_failures || 0,
              cutoff_iso: data.cutoff_iso || ''
            };
            this.jobs = (data.jobs || []).map(x => ({ ...x, _showAi: false, _showAiRaw: false }));
            this.jobsLoaded = true;
          },

          matchValue(row) {
            const fromColumn = row?.ai_row?.match_percentage;
            const fromJson = row?.ai?.match_percentage;
            const value = fromColumn ?? fromJson;
            if (value === null || value === undefined || value === '') return null;
            const n = Number(value);
            return Number.isFinite(n) ? n : null;
          },

          locationValue(row) {
            const fromColumn = row?.ai_row?.location_policy_match;
            const fromJson = row?.ai?.location_policy_match;
            const value = (fromColumn ?? fromJson ?? '').toString().toLowerCase().trim();
            return value || null;
          },

          locationMatches(row) {
            const filter = (this.locationPolicy || 'any').toLowerCase();
            if (filter === 'any') return true;
            const value = this.locationValue(row);
            if (filter === 'non_hybrid') return value !== 'hybrid';
            return value === filter;
          },

          filteredJobs() {
            const q = (this.q || '').toLowerCase().trim();
            const minMatch = Number(this.minAiMatch);
            return (this.jobs || []).filter(r => {
              if (!this.locationMatches(r)) return false;
              if (Number.isFinite(minMatch)) {
                const match = this.matchValue(r);
                if (match === null || match < minMatch) return false;
              }
              if (!q) return true;
              const hay = `${r.site||''} ${r.job_id||''} ${r.title||''} ${r.level||''} ${r.pay||''}`.toLowerCase();
              return hay.includes(q);
            });
          },

          async lookupJob() {
            const q = (this.lookupQ || '').trim();
            if (!q) return;

            this.lookupLoading = true;
            this.lookupLoaded = false;
            this.lookupResults = [];
            this.selectedJobId = null;

            const res = await fetch(`/api/job-lookup?q=${encodeURIComponent(q)}`);
            const data = await res.json();
            this.lookupResults = data.jobs || [];
            this.selectedJobId = this.lookupResults.length ? this.lookupResults[0].id : null;
            this.lookupLoaded = true;
            this.lookupLoading = false;
          },

          async openJobDetail(id) {
            const jobId = Number(id);
            if (!Number.isFinite(jobId) || jobId <= 0) return;

            this.view = 'lookup';
            this.lookupQ = `ID ${jobId}`;
            this.lookupLoading = true;
            this.lookupLoaded = false;
            this.lookupResults = [];
            this.selectedJobId = null;

            const res = await fetch(`/api/job-detail?id=${encodeURIComponent(jobId)}`);
            const data = await res.json();
            this.lookupResults = data.job ? [data.job] : [];
            this.selectedJobId = data.job ? data.job.id : null;
            this.lookupLoaded = true;
            this.lookupLoading = false;
          },

          selectedJob() {
            return (this.lookupResults || []).find(j => j.id === this.selectedJobId) || null;
          },

          // ---------------- DISCOVERY ----------------
          async loadDiscovery() {
            this.discoveryLoading = true;
            this.discoveryMessage = '';
            const res = await fetch('/api/discovery/suggestions');
            const data = await res.json();
            this.discoverySuggestions = data.suggestions || [];
            this.selectedDiscoveryIds = this.selectedDiscoveryIds.filter(id =>
              this.discoverySuggestions.some(item => item.id === id && ['pending','approved'].includes(item.state || 'pending'))
            );
            this.discoveryLoaded = true;
            this.discoveryLoading = false;
          },

          isDiscoverySelected(id) {
            return this.selectedDiscoveryIds.includes(id);
          },

          toggleDiscovery(id) {
            if (this.isDiscoverySelected(id)) {
              this.selectedDiscoveryIds = this.selectedDiscoveryIds.filter(x => x !== id);
            } else {
              this.selectedDiscoveryIds = [...this.selectedDiscoveryIds, id];
            }
          },

          async applyDiscoverySelected() {
            if (!this.selectedDiscoveryIds.length || this.discoverySaving) return;
            this.discoverySaving = true;
            this.discoveryMessage = '';
            try {
              const res = await fetch('/api/discovery/apply', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids: this.selectedDiscoveryIds})
              });
              const data = await res.json();
              if (!res.ok) throw new Error(data.error || 'Apply failed');
              this.discoveryMessage = `Applied ${data.applied.length} suggestion(s); skipped ${data.skipped.length}.`;
              this.selectedDiscoveryIds = [];
              await this.loadDiscovery();
            } catch (err) {
              this.discoveryMessage = err?.message || 'Apply failed.';
            } finally {
              this.discoverySaving = false;
            }
          },

          async rejectDiscoverySelected() {
            if (!this.selectedDiscoveryIds.length || this.discoverySaving) return;
            this.discoverySaving = true;
            this.discoveryMessage = '';
            try {
              const res = await fetch('/api/discovery/reject', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ids: this.selectedDiscoveryIds})
              });
              const data = await res.json();
              if (!res.ok) throw new Error(data.error || 'Reject failed');
              this.discoveryMessage = `Rejected ${data.changed.length} suggestion(s).`;
              this.selectedDiscoveryIds = [];
              await this.loadDiscovery();
            } catch (err) {
              this.discoveryMessage = err?.message || 'Reject failed.';
            } finally {
              this.discoverySaving = false;
            }
          },

          init() {
            this.loadRuns();
            this.loadJobs();
          }
        }
      }
    </script>
  </body>
</html>
"""

STEPS_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Steps JSON Editor</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  </head>
  <body class="bg-slate-50 text-slate-900 min-h-screen">
    <main class="max-w-7xl mx-auto p-4 space-y-4" x-data="stepsEditor()" x-init="load()">
      <header class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
        <div>
          <h1 class="text-2xl font-bold tracking-tight">Steps JSON Editor</h1>
          <p class="text-sm text-slate-500 mt-1">
            <span x-text="path || 'steps.json'"></span>
            <span x-show="siteCount !== null"> · sites=<span x-text="siteCount"></span></span>
            <span x-show="modifiedAt"> · modified=<span x-text="modifiedAt"></span></span>
          </p>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <a href="/"
             class="px-3 py-2 rounded-lg border border-slate-200 bg-white text-sm text-slate-700 hover:bg-slate-50">
            Dashboard
          </a>
          <button type="button" @click="reload()"
                  class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 hover:bg-slate-50">
            Reload
          </button>
          <button type="button" @click="formatValidate()" :disabled="loading || saving"
                  class="px-3 py-2 rounded-lg border border-slate-300 bg-white text-sm text-slate-700 hover:bg-slate-50 disabled:opacity-40">
            Format / Validate
          </button>
          <button type="button" @click="preview()" :disabled="loading || saving"
                  class="px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm hover:bg-indigo-700 disabled:opacity-40">
            Preview changes
          </button>
          <button type="button" @click="save()" :disabled="!canSave() || saving"
                  class="px-3 py-2 rounded-lg bg-emerald-600 text-white text-sm hover:bg-emerald-700 disabled:opacity-40">
            Save changes
          </button>
        </div>
      </header>

      <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4" x-show="message || error || saving || loading">
        <div class="text-sm text-slate-600" x-show="loading">Loading steps.json...</div>
        <div class="text-sm text-slate-600" x-show="saving">Saving steps.json...</div>
        <div class="text-sm text-emerald-700" x-show="message" x-text="message"></div>
        <div class="text-sm text-red-700" x-show="error" x-text="error"></div>
      </section>

      <section class="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <div class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4 space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-sm font-semibold text-slate-800">Raw JSON</h2>
            <span class="text-xs" :class="isDirty() ? 'text-amber-700' : 'text-slate-500'"
                  x-text="isDirty() ? 'Unsaved edits' : 'Loaded'"></span>
          </div>
          <textarea x-model="content" spellcheck="false"
                    @input="previewContent=''; diff=''; hasChanges=false"
                    class="w-full h-[42rem] font-mono text-xs leading-5 rounded-lg border border-slate-300 bg-slate-950 text-slate-50 p-3 focus:outline-none focus:ring-2 focus:ring-indigo-500"></textarea>
        </div>

        <div class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-4 space-y-3">
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-sm font-semibold text-slate-800">Change preview</h2>
            <span class="text-xs text-slate-500" x-text="hasChanges ? 'Diff ready' : 'No diff previewed'"></span>
          </div>
          <pre class="h-[42rem] overflow-auto rounded-lg border border-slate-200 bg-slate-950 text-slate-50 p-3 text-xs leading-5"
               x-text="diff || 'Preview changes to see a unified diff before saving.'"></pre>
        </div>
      </section>
    </main>

    <script>
      function stepsEditor() {
        return {
          loading: false,
          saving: false,
          path: '',
          modifiedAt: '',
          siteCount: null,
          originalContent: '',
          content: '',
          previewContent: '',
          diff: '',
          hasChanges: false,
          message: '',
          error: '',

          isDirty() {
            return this.content !== this.originalContent;
          },

          canSave() {
            return this.hasChanges && this.previewContent && this.content === this.previewContent;
          },

          async load() {
            this.loading = true;
            this.message = '';
            this.error = '';
            try {
              const res = await fetch('/api/steps');
              const data = await res.json();
              if (!res.ok) throw new Error(data.error || 'Could not load steps.json');
              this.path = data.path || '';
              this.modifiedAt = data.modified_at || '';
              this.siteCount = data.site_count ?? null;
              this.originalContent = data.content || '';
              this.content = this.originalContent;
              this.previewContent = '';
              this.diff = '';
              this.hasChanges = false;
              this.message = 'Loaded steps.json.';
            } catch (err) {
              this.error = err?.message || 'Could not load steps.json';
            } finally {
              this.loading = false;
            }
          },

          async reload() {
            if (this.isDirty() && !window.confirm('Discard unsaved edits and reload steps.json?')) return;
            await this.load();
          },

          async requestPreview() {
            const res = await fetch('/api/steps/preview', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({content: this.content})
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.error || 'Preview failed');
            return data;
          },

          async formatValidate() {
            this.message = '';
            this.error = '';
            try {
              const data = await this.requestPreview();
              this.content = data.content || '';
              this.previewContent = '';
              this.diff = '';
              this.hasChanges = false;
              this.siteCount = data.site_count ?? this.siteCount;
              this.message = data.has_changes ? 'JSON is valid and formatted.' : 'JSON is valid. No changes.';
            } catch (err) {
              this.error = err?.message || 'Validation failed';
            }
          },

          async preview() {
            this.message = '';
            this.error = '';
            try {
              const data = await this.requestPreview();
              this.content = data.content || '';
              this.previewContent = this.content;
              this.diff = data.diff || '';
              this.hasChanges = Boolean(data.has_changes);
              this.siteCount = data.site_count ?? this.siteCount;
              this.message = data.message || (this.hasChanges ? 'Changes ready to review.' : 'No changes.');
            } catch (err) {
              this.previewContent = '';
              this.diff = '';
              this.hasChanges = false;
              this.error = err?.message || 'Preview failed';
            }
          },

          async save() {
            if (!this.canSave() || this.saving) return;
            if (!window.confirm('Save these reviewed changes to steps.json?')) return;
            this.saving = true;
            this.message = '';
            this.error = '';
            try {
              const res = await fetch('/api/steps/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({content: this.previewContent})
              });
              const data = await res.json();
              if (!res.ok) throw new Error(data.error || 'Save failed');
              this.path = data.path || this.path;
              this.modifiedAt = data.modified_at || this.modifiedAt;
              this.siteCount = data.site_count ?? this.siteCount;
              this.content = data.content || this.previewContent;
              this.originalContent = this.content;
              this.previewContent = '';
              this.diff = '';
              this.hasChanges = false;
              this.message = data.saved
                ? `Saved steps.json. Backup: ${data.backup_path || 'none'}`
                : (data.message || 'No changes to save.');
            } catch (err) {
              this.error = err?.message || 'Save failed';
            } finally {
              this.saving = false;
            }
          }
        }
      }
    </script>
  </body>
</html>
"""

SWIPE_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>Job Swipe</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  </head>
  <body class="bg-slate-50 text-slate-900 min-h-screen">
    <main class="max-w-5xl mx-auto p-4 space-y-4" x-data="jobSwiper()" x-init="fetchJobs()">
      <header class="flex items-center justify-between gap-3">
        <div>
          <h1 class="text-2xl font-bold tracking-tight">Job Swipe</h1>
          <p class="text-sm text-slate-500">
            <span x-text="idx < jobs.length ? `${idx + 1} of ${jobs.length}` : `${jobs.length} reviewed`"></span>
          </p>
        </div>
        <a href="/" class="px-3 py-2 rounded-lg border border-slate-200 bg-white text-sm text-slate-700 hover:bg-slate-50">
          Dashboard
        </a>
      </header>

      <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-6" x-show="loading">
        <div class="text-sm text-slate-500">Loading jobs...</div>
      </section>

      <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-6" x-show="error">
        <div class="text-sm text-red-700" x-text="error"></div>
      </section>

      <template x-if="!loading && !error && jobs.length > 0 && idx < jobs.length">
        <article class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-6 space-y-4">
          <div class="flex flex-col md:flex-row md:items-start md:justify-between gap-3">
            <div>
              <h2 class="text-2xl font-semibold text-slate-950" x-text="currentJob().JobTitle || 'No title'"></h2>
              <div class="mt-2 flex flex-wrap items-center gap-2 text-sm">
                <span class="font-medium">Level:</span>
                <span x-text="currentJob().JobLevel || 'Unknown'"
                      :class="`text-white px-2 py-0.5 rounded ${levelClass()}`"></span>
                <span class="font-medium ml-2">Pay:</span>
                <span x-text="currentJob().JobPay || '-'"></span>
                <span class="font-medium ml-2">Discovered:</span>
                <span x-text="currentJob().DiscoveryDate || '-'"></span>
              </div>
              <div class="mt-1 text-xs text-slate-500">
                ID: <span x-text="currentJob().JobID || '-'"></span>
              </div>
            </div>
            <a :href="currentJob().JobUrl || '#'"
               x-show="currentJob().JobUrl"
               class="inline-flex items-center justify-center px-3 py-2 rounded-lg bg-slate-900 text-white text-sm hover:bg-slate-800"
               target="_blank" rel="noreferrer">
              Open posting
            </a>
          </div>

          <div class="prose prose-sm max-w-none text-slate-800">
            <p class="whitespace-pre-wrap leading-6" x-html="currentJob().JobDescHighlighted || 'No description'"></p>
          </div>

          <div class="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm">
            <span class="font-medium text-slate-700">Keywords:</span>
            <span class="text-slate-700" x-text="currentJob().Keywords || '-'"></span>
          </div>

          <div class="flex flex-col sm:flex-row justify-center gap-3 pt-2">
            <button class="bg-red-600 hover:bg-red-700 text-white px-6 py-3 rounded-lg text-base font-semibold"
                    :disabled="saving"
                    @click="swipe('dislike')">
              Dislike
            </button>
            <button class="bg-emerald-600 hover:bg-emerald-700 text-white px-6 py-3 rounded-lg text-base font-semibold"
                    :disabled="saving"
                    @click="swipe('like')">
              Like
            </button>
          </div>
        </article>
      </template>

      <section class="bg-white rounded-xl shadow-sm ring-1 ring-slate-200 p-8 text-center"
               x-show="!loading && !error && (jobs.length === 0 || idx >= jobs.length)">
        <h2 class="text-2xl font-semibold">No more jobs</h2>
        <button class="mt-4 px-3 py-2 rounded-lg bg-indigo-600 text-white text-sm hover:bg-indigo-700"
                @click="fetchJobs()">
          Refresh
        </button>
      </section>
    </main>

    <script>
      function jobSwiper() {
        return {
          jobs: [],
          idx: 0,
          loading: false,
          saving: false,
          error: '',

          async fetchJobs() {
            this.loading = true;
            this.error = '';
            try {
              const res = await fetch('/api/swipe/jobs');
              if (!res.ok) throw new Error(await res.text());
              this.jobs = await res.json();
              this.idx = 0;
            } catch (err) {
              this.error = err?.message || 'Failed to load swipe jobs.';
            } finally {
              this.loading = false;
            }
          },

          currentJob() {
            return this.jobs[this.idx] || {};
          },

          levelClass() {
            const lvl = (this.currentJob().JobLevel || '').toLowerCase();
            const map = {
              architect: 'bg-red-600',
              senior: 'bg-orange-500',
              leader: 'bg-slate-500',
              junior: 'bg-blue-500',
              unknown: 'bg-slate-400',
            };
            return map[lvl] || 'bg-slate-400';
          },

          async swipe(action) {
            if (this.saving) return;
            const job = this.currentJob();
            this.saving = true;
            this.error = '';
            try {
              const res = await fetch('/api/swipe', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({job, action})
              });
              if (!res.ok) throw new Error(await res.text());
              this.idx++;
              window.scrollTo({top: 0, behavior: 'smooth'});
            } catch (err) {
              this.error = err?.message || 'Failed to save swipe.';
            } finally {
              this.saving = false;
            }
          }
        }
      }
    </script>
  </body>
</html>
"""


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/swipe")
def swipe_page():
    return render_template_string(SWIPE_HTML)


@app.route("/steps")
def steps_page():
    return render_template_string(STEPS_HTML)


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
    try:
        hours = int(request.args.get("hours", "48"))
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
