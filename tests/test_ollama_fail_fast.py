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
from app.models import Base, Job


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


@pytest.mark.parametrize(
    ("configured", "installed"),
    [("deepseek-r1:8b", "deepseek-r1:8b"), ("gemma3", "gemma3:latest")],
)
def test_preflight_accepts_installed_model_and_latest_alias(
    monkeypatch: pytest.MonkeyPatch, configured: str, installed: str
) -> None:
    monkeypatch.setattr(analyzer, "OLLAMA_MODEL", configured)
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
    monkeypatch.setattr(analyzer, "OLLAMA_MODEL", "deepseek-r1:8b")
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

    result = analyzer.analyze_job_worker("resume", task, 1)

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
    monkeypatch.setattr(analyzer, "analyze_job_worker", unexpected_worker)

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
    monkeypatch.setattr(analyzer, "analyze_job_worker", unexpected_worker)
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
    monkeypatch.setattr(analyzer, "analyze_job_worker", unavailable_worker)
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
