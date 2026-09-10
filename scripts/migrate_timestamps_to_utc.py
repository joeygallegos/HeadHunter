from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import utc_now_naive  # noqa: E402
from app.models import DATABASE_URL, engine  # noqa: E402


MIGRATION_ID = "20260717_normalize_timestamps_utc"
STATE_TABLE = "jobscrape_timestamp_migrations"
BACKUP_PREFIX = "jobscrape_tz_20260717_"
LOCK_NAME = "jobscrape_timestamp_utc_migration"
LOCAL_ZONE = ZoneInfo("America/Chicago")

# discovery_date is intentionally present only in the jobs backup. It is used
# to prove that the already-UTC value was not changed by this migration.
TABLE_COLUMNS = {
    "integration_runs": ("started_at", "finished_at"),
    "jobs": ("ai_analyzed_at", "compensation_analyzed_at", "updated_at"),
    "job_changes": ("created_at",),
    "job_swipes": ("created_at",),
    "dashboard_query_reports": ("created_at", "updated_at"),
}
BACKUP_EXTRA_COLUMNS = {"jobs": ("discovery_date",)}


def local_naive_to_utc_naive(value: datetime | None) -> datetime | None:
    """Convert an unambiguous America/Chicago wall time to naive UTC."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    first = value.replace(tzinfo=LOCAL_ZONE, fold=0)
    second = value.replace(tzinfo=LOCAL_ZONE, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise ValueError(f"ambiguous America/Chicago wall time: {value}")

    converted = first.astimezone(timezone.utc)
    round_trip = converted.astimezone(LOCAL_ZONE).replace(tzinfo=None)
    if round_trip != value:
        raise ValueError(f"nonexistent America/Chicago wall time: {value}")
    return converted.replace(tzinfo=None)


def _available_columns() -> dict[str, tuple[str, ...]]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    available: dict[str, tuple[str, ...]] = {}
    for table_name, configured in TABLE_COLUMNS.items():
        if table_name not in tables:
            print(f"skip missing optional table: {table_name}")
            continue
        existing = {column["name"] for column in inspector.get_columns(table_name)}
        columns = tuple(column for column in configured if column in existing)
        if columns:
            available[table_name] = columns
    return available


def _backup_table(table_name: str) -> str:
    return f"{BACKUP_PREFIX}{table_name}"


def _print_preflight(table_columns: dict[str, tuple[str, ...]]) -> None:
    print(
        "Migration is dry-run by default. Stop the scraper, AI scheduler, "
        "and dashboard before --apply."
    )
    with engine.connect() as conn:
        for table_name, columns in table_columns.items():
            row_count = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar_one()
            print(f"{table_name}: rows={row_count}")
            for column in columns:
                values = conn.execute(
                    text(
                        f"SELECT COUNT(`{column}`), MIN(`{column}`), MAX(`{column}`) "
                        f"FROM `{table_name}`"
                    )
                ).one()
                print(f"  {column}: non_null={values[0]} min={values[1]} max={values[2]}")


def _ensure_state_and_backups(table_columns: dict[str, tuple[str, ...]]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS `{STATE_TABLE}` (
                    migration_id VARCHAR(128) PRIMARY KEY,
                    status VARCHAR(32) NOT NULL,
                    applied_at_utc DATETIME NULL,
                    restored_at_utc DATETIME NULL
                )
                """
            )
        )
        state = conn.execute(
            text(f"SELECT status FROM `{STATE_TABLE}` WHERE migration_id=:migration_id"),
            {"migration_id": MIGRATION_ID},
        ).scalar_one_or_none()
        if state == "applied":
            raise SystemExit("Migration is already applied; refusing to shift timestamps twice.")

        existing_tables = set(inspect(conn).get_table_names())
        for table_name, columns in table_columns.items():
            backup = _backup_table(table_name)
            if backup in existing_tables:
                continue
            selected = ("id", *columns, *BACKUP_EXTRA_COLUMNS.get(table_name, ()))
            selected_sql = ", ".join(f"`{column}`" for column in selected)
            conn.execute(
                text(
                    f"CREATE TABLE `{backup}` AS "
                    f"SELECT {selected_sql} FROM `{table_name}`"
                )
            )

        if state is None:
            conn.execute(
                text(
                    f"INSERT INTO `{STATE_TABLE}` (migration_id, status) "
                    "VALUES (:migration_id, 'backed_up')"
                ),
                {"migration_id": MIGRATION_ID},
            )
        else:
            conn.execute(
                text(
                    f"UPDATE `{STATE_TABLE}` SET status='backed_up', "
                    "applied_at_utc=NULL, restored_at_utc=NULL WHERE migration_id=:migration_id"
                ),
                {"migration_id": MIGRATION_ID},
            )


