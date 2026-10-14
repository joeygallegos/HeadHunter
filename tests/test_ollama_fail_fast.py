from __future__ import annotations

import io
import json
import time
import urllib.error
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import analyze_jobs_ollama as analyzer
from app.ai_profiles import generation_config_hash, load_job_profile
from app.models import Base, Job


def test_detail_profile_uses_legacy_settings_until_overridden(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "baseline-model")
    monkeypatch.setenv("OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "600")
    monkeypatch.setenv("AI_THINKING_RETRY_NUM_PREDICT", "1200")
    monkeypatch.setenv("OLLAMA_THINK", "high")
    for suffix in (
        "MODEL",
        "THINK",
        "NUM_CTX",
        "NUM_PREDICT",
        "RETRY_NUM_PREDICT",
        "TEMPERATURE",
        "TIMEOUT_SEC",
        "MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(f"AI_DETAIL_{suffix}", raising=False)

    baseline = load_job_profile("detail")
    assert (baseline.model, baseline.num_ctx, baseline.num_predict) == (
        "baseline-model", 4096, 600
    )
    assert (baseline.retry_num_predict, baseline.think) == (1200, "high")

    monkeypatch.setenv("AI_DETAIL_MODEL", "detail-model")
    monkeypatch.setenv("AI_DETAIL_NUM_CTX", "8192")
    monkeypatch.setenv("AI_DETAIL_THINK", "false")
    overridden = load_job_profile("detail")
    assert (overridden.model, overridden.num_ctx, overridden.think) == (
        "detail-model", 8192, False
    )
    assert overridden.num_predict == 600


def test_specialized_profiles_preserve_existing_defaults(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_NUM_CTX", "32768")
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "6144")
    monkeypatch.setenv("AI_THINKING_RETRY_NUM_PREDICT", "12288")
    monkeypatch.setenv("OLLAMA_THINK", "high")
    for key in (
        "AI_SCREEN_THINK",
        "AI_SCREEN_NUM_PREDICT",
        "AI_SCREEN_RETRY_NUM_PREDICT",
        "APPLICATION_PREP_THINK",
        "APPLICATION_PREP_NUM_CTX",
        "APPLICATION_PREP_NUM_PREDICT",
        "APPLICATION_PREP_RETRY_NUM_PREDICT",
    ):
        monkeypatch.delenv(key, raising=False)

    screen = load_job_profile("screen")
    prep = load_job_profile("application_prep")
    assert (screen.think, screen.num_predict, screen.retry_num_predict) == (
        False,
        512,
        2048,
    )
    assert (prep.think, prep.num_ctx, prep.num_predict, prep.retry_num_predict) == (
        False, 65536, 6144, 12288
    )


def test_generation_hash_changes_with_prompt_or_profile(monkeypatch, tmp_path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("first prompt", encoding="utf-8")
    baseline = generation_config_hash(load_job_profile("fit_brief"), str(prompt), 1)

    prompt.write_text("updated prompt", encoding="utf-8")
    assert generation_config_hash(load_job_profile("fit_brief"), str(prompt), 1) != baseline

    prompt.write_text("first prompt", encoding="utf-8")
    monkeypatch.setenv("AI_FIT_BRIEF_TEMPERATURE", "0.4")
    assert generation_config_hash(load_job_profile("fit_brief"), str(prompt), 1) != baseline


class _Response:
    """Minimal context-managed urllib response used by preflight tests."""

    def __init__(self, payload: object, *, raw: bool = False) -> None:
        body = payload if raw else json.dumps(payload)
        self._body = str(body).encode("utf-8")

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_match_prompt_defines_experience_fit_without_a_score_cap() -> None:
    prompt = analyzer.render_system_prompt()

    assert '"underqualified" when there is a material shortfall' in prompt
    assert '"qualified" when the resume meets the stated experience requirements' in prompt
    assert '"overqualified" when the resume materially exceeds' in prompt
    assert "not a hiring decision and not a score cap" in prompt


def test_second_pass_reuses_experience_fit_calibration() -> None:
    prompt = analyzer.review_prompt('{"match_percentage":75}')

    assert "material shortfall against an explicit experience" in prompt
    assert "not a hiring decision or score cap" in prompt


@pytest.mark.parametrize(
    ("configured", "installed"),
    [("deepseek-r1:8b", "deepseek-r1:8b"), ("gemma3", "gemma3:latest")],
)
def test_preflight_accepts_installed_model_and_latest_alias(
    monkeypatch: pytest.MonkeyPatch, configured: str, installed: str
) -> None:
    monkeypatch.setattr(
        analyzer,
        "get_ai_profiles",
        lambda: {"detail": SimpleNamespace(model=configured)},
    )
    monkeypatch.setattr(
        analyzer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(
            {"models": [{"name": installed, "model": installed}]}
        ),
    )
    messages: list[str] = []
    monkeypatch.setattr(analyzer, "log", messages.append)

    analyzer.check_ollama_prerequisites()

    assert messages == [
        f"[ok] Ollama ready at {analyzer.OLLAMA_BASE_URL} | model={configured}"
    ]


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
        TimeoutError("timed out"),
    ],
)
def test_preflight_starts_ollama_after_unreachable_server(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    calls = 0
    popen_calls: list[list[str]] = []
    messages: list[str] = []
    monkeypatch.setattr(
        analyzer,
        "get_ai_profiles",
        lambda: {"detail": SimpleNamespace(model=analyzer.OLLAMA_MODEL)},
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return _Response({"models": [{"name": analyzer.OLLAMA_MODEL}]})

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fail)
    monkeypatch.setattr(
        analyzer.subprocess,
        "Popen",
        lambda args, **_kwargs: popen_calls.append(args),
    )
    monkeypatch.setattr(analyzer, "log", messages.append)

    analyzer.check_ollama_prerequisites()

    assert calls == 3
    assert popen_calls == [["ollama", "serve"]]
    assert any("starting 'ollama serve'" in message for message in messages)
    assert messages[-1] == (
        f"[ok] Ollama ready at {analyzer.OLLAMA_BASE_URL} | model={analyzer.OLLAMA_MODEL}"
    )


def test_preflight_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        analyzer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response("not-json", raw=True),
    )
    monkeypatch.setattr(
        analyzer.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("ollama serve should not start"),
    )

    with pytest.raises(analyzer.OllamaPrerequisiteError, match="invalid JSON"):
        analyzer.check_ollama_prerequisites()


def test_preflight_reports_http_status_without_response_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = urllib.error.HTTPError(
        f"{analyzer.OLLAMA_BASE_URL}/api/tags",
        503,
        "unavailable",
        {},
        io.BytesIO(b"sensitive response details"),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", fail)
    monkeypatch.setattr(
        analyzer.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("ollama serve should not start"),
    )

    with pytest.raises(analyzer.OllamaPrerequisiteError) as exc_info:
        analyzer.check_ollama_prerequisites()

    assert "HTTP 503" in str(exc_info.value)
    assert "sensitive" not in str(exc_info.value)


def test_preflight_reports_exact_pull_command_for_missing_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        analyzer,
        "get_ai_profiles",
        lambda: {"detail": SimpleNamespace(model="deepseek-r1:8b")},
    )
    monkeypatch.setattr(
        analyzer.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"models": [{"name": "gemma3"}]}),
    )
    monkeypatch.setattr(
        analyzer.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("ollama serve should not start"),
    )

    with pytest.raises(analyzer.OllamaPrerequisiteError) as exc_info:
        analyzer.check_ollama_prerequisites()

    assert "ollama pull deepseek-r1:8b" in str(exc_info.value)


