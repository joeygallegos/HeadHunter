import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

import analyze_jobs_ollama as analyzer
import dashboard
import email_job_digest as digest
from app import models
from app.models import (
    Base,
    IntegrationRun,
    Job,
    JobAIRefreshRequest,
    JobAnalysisRetry,
    JobApplicationPrep,
    JobMatchScreen,
    JobSwipe,
)


def test_reasoning_only_screen_gets_one_bounded_retry(monkeypatch):
    calls = []
    monkeypatch.setenv("AI_SCREEN_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("AI_SCREEN_THINK", "false")
    monkeypatch.setenv("AI_SCREEN_NUM_PREDICT", "128")
    monkeypatch.setenv("AI_SCREEN_RETRY_NUM_PREDICT", "512")

    def invoke(messages, **options):
        calls.append(options)
        if len(calls) == 1:
            raise analyzer.OllamaEmptyContentError(
                done_reason="length", thinking_chars=643, eval_count=128,
            )
        return '{"match_percentage":40,"experience_match":"qualified"}'

    monkeypatch.setattr(analyzer, "invoke_ollama_json", invoke)
    task = analyzer.JobTask(1, "site", "job", "Title", "Description", "", 1, None)
    result = analyzer.analyze_routed_job_worker("resume", task, 1)
    assert result.status == "screened_out"
    assert [call["num_predict"] for call in calls] == [128, 512]
    assert [call["profile"].name for call in calls] == ["screen", "screen"]
    assert [call["profile"].think for call in calls] == [False, False]


def test_exhausted_screen_preserves_safe_diagnostics(monkeypatch):
    monkeypatch.setattr(analyzer, "AI_MAX_ATTEMPTS", 4)
    calls = []

    def invoke(messages, **options):
        calls.append(options["num_predict"])
        raise analyzer.OllamaEmptyContentError(
            done_reason="length", thinking_chars=643, eval_count=128,
        )

    monkeypatch.setattr(analyzer, "invoke_ollama_json", invoke)
    task = analyzer.JobTask(1, "site", "job", "Title", "private posting", "", 1, None)
    result = analyzer.analyze_routed_job_worker("private resume", task, 1)
    assert result.status == "schema_error"
    assert len(calls) == 2
    assert "done_reason=length" in result.error_text
    assert "thinking_chars=643" in result.error_text
    assert "private" not in result.error_text
    assert result.screen_payload is None


def test_screen_exception_does_not_log_provider_payload(monkeypatch):
    def invoke(*args, **kwargs):
        raise RuntimeError("private resume and provider output")

    monkeypatch.setattr(analyzer, "invoke_ollama_json", invoke)
    task = analyzer.JobTask(1, "site", "job", "Title", "Description", "", 1, None)
    result = analyzer.analyze_routed_job_worker("resume", task, 1)
    assert "RuntimeError" in result.error_text
    assert "private" not in result.error_text


def _database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'screen-digest.db'}")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _jobs(session):
    run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 11))
    session.add(run)
    session.flush()
    jobs = [Job(site="site", job_id=name, title=name, desc="Description", run_id=run.id,
                first_seen_run_id=run.id, ai_match_percentage=score)
            for name, score in [("final", 90), ("screened", None), ("pending", None)]]
    session.add_all(jobs)
    session.flush()
    return run, jobs


def _refresh_job(session, job_id="refresh-job", **overrides):
    run = IntegrationRun(user="test", mode="steps")
    session.add(run)
    session.flush()
    values = {
        "site": "site",
        "job_id": job_id,
        "title": "Security Engineer",
        "desc": "Build reliable controls.",
        "run_id": run.id,
        "is_active": True,
        "content_hash": f"hash-{job_id}",
        "ai_analysis": '{"match_percentage":80}',
        "ai_match_percentage": 80,
    }
    values.update(overrides)
    job = Job(**values)
    session.add(job)
    session.flush()
    return job


def test_init_db_creates_priority_ai_refresh_queue(monkeypatch, tmp_path):
    engine, _factory = _database(tmp_path)
    monkeypatch.setattr(models, "engine", engine)

    models.init_db()

    columns = {
        column["name"]
        for column in sa_inspect(engine).get_columns("job_ai_refresh_requests")
    }
    assert {
        "job_pk",
        "status",
        "request_generation",
        "requested_at",
        "started_at",
        "finished_at",
        "lease_expires_at",
        "worker_token",
        "current_stage",
        "outcome_json",
        "error_text",
    }.issubset(columns)


def test_job_refresh_endpoint_queues_and_coalesces(monkeypatch, tmp_path):
    engine, factory = _database(tmp_path)
    with factory() as session:
        job = _refresh_job(session)
        session.commit()
        job_id = job.id
    monkeypatch.setattr(dashboard, "engine", engine)
    monkeypatch.setattr(dashboard, "SessionLocal", factory)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", True)

    client = dashboard.app.test_client()
    first = client.post(f"/api/jobs/{job_id}/refresh-ai")
    duplicate = client.post(f"/api/jobs/{job_id}/refresh-ai")

    assert first.status_code == 202
    assert first.get_json()["coalesced"] is False
    assert first.get_json()["refresh"]["status"] == "queued"
    assert duplicate.status_code == 202
    assert duplicate.get_json()["coalesced"] is True
    with factory() as session:
        rows = session.query(JobAIRefreshRequest).all()
        assert len(rows) == 1
        assert rows[0].request_generation == 1