def _converted_rows(conn, table_name: str, columns: Iterable[str]) -> list[dict]:
    backup = _backup_table(table_name)
    column_list = tuple(columns)
    selected_sql = ", ".join(["`id`", *(f"`{column}`" for column in column_list)])
    rows = conn.execute(text(f"SELECT {selected_sql} FROM `{backup}` ORDER BY `id`")).mappings()
    return [
        {
            "id": row["id"],
            **{column: local_naive_to_utc_naive(row[column]) for column in column_list},
        }
        for row in rows
    ]


def _update_from_payload(
    conn, table_name: str, columns: tuple[str, ...], payload: list[dict]
) -> None:
    if not payload:
        return
    # PyMySQL can batch INSERT values efficiently. A temporary staging table
    # then reduces each live-table conversion to one joined UPDATE instead of
    # thousands of remote UPDATE round trips.
    stage = f"jobscrape_tz_stage_{table_name}"
    datetime_columns = ", ".join(f"`{column}` DATETIME NULL" for column in columns)
    conn.execute(
        text(
            f"CREATE TEMPORARY TABLE `{stage}` ("
            f"`id` INT PRIMARY KEY, {datetime_columns})"
        )
    )
    names = ("id", *columns)
    insert_columns = ", ".join(f"`{name}`" for name in names)
    insert_values = ", ".join(f":{name}" for name in names)
    conn.execute(
        text(f"INSERT INTO `{stage}` ({insert_columns}) VALUES ({insert_values})"),
        payload,
    )
    assignments = ", ".join(f"live.`{column}`=stage.`{column}`" for column in columns)
    conn.execute(
        text(
            f"UPDATE `{table_name}` live JOIN `{stage}` stage ON stage.id=live.id "
            f"SET {assignments}"
        )
    )
    conn.execute(text(f"DROP TEMPORARY TABLE `{stage}`"))


