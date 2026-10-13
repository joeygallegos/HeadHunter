from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import analyze_market_signals as market
from app.models import Base, IntegrationRun, Job


def _temporary_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'market.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_job(
    session,
    *,
    job_id: str,
    match: int,
    active: bool = True,
    overlap: str = '["Python", "python", "Vulnerability Management"]',
    missing: str = '["Kubernetes"]',
) -> None:
    session.add(
        Job(
            site="example",
            job_id=job_id,
            title=f"Security Engineer {job_id}",
            url=f"https://example.test/{job_id}",
            desc="Security work.",
            is_active=active,
            run_id=1,
            ai_match_percentage=match,
            ai_keywords_overlap=overlap,
            ai_missing_keywords=missing,
            ai_analyzed_at=datetime(2026, 10, 1),
        )
    )


def _job(
    job_id: int,
    *,
    overlap: str,
    missing: str,
    match: int = 80,
    title: str = "Security Engineer",
) -> market.MarketJob:
    return market.MarketJob(
        id=job_id,
        site="example",
        job_id=str(job_id),
        title=title,
        url=f"https://example.test/{job_id}",
        is_active=True,
        ai_match_percentage=match,
        ai_analyzed_at=datetime(2026, 10, job_id),
        overlap_value=overlap,
        missing_value=missing,
    )


def test_help_explains_behavior_outputs_and_common_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        market.parse_args(["--help"])

    help_text = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "Key behavior:" in help_text
    assert "The script is read-only" in help_text
    assert "Ollama is optional" in help_text
    assert "Output files:" in help_text
    assert "Common commands:" in help_text
    assert "--include-inactive --min-match 80" in help_text


def test_selector_uses_inclusive_threshold_and_active_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    session_factory = _temporary_session_factory(tmp_path)
    with session_factory() as session:
        session.add(IntegrationRun(id=1, user="test", mode="test"))
        session.flush()
        _seed_job(session, job_id="74", match=74)
        _seed_job(session, job_id="75", match=75)
        _seed_job(session, job_id="inactive", match=90, active=False)
        session.commit()
    monkeypatch.setattr(market, "SessionLocal", session_factory)

    active = market.select_market_jobs(min_match=75)
    all_jobs = market.select_market_jobs(min_match=75, include_inactive=True)

    assert [job.job_id for job in active] == ["75"]
    assert {job.job_id for job in all_jobs} == {"75", "inactive"}


def test_raw_signals_count_each_term_once_per_job_and_mark_mixed() -> None:
    jobs = [
        _job(1, overlap='["Python", " python ", "Threat Hunting"]', missing='["Kubernetes"]'),
        _job(2, overlap='["PYTHON", "Kubernetes"]', missing='["Threat Hunting"]'),
    ]

    result = market.build_raw_signals(jobs)
    signals = {signal["key"]: signal for signal in result["signals"]}

    assert signals["python"]["job_count"] == 2
    assert signals["python"]["overlap_job_count"] == 2
    assert signals["python"]["prevalence_percentage"] == 100.0
    assert signals["kubernetes"]["category"] == "mixed"
    assert signals["threat hunting"]["category"] == "mixed"
    assert len(signals["python"]["evidence"]) == 2


def test_raw_signals_report_empty_and_malformed_payloads() -> None:
    jobs = [
        _job(1, overlap="not-json", missing="[]"),
        _job(2, overlap='["Python"]', missing=""),
    ]

    result = market.build_raw_signals(jobs)

    assert result["data_quality"] == {
        "missing_empty": 2,
        "overlap_malformed": 1,
        "overlap_valid": 1,
    }


def test_group_validation_rejects_unknown_or_reused_ids() -> None:
    with pytest.raises(ValueError, match="valid"):
        market.validate_grouping_payload(
            {"groups": [{"canonical_name": "Go", "member_ids": ["s0", "s9"]}]},
            2,
        )
    with pytest.raises(ValueError, match="only one group"):
        market.validate_grouping_payload(
            {
                "groups": [
                    {"canonical_name": "Go", "member_ids": ["s0", "s1"]},
                    {"canonical_name": "Other", "member_ids": ["s1", "s2"]},
                ]
            },
            3,
        )


def test_group_validation_rejects_broad_and_narrow_skill_merge() -> None:
    with pytest.raises(ValueError, match="broader skill"):
        market.validate_grouping_payload(
            {"groups": [{"canonical_name": "Python", "member_ids": ["s0", "s1"]}]},
            2,
            candidate_labels=["Python", "Python Automation"],
        )

    groups = market.validate_grouping_payload(
        {"groups": [{"canonical_name": "Go", "member_ids": ["s0", "s1", "s2"]}]},
        3,
        candidate_labels=["Go", "Golang", "Go language"],
    )
    assert groups[0]["canonical_name"] == "Go"


def test_apply_groups_recomputes_distinct_job_counts() -> None:
    jobs = [
        _job(1, overlap='["Go", "Golang"]', missing="[]"),
        _job(2, overlap='["Golang"]', missing='["Go language"]'),
    ]
    raw = market.build_raw_signals(jobs)["signals"]
    indexes = {signal["key"]: f"s{index}" for index, signal in enumerate(raw)}
    groups = [
        {
            "canonical_name": "Go",
            "member_ids": [indexes["go"], indexes["golang"], indexes["go language"]],
        }
    ]

    grouped = market.apply_groups(raw, groups, cohort_size=2)

    assert len(grouped) == 1
    assert grouped[0]["job_count"] == 2
    assert grouped[0]["overlap_job_count"] == 2
    assert grouped[0]["missing_job_count"] == 1
    assert grouped[0]["category"] == "mixed"
    assert set(grouped[0]["grouped_from"]) == {"Go", "Golang", "Go language"}


def test_ollama_failure_retains_raw_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    jobs = [_job(1, overlap='["Python"]', missing='["Kubernetes"]')]
    monkeypatch.setattr(
        market,
        "group_with_ollama",
        lambda *_args, **_kwargs: ([], "Ollama unavailable"),
    )

    payload = market.build_report_payload(
        jobs,
        min_match=75,
        include_inactive=False,
        use_ollama=True,
        ollama_max_terms=10,
    )

    assert payload["warning"] == "Ollama unavailable"
    assert payload["signals"] == payload["raw_signals"]


def test_reports_escape_job_content_and_retain_full_json(tmp_path) -> None:
    unsafe_job = _job(
        1,
        overlap='["Python <script>"]',
        missing="[]",
        title='<img src=x onerror="alert(1)">',
    )
    unsafe_job = market.MarketJob(
        **{**unsafe_job.__dict__, "url": "javascript:alert(1)"}
    )
    jobs = [unsafe_job]
    payload = market.build_report_payload(
        jobs,
        min_match=75,
        include_inactive=False,
        use_ollama=False,
        ollama_max_terms=10,
    )

    written = market.write_reports(payload, tmp_path, display_limit=1)
    html_report = (tmp_path / "market_signals.html").read_text(encoding="utf-8")
    json_report = json.loads((tmp_path / "market_signals.json").read_text(encoding="utf-8"))

    assert set(written) == {"json", "markdown", "html"}
    assert "<script>" not in html_report
    assert "<img src=x" not in html_report
    assert "&lt;script&gt;" in html_report
    assert "javascript:" not in html_report
    assert len(json_report["signals"]) == 1
