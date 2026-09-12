#!/usr/bin/env python3
"""
Remove repeated boilerplate chunks from job descriptions per SITE.

Rule:
- A chunk is queued for removal if it appears in at least N jobs per site (--min-repeat).

Default output (no --verbose):
- Site summary showing which common chunks will be removed.

Verbose output (--verbose):
- Debug stats + (optionally) per-job removals.

Commands:
  plan      Show before/after or diff (also prints site summary unless --no-summary)
  removals  Print site summary by default; with --verbose prints per-job removals
  apply     Apply updates to DB (supports --dry-run); prints site summary unless --no-summary

Security:
- Do NOT print DATABASE_URL; it contains credentials.
"""

import argparse
import difflib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional

from sqlalchemy import select, text as sa_text
from sqlalchemy.orm import Session

from app.models import SessionLocal, Job


# -------------------------------
# Text chunking / normalization
# -------------------------------

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")  # remove punctuation for matching
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _split_sentences(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = _SENT_SPLIT_RE.split(text)
    out: List[str] = []
    for p in parts:
        p = _WS_RE.sub(" ", p.strip())
        if len(p) < 20:
            continue
        out.append(p)
    return out


def _norm_text(s: str) -> str:
    """Normalize for matching; replace URLs so link variations don't break repeats."""
    s = (s or "").lower()
    s = _URL_RE.sub("<URL>", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _strip_bullets_keep_text(desc: str) -> Tuple[str, List[str]]:
    """Return (non_bullet_text, bullet_lines) preserving bullets unchanged."""
    lines = (desc or "").replace("\r\n", "\n").splitlines()
    bullets: List[str] = []
    non_bullets: List[str] = []
    for ln in lines:
        if _BULLET_LINE_RE.match(ln):
            bullets.append(ln.rstrip())
        else:
            non_bullets.append(ln.rstrip())
    return "\n".join(non_bullets).strip(), [b for b in bullets if b.strip()]


def extract_chunks(desc: str, *, window: int, min_chars: int) -> List[str]:
    """
    Extract multi-sentence chunks using a sliding window.
    Bullets are ignored (not eligible for template detection).
    """
    text, _bullets = _strip_bullets_keep_text(desc)
    sents = _split_sentences(text)
    if len(sents) < window:
        return []

    chunks: List[str] = []
    for i in range(0, len(sents) - window + 1):
        chunk = " ".join(sents[i : i + window]).strip()
        if len(chunk) >= min_chars:
            chunks.append(chunk)
    return chunks


def remove_template_chunks(
    desc: str,
    template: set,
    *,
    window: int,
    min_chars: int,
) -> Tuple[str, int, List[str]]:
    """
    Remove any sentences that belong to any matched template chunk.
    Returns: (after_text, removed_chunks_count, removed_chunks_texts)
    """
    text, bullets = _strip_bullets_keep_text(desc)
    sents = _split_sentences(text)

    if len(sents) < window or not template:
        after = (desc or "").strip()
        return after, 0, []

    remove_sent_idx = set()
    removed_chunks: List[str] = []

    for i in range(0, len(sents) - window + 1):
        chunk = " ".join(sents[i : i + window]).strip()
        if len(chunk) < min_chars:
            continue
        if _norm_text(chunk) in template:
            removed_chunks.append(chunk)
            for j in range(i, i + window):
                remove_sent_idx.add(j)

    kept_sents = [s for idx, s in enumerate(sents) if idx not in remove_sent_idx]
    rebuilt = "\n".join([k for k in kept_sents if k.strip()]).strip()

    # Re-append bullets unchanged (at the end) so requirements remain
    if bullets:
        rebuilt = (rebuilt + "\n\n" + "\n".join(bullets)).strip()

    return rebuilt, len(removed_chunks), removed_chunks


# -------------------------------
# Query helpers
# -------------------------------

LIMIT = 0  # overridden by CLI


def select_job_ids(session: Session) -> List[int]:
    where = []
    params: Dict[str, Any] = {}
    where.append("(ai_analysis IS NULL OR ai_analysis = '')")
    where_sql = " AND ".join(where) if where else "1=1"
    limit_sql = f" LIMIT {LIMIT}" if LIMIT > 0 else ""
    sql = f"SELECT id FROM jobs WHERE {where_sql}{limit_sql}"
    rows = session.execute(sa_text(sql), params).fetchall()
    return [r[0] for r in rows]


# -------------------------------
# Processing
# -------------------------------


@dataclass
class JobChange:
    job_db_id: int
    job_key: str  # site:job_id
    site: str
    before: str
    after: str
    removed_chunks: int


def compute_site_template_min_repeat(
    descs: List[str],
    *,
    min_repeat: int,
    window: int,
    min_chars: int,
) -> Tuple[set, Dict[str, str], Counter]:
    """
    Returns:
      template_keys: set of normalized chunks to remove
      examples: dict[norm_key] -> example original chunk text
      presence: Counter[norm_key] -> number of jobs containing the chunk
    """
    n = len(descs)
    if n < min_repeat:
        return set(), {}, Counter()

    presence: Counter = Counter()
    examples: Dict[str, str] = {}

    for d in descs:
        chunks = extract_chunks(d, window=window, min_chars=min_chars)
        uniq_norm = set()
        for c in chunks:
            k = _norm_text(c)
            if not k:
                continue
            uniq_norm.add(k)
            # keep first seen example for summary printing
            if k not in examples:
                examples[k] = c
        for k in uniq_norm:
            presence[k] += 1

    template_keys = {k for k, c in presence.items() if c >= min_repeat}
    return template_keys, examples, presence


def process_and_plan_changes(
    *,
    min_repeat: int = 2,
    window: int = 2,
    min_chars: int = 80,
    require_change_min_len: int = 1,
    only_site: Optional[str] = None,
    verbose: bool = False,
) -> Tuple[
    List[JobChange],
    Dict[str, set],
    Dict[str, Dict[str, str]],
    Dict[str, Counter],
    Dict[str, int],
]:
    """
    Returns:
      changes
      templates_by_site: site -> set(norm_keys)
      examples_by_site: site -> {norm_key: example_text}
      presence_by_site: site -> Counter(norm_key -> job_count_with_chunk)
      jobs_per_site: site -> job count
    """
    with SessionLocal() as s:
        ids = select_job_ids(s)
        if not ids:
            print("[ok] No jobs matched filter criteria.")
            return [], {}, {}, {}, {}

        jobs = list(s.execute(select(Job).where(Job.id.in_(ids))).scalars())

        grouped: Dict[str, List[Job]] = defaultdict(list)
        for j in jobs:
            site = (j.site or "Unknown").strip().lower() or "unknown"
            if only_site and site != only_site.strip().lower():
                continue
            grouped[site].append(j)

        jobs_per_site = {k: len(v) for k, v in grouped.items()}

        if verbose:
            sites_sorted = sorted(jobs_per_site.items(), key=lambda kv: (-kv[1], kv[0]))
            print(
                f"[dbg] total_jobs_selected={len(jobs)} sites_in_run={len(sites_sorted)}"
            )
            print("[dbg] jobs_per_site:")
            for site, n in sites_sorted:
                print(f"  - {site}: {n}")

        templates_by_site: Dict[str, set] = {}
        examples_by_site: Dict[str, Dict[str, str]] = {}
        presence_by_site: Dict[str, Counter] = {}
        changes: List[JobChange] = []

        for site, site_jobs in grouped.items():
            descs = [(j.desc or "") for j in site_jobs]
            n = len(descs)

            template, examples, presence = compute_site_template_min_repeat(
                descs,
                min_repeat=min_repeat,
                window=window,
                min_chars=min_chars,
            )

            templates_by_site[site] = template
            examples_by_site[site] = examples
            presence_by_site[site] = presence

            if verbose:
                eligible = n >= min_repeat
                print(
                    f"[dbg] site={site!r} jobs={n} eligible={eligible} "
                    f"min_repeat={min_repeat} template_chunks={len(template)}"
                )

            if not template:
                continue

            planned = 0
            for j in site_jobs:
                before = j.desc or ""
                after, removed_chunks_count, _ = remove_template_chunks(
                    before, template, window=window, min_chars=min_chars
                )
                if after != before and len(after.strip()) >= require_change_min_len:
                    planned += 1
                    changes.append(
                        JobChange(
                            job_db_id=j.id,
                            job_key=f"{j.site}:{j.job_id}",
                            site=site,
                            before=before,
                            after=after,
                            removed_chunks=removed_chunks_count,
                        )
                    )

            if verbose:
                print(f"[dbg] site={site!r} planned_updates={planned}")

        return (
            changes,
            templates_by_site,
            examples_by_site,
            presence_by_site,
            jobs_per_site,
        )


# -------------------------------
# Output helpers
# -------------------------------


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 3)].rstrip() + "..."


