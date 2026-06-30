# /run.py
from __future__ import annotations

from datetime import datetime, timezone
import getpass
import hashlib
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.scraper import StepScraper
from app.json_writer import save_site_json
from app.utils import (
    ensure_nltk,
    extract_keywords,
    get_job_level,
    scan_for_pay_range,
    remove_canned_text,
)
from app.models import SessionLocal, init_db, IntegrationRun, Job, JobChange

OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# ------------------------------- logging -------------------------------
_LOGGER_NAME = "app"
_LOG_DIR = None
_RUN_FILE_HANDLER: RotatingFileHandler | None = None


class _ContextAdapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = dict(self.extra)
        incoming = kwargs.get("extra") or {}
        extra.update(incoming)
        kwargs["extra"] = extra
        return msg, kwargs


class DefaultContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "run_id"):
            record.run_id = "-"
        if not hasattr(record, "site"):
            record.site = "-"
        if not hasattr(record, "job_id"):
            record.job_id = "-"
        return True


class Timer:
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
    fmt = "%(asctime)s %(levelname)s run=%(run_id)s site=%(site)s job=%(job_id)s %(name)s:%(funcName)s:%(lineno)d | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(getattr(logging, level, logging.INFO))
    df = DefaultContextFilter()
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(getattr(logging, level, logging.INFO))
    sh.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    sh.addFilter(df)
    root.addHandler(sh)
    fh = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(getattr(logging, level, logging.INFO))
    fh.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    fh.addFilter(df)
    root.addHandler(fh)
    root.addFilter(df)
    logging.getLogger("sqlalchemy.engine").setLevel(
        getattr(logging, sql_level, logging.WARNING)
    )
    logging.getLogger("sqlalchemy.pool").setLevel(
        getattr(logging, sql_level, logging.WARNING)
    )
    logger = logging.getLogger(_LOGGER_NAME)

    def _excepthook(exc_type, exc, tb):
        _ContextAdapter(logger, {"run_id": "-", "site": "-", "job_id": "-"}).critical(
            "Unhandled exception", exc_info=(exc_type, exc, tb)
        )
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = _excepthook
    return logger