def _validate(table_columns: dict[str, tuple[str, ...]]) -> None:
    with engine.connect() as conn:
        for table_name, columns in table_columns.items():
            backup = _backup_table(table_name)
            live_count = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`")).scalar_one()
            backup_count = conn.execute(text(f"SELECT COUNT(*) FROM `{backup}`")).scalar_one()
            if live_count != backup_count:
                raise RuntimeError(
                    f"row-count mismatch for {table_name}: {live_count} != {backup_count}"
                )
            for column in columns:
                live_nulls = conn.execute(
                    text(f"SELECT COUNT(*) FROM `{table_name}` WHERE `{column}` IS NULL")
                ).scalar_one()
                backup_nulls = conn.execute(
                    text(f"SELECT COUNT(*) FROM `{backup}` WHERE `{column}` IS NULL")
                ).scalar_one()
                if live_nulls != backup_nulls:
                    raise RuntimeError(f"null-count mismatch for {table_name}.{column}")

        jobs_backup = _backup_table("jobs")
        changed_discovery = conn.execute(
            text(
                f"SELECT COUNT(*) FROM jobs j JOIN `{jobs_backup}` b ON b.id=j.id "
                "WHERE NOT (j.discovery_date <=> b.discovery_date)"
            )
        ).scalar_one()
        analyzed_before_discovery = conn.execute(
            text(
                "SELECT COUNT(*) FROM jobs WHERE ai_analyzed_at IS NOT NULL "
                "AND ai_analyzed_at < discovery_date"
            )
        ).scalar_one()
        minimum_gap = conn.execute(
            text(
                "SELECT MIN(TIMESTAMPDIFF(SECOND, discovery_date, ai_analyzed_at)) "
                "FROM jobs WHERE ai_analyzed_at IS NOT NULL"
            )
        ).scalar_one()
        print(
            "validation: "
            f"discovery_dates_changed={changed_discovery} "
            f"analyzed_before_discovery={analyzed_before_discovery} "
            f"minimum_analysis_gap_seconds={minimum_gap}"
        )
        if changed_discovery or analyzed_before_discovery:
            raise RuntimeError("post-migration timestamp validation failed")


def apply_migration(table_columns: dict[str, tuple[str, ...]]) -> None:
    _ensure_state_and_backups(table_columns)
    with engine.begin() as conn:
        for table_name, columns in table_columns.items():
            payload = _converted_rows(conn, table_name, columns)
            _update_from_payload(conn, table_name, columns, payload)
            print(f"converted {table_name}: rows={len(payload)}", flush=True)
        conn.execute(
            text(
                f"UPDATE `{STATE_TABLE}` SET status='applied', applied_at_utc=:now, "
                "restored_at_utc=NULL WHERE migration_id=:migration_id"
            ),
            {"migration_id": MIGRATION_ID, "now": utc_now_naive()},
        )
    _validate(table_columns)


def restore_migration(table_columns: dict[str, tuple[str, ...]]) -> None:
    existing_tables = set(inspect(engine).get_table_names())
    missing = [
        _backup_table(table)
        for table in table_columns
        if _backup_table(table) not in existing_tables
    ]
    if missing:
        raise SystemExit(f"Cannot restore; missing backup tables: {', '.join(missing)}")

    with engine.begin() as conn:
        for table_name, columns in table_columns.items():
            backup = _backup_table(table_name)
            assignments = ", ".join(f"live.`{column}`=backup.`{column}`" for column in columns)
            conn.execute(
                text(
                    f"UPDATE `{table_name}` live JOIN `{backup}` backup ON backup.id=live.id "
                    f"SET {assignments}"
                )
            )
            print(f"restored {table_name}")
        conn.execute(
            text(
                f"UPDATE `{STATE_TABLE}` SET status='restored', restored_at_utc=:now "
                "WHERE migration_id=:migration_id"
            ),
            {"migration_id": MIGRATION_ID, "now": utc_now_naive()},
        )


def _with_lock(action) -> None:
    with engine.connect() as conn:
        acquired = conn.execute(
            text("SELECT GET_LOCK(:name, 10)"), {"name": LOCK_NAME}
        ).scalar_one()
        if acquired != 1:
            raise SystemExit("Could not acquire the timestamp migration lock.")
        try:
            action()
        finally:
            conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": LOCK_NAME})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Normalize JobScrape DATETIME values to UTC.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Back up and convert timestamps.")
    mode.add_argument(
        "--restore", action="store_true", help="Restore original timestamps from backups."
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not DATABASE_URL.startswith("mysql"):
        raise SystemExit("This data migration is MySQL-only.")
    table_columns = _available_columns()
    if "jobs" not in table_columns:
        raise SystemExit("Configured database does not contain the JobScrape jobs table.")
    _print_preflight(table_columns)
    if args.apply:
        _with_lock(lambda: apply_migration(table_columns))
    elif args.restore:
        _with_lock(lambda: restore_migration(table_columns))
    else:
        print(
            "Dry run complete; no database values changed. "
            "Re-run with --apply after stopping writers."
        )


if __name__ == "__main__":
    main()