def test_chat_connection_failure_is_distinct_from_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError(ConnectionRefusedError(10061, "refused"))

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", unavailable)
    with pytest.raises(analyzer.OllamaUnavailableError):
        analyzer.invoke_ollama_json([])

    def timed_out(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("slow generation")

    monkeypatch.setattr(analyzer.urllib.request, "urlopen", timed_out)
    with pytest.raises(TimeoutError, match="request timed out"):
        analyzer.invoke_ollama_json([])


def test_job_worker_does_not_retry_unavailable_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unavailable(*_args: object, **_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        raise analyzer.OllamaUnavailableError("offline")

    monkeypatch.setattr(analyzer, "invoke_ollama_json", unavailable)
    monkeypatch.setattr(analyzer, "build_messages", lambda *_args: [])
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Description", "", 1, None)

    result = analyzer.analyze_routed_job_worker("resume", task, 1)

    assert result.status == "ollama_unavailable"
    assert calls == 1


def _temporary_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ollama-tests.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_regular_run_with_no_jobs_does_not_require_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(analyzer, "SessionLocal", _temporary_session_factory(tmp_path))
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )

    def unexpected_preflight() -> None:
        raise AssertionError("preflight should not run for an empty batch")

    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", unexpected_preflight)

    analyzer.main()


def test_regular_selector_skips_inactive_jobs_even_with_redo(tmp_path) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        active = Job(
            site="site",
            job_id="active",
            title="Active",
            desc="Description",
            pay="",
            run_id=1,
            is_active=True,
        )
        session.add_all(
            [
                active,
                Job(
                    site="site",
                    job_id="missing",
                    title="Missing",
                    desc="Description",
                    pay="",
                    run_id=1,
                    is_active=False,
                ),
            ]
        )
        session.commit()
        active_id = active.id

        selected = analyzer.select_job_ids(session, redo=10)

    assert selected == [active_id]


def test_regular_preflight_failure_exits_before_worker_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            Job(
                site="site",
                job_id="job-1",
                title="Engineer",
                desc="Description",
                pay="",
                run_id=1,
            )
        )
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(analyzer, "SessionLocal", session_factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )

    def failed_preflight() -> None:
        raise analyzer.OllamaPrerequisiteError("offline; ollama serve")

    def unexpected_worker(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("worker must not be submitted after failed preflight")

    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", failed_preflight)
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", unexpected_worker)

    with pytest.raises(SystemExit) as exc_info:
        analyzer.main()

    assert exc_info.value.code == 1


def test_regular_preflight_auto_start_failure_exits_before_worker_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        session.add(
            Job(
                site="site",
                job_id="job-1",
                title="Engineer",
                desc="Description",
                pay="",
                run_id=1,
            )
        )
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    popen_calls: list[list[str]] = []
    messages: list[str] = []
    clock = iter([0.0, 2.0])

    def unreachable_tags() -> str:
        raise analyzer.OllamaReachabilityError("offline; ollama serve")

    def unexpected_worker(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("worker must not be submitted after failed preflight")

    monkeypatch.setattr(analyzer, "SessionLocal", session_factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "_read_ollama_tags", unreachable_tags)
    monkeypatch.setattr(
        analyzer.subprocess,
        "Popen",
        lambda args, **_kwargs: popen_calls.append(args),
    )
    monkeypatch.setattr(analyzer, "OLLAMA_STARTUP_TIMEOUT_SEC", 1)
    monkeypatch.setattr(analyzer.time, "time", lambda: next(clock))
    monkeypatch.setattr(analyzer.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", unexpected_worker)
    monkeypatch.setattr(analyzer, "log", messages.append)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )

    with pytest.raises(SystemExit) as exc_info:
        analyzer.main()

    assert exc_info.value.code == 1
    assert popen_calls == [["ollama", "serve"]]
    assert any("starting 'ollama serve'" in message for message in messages)
    assert any("did not respond within 1s" in message for message in messages)


def test_regular_run_aborts_before_submitting_remaining_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        for index in range(1, 6):
            session.add(
                Job(
                    site="site",
                    job_id=f"job-{index}",
                    title="Engineer",
                    desc="Description",
                    pay="",
                    run_id=1,
                )
            )
        session.commit()

    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    calls = 0

    def unavailable_worker(
        _resume: str, task: analyzer.JobTask, index: int
    ) -> analyzer.JobResult:
        nonlocal calls
        calls += 1
        # Let the producer fill its bounded queue before the first failure.
        time.sleep(0.05)
        return analyzer.JobResult(
            task.id,
            task.site,
            task.job_id,
            "ollama_unavailable",
            None,
            None,
            0.05,
            "offline",
            index,
        )

    messages: list[str] = []
    monkeypatch.setattr(analyzer, "SessionLocal", session_factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", unavailable_worker)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 2)
    monkeypatch.setattr(analyzer, "log", messages.append)
    monkeypatch.setattr(
        analyzer,
        "parse_args",
        lambda: SimpleNamespace(compensation_only=False, redo=None),
    )

    with pytest.raises(SystemExit) as exc_info:
        analyzer.main()

    assert exc_info.value.code == 1
    assert calls < 5
    assert any("unsubmitted=" in message for message in messages)
    assert any("aborting batch" in message for message in messages)


def test_compensation_run_with_no_jobs_does_not_require_ollama(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(analyzer, "SessionLocal", _temporary_session_factory(tmp_path))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "select_compensation_jobs", lambda *_args: [])

    def unexpected_preflight() -> None:
        raise AssertionError("preflight should not run for an empty batch")

    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", unexpected_preflight)
    args = SimpleNamespace(dry_run=True, force=False, sample_per_site=1)

    analyzer.run_compensation_backfill(args)


def test_compensation_selector_skips_inactive_jobs_even_when_stale_or_forced(
    tmp_path,
) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        session.add_all(
            [
                Job(
                    site="site",
                    job_id="active",
                    title="Active",
                    desc="Description",
                    pay="",
                    run_id=1,
                    is_active=True,
                    compensation_schema_version=None,
                ),
                Job(
                    site="site",
                    job_id="missing",
                    title="Missing",
                    desc="Description",
                    pay="",
                    run_id=1,
                    is_active=False,
                    compensation_schema_version=None,
                ),
            ]
        )
        session.commit()

        stale_only = analyzer.select_compensation_jobs(
            session, SimpleNamespace(force=False, sample_per_site=None)
        )
        forced = analyzer.select_compensation_jobs(
            session, SimpleNamespace(force=True, sample_per_site=None)
        )

    assert [job.job_id for job in stale_only] == ["active"]
    assert [job.job_id for job in forced] == ["active"]


def test_compensation_run_aborts_before_submitting_remaining_jobs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    jobs = [
        SimpleNamespace(
            id=index,
            site="site",
            job_id=f"job-{index}",
            title="Engineer",
            desc="Description",
            pay="",
            run_id=1,
            content_hash=None,
        )
        for index in range(1, 6)
    ]
    calls = 0

    def unavailable_worker(
        task: analyzer.JobTask, index: int
    ) -> analyzer.JobResult:
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return analyzer.JobResult(
            task.id,
            task.site,
            task.job_id,
            "ollama_unavailable",
            None,
            None,
            0.05,
            "offline",
            index,
        )

    messages: list[str] = []
    monkeypatch.setattr(analyzer, "SessionLocal", _temporary_session_factory(tmp_path))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "select_compensation_jobs", lambda *_args: jobs)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "analyze_compensation_worker", unavailable_worker)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 2)
    monkeypatch.setattr(analyzer, "log", messages.append)
    args = SimpleNamespace(dry_run=True, force=False, sample_per_site=1)

    with pytest.raises(SystemExit) as exc_info:
        analyzer.run_compensation_backfill(args)

    assert exc_info.value.code == 1
    assert calls < 5
    assert any("'unsubmitted':" in message for message in messages)
    assert any("aborting batch" in message for message in messages)


