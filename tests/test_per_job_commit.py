import sys

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import run
from app.models import Base, IntegrationRun, Job, JobChange


@pytest.fixture
def site_context(monkeypatch):
    """Provide isolated persistence without NLTK or filesystem side effects."""
    monkeypatch.setattr(run, "extract_keywords", lambda desc: ["identity"])
    monkeypatch.setattr(run, "remove_canned_text", lambda jobs: jobs)
    monkeypatch.setattr(run, "save_site_json", lambda *_args, **_kwargs: None)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    integration = IntegrationRun(user="tester", mode="test")
    session.add(integration)
    session.commit()
    try:
        yield session, integration.id
    finally:
        session.close()


def _counters():
    return {
        "total_seen": 0,
        "inserted_count": 0,
        "updated_count": 0,
        "missing_count": 0,
        "unchanged_count": 0,
        "error_count": 0,
    }


def _raw(job_id, title="Role", desc="Description"):
    return {"JobID": job_id, "JobTitle": title, "JobDesc": desc}


def _seed_job(session, run_id, raw, *, active=True, last_seen_run_id=None):
    job = run._normalize_job("example", run_id, raw)
    assert job is not None
    job.is_active = active
    job.first_seen_run_id = run_id
    job.last_seen_run_id = last_seen_run_id
    session.add(job)
    return job


def _process(session, run_id, jobs, counters, *, per_job=True, savepoints=False):
    run._process_site(
        session,
        "example",
        jobs,
        run_id,
        counters,
        use_savepoints=savepoints,
        per_job_commit=per_job,
        logger=run.get_logger(),
    )


def _fail_commit_on(session, monkeypatch, *attempts):
    """Fail selected future commits before the database transaction completes."""
    real_commit = session.commit
    commit_attempt = 0

    def controlled_commit():
        nonlocal commit_attempt
        commit_attempt += 1
        if commit_attempt in attempts:
            raise RuntimeError(f"simulated commit failure {commit_attempt}")
        real_commit()

    monkeypatch.setattr(session, "commit", controlled_commit)


def test_per_job_happy_path_covers_every_row_operation(site_context):
    session, run_id = site_context
    old_update = _seed_job(session, run_id, _raw("update", "Old title"))
    unchanged = _seed_job(session, run_id, _raw("same"))
    missing = _seed_job(session, run_id, _raw("missing"))
    session.commit()
    counters = _counters()

    _process(
        session,
        run_id,
        [_raw("update", "New title"), _raw("same"), _raw("insert")],
        counters,
    )

    session.refresh(old_update)
    session.refresh(unchanged)
    session.refresh(missing)
    assert old_update.title == "New title"
    assert unchanged.is_active is True
    assert unchanged.last_seen_run_id == run_id
    assert missing.is_active is False
    assert counters == {
        "total_seen": 3,
        "inserted_count": 1,
        "updated_count": 1,
        "missing_count": 1,
        "unchanged_count": 1,
        "error_count": 0,
    }
    changes = session.execute(select(JobChange)).scalars().all()
    assert sorted(change.change_type for change in changes) == [
        "insert",
        "missing",
        "update",
    ]


def test_failed_insert_restores_bookkeeping_and_retries_same_job(
    site_context, monkeypatch
):
    session, run_id = site_context
    _fail_commit_on(session, monkeypatch, 1)
    counters = _counters()
    first = _raw("Req1", "First", "One")

    # Req1 proves a rolled-back ORM object is not cached; Req2 proves processing
    # continues beyond the retried row.
    _process(session, run_id, [first, first, _raw("Req2", "Second", "Two")], counters)

    jobs = session.execute(select(Job).order_by(Job.job_id)).scalars().all()
    changes = session.execute(
        select(JobChange).order_by(JobChange.job_id_text)
    ).scalars().all()
    assert [job.job_id for job in jobs] == ["Req1", "Req2"]
    assert [change.job_id_text for change in changes] == ["Req1", "Req2"]
    assert counters == {
        "total_seen": 3,
        "inserted_count": 2,
        "updated_count": 0,
        "missing_count": 0,
        "unchanged_count": 0,
        "error_count": 1,
    }


def test_failed_update_rolls_back_values_and_continues(site_context, monkeypatch):
    session, run_id = site_context
    existing = _seed_job(session, run_id, _raw("existing", "Original"))
    session.commit()
    _fail_commit_on(session, monkeypatch, 1)
    counters = _counters()

    _process(
        session,
        run_id,
        [_raw("existing", "Changed"), _raw("later", "Later")],
        counters,
    )

    session.refresh(existing)
    assert existing.title == "Original"
    assert session.execute(
        select(Job).where(Job.job_id == "later")
    ).scalar_one_or_none() is not None
    assert counters["updated_count"] == 0
    assert counters["inserted_count"] == 1
    assert counters["error_count"] == 1