def test_job_refresh_requeues_terminal_generations_and_resets_private_state(
    monkeypatch, tmp_path
):
    engine, factory = _database(tmp_path)
    with factory() as session:
        job = _refresh_job(session)
        session.commit()
        job_id = job.id
    monkeypatch.setattr(dashboard, "engine", engine)
    monkeypatch.setattr(dashboard, "SessionLocal", factory)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", True)
    dashboard.queue_job_ai_refresh(job_id)

    with factory() as session:
        refresh = session.get(JobAIRefreshRequest, job_id)
        refresh.status = "running"
        refresh.worker_token = "private-worker-token"
        session.commit()
    assert dashboard.queue_job_ai_refresh(job_id)["coalesced"] is True

    for expected_generation, terminal_status in ((2, "done"), (3, "failed")):
        with factory() as session:
            refresh = session.get(JobAIRefreshRequest, job_id)
            refresh.status = terminal_status
            refresh.error_text = "private provider output"
            session.commit()
        result = dashboard.queue_job_ai_refresh(job_id)
        assert result["refresh"]["request_generation"] == expected_generation
        assert result["refresh"]["status"] == "queued"
        with factory() as session:
            refresh = session.get(JobAIRefreshRequest, job_id)
            assert refresh.worker_token is None
            assert refresh.error_text is None


def test_job_refresh_endpoint_rejects_missing_inactive_and_empty(monkeypatch, tmp_path):
    engine, factory = _database(tmp_path)
    with factory() as session:
        inactive = _refresh_job(session, "inactive", is_active=False)
        empty = _refresh_job(session, "empty", title="", desc="")
        session.commit()
        inactive_id = inactive.id
        empty_id = empty.id
    monkeypatch.setattr(dashboard, "engine", engine)
    monkeypatch.setattr(dashboard, "SessionLocal", factory)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", True)
    client = dashboard.app.test_client()

    assert client.post("/api/jobs/999999/refresh-ai").status_code == 404
    assert client.post(f"/api/jobs/{inactive_id}/refresh-ai").status_code == 409
    assert client.post(f"/api/jobs/{empty_id}/refresh-ai").status_code == 409
    with factory() as session:
        assert session.query(JobAIRefreshRequest).count() == 0


def test_job_detail_serializes_safe_ai_refresh_state(tmp_path):
    _engine, factory = _database(tmp_path)
    with factory() as session:
        job = _refresh_job(session)
        refresh = JobAIRefreshRequest(
            job_pk=job.id,
            status="failed",
            request_generation=4,
            current_stage="detailed_analysis",
            outcome_json='{"screening":"generated","detailed_analysis":"failed"}',
            worker_token="must-not-leak",
            error_text="resume and provider output must not leak",
        )
        session.add(refresh)
        session.commit()
        serialized = dashboard._serialize_job_detail(job, [], ai_refresh=refresh)["ai_refresh"]

    assert serialized["status"] == "failed"
    assert serialized["request_generation"] == 4
    assert serialized["outcomes"]["screening"] == "generated"
    assert serialized["error_message"] == "AI refresh failed. You can retry this job."
    assert "worker_token" not in serialized
    assert "provider output" not in json.dumps(serialized)


def test_job_lookup_template_exposes_refresh_states_and_polling():
    template = dashboard.app.jinja_loader.get_source(
        dashboard.app.jinja_env, "index.html"
    )[0]

    assert '@click="refreshSelectedJobAi()"' in template
    assert ':disabled="aiRefreshBusy(selectedJob())"' in template
    for label in (
        "Refresh AI details",
        "Queued for AI refresh",
        "Refresh complete — refresh again",
        "Refresh failed — retry",
    ):
        assert label in template
    assert "async pollSelectedJobAiRefresh(jobId, token)" in template
    assert "/api/job-detail?id=${encodeURIComponent(jobId)}`" in template
    assert "/refresh-ai`" in template


def test_downstream_hashes_change_with_ai_analysis_despite_fixed_content_hash():
    common = {
        "id": 1,
        "site": "site",
        "job_id": "hash-check",
        "title": "Security Lead",
        "desc": "Lead the program.",
        "ai_match_percentage": 80,
        "content_hash": "unchanged-source-hash",
    }
    brief_a = analyzer.FitBriefTask(ai_analysis='{"match_percentage":80}', **common)
    brief_b = analyzer.FitBriefTask(ai_analysis='{"match_percentage":90}', **common)
    prep_a = analyzer.ApplicationPrepTask(
        prep_id=2, ai_analysis='{"match_percentage":80}', **common
    )
    prep_b = analyzer.ApplicationPrepTask(
        prep_id=2, ai_analysis='{"match_percentage":90}', **common
    )

    assert analyzer.fit_brief_job_hash(brief_a) != analyzer.fit_brief_job_hash(brief_b)
    assert analyzer.application_prep_job_hash(prep_a) != analyzer.application_prep_job_hash(
        prep_b
    )