def print_site_summary(
    templates_by_site: Dict[str, set],
    examples_by_site: Dict[str, Dict[str, str]],
    presence_by_site: Dict[str, Counter],
    jobs_per_site: Dict[str, int],
    *,
    max_chunks_per_site: int = 10,
    max_chars: int = 220,
    min_repeat: int = 2,
) -> None:
    """
    Prints:
      disney:
      - (2/17) <chunk example>
      - (2/17) <chunk example>
    """
    sites = sorted(templates_by_site.keys())
    any_printed = False

    for site in sites:
        template = templates_by_site.get(site, set())
        if not template:
            continue

        presence = presence_by_site.get(site, Counter())
        examples = examples_by_site.get(site, {})
        total_jobs = jobs_per_site.get(site, 0)

        # sort by most common first, then stable by text
        ranked = sorted(
            list(template),
            key=lambda k: (-presence.get(k, 0), examples.get(k, "")),
        )

        print(f"\n{site}:")
        shown = 0
        for k in ranked:
            c = presence.get(k, 0)
            if c < min_repeat:
                continue
            ex = examples.get(k, "")
            if not ex:
                continue
            print(f"- ({c}/{total_jobs}) {_truncate(ex, max_chars)}")
            shown += 1
            if shown >= max_chunks_per_site:
                break

        any_printed = True

    if not any_printed:
        print("[ok] No common chunks matched the removal rule for any site.")