def test_failed_unchanged_touch_rolls_back_reactivation(site_context, monkeypatch):
    session, run_id = site_context
    existing = _seed_job(
        session,
        run_id,
        _raw("existing"),
        active=False,
        last_seen_run_id=None,
    )
    session.commit()
    _fail_commit_on(session, monkeypatch, 1)
    counters = _counters()

    _process(session, run_id, [_raw("existing"), _raw("later")], counters)

    session.refresh(existing)
    assert existing.is_active is False
    assert existing.last_seen_run_id is None
    assert counters["unchanged_count"] == 0
    assert counters["inserted_count"] == 1
    assert counters["error_count"] == 1


def test_failed_missing_mark_rolls_back_deactivation(site_context, monkeypatch):
    session, run_id = site_context
    existing = _seed_job(session, run_id, _raw("existing"), active=True)
    _seed_job(session, run_id, _raw("seen"), active=True)
    session.commit()
    _fail_commit_on(session, monkeypatch, 2)
    counters = _counters()

    # The seen row commits first; the injected second failure occurs while the
    # other previously active job is being marked missing.
    _process(session, run_id, [_raw("seen")], counters)

    session.refresh(existing)
    assert existing.is_active is True
    assert counters["missing_count"] == 0
    assert counters["unchanged_count"] == 1
    assert counters["error_count"] == 1


def test_savepoint_failure_does_not_count_rolled_back_insert(site_context):
    session, run_id = site_context
    counters = _counters()
    should_fail = True

    def fail_first_change(_mapper, _connection, _target):
        nonlocal should_fail
        if should_fail:
            should_fail = False
            raise RuntimeError("simulated JobChange flush failure")

    event.listen(JobChange, "before_insert", fail_first_change)
    try:
        _process(
            session,
            run_id,
            [_raw("failed"), _raw("saved")],
            counters,
            per_job=False,
            savepoints=True,
        )
        session.commit()
    finally:
        event.remove(JobChange, "before_insert", fail_first_change)

    jobs = session.execute(select(Job).order_by(Job.job_id)).scalars().all()
    assert [job.job_id for job in jobs] == ["saved"]
    assert counters["inserted_count"] == 1
    assert counters["error_count"] == 1


def test_empty_preprocessed_result_does_not_mark_every_job_missing(
    site_context, monkeypatch
):
    session, run_id = site_context
    existing = _seed_job(session, run_id, _raw("existing"), active=True)
    session.commit()
    counters = _counters()
    monkeypatch.setattr(run, "remove_canned_text", lambda _jobs: [])

    _process(session, run_id, [_raw("discarded")], counters)

    session.refresh(existing)
    assert existing.is_active is True
    assert counters == _counters()


def test_malformed_rows_are_skipped_without_stopping_later_jobs(site_context):
    session, run_id = site_context
    counters = _counters()

    _process(session, run_id, [None, [], _raw("valid")], counters)

    assert session.execute(select(Job).where(Job.job_id == "valid")).scalar_one()
    assert counters["total_seen"] == 1
    assert counters["inserted_count"] == 1
    assert counters["error_count"] == 2


def test_none_job_id_is_rejected_instead_of_becoming_literal_none(site_context):
    session, run_id = site_context
    counters = _counters()

    _process(session, run_id, [_raw(None), _raw("valid")], counters)

    assert session.execute(select(Job).where(Job.job_id == "None")).scalar_one_or_none() is None
    assert session.execute(select(Job).where(Job.job_id == "valid")).scalar_one()
    assert counters["total_seen"] == 1
    assert counters["inserted_count"] == 1
    assert counters["error_count"] == 1


def test_all_invalid_rows_do_not_deactivate_existing_jobs(site_context):
    session, run_id = site_context
    existing = _seed_job(session, run_id, _raw("existing"), active=True)
    session.commit()
    counters = _counters()

    _process(session, run_id, [None, {}, _raw(None)], counters)

    session.refresh(existing)
    assert existing.is_active is True
    assert counters["missing_count"] == 0
    assert counters["error_count"] == 3


def test_null_optional_fields_normalize_to_empty_text(site_context):
    _session, run_id = site_context

    job = run._normalize_job(
        "example",
        run_id,
        {
            "JobID": 42,
            "JobTitle": None,
            "JobUrl": None,
            "JobDesc": None,
            "JobPay": None,
            "SalaryNote": None,
        },
    )

    assert job is not None
    assert job.job_id == "42"
    assert job.title == ""
    assert job.url == ""
    assert job.desc == ""
    assert job.pay == "Unknown"