@pytest.mark.parametrize("score", [0, 74, 75, 100])
def test_screen_routes_only_relevant_jobs_to_detail(monkeypatch, score):
    calls = []
    monkeypatch.setattr(analyzer, "AI_DEEP_ANALYSIS_MIN_MATCH", 75)

    def invoke(messages, **options):
        calls.append(options)
        return json.dumps({"match_percentage": score, "experience_match": "qualified"})

    def detailed(resume, task, index):
        calls.append("detail")
        return analyzer.JobResult(task.id, task.site, task.job_id, "ok", "{}", {}, 1, "", index)

    monkeypatch.setattr(analyzer, "invoke_ollama_json", invoke)
    monkeypatch.setattr(analyzer, "analyze_job_worker", detailed)
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Requirements", "", 1, None)
    result = analyzer.analyze_routed_job_worker("resume", task, 1)
    screen_profile = analyzer.get_ai_profile("screen")
    assert calls[0]["num_predict"] == screen_profile.num_predict
    assert calls[0]["profile"] == screen_profile
    assert ("detail" in calls) == (score >= 75)
    assert result.status == ("ok" if score >= 75 else "screened_out")
    assert result.screen_payload["match_percentage"] == score


@pytest.mark.parametrize("payload", [
    {"match_percentage": True, "experience_match": "qualified"},
    {"match_percentage": 101, "experience_match": "qualified"},
    {"match_percentage": "74", "experience_match": "qualified"},
    {"match_percentage": 74, "experience_match": []},
    {"match_percentage": 74, "experience_match": "unknown"},
    {"match_percentage": 74, "experience_match": "qualified", "extra": 1},
    {"error": "Insufficient information provided"},
    [],
])
def test_invalid_screen_remains_retryable(monkeypatch, payload):
    calls = []
    monkeypatch.setattr(analyzer, "AI_MAX_ATTEMPTS", 4)
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: calls.append(kw) or json.dumps(payload))
    monkeypatch.setattr(analyzer, "analyze_job_worker", lambda *a: pytest.fail("invalid screen must not deepen"))
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Requirements", "", 1, None)
    result = analyzer.analyze_routed_job_worker("resume", task, 1)
    assert result.status == "schema_error"
    assert result.screen_payload is None
    assert len(calls) == 2