def test_priority_refresh_claim_is_exclusive_and_respects_leases(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    now = datetime(2026, 10, 14, 12, 0)
    with factory() as session:
        session.add_all(
            [
                JobAIRefreshRequest(
                    job_pk=1,
                    status="queued",
                    requested_at=now - timedelta(minutes=10),
                ),
                JobAIRefreshRequest(
                    job_pk=2,
                    status="running",
                    requested_at=now - timedelta(minutes=9),
                    lease_expires_at=now + timedelta(minutes=5),
                    worker_token="live-owner",
                ),
                JobAIRefreshRequest(
                    job_pk=3,
                    status="running",
                    requested_at=now - timedelta(minutes=8),
                    lease_expires_at=now - timedelta(seconds=1),
                    worker_token="expired-owner",
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(analyzer, "SessionLocal", factory)

    first = analyzer.claim_next_ai_refresh_request(worker_token="worker-a", now=now)
    second = analyzer.claim_next_ai_refresh_request(worker_token="worker-b", now=now)
    third = analyzer.claim_next_ai_refresh_request(worker_token="worker-c", now=now)

    assert first == (1, 1, "worker-a")
    assert second == (3, 1, "worker-b")
    assert third is None
    with factory() as session:
        assert session.get(JobAIRefreshRequest, 1).worker_token == "worker-a"
        assert session.get(JobAIRefreshRequest, 2).worker_token == "live-owner"
        assert session.get(JobAIRefreshRequest, 3).worker_token == "worker-b"


def test_priority_refresh_lease_heartbeats_during_long_stage(monkeypatch):
    heartbeat_seen = analyzer.threading.Event()
    calls = []

    def renew(job_pk, generation, worker_token, stage, **_kwargs):
        calls.append((job_pk, generation, worker_token, stage))
        heartbeat_seen.set()
        return True

    monkeypatch.setattr(analyzer, "update_ai_refresh_stage", renew)

    with analyzer.maintain_ai_refresh_lease(
        7, 3, "owner", "detailed_analysis", interval_seconds=0.01
    ) as lease_owned:
        assert heartbeat_seen.wait(1.0)
        assert lease_owned() is True

    assert calls
    assert set(calls) == {(7, 3, "owner", "detailed_analysis")}


def test_priority_refreshed_job_is_not_submitted_from_prebuilt_batch(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    with factory() as session:
        older = _refresh_job(
            session,
            "scheduled-first",
            discovery_date=datetime(2026, 10, 13, 8, 0),
            ai_analysis=None,
            ai_match_percentage=None,
        )
        newer = _refresh_job(
            session,
            "priority-second",
            discovery_date=datetime(2026, 10, 13, 9, 0),
            ai_analysis=None,
            ai_match_percentage=None,
        )
        session.commit()
        newer_id = newer.id
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    submitted = []
    checkpoint_calls = 0

    def drain(_resume, *, completed_job_ids=None, **_kwargs):
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if completed_job_ids is not None:
            completed_job_ids.add(newer_id)
        return {"processed": 1 if completed_job_ids is not None else 0, "failed": 0}

    def worker(_resume, task, index):
        submitted.append(task.job_id)
        return analyzer.JobResult(
            task.id,
            task.site,
            task.job_id,
            "screened_out",
            None,
            None,
            0.0,
            "",
            index,
            {"match_percentage": 20, "experience_match": "underqualified"},
        )

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )
    monkeypatch.setattr(analyzer, "drain_priority_refresh_queue", drain)
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **_kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **_kwargs: None)
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", worker)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 0)

    analyzer.main()

    assert checkpoint_calls >= 2
    assert submitted == [older.job_id]


def test_exact_priority_refresh_forces_detail_after_low_fresh_screen(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="exact-only",
            title="Security Engineer",
            desc="Build controls.",
            run_id=run.id,
            is_active=True,
            content_hash="fixed-source-hash",
        )
        session.add(job)
        session.flush()
        session.add(JobSwipe(job_pk=job.id, action="like"))
        session.commit()
        job_id = job.id

    screen_tasks = []
    detailed_tasks = []

    def screen_worker(_resume, task, index):
        screen_tasks.append(task)
        return analyzer.JobResult(
            task.id,
            task.site,
            task.job_id,
            "screened_out",
            None,
            None,
            0.0,
            "",
            index,
            {"match_percentage": 20, "experience_match": "underqualified"},
        )

    def detail_worker(_resume, task, index):
        detailed_tasks.append(task)
        return analyzer.JobResult(
            task.id,
            task.site,
            task.job_id,
            "ok",
            '{"match_percentage":20}',
            {"ai_match_percentage": 20, "ai_analyzed_at": datetime.now()},
            0.0,
            "",
            index,
        )

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "update_ai_refresh_stage", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(analyzer, "analyze_match_screen_worker", screen_worker)
    monkeypatch.setattr(analyzer, "analyze_job_worker", detail_worker)

    outcomes = analyzer.process_exact_ai_refresh(job_id, 1, "owner", "resume")

    assert [task.job_id for task in screen_tasks] == ["exact-only"]
    assert screen_tasks[0].cached_screen is None
    assert [task.job_id for task in detailed_tasks] == ["exact-only"]
    assert outcomes == {
        "screening": "generated",
        "detailed_analysis": "generated",
        "fit_brief": "skipped_ineligible",
        "application_prep": "skipped_ineligible",
    }
    with factory() as session:
        job = session.get(Job, job_id)
        screen = session.get(JobMatchScreen, job_id)
        assert job.ai_match_percentage == 20
        assert screen.match_percentage == 20


def test_priority_refresh_failure_is_terminal_and_sanitized(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        session.add(JobAIRefreshRequest(job_pk=7, status="queued"))
        session.commit()

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(
        analyzer,
        "process_exact_ai_refresh",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("private resume and provider response")
        ),
    )

    totals = analyzer.drain_priority_refresh_queue("private resume", preflight=False)

    assert totals == {"processed": 0, "failed": 1}
    with factory() as session:
        refresh = session.get(JobAIRefreshRequest, 7)
        assert refresh.status == "failed"
        assert refresh.worker_token is None
        assert refresh.lease_expires_at is None
        assert refresh.error_text == "AI refresh failed unexpectedly"
        assert "private" not in refresh.error_text


def test_main_checks_priority_refresh_before_scheduled_queues(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    events = []

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )
    monkeypatch.setattr(
        analyzer,
        "drain_priority_refresh_queue",
        lambda *_args, **_kwargs: events.append("priority") or {"processed": 0, "failed": 0},
    )
    monkeypatch.setattr(
        analyzer,
        "select_job_ids",
        lambda *_args, **_kwargs: events.append("scheduled_selection") or [],
    )
    monkeypatch.setattr(
        analyzer,
        "run_application_prep_generation",
        lambda **_kwargs: events.append("application_prep") or {},
    )
    monkeypatch.setattr(
        analyzer,
        "run_fit_brief_generation",
        lambda *_args, **_kwargs: events.append("fit_brief") or {},
    )
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **_kwargs: None)

    analyzer.main()

    assert events[0:2] == ["priority", "scheduled_selection"]
    assert events.index("priority") < events.index("application_prep")
    assert events.index("priority") < events.index("fit_brief")


def test_dashboard_separates_screened_jobs_from_pending(monkeypatch, tmp_path):
    engine, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        final = Job(site="site", job_id="final", title="Final", ai_analysis="{}", run_id=run.id)
        screened = Job(site="site", job_id="screened", title="Screened", run_id=run.id)
        detail_retry = Job(site="site", job_id="detail-retry", title="Retry", run_id=run.id)
        pending = Job(site="site", job_id="pending", title="Pending", run_id=run.id)
        session.add_all([final, screened, detail_retry, pending])
        session.flush()
        session.add_all([
            JobMatchScreen(job_pk=screened.id, input_hash="a" * 64,
                           match_percentage=70, experience_match="qualified"),
            JobMatchScreen(job_pk=detail_retry.id, input_hash="b" * 64,
                           match_percentage=80, experience_match="qualified"),
        ])
        session.commit()

    monkeypatch.setattr(dashboard, "engine", engine)
    monkeypatch.setattr(dashboard, "SessionLocal", factory)
    report = dashboard.fetch_ollama_processing_report()

    assert report["active_count"] == 4
    assert report["processed_count"] == 2
    assert report["final_count"] == 1
    assert report["screened_count"] == 1
    assert report["awaiting_count"] == 2
    assert {job["job_id"] for job in report["jobs"]} == {"detail-retry", "pending"}


def test_dashboard_lookup_view_renders_valid_initial_ui_state():
    response = dashboard.app.test_client().get("/?view=lookup")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'view: "lookup",' in body
    assert "jobDetailPage: false," in body
    assert "initialJob: null," in body
    assert "initialApplicationPrepJobId: 0," in body


def test_job_detail_status_and_reason_share_one_inline_element():
    template = dashboard.app.jinja_loader.get_source(
        dashboard.app.jinja_env,
        "index.html",
    )[0]
    status_start = template.index('<div class="mt-1 text-sm text-slate-600">')
    status_end = template.index("</div>", status_start)
    status_markup = template[status_start:status_end]

    assert 'class="ml-2"' in status_markup
    assert "selectedJob().is_active ? 'Active' : 'Inactive'" in status_markup
    assert (
        "selectedJob().application_prep_eligibility?.reason ? ` - "
        "${selectedJob().application_prep_eligibility.reason}` : ''"
        in status_markup
    )
    assert 'x-show="selectedJob().application_prep_eligibility?.reason"' not in status_markup
    assert 'x-text="selectedJob().application_prep_eligibility?.reason"' not in template


def test_stage1_screen_out_flows_into_dashboard_coverage(monkeypatch, tmp_path):
    """Protect the producer/reporting contract when Stage 1 ends processing."""
    engine, factory = _database(tmp_path)
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        job = Job(site="site", job_id="screened", title="Screened role",
                  desc="Role details", run_id=run.id)
        session.add(job)
        session.commit()
        job_pk = job.id

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(
        analyzer, "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *args, **kwargs: {})
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kwargs: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 0)
    monkeypatch.setattr(
        analyzer,
        "analyze_routed_job_worker",
        lambda _resume, task, index: analyzer.JobResult(
            task.id, task.site, task.job_id, "screened_out", None, None,
            0.0, "", index,
            {"match_percentage": 40, "experience_match": "qualified"},
        ),
    )

    analyzer.main()

    with factory() as session:
        job = session.get(Job, job_pk)
        screen = session.get(JobMatchScreen, job_pk)
        assert job.ai_analysis is None
        assert job.ai_match_percentage is None
        assert screen.match_percentage == 40

    monkeypatch.setattr(dashboard, "engine", engine)
    monkeypatch.setattr(dashboard, "SessionLocal", factory)
    report = dashboard.fetch_ollama_processing_report()

    assert report["active_count"] == 1
    assert report["processed_count"] == 1
    assert report["final_count"] == 0
    assert report["screened_count"] == 1
    assert report["awaiting_count"] == 0
    assert report["jobs"] == []


def test_digest_distinguishes_screened_final_and_pending_without_inference(tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run, jobs = _jobs(session)
        session.add_all([
            JobMatchScreen(job_pk=jobs[0].id, input_hash="old", match_percentage=55,
                           experience_match="qualified"),
            JobMatchScreen(job_pk=jobs[1].id, input_hash="old", match_percentage=40,
                           experience_match="qualified"),
        ])
        session.commit()
        _, selected, _, text, html = digest.build_digest(session, run.id, 25)
        by_name = {job.job_id: job for job in selected}
        assert digest.score_label(by_name["final"]) == "90%"
        assert digest.score_label(by_name["screened"]) == "Last screen: 40% (preliminary)"
        assert digest.score_label(by_name["pending"]) == "Pending analysis"
        assert digest.digest_counts(selected) == (1, 2, 90)
        assert digest.screened_only_count(selected) == 1
        assert "Screened-only jobs: 1" in text
        assert "Pending analysis jobs: 1" in text
        for body in (text, html):
            assert "Last screen: 40% (preliminary)" in body
            assert "Pending analysis" in body
            assert "Not analyzed" not in body
            assert "jobs to review" in body
        assert by_name["screened"].ai_match_percentage is None


def test_digest_works_before_screen_cache_table_exists(tmp_path):
    engine, factory = _database(tmp_path)
    JobMatchScreen.__table__.drop(engine)
    with factory() as session:
        run, _ = _jobs(session)
        session.commit()
        selected = digest.get_newly_discovered_jobs(session, [run.id])
        assert len(selected) == 3
        assert digest.screened_only_count(selected) == 0
        assert not engine.dialect.has_table(session.connection(), "job_match_screens")


@pytest.mark.parametrize("status, expected_failure", [("schema_error", True), ("screened_out", False)])
def test_failed_primary_run_is_not_reported_as_success(monkeypatch, tmp_path, status, expected_failure):
    _, factory = _database(tmp_path)
    with factory() as session:
        _, jobs = _jobs(session)
        jobs[0].ai_match_percentage = None
        session.commit()
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    statuses = []
    brief_calls = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "parse_args", lambda: SimpleNamespace(compensation_only=False, redo=None))
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *args, **kwargs: brief_calls.append(1))
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kwargs: statuses.append(kwargs))
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 0)

    def worker(resume, task, index):
        screen = {"match_percentage": 40, "experience_match": "qualified"} if status == "screened_out" else None
        return analyzer.JobResult(task.id, task.site, task.job_id, status, None, None, 0, "", index, screen)

    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", worker)
    if expected_failure:
        with pytest.raises(SystemExit) as exc:
            analyzer.main()
        assert exc.value.code == 1
        assert statuses[-1]["status"] == "failed"
        assert not brief_calls
    else:
        analyzer.main()
        assert statuses[-1]["status"] == "idle"
        assert brief_calls == [1]


