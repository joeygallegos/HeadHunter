from __future__ import annotations

import argparse
import base64
import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, List, Optional, Sequence, Tuple

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.models import IntegrationRun, Job, SessionLocal


DEFAULT_TOP_N = 25
DEFAULT_MAILGUN_BASE_URL = "https://api.mailgun.net/v3"


@dataclass(frozen=True)
class MailgunConfig:
    api_key: str
    domain: str
    from_email: str
    to_email: str
    base_url: str = DEFAULT_MAILGUN_BASE_URL


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Email a Mailgun digest of newly discovered jobs from the latest completed run."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the email preview without sending it.",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        help="Send a digest for a specific completed integration run.",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=DEFAULT_TOP_N,
        help=f"Number of ranked jobs to include. Default: {DEFAULT_TOP_N}.",
    )
    parser.add_argument(
        "--include-test-runs",
        action="store_true",
        help="Allow test-mode runs. By default only mode='steps' runs are used.",
    )
    return parser.parse_args(argv)


def _fmt_dt(dt: Any) -> str:
    if not dt:
        return "Unknown"
    if isinstance(dt, datetime):
        return dt.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    return str(dt)


def _clean_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _safe_json_list(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw = value
    else:
        try:
            raw = json.loads(str(value))
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def score_label(job: Job) -> str:
    score = getattr(job, "ai_match_percentage", None)
    return f"{score}%" if score is not None else "Not analyzed"


def salary_label(job: Job) -> str:
    return _clean_text(getattr(job, "ai_salary", None)) or _clean_text(
        getattr(job, "pay", None), "Not listed"
    )


def fit_summary(job: Job) -> str:
    return _clean_text(
        getattr(job, "ai_fit_summary", None), "No AI summary available yet."
    )


def get_completed_run(
    session: Session, run_id: Optional[int] = None, include_test_runs: bool = False
) -> Optional[IntegrationRun]:
    stmt = select(IntegrationRun).where(IntegrationRun.finished_at.is_not(None))
    if not include_test_runs:
        stmt = stmt.where(IntegrationRun.mode == "steps")
    if run_id is not None:
        stmt = stmt.where(IntegrationRun.id == run_id)
    else:
        stmt = stmt.order_by(desc(IntegrationRun.finished_at), desc(IntegrationRun.id))
    return session.execute(stmt.limit(1)).scalar_one_or_none()


def get_newly_discovered_jobs(session: Session, run_id: int) -> List[Job]:
    stmt = (
        select(Job)
        .where(Job.first_seen_run_id == run_id)
        .order_by(
            Job.ai_match_percentage.is_(None),
            desc(Job.ai_match_percentage),
            desc(Job.ai_analyzed_at),
            desc(Job.discovery_date),
            desc(Job.id),
        )
    )
    return list(session.execute(stmt).scalars())


def digest_counts(jobs: Sequence[Job]) -> Tuple[int, int, Optional[int]]:
    analyzed = sum(1 for job in jobs if getattr(job, "ai_match_percentage", None) is not None)
    unanalyzed = len(jobs) - analyzed
    scores = [
        int(job.ai_match_percentage)
        for job in jobs
        if getattr(job, "ai_match_percentage", None) is not None
    ]
    return analyzed, unanalyzed, max(scores) if scores else None


def build_subject(run: IntegrationRun, total_new: int, top_n: int) -> str:
    return f"Job digest: {total_new} new jobs from run {run.id} - top {top_n}"


def render_text_digest(run: IntegrationRun, jobs: Sequence[Job], top_n: int) -> str:
    top_jobs = list(jobs[:top_n])
    analyzed, unanalyzed, highest_score = digest_counts(jobs)
    highest_label = f"{highest_score}%" if highest_score is not None else "Not analyzed"

    lines = [
        "Job Digest",
        "",
        f"Run: {run.id}",
        f"Finished: {_fmt_dt(run.finished_at)}",
        f"Mode: {_clean_text(run.mode, 'Unknown')}",
        "",
        "Run summary",
        f"- Total seen: {run.total_seen}",
        f"- Newly discovered: {len(jobs)}",
        f"- Updated: {run.updated_count}",
        f"- Missing: {run.missing_count}",
        f"- Errors: {run.error_count}",
        "",
        "Digest summary",
        f"- Analyzed jobs: {analyzed}",
        f"- Unanalyzed jobs: {unanalyzed}",
        f"- Highest AI score: {highest_label}",
        "",
        f"Top {len(top_jobs)} jobs to apply for",
    ]

    if not top_jobs:
        lines.extend(["", "No newly discovered jobs were found for this run."])
        return "\n".join(lines)

    for index, job in enumerate(top_jobs, start=1):
        overlap = _safe_json_list(getattr(job, "ai_keywords_overlap", None))
        missing = _safe_json_list(getattr(job, "ai_missing_keywords", None))
        experience = _clean_text(getattr(job, "ai_experience_match", None), "Unknown")
        location = _clean_text(getattr(job, "ai_location_policy_match", None), "Unknown")
        level = _clean_text(getattr(job, "level", None), "Unknown")
        url = _clean_text(getattr(job, "url", None), "No URL listed")

        lines.extend(
            [
                "",
                f"{index}. {_clean_text(job.title, 'Untitled job')} ({_clean_text(job.site, 'Unknown site')})",
                f"   Score: {score_label(job)}",
                f"   Salary/pay: {salary_label(job)}",
                f"   Level: {level}",
                f"   AI fit: {fit_summary(job)}",
                f"   Experience: {experience}; Location: {location}",
            ]
        )
        if overlap:
            lines.append(f"   Keyword overlap: {', '.join(overlap)}")
        if missing:
            lines.append(f"   Missing keywords: {', '.join(missing)}")
        lines.append(f"   URL: {url}")

    return "\n".join(lines)


def _e(value: Any) -> str:
    return html.escape(_clean_text(value), quote=True)


def _pill(label: str, value: str) -> str:
    return (
        "<span style=\"display:inline-block;margin:0 6px 6px 0;padding:5px 9px;"
        "border-radius:999px;background:#eef2ff;color:#1f2937;font-size:12px;"
        "line-height:1.2;border:1px solid #dbe4ff\">"
        f"<strong>{_e(label)}:</strong> {_e(value)}</span>"
    )


def _keyword_row(label: str, values: Sequence[str], bg: str) -> str:
    if not values:
        return ""
    chips = "".join(
        "<span style=\"display:inline-block;margin:0 5px 5px 0;padding:4px 8px;"
        f"border-radius:6px;background:{bg};font-size:12px;color:#374151\">"
        f"{_e(value)}</span>"
        for value in values
    )
    return (
        "<div style=\"margin-top:10px\">"
        f"<div style=\"margin-bottom:5px;font-size:12px;font-weight:700;color:#4b5563\">{_e(label)}</div>"
        f"{chips}</div>"
    )


def render_html_digest(run: IntegrationRun, jobs: Sequence[Job], top_n: int) -> str:
    top_jobs = list(jobs[:top_n])
    analyzed, unanalyzed, highest_score = digest_counts(jobs)
    highest_label = f"{highest_score}%" if highest_score is not None else "Not analyzed"
    score_badge = (
        "<span style=\"display:inline-block;padding:6px 10px;border-radius:999px;"
        "background:#dcfce7;color:#166534;font-weight:700;font-size:13px\">"
        f"{_e(highest_label)}</span>"
    )

    summary_items = [
        ("New", len(jobs)),
        ("Analyzed", analyzed),
        ("Unanalyzed", unanalyzed),
        ("Errors", getattr(run, "error_count", 0)),
    ]
    summary_html = "".join(
        "<td style=\"width:25%;padding:10px 6px;text-align:center\">"
        "<div style=\"font-size:20px;font-weight:800;color:#111827;line-height:1.1\">"
        f"{_e(value)}</div>"
        "<div style=\"margin-top:4px;font-size:11px;text-transform:uppercase;"
        "letter-spacing:.04em;color:#6b7280;font-weight:700\">"
        f"{_e(label)}</div></td>"
        for label, value in summary_items
    )

    if not top_jobs:
        jobs_html = (
            "<div style=\"padding:18px;border:1px solid #e5e7eb;border-radius:10px;"
            "background:#ffffff;color:#374151\">No newly discovered jobs were found for this run.</div>"
        )
    else:
        cards = []
        for index, job in enumerate(top_jobs, start=1):
            overlap = _safe_json_list(getattr(job, "ai_keywords_overlap", None))
            missing = _safe_json_list(getattr(job, "ai_missing_keywords", None))
            url = _clean_text(getattr(job, "url", None))
            score = score_label(job)
            score_bg = "#dcfce7" if getattr(job, "ai_match_percentage", None) is not None else "#f3f4f6"
            score_color = "#166534" if getattr(job, "ai_match_percentage", None) is not None else "#4b5563"
            title = _clean_text(getattr(job, "title", None), "Untitled job")
            site = _clean_text(getattr(job, "site", None), "Unknown site")
            link_html = (
                "<a href=\"{url}\" style=\"display:inline-block;margin-top:12px;"
                "padding:10px 13px;border-radius:8px;background:#2563eb;color:#ffffff;"
                "font-size:14px;font-weight:700;text-decoration:none\">Open job</a>"
            ).format(url=_e(url)) if url else ""

            cards.append(
                "<div style=\"margin:0 0 14px 0;padding:16px;border:1px solid #e5e7eb;"
                "border-radius:10px;background:#ffffff\">"
                "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse\">"
                "<tr><td style=\"vertical-align:top;padding-right:10px\">"
                f"<div style=\"font-size:12px;font-weight:800;color:#6b7280\">#{index} · {_e(site)}</div>"
                f"<h2 style=\"margin:4px 0 8px 0;font-size:18px;line-height:1.25;color:#111827\">{_e(title)}</h2>"
                "</td><td style=\"width:1%;vertical-align:top;text-align:right;white-space:nowrap\">"
                f"<span style=\"display:inline-block;padding:7px 10px;border-radius:999px;background:{score_bg};"
                f"color:{score_color};font-size:13px;font-weight:800\">{_e(score)}</span>"
                "</td></tr></table>"
                f"<p style=\"margin:8px 0 12px 0;font-size:14px;line-height:1.5;color:#374151\">{_e(fit_summary(job))}</p>"
                "<div>"
                f"{_pill('Salary', salary_label(job))}"
                f"{_pill('Level', _clean_text(getattr(job, 'level', None), 'Unknown'))}"
                f"{_pill('Experience', _clean_text(getattr(job, 'ai_experience_match', None), 'Unknown'))}"
                f"{_pill('Location', _clean_text(getattr(job, 'ai_location_policy_match', None), 'Unknown'))}"
                "</div>"
                f"{_keyword_row('Keyword overlap', overlap, '#ecfdf5')}"
                f"{_keyword_row('Missing keywords', missing, '#fff7ed')}"
                f"{link_html}"
                "</div>"
            )
        jobs_html = "".join(cards)

    return (
        "<!doctype html><html><head><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<meta http-equiv=\"Content-Type\" content=\"text/html; charset=utf-8\"></head>"
        "<body style=\"margin:0;padding:0;background:#f3f4f6;color:#111827;"
        "font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%\">"
        "<div style=\"display:none;max-height:0;overflow:hidden;color:#f3f4f6\">"
        f"{_e(len(jobs))} new jobs, {highest_label} highest AI score.</div>"
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse;background:#f3f4f6\">"
        "<tr><td align=\"center\" style=\"padding:14px 8px\">"
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" "
        "style=\"max-width:680px;border-collapse:collapse\">"
        "<tr><td style=\"padding:22px 18px;border-radius:12px 12px 0 0;background:#111827;color:#ffffff\">"
        "<div style=\"font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#c7d2fe;font-weight:800\">Job Digest</div>"
        f"<h1 style=\"margin:6px 0 8px 0;font-size:24px;line-height:1.2;color:#ffffff\">Top {len(top_jobs)} jobs to apply for</h1>"
        f"<div style=\"font-size:14px;line-height:1.45;color:#d1d5db\">Run {_e(run.id)} finished {_e(_fmt_dt(run.finished_at))} · Highest score {score_badge}</div>"
        "</td></tr>"
        "<tr><td style=\"background:#ffffff;border-left:1px solid #e5e7eb;border-right:1px solid #e5e7eb\">"
        "<table role=\"presentation\" width=\"100%\" cellspacing=\"0\" cellpadding=\"0\" style=\"border-collapse:collapse\">"
        f"<tr>{summary_html}</tr></table>"
        "</td></tr>"
        "<tr><td style=\"padding:16px 0 0 0\">"
        f"{jobs_html}"
        "</td></tr>"
        "<tr><td style=\"padding:14px 6px 24px 6px;font-size:12px;line-height:1.4;color:#6b7280;text-align:center\">"
        f"Run summary: total seen {_e(getattr(run, 'total_seen', 0))}, updated {_e(getattr(run, 'updated_count', 0))}, missing {_e(getattr(run, 'missing_count', 0))}."
        "</td></tr>"
        "</table></td></tr></table></body></html>"
    )


def load_mailgun_config(environ: Optional[dict[str, str]] = None) -> MailgunConfig:
    env = environ if environ is not None else os.environ
    required = {
        "MAILGUN_API_KEY": env.get("MAILGUN_API_KEY"),
        "MAILGUN_DOMAIN": env.get("MAILGUN_DOMAIN"),
        "MAILGUN_FROM_EMAIL": env.get("MAILGUN_FROM_EMAIL"),
        "JOB_DIGEST_TO_EMAIL": env.get("JOB_DIGEST_TO_EMAIL"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Missing required Mailgun environment variables: " + ", ".join(missing)
        )
    return MailgunConfig(
        api_key=required["MAILGUN_API_KEY"] or "",
        domain=required["MAILGUN_DOMAIN"] or "",
        from_email=required["MAILGUN_FROM_EMAIL"] or "",
        to_email=required["JOB_DIGEST_TO_EMAIL"] or "",
        base_url=(env.get("MAILGUN_BASE_URL") or DEFAULT_MAILGUN_BASE_URL).rstrip("/"),
    )


def build_mailgun_request(
    config: MailgunConfig, subject: str, text_body: str, html_body: str
) -> urllib.request.Request:
    url = f"{config.base_url}/{config.domain}/messages"
    payload = urllib.parse.urlencode(
        {
            "from": config.from_email,
            "to": config.to_email,
            "subject": subject,
            "text": text_body,
            "html": html_body,
        }
    ).encode("utf-8")
    token = base64.b64encode(f"api:{config.api_key}".encode("utf-8")).decode("ascii")
    return urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )


def send_mailgun_digest(
    config: MailgunConfig,
    subject: str,
    text_body: str,
    html_body: str,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    request = build_mailgun_request(config, subject, text_body, html_body)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            return body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Mailgun request failed with HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Mailgun request failed: {exc.reason}") from exc


def build_digest(
    session: Session,
    run_id: Optional[int],
    include_test_runs: bool,
    top_n: int,
) -> Tuple[IntegrationRun, List[Job], str, str, str]:
    if top_n <= 0:
        raise RuntimeError("--top-n must be greater than 0")

    run = get_completed_run(
        session, run_id=run_id, include_test_runs=include_test_runs
    )
    if run is None:
        qualifier = f"run {run_id}" if run_id is not None else "a completed run"
        if include_test_runs:
            raise RuntimeError(f"Could not find {qualifier}.")
        raise RuntimeError(f"Could not find {qualifier} with mode='steps'.")

    jobs = get_newly_discovered_jobs(session, run.id)
    subject = build_subject(run, len(jobs), top_n)
    text_body = render_text_digest(run, jobs, top_n)
    html_body = render_html_digest(run, jobs, top_n)
    return run, jobs, subject, text_body, html_body


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    with SessionLocal() as session:
        run, jobs, subject, text_body, html_body = build_digest(
            session=session,
            run_id=args.run_id,
            include_test_runs=args.include_test_runs,
            top_n=args.top_n,
        )

    if args.dry_run:
        print(f"Subject: {subject}")
        print("")
        print(text_body)
        return 0

    config = load_mailgun_config()
    send_mailgun_digest(config, subject, text_body, html_body)
    print(f"Sent job digest for run {run.id} with {len(jobs)} newly discovered jobs.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
