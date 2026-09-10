from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from dotenv import load_dotenv
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv(".env")

from app.models import IntegrationRun, JobChange, SessionLocal
from app.db import utc_now_naive


COMPENSATION_FIELDS = {
    "pay",
    "ai_salary",
    "base_pay_low",
    "base_pay_high",
    "pay_currency",
    "pay_period",
    "ote_low",
    "ote_high",
    "bonus_offered",
    "equity_offered",
    "commission_offered",
    "multiple_pay_ranges",
    "compensation_text",
    "compensation_notes",
    "compensation_source",
    "compensation_analyzed_at",
    "compensation_schema_version",
}

EMPTY_LABELS = {"", "Unknown", "unknown", "No compensation", "no compensation"}
BACKUP_TABLE = "data_repair_missing_ai_compensation_backup"


def meaningful(value: Any) -> bool:
    return value is not None and str(value).strip() not in EMPTY_LABELS


def zero_range(value: Any) -> bool:
    numbers = [
        float(part.replace(",", ""))
        for part in re.findall(r"\d+(?:,\d{3})*(?:\.\d+)?", str(value or ""))
    ]
    return bool(numbers) and all(number == 0 for number in numbers)


def salary_like(value: Any) -> bool:
    if not meaningful(value) or zero_range(value):
        return False
    text_value = str(value).strip()
    lower_value = text_value.lower()
    if (
        "$" in text_value
        or "usd" in lower_value
        or "/hr" in lower_value
        or "hour" in lower_value
        or "salary" in lower_value
    ):
        return True
    numbers = [
        int(part.replace(",", ""))
        for part in re.findall(r"\b\d{2,3}(?:,\d{3})+\b|\b\d{5,6}\b", text_value)
    ]
    return any(15_000 <= number <= 1_000_000 for number in numbers)


def parse_changed_fields(value: Any) -> tuple[set[str], Optional[dict]]:
    if not value:
        return set(), None
    text_value = str(value)
    if text_value.startswith("{"):
        try:
            payload = json.loads(text_value)
        except json.JSONDecodeError:
            return set(), None
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if isinstance(fields, list):
            return {str(field) for field in fields}, payload
        return set(), payload if isinstance(payload, dict) else None
    return {part.strip() for part in text_value.split(",") if part.strip()}, None


def load_change_rows(session) -> List[Dict[str, Any]]:
    return [
        dict(row)
        for row in session.execute(
            text(
                """
                SELECT
                    id,
                    job_pk,
                    site,
                    job_id_text,
                    change_type,
                    change_source,
                    created_at,
                    changed_fields
                FROM job_changes
                WHERE job_pk IS NOT NULL
                ORDER BY job_pk, created_at, id
                """
            )
        ).mappings()
    ]


def load_jobs(session) -> Dict[int, Dict[str, Any]]:
    return {
        int(row["id"]): dict(row)
        for row in session.execute(
            text(
                """
                SELECT
                    id,
                    site,
                    job_id,
                    title,
                    is_active,
                    run_id,
                    content_hash,
                    pay,
                    ai_salary
                FROM jobs
                """
            )
        ).mappings()
    }


