from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import analyze_jobs_ollama as analyzer
import dashboard
import email_job_digest as digest
from app.models import (
    Base,
    IntegrationRun,
    Job,
    JobAnalysisRetry,
    JobApplicationPrep,
    JobMatchScreen,
    JobSwipe,
)


def test_reasoning_only_screen_gets_one_bounded_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(analyzer, "AI_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(analyzer, "AI_SCREEN_NUM_PREDICT", 128)
    monkeypatch.setattr(analyzer, "AI_SCREEN_RETRY_NUM_PREDICT", 512)

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
    assert calls == [{"num_predict": 128, "think": False},
                     {"num_predict": 512, "think": False}]


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

    assert "selectedJob().is_active ? 'Active' : 'Inactive'" in status_markup
    assert 'class="text-red-700"' in status_markup
    assert (
        'x-show="selectedJob().application_prep_eligibility?.reason"'
        in status_markup
    )
    assert 'x-text="` - ${selectedJob().application_prep_eligibility.reason}`"' in status_markup
    assert (
        'x-text="selectedJob().application_prep_eligibility?.reason"'
        not in template
    )


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
    original_think = analyzer.OLLAMA_THINK
    original_review = analyzer.AI_SECOND_PASS_REVIEW
    baseline = analyzer.analysis_retry_input_hash("resume", task)

    monkeypatch.setattr(analyzer, "OLLAMA_THINK", "medium" if original_think != "medium" else "high")
    assert analyzer.analysis_retry_input_hash("resume", task) != baseline

    monkeypatch.setattr(analyzer, "OLLAMA_THINK", original_think)
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