def test_failed_fifo_job_yields_to_new_work_and_remains_retryable(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 12))
        session.add(run)
        session.flush()
        session.add_all([
            Job(site="site", job_id="old", title="Old", desc="Description", run_id=run.id,
                first_seen_run_id=run.id, discovery_date=datetime(2026, 10, 10)),
            Job(site="site", job_id="new", title="New", desc="Description", run_id=run.id,
                first_seen_run_id=run.id, discovery_date=datetime(2026, 10, 12)),
        ])
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "parse_args", lambda: SimpleNamespace(compensation_only=False, redo=None))
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *args, **kwargs: {})
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kwargs: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 1)

    def worker(resume_text, task, index):
        submitted.append(task.job_id)
        if task.job_id == "old":
            return analyzer.JobResult(task.id, task.site, task.job_id, "schema_error",
                                      None, None, 0, "invalid JSON", index)
        screen = {"match_percentage": 40, "experience_match": "qualified"}
        return analyzer.JobResult(task.id, task.site, task.job_id, "screened_out",
                                  None, None, 0, "", index, screen)

    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", worker)

    with pytest.raises(SystemExit):
        analyzer.main()
    analyzer.main()
    assert submitted == ["old", "new"]

    # Once its bounded delay expires, the failed row returns to the queue; it
    # was deferred for fairness rather than silently rejected or abandoned.
    with factory() as session:
        retry = session.execute(
            analyzer.select(JobAnalysisRetry).where(JobAnalysisRetry.failure_count == 1)
        ).scalar_one()
        assert session.execute(
            analyzer.select(JobAnalysisRetry).join(Job, Job.id == JobAnalysisRetry.job_pk)
            .where(Job.job_id == "new")
        ).scalar_one_or_none() is None
        retry.retry_after = datetime.now() - timedelta(minutes=1)
        session.commit()
    with pytest.raises(SystemExit):
        analyzer.main()
    assert submitted == ["old", "new", "old"]
    with factory() as session:
        assert session.get(JobAnalysisRetry, retry.job_pk).failure_count == 2