def add_run_file_handler(run_label: str) -> None:
    global _RUN_FILE_HANDLER
    if _RUN_FILE_HANDLER:
        return
    if not _LOG_DIR:
        _ensure_log_dir()
    path = os.path.join(_LOG_DIR, f"run_{run_label}.log")
    h = RotatingFileHandler(path, maxBytes=50_000_000, backupCount=1, encoding="utf-8")
    h.setLevel(logging.DEBUG)
    fmt = "%(asctime)s %(levelname)s run=%(run_id)s site=%(site)s job=%(job_id)s %(name)s:%(funcName)s:%(lineno)d | %(message)s"
    h.setFormatter(logging.Formatter(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S"))
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
        "ITEM_DELAY_MS": os.getenv("ITEM_DELAY_MS", ""),
    }
    logger.info("startup env %s | base_dir=%s", env, BASE_DIR)


# ------------------------------- helpers -------------------------------
def _norm_text(x: str | None) -> str:
    return (x or "").replace("\u202f", " ").replace("\u00a0", " ").strip()


def _missing_extraction_fields(jobs_raw: List[Dict[str, Any]]) -> Dict[str, List[int]]:
    required_fields = ["JobID", "JobTitle", "JobUrl", "JobDesc"]
    missing: Dict[str, List[int]] = {field: [] for field in required_fields}
    for idx, job in enumerate(jobs_raw, 1):
        for field in required_fields:
            if not _norm_text(str(job.get(field, ""))):
                missing[field].append(idx)
    return missing


def _log_test_extraction_summary(
    logger: logging.LoggerAdapter,
    site: str,
    jobs_raw: List[Dict[str, Any]],
) -> None:
    missing = _missing_extraction_fields(jobs_raw)
    logger.info("test extraction site=%s jobs_found=%d", site, len(jobs_raw))
    print(f"[test] {site}: jobs_found={len(jobs_raw)}")
    for field, row_numbers in missing.items():
        logger.info(
            "test extraction site=%s missing_%s=%d rows=%s",
            site,
            field,
            len(row_numbers),
            row_numbers,
        )
        print(f"[test] {site}: missing_{field}={len(row_numbers)}")
        if row_numbers:
            print(f"[test] {site}: missing_{field}_rows={row_numbers}")


def _print_test_extracted_values(site: str, jobs_raw: List[Dict[str, Any]]) -> None:
    if not jobs_raw:
        return

    preferred = ["JobID", "JobTitle", "JobUrl", "JobPay", "JobDesc"]
    discovered = sorted({key for job in jobs_raw for key in job.keys()})
    keys = [key for key in preferred if key in discovered]
    keys.extend(key for key in discovered if key not in keys)

    for idx, job in enumerate(jobs_raw, 1):
        print(f"[test] {site}: row={idx}")
        for key in keys:
            value = job.get(key, "")
            text = _norm_text(str(value))
            if key == "JobDesc" and len(text) > 500:
                text = f"{text[:500]}... [len={len(_norm_text(str(value)))}]"
            print(f"[test] {site}: row={idx} {key}={text}")


def _job_hash(
    title: str, url: str, desc: str, keywords: str, level: str, pay: str
) -> str:
    canon = "|".join([title, url, desc, keywords, level, pay]).encode("utf-8", "ignore")
    return hashlib.sha256(canon).hexdigest()


def _changed_fields(old: Job, new: Job) -> List[str]:
    fields = ["title", "url", "desc", "keywords", "level", "pay"]
    return [f for f in fields if (getattr(old, f) or "") != (getattr(new, f) or "")]


def _normalize_job(site: str, run_id: int, job_data: Dict[str, Any]) -> Job | None:
    job_id = _norm_text(str(job_data.get("JobID", "")))
    job_id = job_id.strip()
    if not job_id:
        return None

    title = _norm_text(str(job_data.get("JobTitle", "")))
    url = _norm_text(str(job_data.get("JobUrl", "")))
    desc = _norm_text(str(job_data.get("JobDesc", "")))

    # Make keywords stable across runs (avoid reorder-triggered updates)
    kw = extract_keywords(desc) or []
    kw = sorted({k.strip() for k in kw if k and k.strip()})
    keywords = ", ".join(kw)

    level = get_job_level(title)

    pay_hits = scan_for_pay_range(desc) or []
    pay = pay_hits[0] if pay_hits else "Unknown"

    # NOTE: Keep setting discovery_date here for inserts,
    # but DO NOT copy it onto existing rows during updates.
    discovery_date = datetime.now(timezone.utc)

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


# ------------------------------- delta logic -------------------------------
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
    site_log = get_logger(run_id=run_id, site=site)
    if not jobs_raw:
        site_log.info("no jobs for site")
        return

    def _canon_job_id(x: str | None) -> str:
        # Canonical form for comparisons (prevents case/whitespace mismatch issues)
        return _norm_text(x or "").strip().lower()

    def _do_commit():
        try:
            s.commit()
        except Exception:
            s.rollback()
            site_log.exception("commit failed")
            raise

    with Timer(f"{site} preproc+save_json", logger=site_log):
        jobs_raw = remove_canned_text(jobs_raw)
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        save_site_json(OUTPUT_DIR, site, jobs_raw)

    # Load existing jobs for the site, keyed by canonical job_id
    existing_rows = s.execute(select(Job).where(Job.site == site)).scalars().all()
    existing_by_cid: Dict[str, Job] = {
        _canon_job_id(r.job_id): r for r in existing_rows
    }
    missing_cids = set(existing_by_cid.keys())

    def _fetch_existing_by_cid(cid: str) -> Job | None:
        # In case of collation/case-insensitive uniqueness, find the existing row reliably
        return (
            s.execute(
                select(Job).where(
                    Job.site == site,
                    func.lower(func.trim(Job.job_id)) == cid,
                )
            )
            .scalars()
            .first()
        )

    def _run_op(op_fn, row_log, on_integrity=None):
        if per_job_commit:
            try:
                op_fn()
                _do_commit()
                return True
            except IntegrityError:
                s.rollback()
                counters["error_count"] += 1
                row_log.exception("integrity error")
                if on_integrity:
                    on_integrity()
                return False
            except Exception:
                s.rollback()
                counters["error_count"] += 1
                row_log.exception("row operation failed")
                return False

        if use_savepoints:
            try:
                with s.begin_nested():
                    op_fn()
                return True
            except IntegrityError:
                counters["error_count"] += 1
                row_log.exception("integrity error (savepoint)")
                if on_integrity:
                    on_integrity()
                return False
            except Exception:
                counters["error_count"] += 1
                row_log.exception("row operation failed (savepoint)")
                return False

        # No savepoints: must rollback on IntegrityError or session becomes unusable
        try:
            op_fn()
            return True
        except IntegrityError:
            s.rollback()
            counters["error_count"] += 1
            row_log.exception("integrity error (no sp)")
            if on_integrity:
                on_integrity()
            return False
        except Exception:
            s.rollback()
            counters["error_count"] += 1
            row_log.exception("row operation failed (no sp)")
            return False

    for jd in jobs_raw:
        job_id_text = _norm_text(str(jd.get("JobID", ""))) or "-"
        row_log = get_logger(run_id=run_id, site=site, job_id=job_id_text)

        try:
            job_obj = _normalize_job(site, run_id, jd)
        except Exception:
            counters["error_count"] += 1
            raw_cid = _canon_job_id(job_id_text)
            if raw_cid and raw_cid != "-":
                missing_cids.discard(raw_cid)
            row_log.exception("skip row: normalization failed")
            continue

        if not job_obj:
            counters["error_count"] += 1
            row_log.warning("skip row: missing JobID")
            continue

        counters["total_seen"] += 1

        cid = _canon_job_id(job_obj.job_id)

        # If we saw this ID in the scrape, it is not missing (even if insert/update fails later)
        missing_cids.discard(cid)

        prev = existing_by_cid.get(cid)

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
                    change_source="site",
                    old_hash=None,
                    new_hash=job_obj.content_hash,
                    changed_fields="title,url,desc,keywords,level,pay",
                )
            )
            counters["inserted_count"] += 1
            row_log.info("inserted")
            existing_by_cid[cid] = job_obj

        def _update_existing(target: Job):
            old_hash = target.content_hash
            changed = _changed_fields(target, job_obj)

            target.title = job_obj.title
            target.url = job_obj.url
            target.desc = job_obj.desc
            target.keywords = job_obj.keywords
            target.level = job_obj.level
            target.pay = job_obj.pay

            # CRITICAL FIX: never overwrite discovery_date on update
            # target.discovery_date stays as first-seen timestamp

            target.content_hash = job_obj.content_hash
            target.is_active = True
            target.last_seen_run_id = run_id

            s.add(
                JobChange(
                    run_id=run_id,
                    job_pk=target.id,
                    job_id_text=target.job_id,
                    site=site,
                    change_type="update",
                    change_source="site",
                    old_hash=old_hash,
                    new_hash=job_obj.content_hash,
                    changed_fields=",".join(changed),
                )
            )
            counters["updated_count"] += 1
            row_log.info("updated fields=%s", changed)

        def _touch_existing(target: Job):
            # Seen but content unchanged
            target.is_active = True
            target.last_seen_run_id = run_id
            counters["unchanged_count"] += 1
            row_log.debug("unchanged")

        if prev is None:

            def _on_insert_integrity():
                # If insert failed, treat the existing row as "seen" and update/touch it
                existing = _fetch_existing_by_cid(cid)
                if not existing:
                    return
                existing_by_cid[cid] = existing
                # We already missing_cids.discard(cid) above, so it won't be marked missing.
                if existing.content_hash != job_obj.content_hash:
                    _run_op(lambda: _update_existing(existing), row_log)
                else:
                    _run_op(lambda: _touch_existing(existing), row_log)

            _run_op(_insert_job, row_log, on_integrity=_on_insert_integrity)
            continue

        # Existing row path
        if prev.content_hash != job_obj.content_hash:
            _run_op(lambda: _update_existing(prev), row_log)
        else:
            _run_op(lambda: _touch_existing(prev), row_log)

    # Mark missing: anything previously active that we did NOT see this run
    if missing_cids:
        # Convert canonical IDs back to stored job_id values for querying
        missing_job_ids = [
            existing_by_cid[cid].job_id
            for cid in missing_cids
            if cid in existing_by_cid
        ]
        if missing_job_ids:
            site_log.info("marking missing count=%d", len(missing_job_ids))
            rows = (
                s.execute(
                    select(Job).where(Job.site == site, Job.job_id.in_(missing_job_ids))
                )
                .scalars()
                .all()
            )

            for r in rows:
                if not r.is_active:
                    continue

                def _mark_missing():
                    r.is_active = False
                    s.add(
                        JobChange(
                            run_id=run_id,
                            job_pk=r.id,
                            job_id_text=r.job_id,
                            site=site,
                            change_type="missing",
                            change_source="site",
                            old_hash=r.content_hash,
                            new_hash=None,
                            changed_fields="",
                        )
                    )
                    counters["missing_count"] += 1
                    get_logger(run_id=run_id, site=site, job_id=r.job_id).info(
                        "marked missing"
                    )

                _run_op(
                    _mark_missing, get_logger(run_id=run_id, site=site, job_id=r.job_id)
                )


