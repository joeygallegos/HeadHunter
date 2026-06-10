# dashboard.py
from __future__ import annotations

import json
import os
import html
import re
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template_string, request
from sqlalchemy import func, or_, select, text as sa_text

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
from app.models import JobChange, SessionLocal, IntegrationRun, Job  # type: ignore


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
LOCAL_TZ = "America/Chicago"
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


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


# ----------------------------------------------------------------------
# Swipe page: Google Sheets-backed review queue
# ----------------------------------------------------------------------
def _load_sheet_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            "config.json file not found. Please ensure it exists in the project root."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as config_file:
        return json.load(config_file)


def _init_google_sheet():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise RuntimeError(
            "Swipe page requires gspread and google-auth; install requirements.txt."
        ) from exc

    config = _load_sheet_config()
    creds_path = os.path.join(BASE_DIR, config["GOOGLE_CREDENTIALS_PATH"])
    creds = Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    client = gspread.authorize(creds)
    return client.open_by_key(config["SHEET_ID"]).worksheet(config["SHEET_NAME"])


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
        out.append(f"<mark class='bg-yellow-200'>{html.escape(text[start:stop])}</mark>")
        cursor = stop
    if cursor < len(text):
        out.append(html.escape(text[cursor:]))
    return "".join(out)


def fetch_swipe_jobs() -> List[Dict[str, Any]]:
    sheet = _init_google_sheet()
    jobs = sheet.get_all_records()
    filtered_jobs = [job for job in jobs if not job.get("Swipe")]
    for job in filtered_jobs:
        job["JobDescHighlighted"] = highlight_as_you_will(
            str(job.get("JobTitle", "")), str(job.get("JobDesc", ""))
        )
    return filtered_jobs


def record_swipe(job: Dict[str, Any], action: str) -> bool:
    sheet = _init_google_sheet()
    jobs = sheet.get_all_records()
    row_idx = None
    for idx, row in enumerate(jobs, start=2):
        if row.get("JobUrl") == job.get("JobUrl"):
            row_idx = idx
            break

    if not row_idx:
        return False

    headers = sheet.row_values(1)
    if "Swipe" not in headers:
        sheet.update_cell(1, len(headers) + 1, "Swipe")
        swipe_col = len(headers) + 1
    else:
        swipe_col = headers.index("Swipe") + 1
    sheet.update_cell(row_idx, swipe_col, action)
    return True


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
# Data access: Jobs discovered in last N hours (default 48)
# ----------------------------------------------------------------------
def fetch_jobs_last_hours(
    hours: int = 48, limit: int = 500, min_match: Optional[int] = None
) -> Dict[str, Any]:
    all_time = hours <= 0
    hours = 0 if all_time else max(1, min(hours, 24 * 30))
    limit = max(1, min(limit, 5000))
    if min_match is not None:
        min_match = max(0, min(int(min_match), 100))

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
            Jobs discovered (last 48h)
          </button>
          <button @click="view='lookup'"
                  :class="view==='lookup' ? 'bg-indigo-600 text-white' : 'bg-white text-slate-700'"
                  class="px-3 py-2 rounded-lg border border-slate-200 text-sm">
            Job lookup
          </button>
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

      <!-- ---------------- JOBS VIEW ---------------- -->
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
                Jobs <span x-text="hours > 0 ? `discovered in last ${hours} hours` : 'from all time'"></span>
              </h2>
              <div class="text-xs text-slate-500" x-show="jobsLoaded">
                returned=<span x-text="jobsMeta.returned"></span>,
                min_match=<span x-text="jobsMeta.min_match ?? '-'"></span>,
                parse_failures=<span x-text="jobsMeta.parse_failures"></span>,
                cutoff=<span x-text="jobsMeta.cutoff_iso"></span>
              </div>
            </div>

            <div class="mt-3 overflow-x-auto">
              <table class="min-w-full text-sm">
                <thead class="text-left text-slate-600 border-b">
                  <tr>
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
          jobs: [],
          jobsMeta: { returned: 0, parse_failures: 0, cutoff_iso: '', min_match: null },
          q: '',

          // Lookup state
          lookupQ: '',
          lookupLoaded: false,
          lookupLoading: false,
          lookupResults: [],
          selectedJobId: null,

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
            const res = await fetch(`/api/jobs?${params.toString()}`);
            const data = await res.json();
            this.jobsMeta = {
              returned: data.returned || 0,
              min_match: data.min_match ?? null,
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

          filteredJobs() {
            const q = (this.q || '').toLowerCase().trim();
            const minMatch = Number(this.minAiMatch);
            return (this.jobs || []).filter(r => {
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

          selectedJob() {
            return (this.lookupResults || []).find(j => j.id === this.selectedJobId) || null;
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
            return jsonify(error="job not found in sheet"), 404
        return jsonify(success=True)
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
    return jsonify(fetch_jobs_last_hours(hours=hours, limit=limit, min_match=min_match))


@app.route("/api/job-lookup")
def api_job_lookup():
    q = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", "25"))
    except Exception:
        limit = 25
    return jsonify(fetch_job_lookup(q, limit=limit))


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