def test_redo_bypasses_retry_backoff(tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 12))
        session.add(run)
        session.flush()
        job = Job(site="site", job_id="job", title="Job", desc="Description", run_id=run.id,
                  first_seen_run_id=run.id)
        session.add(job)
        session.flush()
        session.add(JobAnalysisRetry(job_pk=job.id, failure_count=3,
                                     input_hash="old-input",
                                     last_failed_at=datetime.now(),
                                     retry_after=datetime.now() + timedelta(days=1)))
        session.commit()
        assert analyzer.select_job_ids(session, redo=1) == [job.id]


def test_reprocessing_selector_prioritizes_pending_then_least_recent(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", False)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 12))
        session.add(run)
        session.flush()
        recent = Job(site="site", job_id="recent", title="Recent", run_id=run.id,
                     first_seen_run_id=run.id, ai_analysis="{}",
                     ai_analyzed_at=datetime(2026, 10, 12))
        pending = Job(site="site", job_id="pending", title="Pending", run_id=run.id,
                      first_seen_run_id=run.id)
        older = Job(site="site", job_id="older", title="Older", run_id=run.id,
                    first_seen_run_id=run.id, ai_analysis="{}",
                    ai_analyzed_at=datetime(2026, 10, 10))
        session.add_all([recent, pending, older])
        session.commit()
        assert analyzer.select_job_ids(session) == [pending.id, older.id, recent.id]


def test_changed_retry_fingerprint_resets_failure_count(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    monkeypatch.setattr(analyzer, "AI_RETRY_BASE_MINUTES", 15)
    monkeypatch.setattr(analyzer, "AI_RETRY_MAX_MINUTES", 60)
    failed_at = datetime(2026, 10, 12, 12, 0)
    with factory() as session:
        session.add(JobAnalysisRetry(job_pk=99, input_hash="old", failure_count=8,
                                     last_failed_at=failed_at,
                                     retry_after=failed_at + timedelta(minutes=60)))
        session.commit()
        retry = analyzer.record_analysis_failure(session, 99, "new", failed_at)
        assert retry.failure_count == 1
        assert retry.retry_after == failed_at + timedelta(minutes=15)


def test_thinking_and_review_settings_change_retry_fingerprint(monkeypatch):
    task = analyzer.JobTask(1, "site", "job", "Title", "Description", "", 1, None)
    original_think = analyzer.get_ai_profile("detail").think
    original_review = analyzer.AI_SECOND_PASS_REVIEW
    baseline = analyzer.analysis_retry_input_hash("resume", task)

    monkeypatch.setenv(
        "AI_DETAIL_THINK", "medium" if original_think != "medium" else "high"
    )
    assert analyzer.analysis_retry_input_hash("resume", task) != baseline

    monkeypatch.setenv("AI_DETAIL_THINK", str(original_think))
    monkeypatch.setattr(analyzer, "AI_SECOND_PASS_REVIEW", not original_review)
    assert analyzer.analysis_retry_input_hash("resume", task) != baseline


def test_changed_input_bypasses_backoff_and_success_clears_retry(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 12))
        session.add(run)
        session.flush()
        job = Job(site="site", job_id="job", title="Changed", desc="New evidence", run_id=run.id,
                  first_seen_run_id=run.id)
        session.add(job)
        session.flush()
        job_id = job.id
        session.add(JobAnalysisRetry(job_pk=job_id, input_hash="stale-input", failure_count=5,
                                     last_failed_at=datetime.now(),
                                     retry_after=datetime.now() + timedelta(days=1)))
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "parse_args", lambda: SimpleNamespace(compensation_only=False, redo=None))
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *args, **kwargs: {})
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kwargs: None)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 1)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)

    def worker(resume_text, task, index):
        submitted.append(task.job_id)
        screen = {"match_percentage": 40, "experience_match": "qualified"}
        return analyzer.JobResult(task.id, task.site, task.job_id, "screened_out",
                                  None, None, 0, "", index, screen)

    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", worker)
    analyzer.main()
    assert submitted == ["job"]
    with factory() as session:
        assert session.get(JobAnalysisRetry, job_id) is None