def test_case_and_whitespace_duplicate_is_one_persisted_job(site_context):
    session, run_id = site_context
    counters = _counters()

    _process(session, run_id, [_raw(" Req1 "), _raw("req1")], counters)

    jobs = session.execute(select(Job)).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].job_id == "Req1"
    assert counters["inserted_count"] == 1
    assert counters["unchanged_count"] == 1
    assert counters["error_count"] == 0


def test_update_preserves_discovery_and_invalidates_stale_compensation(site_context):
    session, run_id = site_context
    existing = _seed_job(session, run_id, _raw("existing", desc="Old evidence"))
    original_discovery = existing.discovery_date
    existing.compensation_schema_version = 7
    session.commit()
    counters = _counters()

    _process(
        session,
        run_id,
        [_raw("existing", desc="New compensation evidence")],
        counters,
    )

    session.refresh(existing)
    assert existing.discovery_date == original_discovery
    assert existing.compensation_schema_version is None
    assert counters["updated_count"] == 1


def test_main_selects_per_job_mode_with_whitespace_and_finalizes_run(
    monkeypatch, tmp_path
):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    class FakeScraper:
        def __init__(self, _steps_path):
            pass

        def run_iter(self):
            yield "example", [_raw("one"), _raw("two")]

        def run(self):
            pytest.fail("per_job mode must stream with run_iter")

    monkeypatch.setattr(run, "SessionLocal", factory)
    monkeypatch.setattr(run, "StepScraper", FakeScraper)
    monkeypatch.setattr(run, "init_db", lambda: None)
    monkeypatch.setattr(run, "ensure_nltk", lambda: None)
    monkeypatch.setattr(run, "extract_keywords", lambda _desc: ["identity"])
    monkeypatch.setattr(run, "remove_canned_text", lambda jobs: jobs)
    monkeypatch.setattr(run, "save_site_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run, "setup_logging", lambda: None)
    monkeypatch.setattr(run, "add_run_file_handler", lambda _label: None)
    monkeypatch.setattr(run, "log_startup_environment", lambda _logger: None)
    monkeypatch.setattr(run, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["run.py", "steps"])
    monkeypatch.setenv("DB_COMMIT_MODE", " PER_JOB ")

    run.main()

    with factory() as session:
        integration = session.execute(select(IntegrationRun)).scalar_one()
        assert integration.finished_at is not None
        assert integration.total_seen == 2
        assert integration.inserted_count == 2
        assert integration.error_count == 0
        assert session.execute(select(Job)).scalars().all()


def test_per_site_outer_commit_failure_restores_site_counters(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    real_factory = sessionmaker(bind=engine)

    class BeginContext:
        def __init__(self, session, *, fail):
            self.session = session
            self.transaction = session.begin()
            self.fail = fail

        def __enter__(self):
            return self.session

        def __exit__(self, exc_type, exc, traceback):
            try:
                if exc_type is None and self.fail:
                    # Match a database error raised while the outer site transaction
                    # commits, after all row savepoints have already succeeded.
                    self.session.rollback()
                    raise RuntimeError("simulated per-site commit failure")
                return self.transaction.__exit__(exc_type, exc, traceback)
            finally:
                self.session.close()

    class ControlledFactory:
        def __init__(self):
            self.begin_count = 0

        def __call__(self):
            return real_factory()

        def begin(self):
            self.begin_count += 1
            return BeginContext(
                real_factory(),
                fail=self.begin_count == 2,
            )

    class FakeScraper:
        def __init__(self, _steps_path):
            pass

        def run_iter(self):
            yield "example", [_raw("rolled-back")]

    controlled_factory = ControlledFactory()
    monkeypatch.setattr(run, "SessionLocal", controlled_factory)
    monkeypatch.setattr(run, "StepScraper", FakeScraper)
    monkeypatch.setattr(run, "init_db", lambda: None)
    monkeypatch.setattr(run, "ensure_nltk", lambda: None)
    monkeypatch.setattr(run, "extract_keywords", lambda _desc: ["identity"])
    monkeypatch.setattr(run, "remove_canned_text", lambda jobs: jobs)
    monkeypatch.setattr(run, "save_site_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(run, "setup_logging", lambda: None)
    monkeypatch.setattr(run, "add_run_file_handler", lambda _label: None)
    monkeypatch.setattr(run, "log_startup_environment", lambda _logger: None)
    monkeypatch.setattr(run, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["run.py", "steps"])
    monkeypatch.setenv("DB_COMMIT_MODE", "per_site")

    run.main()

    with real_factory() as session:
        integration = session.execute(select(IntegrationRun)).scalar_one()
        assert integration.finished_at is not None
        assert integration.total_seen == 0
        assert integration.inserted_count == 0
        assert integration.error_count == 1