def find_recoverable_rows(
    changes: Iterable[Dict[str, Any]], jobs: Dict[int, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    changes_by_job: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for row in changes:
        changes_by_job[int(row["job_pk"])].append(row)

    recoverable: Dict[int, Dict[str, Any]] = {}
    for job_pk, events in changes_by_job.items():
        for index, missing_event in enumerate(events):
            if missing_event["change_type"] != "missing":
                continue

            window_end = len(events)
            for later_index in range(index + 1, len(events)):
                later_event = events[later_index]
                if later_event["change_source"] == "site" and later_event[
                    "change_type"
                ] in {"insert", "update"}:
                    window_end = later_index
                    break

            for ai_event in events[index + 1 : window_end]:
                if not (
                    ai_event["change_source"] == "ai"
                    and ai_event["change_type"] == "update"
                ):
                    continue
                fields, payload = parse_changed_fields(ai_event["changed_fields"])
                if not (fields & COMPENSATION_FIELDS) or not isinstance(payload, dict):
                    continue

                before = payload.get("before") or {}
                after = payload.get("after") or {}
                job = jobs.get(job_pk) or {}
                restore_pay = (not salary_like(job.get("pay"))) and salary_like(
                    before.get("pay")
                )
                restore_ai_salary = (not meaningful(job.get("ai_salary"))) and meaningful(
                    before.get("ai_salary")
                )
                after_lost = (not meaningful(after.get("ai_salary"))) and (
                    not salary_like(after.get("pay"))
                )
                if not after_lost or not (restore_pay or restore_ai_salary):
                    continue

                candidate = {
                    "job_pk": job_pk,
                    "site": missing_event["site"],
                    "job_id": missing_event["job_id_text"],
                    "title": job.get("title"),
                    "is_active": bool(job.get("is_active")),
                    "run_id": job.get("run_id"),
                    "content_hash": job.get("content_hash"),
                    "source_change_id": ai_event["id"],
                    "missing_at": missing_event["created_at"],
                    "ai_overwrite_at": ai_event["created_at"],
                    "current_pay": job.get("pay"),
                    "current_ai_salary": job.get("ai_salary"),
                    "restore_pay": before.get("pay") if restore_pay else None,
                    "restore_ai_salary": before.get("ai_salary")
                    if restore_ai_salary
                    else None,
                }
                existing = recoverable.get(job_pk)
                if existing is None or ai_event["created_at"] > existing[
                    "ai_overwrite_at"
                ]:
                    recoverable[job_pk] = candidate

    return sorted(recoverable.values(), key=lambda row: (row["site"], row["job_id"]))


def ensure_backup_table(session) -> None:
    session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {BACKUP_TABLE} (
                repair_run_id VARCHAR(64) NOT NULL,
                job_pk INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                site VARCHAR(255),
                job_id VARCHAR(255),
                title TEXT,
                source_change_id INTEGER,
                missing_at DATETIME,
                ai_overwrite_at DATETIME,
                old_pay VARCHAR(255),
                old_ai_salary VARCHAR(255),
                restored_pay VARCHAR(255),
                restored_ai_salary VARCHAR(255),
                was_active BOOLEAN,
                PRIMARY KEY (repair_run_id, job_pk)
            )
            """
        )
    )


def apply_repairs(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    repair_run_id = utc_now_naive().strftime("repair_missing_ai_comp_%Y%m%d%H%M%S")
    with SessionLocal() as session:
        ensure_backup_table(session)
        run = IntegrationRun(
            user="repair_missing_ai_compensation_overwrites.py",
            mode="data_repair",
            notes=(
                "Restored pay/ai_salary values overwritten by AI processing after "
                "jobs were marked missing."
            ),
        )
        session.add(run)
        session.flush()

        repaired = 0
        for row in rows:
            current = session.execute(
                text(
                    """
                    SELECT id, pay, ai_salary, run_id, content_hash
                    FROM jobs
                    WHERE id = :job_pk
                    """
                ),
                {"job_pk": row["job_pk"]},
            ).mappings().one_or_none()
            if current is None:
                continue

            set_parts = []
            params: Dict[str, Any] = {"job_pk": row["job_pk"]}
            after: Dict[str, Any] = {}
            changed_fields: List[str] = []

            if row["restore_pay"] is not None and not salary_like(current["pay"]):
                set_parts.append("pay = :pay")
                params["pay"] = row["restore_pay"]
                after["pay"] = row["restore_pay"]
                changed_fields.append("pay")
            else:
                after["pay"] = current["pay"]

            if row["restore_ai_salary"] is not None and not meaningful(
                current["ai_salary"]
            ):
                set_parts.append("ai_salary = :ai_salary")
                params["ai_salary"] = row["restore_ai_salary"]
                after["ai_salary"] = row["restore_ai_salary"]
                changed_fields.append("ai_salary")
            else:
                after["ai_salary"] = current["ai_salary"]

            if not set_parts:
                continue

            session.execute(
                text(
                    f"""
                    INSERT INTO {BACKUP_TABLE} (
                        repair_run_id,
                        job_pk,
                        created_at,
                        site,
                        job_id,
                        title,
                        source_change_id,
                        missing_at,
                        ai_overwrite_at,
                        old_pay,
                        old_ai_salary,
                        restored_pay,
                        restored_ai_salary,
                        was_active
                    )
                    VALUES (
                        :repair_run_id,
                        :job_pk,
                        :created_at,
                        :site,
                        :job_id,
                        :title,
                        :source_change_id,
                        :missing_at,
                        :ai_overwrite_at,
                        :old_pay,
                        :old_ai_salary,
                        :restored_pay,
                        :restored_ai_salary,
                        :was_active
                    )
                    """
                ),
                {
                    "repair_run_id": repair_run_id,
                    "job_pk": row["job_pk"],
                    "created_at": utc_now_naive(),
                    "site": row["site"],
                    "job_id": row["job_id"],
                    "title": row["title"],
                    "source_change_id": row["source_change_id"],
                    "missing_at": row["missing_at"],
                    "ai_overwrite_at": row["ai_overwrite_at"],
                    "old_pay": current["pay"],
                    "old_ai_salary": current["ai_salary"],
                    "restored_pay": row["restore_pay"],
                    "restored_ai_salary": row["restore_ai_salary"],
                    "was_active": row["is_active"],
                },
            )

            session.execute(
                text(
                    f"""
                    UPDATE jobs
                    SET {", ".join(set_parts)}
                    WHERE id = :job_pk
                    """
                ),
                params,
            )
            session.add(
                JobChange(
                    run_id=run.id,
                    job_pk=row["job_pk"],
                    job_id_text=row["job_id"],
                    site=row["site"],
                    change_type="update",
                    change_source="repair",
                    old_hash=current["content_hash"],
                    new_hash=current["content_hash"],
                    changed_fields=json.dumps(
                        {
                            "repair": "missing_ai_compensation_overwrite",
                            "source_change_id": row["source_change_id"],
                            "fields": changed_fields,
                            "before": {
                                "pay": current["pay"],
                                "ai_salary": current["ai_salary"],
                            },
                            "after": after,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )
            repaired += 1

        run.finished_at = utc_now_naive()
        run.total_seen = len(rows)
        run.updated_count = repaired
        session.commit()
        print(f"repair_run_id={repair_run_id}")
        return repaired


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Restore salary fields overwritten by AI after missing-job events."
    )
    parser.add_argument("--apply", action="store_true", help="Write the repair to the DB.")
    args = parser.parse_args()

    with SessionLocal() as session:
        rows = find_recoverable_rows(load_change_rows(session), load_jobs(session))

    print(f"recoverable_rows={len(rows)} apply={args.apply}")
    for row in rows:
        print(json.dumps(row, default=str, ensure_ascii=False))

    if args.apply:
        repaired = apply_repairs(rows)
        print(f"repaired_rows={repaired}")


if __name__ == "__main__":
    main()