def test_cached_screen_retries_detail_without_model_screen(monkeypatch):
    monkeypatch.setattr(analyzer, "AI_DEEP_ANALYSIS_MIN_MATCH", 75)
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: pytest.fail("cache should avoid screening"))
    monkeypatch.setattr(analyzer, "analyze_job_worker", lambda resume, task, index:
                        analyzer.JobResult(task.id, task.site, task.job_id, "schema_error", None, None, 1, "invalid detail", index))
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Requirements", "", 1, None,
                            cached_screen={"match_percentage": 80, "experience_match": "qualified"})
    result = analyzer.analyze_routed_job_worker("resume", task, 1)
    assert result.status == "schema_error"
    assert result.screen_payload == task.cached_screen


def test_screen_hash_tracks_evidence_but_not_routing_threshold(monkeypatch):
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Requirements", "", 1, "unchanged")
    original = analyzer.screen_input_hash("resume", task)
    assert analyzer.screen_input_hash("updated resume", task) != original
    monkeypatch.setattr(analyzer, "AI_DEEP_ANALYSIS_MIN_MATCH", 25)
    assert analyzer.screen_input_hash("resume", task) == original
    task.desc = "New requirements"
    assert analyzer.screen_input_hash("resume", task) != original
    task.desc = "Requirements"
    monkeypatch.setenv("AI_SCREEN_MODEL", "different-model")
    assert analyzer.screen_input_hash("resume", task) != original