def test_empty_posting_does_not_consume_bounded_queue(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps", finished_at=datetime(2026, 10, 12))
        session.add(run)
        session.flush()
        session.add_all([
            Job(site="site", job_id="empty", title="", desc="", run_id=run.id,
                first_seen_run_id=run.id, discovery_date=datetime(2026, 10, 10)),
            Job(site="site", job_id="valid", title="Valid", desc="Evidence", run_id=run.id,
                first_seen_run_id=run.id, discovery_date=datetime(2026, 10, 12)),
        ])
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    submitted = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "parse_args", lambda: SimpleNamespace(compensation_only=False, redo=None))
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kwargs: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *args, **kwargs: {})
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kwargs: None)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    monkeypatch.setattr(analyzer, "LIMIT", 1)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)

    def worker(resume_text, task, index):
        submitted.append(task.job_id)
        screen = {"match_percentage": 40, "experience_match": "qualified"}
        return analyzer.JobResult(task.id, task.site, task.job_id, "screened_out",
                                  None, None, 0, "", index, screen)

    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", worker)
    analyzer.main()
    assert submitted == ["valid"]


def test_fit_brief_prompt_treats_compatible_remote_work_as_non_risk():
    prompt = analyzer.render_fit_brief_system_prompt()

    assert "Compatible remote work is not a risk" in prompt
    assert "Prioritize the most consequential concerns" in prompt
    assert "Use [] if none are meaningful" in prompt