def print_change(
    change: JobChange, *, max_chars: int = 1200, show_diff: bool = False
) -> None:
    print(
        f"\n=== {change.site} | {change.job_key} | removed_chunks={change.removed_chunks} ==="
    )

    if show_diff:
        a = (change.before or "").splitlines(keepends=True)
        b = (change.after or "").splitlines(keepends=True)
        text = "".join(difflib.unified_diff(a, b, fromfile="before", tofile="after"))
        print(
            text[:max_chars]
            + ("\n...[diff truncated]" if len(text) > max_chars else "")
        )
        return

    before = (change.before or "").strip()
    after = (change.after or "").strip()
    print("--- BEFORE ---")
    print(before[:max_chars] + ("\n...[truncated]" if len(before) > max_chars else ""))
    print("--- AFTER ---")
    print(after[:max_chars] + ("\n...[truncated]" if len(after) > max_chars else ""))


def print_removals_per_job(
    changes: List[JobChange],
    templates_by_site: Dict[str, set],
    *,
    window: int,
    min_chars: int,
    max_items: Optional[int] = None,
    dedupe: bool = True,
) -> None:
    printed = 0
    for c in changes:
        template = templates_by_site.get(c.site, set())
        _after, _removed_chunks_count, removed_chunks = remove_template_chunks(
            c.before, template, window=window, min_chars=min_chars
        )

        if dedupe:
            seen = set()
            uniq = []
            for x in removed_chunks:
                k = _norm_text(x)
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(x)
            removed_chunks = uniq

        if not removed_chunks:
            continue

        print(
            f"\n=== {c.site} | {c.job_key} | removed_chunks={len(removed_chunks)} ==="
        )
        for x in removed_chunks:
            print(f"- {x}")

        printed += 1
        if max_items is not None and printed >= max_items:
            break


