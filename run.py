# /run.py
from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List

# Ensure package import from any cwd
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Load .env here so env vars are available to this module
from dotenv import load_dotenv

load_dotenv()

# SQLAlchemy
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

# Project imports
from app.scraper import StepScraper
from app.json_writer import save_site_json
from app.utils import (
    ensure_nltk,
    extract_keywords,
    get_job_level,
    scan_for_pay_range,
    remove_canned_text,
)
from app.models import (
    SessionLocal,
    init_db,
    IntegrationRun,
    Job,
    JobChange,
)

OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# -------------------------------
# Logging (single-file)
# -------------------------------

_LOGGER_NAME = "app"
_LOG_DIR = None
_RUN_FILE_HANDLER: RotatingFileHandler | None = None


class _ContextAdapter(logging.LoggerAdapter):
    """Inject stable fields so every line has run/site/job (why: actionable logs)."""

    def process(self, msg, kwargs):
        extra = dict(self.extra)
        incoming = kwargs.get("extra") or {}
        extra.update(incoming)
        kwargs["extra"] = extra
        return msg, kwargs


class DefaultContextFilter(logging.Filter):
    """Guarantee required fields exist on *all* records to avoid KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        if not hasattr(record, "site"):
            record.site = "-"
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


class Timer:
    """Duration helper, logs success/failure durations."""

    def __init__(self, label: str, logger: logging.Logger, **ctx):
        self.label = label
        self.logger = _ContextAdapter(logger, ctx)
        self.t0 = None

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        dt = (time.perf_counter() - self.t0) if self.t0 else 0.0
        if exc:
            self.logger.exception("%s failed in %.3fs", self.label, dt)
        else:
            self.logger.info("%s done in %.3fs", self.label, dt)


def _ensure_log_dir() -> str:
    global _LOG_DIR
    if _LOG_DIR:
        return _LOG_DIR
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    _LOG_DIR = log_dir
    return log_dir


def _mask_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        if "://" in url and "@" in url:
            scheme, rest = url.split("://", 1)
            creds_host = rest.split("@", 1)
            if len(creds_host) == 2 and ":" in creds_host[0]:
                user = creds_host[0].split(":", 1)[0]
                return f"{scheme}://{user}:***@{creds_host[1]}"
    except Exception:
        return "<masked>"
    return url


def setup_logging(
    level: str | None = None, sql_level: str | None = None
) -> logging.Logger:
    log_dir = _ensure_log_dir()
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    sql_level = (sql_level or os.getenv("LOG_SQL", "WARNING")).upper()

    fmt = (
        "%(asctime)s %(levelname)s "
        "run=%(run_id)s site=%(site)s job=%(job_id)s "
        "%(name)s:%(funcName)s:%(lineno)d | %(message)s"
    )
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Root logger
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, level, logging.INFO))

    # Defaulting filter to prevent KeyError on third-party records
    default_filter = DefaultContextFilter()

    # Console
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(getattr(logging, level, logging.INFO))
    sh.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    sh.addFilter(default_filter)
    root.addHandler(sh)

    # Rotating app.log
    fh = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(getattr(logging, level, logging.INFO))
    fh.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    fh.addFilter(default_filter)
    root.addHandler(fh)

    # Also attach to root so *all* child loggers inherit the default fields
    root.addFilter(default_filter)

    # SQLAlchemy logs
    logging.getLogger("sqlalchemy.engine").setLevel(
        getattr(logging, sql_level, logging.WARNING)
    )
    logging.getLogger("sqlalchemy.pool").setLevel(
        getattr(logging, sql_level, logging.WARNING)
    )

    logger = logging.getLogger(_LOGGER_NAME)

    # Unhandled exception capture
    def _excepthook(exc_type, exc, tb):
        _ContextAdapter(logger, {"run_id": "-", "site": "-", "job_id": "-"}).critical(
            "Unhandled exception", exc_info=(exc_type, exc, tb)
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return logger


def add_run_file_handler(run_label: str) -> None:
    """Attach a per-run rotating file if not yet attached."""
    global _RUN_FILE_HANDLER
    if _RUN_FILE_HANDLER:
        return
    if not _LOG_DIR:
        _ensure_log_dir()
    path = os.path.join(_LOG_DIR, f"run_{run_label}.log")
    h = RotatingFileHandler(path, maxBytes=50_000_000, backupCount=1, encoding="utf-8")
    h.setLevel(logging.DEBUG)
    fmt = (
        "%(asctime)s %(levelname)s "
        "run=%(run_id)s site=%(site)s job=%(job_id)s "
        "%(name)s:%(funcName)s:%(lineno)d | %(message)s"
    )
    h.setFormatter(logging.Formatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S"))
    # IMPORTANT: add the same defaulting filter here too
    h.addFilter(DefaultContextFilter())
    logging.getLogger().addHandler(h)
    _RUN_FILE_HANDLER = h


def get_logger(**extra_ctx: Any) -> logging.LoggerAdapter:
    ctx = {"run_id": "-", "site": "-", "job_id": "-"}
    ctx.update({k: (v if v is not None else "-") for k, v in extra_ctx.items()})
    return _ContextAdapter(logging.getLogger(_LOGGER_NAME), ctx)


def log_startup_environment(logger: logging.LoggerAdapter) -> None:
    env = {
        "DB_COMMIT_MODE": os.getenv("DB_COMMIT_MODE", ""),
        "DATABASE_URL": _mask_url(os.getenv("DATABASE_URL")),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", ""),
        "LOG_SQL": os.getenv("LOG_SQL", ""),
    }
    logger.info("startup env %s | base_dir=%s", env, BASE_DIR)


# -------------------------------
# Helpers
# -------------------------------


def _norm_text(x: str | None) -> str:
    return (x or "").replace("\u202f", " ").replace("\u00a0", " ").strip()


def _job_hash(
    title: str, url: str, desc: str, keywords: str, level: str, pay: str
) -> str:
    canon = "|".join([title, url, desc, keywords, level, pay]).encode("utf-8", "ignore")
    return hashlib.sha256(canon).hexdigest()


def _changed_fields(old: Job, new: Job) -> List[str]:
    fields = ["title", "url", "desc", "keywords", "level", "pay"]
    return [f for f in fields if (getattr(old, f) or "") != (getattr(new, f) or "")]


def _normalize_job(site: str, run_id: int, job_data: Dict[str, Any]) -> Job | None:
    job_id = str(job_data.get("JobID", "")).strip()
    if not job_id:
        return None
    title = _norm_text(str(job_data.get("JobTitle", "")).strip())
    url = _norm_text(str(job_data.get("JobUrl", "")).strip())
    desc = _norm_text(str(job_data.get("JobDesc", "")).strip())
    keywords = ", ".join(extract_keywords(desc))
    level = get_job_level(title)
    pay_hits = scan_for_pay_range(desc)
    pay = pay_hits[0] if pay_hits else "Unknown"
    discovery_date = time.strftime("%m/%d/%Y")

    job = Job(
        job_id=job_id,
        site=site,
        title=title,
        url=url,
        desc=desc,
        keywords=keywords,
        level=level,
        pay=pay,
        discovery_date=discovery_date,
        run_id=run_id,
    )
    job.content_hash = _job_hash(title, url, desc, keywords, level, pay)
    return job


# -------------------------------
# Delta logic
# -------------------------------


def _process_site(
    s,
    site: str,
    jobs_raw: List[Dict[str, Any]],
    run_id: int,
    counters: Dict[str, int],
    *,
    use_savepoints: bool,
    per_job_commit: bool,
    logger,
) -> None:
    """Perform inserts/updates/missing for a single site."""
    site_log = get_logger(run_id=run_id, site=site)
    if not jobs_raw:
        site_log.info("no jobs for site")
        return

    with Timer(f"{site} preproc+save_json", logger=site_log):
        jobs_raw = remove_canned_text(jobs_raw)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        save_site_json(OUTPUT_DIR, site, jobs_raw)

    existing_by_id: Dict[str, Job] = {
        row.job_id: row
        for row in s.execute(select(Job).where(Job.site == site)).scalars()
    }
    missing_ids = set(existing_by_id.keys())

    def _do_commit():
        try:
            s.commit()
        except Exception:
            s.rollback()
            site_log.exception("commit failed")
            raise

    for jd in jobs_raw:
        job_obj = _normalize_job(site, run_id, jd)
        job_id = str(jd.get("JobID", "")).strip() or "-"
        row_log = get_logger(run_id=run_id, site=site, job_id=job_id)

        if not job_obj:
            counters["error_count"] += 1
            row_log.warning("skip row: missing JobID")
            continue

        counters["total_seen"] += 1
        prev = existing_by_id.get(job_obj.job_id)

        def _insert_job():
            job_obj.is_active = True
            job_obj.first_seen_run_id = run_id
            job_obj.last_seen_run_id = run_id
            s.add(job_obj)
            s.flush()
            s.add(
                JobChange(
                    run_id=run_id,
                    job_pk=job_obj.id,
                    job_id_text=job_obj.job_id,
                    site=site,
                    change_type="insert",
                    old_hash=None,
                    new_hash=job_obj.content_hash,
                    changed_fields="title,url,desc,keywords,level,pay",
                )
            )
            counters["inserted_count"] += 1
            row_log.info("inserted")
            existing_by_id[job_obj.job_id] = job_obj

        def _update_job():
            old_hash = prev.content_hash
            changed = _changed_fields(prev, job_obj)
            prev.title = job_obj.title
            prev.url = job_obj.url
            prev.desc = job_obj.desc
            prev.keywords = job_obj.keywords
            prev.level = job_obj.level
            prev.pay = job_obj.pay
            prev.discovery_date = job_obj.discovery_date
            prev.content_hash = job_obj.content_hash
            prev.is_active = True
            prev.last_seen_run_id = run_id
            s.add(
                JobChange(
                    run_id=run_id,
                    job_pk=prev.id,
                    job_id_text=prev.job_id,
                    site=site,
                    change_type="update",
                    old_hash=old_hash,
                    new_hash=job_obj.content_hash,
                    changed_fields=",".join(changed),
                )
            )
            counters["updated_count"] += 1
            row_log.info("updated fields=%s", changed)

        if prev is None:
            if per_job_commit:
                try:
                    _insert_job()
                    _do_commit()
                except IntegrityError:
                    s.rollback()
                    counters["error_count"] += 1
                    row_log.exception("insert integrity error")
                continue
            elif use_savepoints:
                try:
                    with s.begin_nested():
                        _insert_job()
                except IntegrityError:
                    counters["error_count"] += 1
                    row_log.exception("insert integrity error (savepoint)")
                continue
            else:
                try:
                    _insert_job()
                except IntegrityError:
                    counters["error_count"] += 1
                    row_log.exception("insert integrity error (no sp)")
                continue

        # Existing
        missing_ids.discard(job_obj.job_id)
        if prev.content_hash != job_obj.content_hash:
            if per_job_commit:
                try:
                    _update_job()
                    _do_commit()
                except IntegrityError:
                    s.rollback()
                    counters["error_count"] += 1
                    row_log.exception("update integrity error")
            elif use_savepoints:
                try:
                    with s.begin_nested():
                        _update_job()
                except IntegrityError:
                    counters["error_count"] += 1
                    row_log.exception("update integrity error (savepoint)")
            else:
                try:
                    _update_job()
                except IntegrityError:
                    counters["error_count"] += 1
                    row_log.exception("update integrity error (no sp)")
        else:
            prev.is_active = True
            prev.last_seen_run_id = run_id
            if per_job_commit:
                _do_commit()
            else:
                counters["unchanged_count"] += 1
            row_log.debug("unchanged")

    # Mark missing after processing all seen items
    if missing_ids:
        site_log.info("marking missing count=%d", len(missing_ids))
        rows = (
            s.execute(
                select(Job).where(Job.site == site, Job.job_id.in_(list(missing_ids)))
            )
            .scalars()
            .all()
        )
        for r in rows:
            if r.is_active:

                def _mark_missing():
                    r.is_active = False
                    s.add(
                        JobChange(
                            run_id=run_id,
                            job_pk=r.id,
                            job_id_text=r.job_id,
                            site=site,
                            change_type="missing",
                            old_hash=r.content_hash,
                            new_hash=None,
                            changed_fields="",
                        )
                    )
                    counters["missing_count"] += 1
                    get_logger(run_id=run_id, site=site, job_id=r.job_id).info(
                        "marked missing"
                    )

                if per_job_commit:
                    _mark_missing()
                    _do_commit()
                elif use_savepoints:
                    with s.begin_nested():
                        _mark_missing()
                else:
                    _mark_missing()


# -------------------------------
# Main
# -------------------------------


def main():
    setup_logging()
    base_log = get_logger()
    log_startup_environment(base_log)

    if len(sys.argv) < 2:
        base_log.error("Usage: python run.py [initdb|steps|test|download]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "download":
        with Timer("NLTK ensure", logger=base_log):
            ensure_nltk()
        base_log.info("NLTK ready")
        sys.exit(0)

    commit_mode = os.getenv("DB_COMMIT_MODE", "all_at_end").lower()
    if commit_mode not in {"all_at_end", "per_site", "per_job"}:
        base_log.warning(
            "invalid DB_COMMIT_MODE=%s; defaulting to all_at_end", commit_mode
        )
        commit_mode = "all_at_end"

    with Timer("init_db", logger=base_log):
        init_db()

    test_mode = cmd == "test"
    steps_path = os.path.join(BASE_DIR, "test.json" if test_mode else "steps.json")

    with Timer("ensure_nltk", logger=base_log):
        ensure_nltk()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    with SessionLocal.begin() as s:
        run = IntegrationRun(
            user=getpass.getuser(), mode=("test" if test_mode else "steps")
        )
        s.add(run)
        s.flush()
        run_id = run.id

    ts_label = time.strftime("%Y%m%d_%H%M%S")
    add_run_file_handler(f"{ts_label}_{run_id}")
    run_log = get_logger(run_id=run_id)
    run_log.info("run opened | commit_mode=%s | steps_path=%s", commit_mode, steps_path)

    with Timer("scraper.run", logger=run_log):
        scraper = StepScraper(steps_path, headless=False)
        site_to_jobs = scraper.run()
    run_log.info("scraper returned sites=%d", len(site_to_jobs))

    counters = {
        "total_seen": 0,
        "inserted_count": 0,
        "updated_count": 0,
        "missing_count": 0,
        "unchanged_count": 0,
        "error_count": 0,
    }

    try:
        if commit_mode == "per_job":
            with SessionLocal() as s:
                for site, jobs_raw in site_to_jobs.items():
                    if not jobs_raw:
                        get_logger(run_id=run_id, site=site).info("skip empty site")
                        continue
                    with Timer(f"persist {site}", logger=run_log, site=site):
                        _process_site(
                            s,
                            site,
                            jobs_raw,
                            run_id,
                            counters,
                            use_savepoints=False,
                            per_job_commit=True,
                            logger=run_log,
                        )

        elif commit_mode == "per_site":
            with SessionLocal() as s:
                for site, jobs_raw in site_to_jobs.items():
                    if not jobs_raw:
                        get_logger(run_id=run_id, site=site).info("skip empty site")
                        continue
                    with s.begin():
                        with Timer(f"persist {site}", logger=run_log, site=site):
                            _process_site(
                                s,
                                site,
                                jobs_raw,
                                run_id,
                                counters,
                                use_savepoints=True,
                                per_job_commit=False,
                                logger=run_log,
                            )

        else:  # all_at_end
            with SessionLocal() as s:
                with s.begin():
                    for site, jobs_raw in site_to_jobs.items():
                        if not jobs_raw:
                            get_logger(run_id=run_id, site=site).info("skip empty site")
                            continue
                        with Timer(f"persist {site}", logger=run_log, site=site):
                            _process_site(
                                s,
                                site,
                                jobs_raw,
                                run_id,
                                counters,
                                use_savepoints=True,
                                per_job_commit=False,
                                logger=run_log,
                            )
    except Exception:
        run_log.exception("persistence phase crashed")
        raise

    with Timer("finalize run", logger=run_log):
        with SessionLocal.begin() as s:
            r = s.get(IntegrationRun, run_id)
            r.total_seen = counters["total_seen"]
            r.inserted_count = counters["inserted_count"]
            r.updated_count = counters["updated_count"]
            r.missing_count = counters["missing_count"]
            r.unchanged_count = counters["unchanged_count"]
            r.error_count = counters["error_count"]
            r.finished_at = func.now()
        run_log.info(
            "done totals total=%d new=%d upd=%d missing=%d same=%d err=%d",
            counters["total_seen"],
            counters["inserted_count"],
            counters["updated_count"],
            counters["missing_count"],
            counters["unchanged_count"],
            counters["error_count"],
        )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        get_logger().exception("fatal error in main")
        sys.exit(2)