def test_fit_brief_prompt_uses_direct_unnamed_match_explanations():
    prompt = analyzer.render_fit_brief_system_prompt()

    assert "Do not refer to the applicant by name" in prompt
    assert "explain the evidence-to-requirement connection directly" in prompt
    assert "do not begin with or repeatedly use the applicant's name" in prompt


def test_fit_brief_repair_explains_the_string_array_contract():
    prompt = analyzer.fit_brief_repair_prompt("{}", "strongest_matches must be list[str]")
    assert "arrays of strings, not objects" in prompt
    assert "Validation failure: strongest_matches must be list[str]" in prompt


def test_fit_brief_worker_supplies_actionable_type_repair(monkeypatch):
    import json

    valid = {"score_explanation": "Strong alignment.", "strongest_matches": ["Relevant experience"],
             "concerns_or_gaps": [], "risk_flags": [], "interview_angle": "Explain the experience.",
             "resume_bullet_matches": []}
    calls = []

    def invoke(messages, **options):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps({**valid, "strongest_matches": [{"alignment": "Relevant experience"}]})
        repair = messages[-1].content
        assert "strongest_matches must be list[str]" in repair
        assert "arrays of strings, not objects" in repair
        return json.dumps(valid)

    monkeypatch.setattr(analyzer, "invoke_ollama_json", invoke)
    monkeypatch.setattr(analyzer, "AI_MAX_ATTEMPTS", 2)
    task = analyzer.FitBriefTask(1, "site", "job", "Title", "Description", "{}", 90, None)
    result = analyzer.analyze_fit_brief_worker("resume", task, 1)
    assert result.status == "ok"
    assert len(calls) == 2


def test_digest_without_final_scores_does_not_promote_a_screen_to_final():
    job = Job(job_id="screened", site="site", title="screened", run_id=1)
    job._digest_screen = JobMatchScreen(match_percentage=70, experience_match="qualified")
    context = digest.DigestContext(runs=[], label="test", date_label="test")
    for body in (digest.render_text_digest(context, [job], 1),
                 digest.render_html_digest(context, [job], 1)):
        assert "No final scores yet" in body
        assert "Last screen: 70% (preliminary)" in body
    assert digest.digest_counts([job]) == (0, 1, None)


def test_application_prep_heartbeats_during_model_wait_and_restores_parent_status(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        job = Job(
            site="site", job_id="priority-job", title="Senior Python Engineer",
            desc="Build reliable services.", run_id=run.id,
            ai_match_percentage=90, content_hash="job-hash",
        )
        session.add(job)
        session.flush()
        session.add_all([
            JobSwipe(job_pk=job.id, action="like"),
            JobApplicationPrep(job_pk=job.id, status="queued"),
        ])
        session.commit()

    publications = []
    wait_calls = 0

    def controlled_wait(futures, **_kwargs):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            return set(), set(futures)
        return set(futures), set()

    def worker(_resume, task, index):
        return analyzer.JobResult(
            task.id, task.site, task.job_id, "ok", "{}",
            {
                "prep_json": "{}",
                "resume_hash": "resume-hash",
                "responsibilities_hash": "",
                "job_content_hash": "job-hash",
                "generated_at": datetime(2026, 10, 12),
                "schema_version": analyzer.APPLICATION_PREP_SCHEMA_VERSION,
            },
            1.0, "", index,
        )

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "wait", controlled_wait)
    monkeypatch.setattr(analyzer, "analyze_application_prep_worker", worker)
    monkeypatch.setattr(
        analyzer, "publish_ai_processing_status", lambda **kwargs: publications.append(kwargs)
    )

    analyzer.run_application_prep_generation(
        resume_text="resume",
        preflight=False,
        restore_status=lambda: publications.append({
            "status": "running",
            "message": "Parent analysis restored.",
            "total_jobs": 9,
            "completed_jobs": 4,
        }),
    )

    waiting = next(
        item for item in publications
        if item.get("message") == "Application Prep is waiting for Ollama responses."
    )
    assert waiting["status"] == "running"
    assert waiting["current_jobs"][0]["stage"] == "application_prep"
    assert waiting["current_jobs"][0]["job_id"] == "priority-job"
    assert publications[-1] == {
        "status": "running",
        "message": "Parent analysis restored.",
        "total_jobs": 9,
        "completed_jobs": 4,
    }
    assert not any(item.get("status") == "idle" for item in publications)