def apply_changes(changes: List[JobChange]) -> int:
    if not changes:
        return 0

    with SessionLocal() as s:
        ids = [c.job_db_id for c in changes]
        jobs = {
            j.id: j for j in s.execute(select(Job).where(Job.id.in_(ids))).scalars()
        }

        updated = 0
        for c in changes:
            j = jobs.get(c.job_db_id)
            if not j:
                continue
            if (j.desc or "") != c.after:
                j.desc = c.after
                updated += 1

        s.commit()
        return updated


# -------------------------------
# CLI
# -------------------------------


def add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--limit", type=int, default=0, help="Limit jobs for testing (0 = no limit)"
    )
    p.add_argument(
        "--only-site",
        type=str,
        default=None,
        help="Only process this site (e.g. disney)",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Debug stats + per-job removals in 'removals'",
    )

    p.add_argument(
        "--min-repeat",
        type=int,
        default=2,
        help="Remove chunks that repeat in at least N jobs per site",
    )
    p.add_argument(
        "--window", type=int, default=2, help="Sentence window size for chunking"
    )
    p.add_argument("--min-chars", type=int, default=80, help="Minimum chars per chunk")
    p.add_argument(
        "--min-after-len",
        type=int,
        default=1,
        help="Minimum length of 'after' to accept change",
    )
    p.add_argument(
        "--max-print", type=int, default=1200, help="Max chars to print per section"
    )

    # summary controls
    p.add_argument(
        "--no-summary", action="store_true", help="Do not print site summary"
    )
    p.add_argument(
        "--summary-max-chunks",
        type=int,
        default=10,
        help="Max chunks to show per site in summary",
    )
    p.add_argument(
        "--summary-max-chars",
        type=int,
        default=220,
        help="Max characters per chunk in summary",
    )


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="Print planned updates (before/after or diff)")
    add_common_args(p_plan)
    p_plan.add_argument(
        "--diff", action="store_true", help="Show unified diff instead of before/after"
    )

    p_rem = sub.add_parser(
        "removals", help="Show removals (summary by default; per-job with --verbose)"
    )
    add_common_args(p_rem)
    p_rem.add_argument(
        "--no-dedupe", action="store_true", help="Do not dedupe removed chunks per job"
    )
    p_rem.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Stop after printing this many jobs (per-job mode)",
    )

    p_apply = sub.add_parser("apply", help="Apply updates to DB")
    add_common_args(p_apply)
    p_apply.add_argument(
        "--dry-run", action="store_true", help="Do not update DB; just print counts"
    )

    args = ap.parse_args()

    global LIMIT
    LIMIT = args.limit

    changes, templates_by_site, examples_by_site, presence_by_site, jobs_per_site = (
        process_and_plan_changes(
            min_repeat=args.min_repeat,
            window=args.window,
            min_chars=args.min_chars,
            require_change_min_len=args.min_after_len,
            only_site=args.only_site,
            verbose=args.verbose,
        )
    )

    # Default: print site summary unless explicitly disabled
    if not args.no_summary:
        print_site_summary(
            templates_by_site,
            examples_by_site,
            presence_by_site,
            jobs_per_site,
            max_chunks_per_site=args.summary_max_chunks,
            max_chars=args.summary_max_chars,
            min_repeat=args.min_repeat,
        )

    print(
        f"\n[ok] Planned {len(changes)} job updates across {len(templates_by_site)} sites."
    )

    if args.cmd == "plan":
        # Keep plan output explicit: show diffs/before-after
        for c in changes:
            print_change(c, max_chars=args.max_print, show_diff=args.diff)
        return

    if args.cmd == "removals":
        # Default removals mode is summary only.
        # If --verbose, also show per-job removals.
        if args.verbose:
            print_removals_per_job(
                changes,
                templates_by_site,
                window=args.window,
                min_chars=args.min_chars,
                max_items=args.max_items,
                dedupe=not args.no_dedupe,
            )
        return

    if args.cmd == "apply":
        if args.dry_run:
            print("[dry-run] No database updates applied.")
            return
        updated = apply_changes(changes)
        print(f"[ok] Updated {updated} jobs.")
        return


if __name__ == "__main__":
    main()