def _configure_screen_run(monkeypatch, tmp_path):
    factory = _temporary_session_factory(tmp_path)
    resume = tmp_path / "resume.txt"
    resume.write_text("resume", encoding="utf-8")
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "RESUME_PATH", str(resume))
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "publish_ai_processing_status", lambda **kw: None)
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kw: {})
    monkeypatch.setattr(analyzer, "run_fit_brief_generation", lambda *a, **kw: {})
    monkeypatch.setattr(analyzer, "parse_args", lambda: SimpleNamespace(compensation_only=False, redo=None))
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "AI_DEEP_ANALYSIS_MIN_MATCH", 75)
    monkeypatch.setattr(analyzer, "LIMIT", 0)
    monkeypatch.setattr(analyzer, "ONLY_EMPTY", True)
    monkeypatch.setattr(analyzer, "SITE_FILTER", "")
    return factory, resume


def test_screen_cache_schema_does_not_require_references_privilege():
    from app.models import JobMatchScreen

    table = JobMatchScreen.__table__
    assert table.c.job_pk.primary_key is True
    assert not table.foreign_keys


def test_screen_cache_persists_without_overwriting_final_fields(monkeypatch, tmp_path):
    from app.models import JobMatchScreen
    factory, resume = _configure_screen_run(monkeypatch, tmp_path)
    with factory() as session:
        job = Job(site="site", job_id="job", title="Engineer", desc="Requirements", pay="$100,000", run_id=1)
        session.add(job)
        session.commit()
        job_id = job.id
    calls = []
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: calls.append("preflight"))
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: calls.append("screen") or
                        '{"match_percentage":30,"experience_match":"underqualified"}')
    analyzer.main()
    with factory() as session:
        assert session.get(JobMatchScreen, job_id).match_percentage == 30
        job = session.get(Job, job_id)
        assert job.ai_analysis is None
        assert job.ai_match_percentage is None
        assert job.pay == "$100,000"
    analyzer.main()
    assert calls == ["preflight", "screen"]
    resume.write_text("updated resume", encoding="utf-8")
    analyzer.main()
    assert calls == ["preflight", "screen", "preflight", "screen"]