def test_fit_brief_priority_check_restores_fit_brief_stage(monkeypatch, tmp_path):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        session.add(Job(
            site="site", job_id="brief-job", title="Senior Python Engineer",
            desc="Build reliable services.", run_id=run.id,
            ai_match_percentage=90, ai_analysis="{}",
        ))
        session.commit()

    publications = []

    def priority_check(**kwargs):
        publications.append({"status": "running", "message": "Application Prep active."})
        kwargs["restore_status"]()
        return {}

    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "run_application_prep_generation", priority_check)
    monkeypatch.setattr(
        analyzer,
        "analyze_fit_brief_worker",
        lambda _resume, task, index: analyzer.JobResult(
            task.id, task.site, task.job_id, "schema_error",
            None, None, 0.0, "invalid", index,
        ),
    )
    monkeypatch.setattr(
        analyzer, "publish_ai_processing_status", lambda **kwargs: publications.append(kwargs)
    )

    analyzer.run_fit_brief_generation("resume", preflight=False)

    priority_index = next(
        index for index, item in enumerate(publications)
        if item.get("message") == "Application Prep active."
    )
    restored = publications[priority_index + 1]
    assert restored["status"] == "running"
    assert restored["message"].startswith("Resuming Stage 3 Fit Brief")
    assert publications[-1]["status"] == "idle"
    assert publications[-1]["message"] == "Stage 3 Fit Brief generation finished."


def test_application_prep_outage_does_not_restore_parent_and_requeues_outstanding_work(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    prep_ids = []
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        for suffix in ("one", "two"):
            job = Job(
                site="site", job_id=suffix, title="Python Engineer",
                desc="Build services.", run_id=run.id,
                ai_match_percentage=90, content_hash=f"hash-{suffix}",
            )
            session.add(job)
            session.flush()
            prep = JobApplicationPrep(job_pk=job.id, status="queued")
            session.add_all([JobSwipe(job_pk=job.id, action="like"), prep])
            session.flush()
            prep_ids.append(prep.id)
        session.commit()

    publications = []
    restored = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(
        analyzer,
        "analyze_application_prep_worker",
        lambda _resume, task, index: analyzer.JobResult(
            task.id, task.site, task.job_id, "ollama_unavailable",
            None, None, 0.0, "offline", index,
        ),
    )
    monkeypatch.setattr(
        analyzer, "publish_ai_processing_status", lambda **kwargs: publications.append(kwargs)
    )

    with pytest.raises(SystemExit):
        analyzer.run_application_prep_generation(
            resume_text="resume",
            preflight=False,
            restore_status=lambda: restored.append(True),
        )

    assert restored == []
    assert publications[-1]["status"] == "failed"
    with factory() as session:
        preps = [session.get(JobApplicationPrep, prep_id) for prep_id in prep_ids]
        assert [prep.status for prep in preps] == ["queued", "queued"]
        assert all(prep.started_at is None for prep in preps)


def test_empty_standalone_application_prep_clears_stale_dashboard_state(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    publications = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(
        analyzer, "publish_ai_processing_status", lambda **kwargs: publications.append(kwargs)
    )

    analyzer.run_application_prep_generation(resume_text="resume", preflight=False)

    assert publications[-1]["status"] == "idle"
    assert publications[-1]["message"] == "Application Prep queue is empty."
    assert publications[-1]["finished_at"] is not None


def test_application_prep_coordinator_exception_requeues_running_work(
    monkeypatch, tmp_path
):
    _, factory = _database(tmp_path)
    with factory() as session:
        run = IntegrationRun(user="test", mode="steps")
        session.add(run)
        session.flush()
        job = Job(
            site="site", job_id="coordinator-error", title="Python Engineer",
            desc="Build services.", run_id=run.id,
            ai_match_percentage=90, content_hash="job-hash",
        )
        session.add(job)
        session.flush()
        prep = JobApplicationPrep(job_pk=job.id, status="queued")
        session.add_all([JobSwipe(job_pk=job.id, action="like"), prep])
        session.commit()
        prep_id = prep.id

    publications = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "wait", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("wait failed")))
    monkeypatch.setattr(
        analyzer,
        "analyze_application_prep_worker",
        lambda _resume, task, index: analyzer.JobResult(
            task.id, task.site, task.job_id, "schema_error",
            None, None, 0.0, "invalid", index,
        ),
    )
    monkeypatch.setattr(
        analyzer, "publish_ai_processing_status", lambda **kwargs: publications.append(kwargs)
    )

    with pytest.raises(RuntimeError, match="wait failed"):
        analyzer.run_application_prep_generation(resume_text="resume", preflight=False)

    with factory() as session:
        prep = session.get(JobApplicationPrep, prep_id)
        assert prep.status == "queued"
        assert prep.started_at is None
    assert publications[-1]["status"] == "failed"
    assert publications[-1]["message"] == "Application Prep stopped because its coordinator failed."