# ------------------------------- main -------------------------------
def main():
    setup_logging()
    base_log = get_logger()
    log_startup_environment(base_log)

    if len(sys.argv) < 2:
        base_log.error("Usage: python run.py [initdb|steps|test|download|reprocess]")
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "download":
        with Timer("NLTK ensure", logger=base_log):
            ensure_nltk(verbose=True)
        base_log.info("NLTK ready")
        sys.exit(0)

    if cmd == "reprocess":
        # loop all jobs and reprocess job levels
        with SessionLocal() as s:
            rows = s.execute(select(Job)).scalars().all()
            for i, r in enumerate(rows, 1):
                r.level = get_job_level(r.title)

                if r.level == "Unknown":
                    print(
                        f"Failed reprocess job_id={r.job_id} level={r.level} - title={r.title}"
                    )
                s.add(r)
                if i % 25 == 0:
                    print(f"Processed {i} jobs...")
            s.commit()
            base_log.info("reprocessed %d jobs", len(rows))
        sys.exit(0)

    if cmd == "test":
        steps_path = os.path.join(BASE_DIR, "test.json")
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        test_log = get_logger(run_id="test")
        test_log.info("test extraction only | steps_path=%s", steps_path)
        scraper = StepScraper(steps_path)
        with Timer("scraper.test", logger=test_log):
            for site, jobs_raw in scraper.run_iter():
                save_site_json(OUTPUT_DIR, site, jobs_raw)
                _log_test_extraction_summary(test_log, site, jobs_raw)
                _print_test_extracted_values(site, jobs_raw)
        return

    commit_mode = os.getenv("DB_COMMIT_MODE", "all_at_end").lower()
    if commit_mode not in {"all_at_end", "per_site", "per_job"}:
        base_log.warning(
            "invalid DB_COMMIT_MODE=%s; defaulting to all_at_end", commit_mode
        )
        commit_mode = "all_at_end"

    with Timer("init_db", logger=base_log):
        init_db()
    steps_path = os.path.join(BASE_DIR, "steps.json")

    with Timer("ensure_nltk", logger=base_log):
        ensure_nltk()
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Open run row
    with SessionLocal.begin() as s:
        run = IntegrationRun(user=getpass.getuser(), mode="steps")
        s.add(run)
        s.flush()
        run_id = run.id

    ts_label = time.strftime("%Y%m%d_%H%M%S")
    add_run_file_handler(f"{ts_label}_{run_id}")
    run_log = get_logger(run_id=run_id)
    run_log.info("run opened | commit_mode=%s | steps_path=%s", commit_mode, steps_path)

    scraper = StepScraper(steps_path)

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
            run_log.info("streaming mode: persisting per job")
            with SessionLocal() as s:
                for site, jobs_raw in scraper.run_iter():
                    site_log = get_logger(run_id=run_id, site=site)
                    if not jobs_raw:
                        site_log.info("skip empty site")
                        continue
                    try:
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
                    except Exception:
                        s.rollback()
                        counters["error_count"] += 1
                        site_log.exception(
                            "site persistence failed in per_job mode; continuing"
                        )
                        continue
                    site_log.info(
                        "site persisted (per_job) | totals so far new=%d upd=%d miss=%d same=%d err=%d",
                        counters["inserted_count"],
                        counters["updated_count"],
                        counters["missing_count"],
                        counters["unchanged_count"],
                        counters["error_count"],
                    )

        elif commit_mode == "per_site":
            run_log.info("streaming mode: persisting per site")
            for site, jobs_raw in scraper.run_iter():
                site_log = get_logger(run_id=run_id, site=site)
                if not jobs_raw:
                    site_log.info("skip empty site")
                    continue
                site_log.info("begin site transaction")
                try:
                    with SessionLocal.begin() as s:
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
                    site_log.info(
                        "commit site transaction (ok) | totals so far new=%d upd=%d miss=%d same=%d err=%d",
                        counters["inserted_count"],
                        counters["updated_count"],
                        counters["missing_count"],
                        counters["unchanged_count"],
                        counters["error_count"],
                    )
                except Exception:
                    site_log.exception("rollback site transaction (error)")
                    continue

        else:  # all_at_end
            run_log.info("bulk mode: scraping all then one big transaction")
            with Timer("scraper.run", logger=run_log):
                site_to_jobs = scraper.run()
            run_log.info("scraper returned sites=%d", len(site_to_jobs))
            with SessionLocal() as s:
                with s.begin():  # one big transaction
                    for site, jobs_raw in site_to_jobs.items():
                        site_log = get_logger(run_id=run_id, site=site)
                        if not jobs_raw:
                            site_log.info("skip empty site")
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
