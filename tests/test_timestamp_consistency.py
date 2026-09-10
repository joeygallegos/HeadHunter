from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dashboard
from app.db import _set_mysql_session_utc, utc_now_naive
from app import models
from app.models import Base, IntegrationRun, Job
from scripts.migrate_timestamps_to_utc import local_naive_to_utc_naive


class _FakeCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance


def test_utc_now_naive_returns_current_naive_utc() -> None:
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    value = utc_now_naive()
    after = datetime.now(timezone.utc).replace(tzinfo=None)

    assert value.tzinfo is None
    assert before <= value <= after


def test_mysql_connection_initializes_session_timezone_to_utc() -> None:
    connection = _FakeConnection()

    _set_mysql_session_utc(connection, None)

    assert connection.cursor_instance.statements == ["SET SESSION time_zone = '+00:00'"]
    assert connection.cursor_instance.closed is True


def test_dashboard_formats_stored_utc_as_central_with_dst_label() -> None:
    assert dashboard._fmt_dt(datetime(2026, 1, 15, 18, 0, 0)) == "2026-01-15 12:00:00 CST"
    assert dashboard._fmt_dt(datetime(2026, 7, 16, 11, 13, 39)) == "2026-07-16 06:13:39 CDT"
    assert dashboard._fmt_dt(datetime(2026, 7, 16, 15, 34, 3)) == "2026-07-16 10:34:03 CDT"


def test_dashboard_normalizes_aware_values_before_display() -> None:
    aware = datetime(2026, 7, 16, 11, 13, 39, tzinfo=timezone.utc)

    assert dashboard._fmt_dt(aware) == "2026-07-16 06:13:39 CDT"


def test_dashboard_uses_configured_display_timezone(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "resolve_display_timezone", lambda *_args, **_kwargs: "America/Los_Angeles")

    assert dashboard._fmt_dt(datetime(2026, 7, 16, 15, 34, 3)) == "2026-07-16 08:34:03 PDT"


def test_dashboard_falls_back_to_chicago_for_invalid_timezone(monkeypatch) -> None:
    monkeypatch.setattr(dashboard, "resolve_display_timezone", lambda *_args, **_kwargs: "Not/A_Real_Zone")

    assert dashboard._fmt_dt(datetime(2026, 7, 16, 15, 34, 3)) == "2026-07-16 10:34:03 CDT"


def test_local_migration_conversion_handles_cst_and_cdt() -> None:
    assert local_naive_to_utc_naive(datetime(2026, 1, 15, 12, 0, 0)) == datetime(
        2026, 1, 15, 18, 0, 0
    )
    assert local_naive_to_utc_naive(datetime(2026, 7, 15, 12, 0, 0)) == datetime(
        2026, 7, 15, 17, 0, 0
    )
    assert local_naive_to_utc_naive(None) is None


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 3, 8, 2, 30, 0), datetime(2026, 11, 1, 1, 30, 0)],
)
def test_local_migration_rejects_nonexistent_or_ambiguous_wall_times(value: datetime) -> None:
    with pytest.raises(ValueError):
        local_naive_to_utc_naive(value)


def test_init_db_adds_change_details_to_existing_job_changes_table(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with test_engine.begin() as conn:
        conn.execute(text("CREATE TABLE job_changes (id INTEGER PRIMARY KEY, changed_fields TEXT)"))

    monkeypatch.setattr(models, "engine", test_engine)

    models.init_db()

    columns = {col["name"] for col in sa_inspect(test_engine).get_columns("job_changes")}
    assert "change_details" in columns


def test_recent_hours_filter_compares_naive_utc(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine)
    monkeypatch.setattr(dashboard, "engine", test_engine)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", True)
    monkeypatch.setattr(dashboard, "utc_now_naive", lambda: datetime(2026, 7, 16, 12, 0, 0))

    with Session() as session:
        run = IntegrationRun(
            started_at=datetime(2026, 7, 16, 10, 0, 0),
            finished_at=datetime(2026, 7, 16, 10, 5, 0),
            user="tester",
            mode="test",
        )
        session.add(run)
        session.flush()
        for job_id, discovered in (
            ("recent", datetime(2026, 7, 16, 11, 30, 0)),
            ("old", datetime(2026, 7, 16, 10, 30, 0)),
        ):
            session.add(
                Job(
                    job_id=job_id,
                    site="example",
                    title=job_id,
                    discovery_date=discovered,
                    run_id=run.id,
                )
            )
        session.commit()

    result = dashboard.fetch_jobs_last_hours(hours=1)

    assert result["returned"] == 1
    assert result["jobs"][0]["job_id"] == "recent"
    assert result["jobs"][0]["age_hours"] == 0.5
    assert result["cutoff_iso"] == "2026-07-16T11:00:00Z"