def test_limit_does_not_starve_jobs_after_cached_screen_out(monkeypatch, tmp_path):
    from app.models import JobMatchScreen
    factory, _ = _configure_screen_run(monkeypatch, tmp_path)
    monkeypatch.setattr(analyzer, "LIMIT", 1)
    with factory() as session:
        first = Job(site="site", job_id="first", title="Engineer", desc="Requirements", run_id=1)
        second = Job(site="site", job_id="second", title="Engineer", desc="Requirements", run_id=1)
        session.add_all([first, second])
        session.commit()
        task = analyzer.JobTask(first.id, "site", "first", "Engineer", "Requirements", "", 1, None)
        session.add(JobMatchScreen(job_pk=first.id, input_hash=analyzer.screen_input_hash("resume", task),
                                  match_percentage=30, experience_match="qualified"))
        session.commit()
    processed = []
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", lambda resume, task, index:
                        processed.append(task.job_id) or analyzer.JobResult(task.id, task.site, task.job_id,
                            "skip", None, None, 0, "", index))
    analyzer.main()
    assert processed == ["second"]


def test_priority_queue_is_checked_between_background_batches(monkeypatch, tmp_path):
    factory, _ = _configure_screen_run(monkeypatch, tmp_path)
    with factory() as session:
        session.add_all([Job(site="site", job_id=str(i), title="Engineer", desc="Requirements", run_id=1)
                         for i in range(3)])
        session.commit()
    events = []
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kw: events.append("priority") or {})
    monkeypatch.setattr(analyzer, "analyze_routed_job_worker", lambda resume, task, index:
                        events.append(task.job_id) or analyzer.JobResult(task.id, task.site, task.job_id,
                            "skip", None, None, 0, "", index))
    analyzer.main()
    assert events == ["priority", "0", "priority", "1", "priority", "2", "priority"]


def test_explicit_redo_bypasses_screen(monkeypatch):
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: pytest.fail("redo should bypass screening"))
    monkeypatch.setattr(analyzer, "analyze_job_worker", lambda resume, task, index:
                        analyzer.JobResult(task.id, task.site, task.job_id, "ok", "{}", {}, 0, "", index))
    task = analyzer.JobTask(1, "site", "job", "Engineer", "Requirements", "", 1, None, force_detail=True)
    assert analyzer.analyze_routed_job_worker("resume", task, 1).status == "ok"


def test_routed_analysis_stores_reviewed_score_separately_from_screen(monkeypatch, tmp_path):
    from app.models import JobMatchScreen
    factory, _ = _configure_screen_run(monkeypatch, tmp_path)
    with factory() as session:
        job = Job(site="site", job_id="job", title="Engineer", desc="Requirements", run_id=1)
        session.add(job)
        session.commit()
        job_id = job.id
    compensation = {key: None for key in analyzer.COMPENSATION_KEYS}
    compensation["multiple_pay_ranges"] = False
    detail = {"match_percentage": 90, "compensation": compensation,
              "fit_summary": "A strong match for the stated requirements.",
              "keywords_overlap": [], "missing_keywords": [],
              "experience_match": "qualified", "location_policy_match": "unknown"}
    reviewed = {**detail, "match_percentage": 92}
    responses = [json.dumps({"match_percentage": 80, "experience_match": "qualified"}),
                 json.dumps(detail), json.dumps(reviewed)]
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "AI_SECOND_PASS_REVIEW", True)
    monkeypatch.setattr(analyzer, "AI_REVIEW_MIN_MATCH", None)
    monkeypatch.setattr(analyzer, "AI_REVIEW_MAX_MATCH", None)
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: responses.pop(0))
    analyzer.main()
    assert responses == []
    with factory() as session:
        assert session.get(JobMatchScreen, job_id).match_percentage == 80
        assert session.get(Job, job_id).ai_match_percentage == 92


def test_lower_threshold_reuses_screen_without_rescreening(monkeypatch, tmp_path):
    from app.models import JobMatchScreen
    factory, _ = _configure_screen_run(monkeypatch, tmp_path)
    with factory() as session:
        job = Job(site="site", job_id="job", title="Engineer", desc="Requirements", run_id=1)
        session.add(job)
        session.commit()
        task = analyzer.JobTask(job.id, "site", "job", "Engineer", "Requirements", "", 1, None)
        session.add(JobMatchScreen(job_pk=job.id, input_hash=analyzer.screen_input_hash("resume", task),
                                  match_percentage=70, experience_match="qualified"))
        session.commit()
    monkeypatch.setattr(analyzer, "AI_DEEP_ANALYSIS_MIN_MATCH", 65)
    monkeypatch.setattr(analyzer, "check_ollama_prerequisites", lambda: None)
    monkeypatch.setattr(analyzer, "invoke_ollama_json", lambda *a, **kw: pytest.fail("fresh score should reroute without inference"))
    calls = []
    monkeypatch.setattr(analyzer, "analyze_job_worker", lambda resume, task, index:
                        calls.append(task.job_id) or analyzer.JobResult(task.id, task.site, task.job_id,
                            "schema_error", None, None, 0, "detail failure", index))
    with pytest.raises(SystemExit) as exc:
        analyzer.main()
    assert exc.value.code == 1
    assert calls == ["job"]


def test_fit_brief_batches_yield_to_application_prep(monkeypatch, tmp_path):
    factory = _temporary_session_factory(tmp_path)
    with factory() as session:
        session.add_all([Job(site="site", job_id=str(i), title="Engineer", desc="Requirements",
                             run_id=1, ai_match_percentage=90, ai_analysis="{}") for i in range(3)])
        session.commit()
    events = []
    monkeypatch.setattr(analyzer, "SessionLocal", factory)
    monkeypatch.setattr(analyzer, "init_db", lambda: None)
    monkeypatch.setattr(analyzer, "AI_FIT_BRIEF_MIN_MATCH", 75)
    monkeypatch.setattr(analyzer, "AI_CONCURRENCY", 1)
    monkeypatch.setattr(analyzer, "AI_MAX_INFLIGHT", 1)
    monkeypatch.setattr(analyzer, "run_application_prep_generation", lambda **kw: events.append("priority") or {})
    monkeypatch.setattr(analyzer, "analyze_fit_brief_worker", lambda resume, task, index:
                        events.append(task.job_id) or analyzer.JobResult(task.id, task.site, task.job_id,
                            "schema_error", None, None, 0, "brief failure", index))
    analyzer.run_fit_brief_generation("resume", preflight=False)
    # Fit Brief selection remains highest-score, newest-first; each batch gives
    # newly queued Application Prep the next available inference slot.
    assert events == ["priority", "2", "priority", "1", "priority", "0"]
