"""Analyze stored job postings against a resume using a local Ollama model.

The script reads jobs from the configured SQLAlchemy database, asks a chat
model for a strict JSON job-fit assessment, validates the response, and stores
the compact JSON back on each job row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import string
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
CANONICAL_AI_LOG_NAME = "ai-analysis.log"
LOG_FILE = os.path.join(LOG_DIR, CANONICAL_AI_LOG_NAME)


def _append_to_ai_log(text: str) -> None:
    """Best-effort append to the canonical AI processing log."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass


def _log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    """Record unhandled failures in the canonical AI analysis log."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    ts = datetime.now().strftime("%H:%M:%S")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [fatal] Unhandled exception\n")
            traceback.print_exception(exc_type, exc_value, exc_traceback, file=f)
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _log_uncaught_exception

from nltk.tokenize import sent_tokenize

# Optional .env loading keeps the script usable from scheduled jobs and shells.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

# LangChain / Ollama
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage

# DB
from sqlalchemy import inspect as sa_inspect, or_, select, text as sa_text
from sqlalchemy.orm import Session
from app.models import (
    SessionLocal,
    Job,
    JobSwipe,
    JobChange,
    JobFitBrief,
    JobApplicationPrep,
    init_db,
)
from app.db import utc_now_naive
from app.compensation import (
    choose_deterministic_compensation,
    compensation_has_values,
    format_compensation_summary,
)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--redo must be an integer greater than 0") from exc
    if n <= 0:
        raise argparse.ArgumentTypeError("--redo must be greater than 0")
    return n


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze stored job postings against a resume using Ollama."
    )
    parser.add_argument(
        "--redo",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "Re-run AI analysis for the newest N discovered jobs, including jobs "
            "that already have AI analysis."
        ),
    )
    parser.add_argument(
        "--compensation-only",
        action="store_true",
        help="Reprocess compensation without changing job-match analysis fields.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_jobs",
        help="Include every active job in compensation-only mode.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Reprocess jobs already completed with the current compensation schema, "
            "or regenerate existing fit briefs in --fit-briefs-only mode."
        ),
    )
    parser.add_argument(
        "--fit-briefs-only",
        action="store_true",
        help="Generate applicant-facing fit briefs for already analyzed eligible jobs.",
    )
    parser.add_argument(
        "--application-prep-only",
        action="store_true",
        help="Generate job-specific application prep for liked move-forward jobs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run and validate compensation extraction without writing database changes.",
    )
    parser.add_argument(
        "--sample-per-site",
        type=_positive_int,
        default=None,
        metavar="N",
        help="In compensation-only mode, process only the newest N jobs per site.",
    )
    args = parser.parse_args(argv)
    exclusive_modes = [
        bool(args.compensation_only),
        bool(args.fit_briefs_only),
        bool(args.application_prep_only),
    ]
    if sum(1 for enabled in exclusive_modes if enabled) > 1:
        parser.error("choose only one of --compensation-only, --fit-briefs-only, or --application-prep-only")
    if args.all_jobs and not args.compensation_only:
        parser.error("--all requires --compensation-only")
    if (args.dry_run or args.sample_per_site) and not args.compensation_only:
        parser.error("--dry-run and --sample-per-site require --compensation-only")
    if args.force and not (
        args.compensation_only or args.fit_briefs_only or args.application_prep_only
    ):
        parser.error("--force requires --compensation-only, --fit-briefs-only, or --application-prep-only")
    if args.compensation_only and not (args.all_jobs or args.sample_per_site):
        parser.error("--compensation-only requires --all or --sample-per-site N")
    if args.all_jobs and args.sample_per_site:
        parser.error("choose either --all or --sample-per-site, not both")
    if args.compensation_only and args.redo is not None:
        parser.error("--redo cannot be combined with --compensation-only")
    return args

# -------------------------------
# Config
# -------------------------------
RESUME_PATH = os.getenv("RESUME_PATH", "resume.txt")
AI_SYSTEM_PROMPT_TEMPLATE_PATH = os.getenv(
    "AI_SYSTEM_PROMPT_TEMPLATE_PATH",
    os.path.join(BASE_DIR, "prompts", "job_match_system.txt"),
)
FIT_BRIEF_SYSTEM_PROMPT_PATH = os.getenv(
    "FIT_BRIEF_SYSTEM_PROMPT_PATH",
    os.path.join(BASE_DIR, "prompts", "fit_brief_system.txt"),
)
APPLICATION_PREP_SYSTEM_PROMPT_PATH = os.getenv(
    "APPLICATION_PREP_SYSTEM_PROMPT_PATH",
    os.path.join(BASE_DIR, "prompts", "application_prep_system.txt"),
)
COMPENSATION_SYSTEM_PROMPT_PATH = os.getenv(
    "COMPENSATION_SYSTEM_PROMPT_PATH",
    os.path.join(BASE_DIR, "prompts", "compensation_system.txt"),
)
COMPENSATION_SCHEMA_VERSION = 1
FIT_BRIEF_SCHEMA_VERSION = 1
APPLICATION_PREP_SCHEMA_VERSION = 1
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
ONLY_EMPTY = os.getenv("ONLY_EMPTY", "true").lower() == "true"
SITE_FILTER = os.getenv("SITE_FILTER", "").strip()
LIMIT = int(os.getenv("LIMIT", "0"))
TOKEN_THRESHOLD = int(os.getenv("AI_TOKEN_THRESHOLD", "4096"))
MAX_JOB_DESC_TOKENS = int(os.getenv("MAX_JOB_DESC_TOKENS", "700"))
MAX_RESUME_TOKENS = int(os.getenv("MAX_RESUME_TOKENS", "500"))
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "2048"))
OLLAMA_NUM_PREDICT = int(os.getenv("OLLAMA_NUM_PREDICT", "300"))
AI_THINKING_RETRY_NUM_PREDICT = max(
    OLLAMA_NUM_PREDICT,
    int(os.getenv("AI_THINKING_RETRY_NUM_PREDICT", str(OLLAMA_NUM_PREDICT * 3))),
)
APPLICATION_PREP_NUM_PREDICT = max(
    OLLAMA_NUM_PREDICT,
    int(os.getenv("APPLICATION_PREP_NUM_PREDICT", "4096")),
)
APPLICATION_PREP_RETRY_NUM_PREDICT = max(
    APPLICATION_PREP_NUM_PREDICT,
    AI_THINKING_RETRY_NUM_PREDICT,
    int(os.getenv("APPLICATION_PREP_RETRY_NUM_PREDICT", str(APPLICATION_PREP_NUM_PREDICT * 2))),
)
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
KEYWORD_LIST_LIMIT = int(os.getenv("AI_KEYWORD_LIST_LIMIT", "5"))
FIT_SUMMARY_MAX_CHARS = int(os.getenv("AI_FIT_SUMMARY_MAX_CHARS", "400"))
AI_CONCURRENCY = max(1, int(os.getenv("AI_CONCURRENCY", "2")))
AI_MAX_INFLIGHT = max(
    AI_CONCURRENCY, int(os.getenv("AI_MAX_INFLIGHT", str(AI_CONCURRENCY * 2)))
)
AI_BATCH_LOG_EVERY = max(1, int(os.getenv("AI_BATCH_LOG_EVERY", "10")))
AI_REQUEST_TIMEOUT_SEC = max(10, int(os.getenv("AI_REQUEST_TIMEOUT_SEC", "120")))
OLLAMA_PREFLIGHT_TIMEOUT_SEC = max(
    1, int(os.getenv("OLLAMA_PREFLIGHT_TIMEOUT_SEC", "5"))
)
OLLAMA_STARTUP_TIMEOUT_SEC = max(1, int(os.getenv("OLLAMA_STARTUP_TIMEOUT_SEC", "15")))
OLLAMA_STARTUP_POLL_SEC = max(
    0.1, float(os.getenv("OLLAMA_STARTUP_POLL_SEC", "0.5"))
)
AI_WAIT_HEARTBEAT_SEC = max(5, int(os.getenv("AI_WAIT_HEARTBEAT_SEC", "15")))
AI_MAX_ATTEMPTS = max(1, int(os.getenv("AI_MAX_ATTEMPTS", "4")))
AI_SECOND_PASS_REVIEW = os.getenv("AI_SECOND_PASS_REVIEW", "true").lower() == "true"
_AI_REVIEW_MIN_MATCH_RAW = os.getenv("AI_REVIEW_MIN_MATCH")
_AI_REVIEW_MAX_MATCH_RAW = os.getenv("AI_REVIEW_MAX_MATCH")
AI_REVIEW_MIN_MATCH = (
    int(_AI_REVIEW_MIN_MATCH_RAW) if _AI_REVIEW_MIN_MATCH_RAW not in {None, ""} else None
)
AI_REVIEW_MAX_MATCH = (
    int(_AI_REVIEW_MAX_MATCH_RAW) if _AI_REVIEW_MAX_MATCH_RAW not in {None, ""} else None
)
AI_FIT_BRIEF_MIN_MATCH = int(os.getenv("AI_FIT_BRIEF_MIN_MATCH", "75"))


def parse_ollama_think(value: str) -> Any:
    """Parse Ollama's boolean or level-based thinking control."""
    normalized = (value or "").strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"low", "medium", "high", "max"}:
        return normalized
    raise ValueError(
        "OLLAMA_THINK must be one of false, true, low, medium, high, or max"
    )


OLLAMA_THINK = parse_ollama_think(os.getenv("OLLAMA_THINK", "false"))
APPLICATION_PREP_THINK = parse_ollama_think(os.getenv("APPLICATION_PREP_THINK", "false"))

REQUIRED_KEYS = {
    "match_percentage",
    "compensation",
    "fit_summary",
    "keywords_overlap",
    "missing_keywords",
    "experience_match",
    "location_policy_match",
}

COMPENSATION_KEYS = {
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
}
PAY_PERIODS = {"year", "hour", "month", "week", "day"}

EXP_ALLOWED = {"underqualified", "qualified", "overqualified"}
LOC_ALLOWED = {"remote", "hybrid", "onsite", "unknown"}


@dataclass
class JobTask:
    id: int
    site: str
    job_id: str
    title: str
    desc: str
    pay: str
    run_id: int
    content_hash: Optional[str]


@dataclass
class JobResult:
    id: int
    site: str
    job_id: str
    status: str  # ok | skip | llm_error | schema_error | ollama_unavailable
    payload_json: Optional[str]
    update_params: Optional[Dict[str, Any]]
    llm_seconds: float
    error_text: str
    index: int


@dataclass
class FitBriefTask:
    id: int
    site: str
    job_id: str
    title: str
    desc: str
    ai_analysis: str
    ai_match_percentage: Optional[int]
    content_hash: Optional[str]
    existing_resume_hash: Optional[str] = None
    existing_job_content_hash: Optional[str] = None


@dataclass
class ApplicationPrepTask:
    id: int
    prep_id: int
    site: str
    job_id: str
    title: str
    desc: str
    ai_analysis: str
    ai_match_percentage: Optional[int]
    content_hash: Optional[str]


class OllamaEmptyContentError(RuntimeError):
    """Raised when Ollama returns reasoning text but no final answer."""

    def __init__(self, done_reason: Any, thinking_chars: int, eval_count: Any) -> None:
        self.done_reason = done_reason
        self.thinking_chars = thinking_chars
        self.eval_count = eval_count
        super().__init__(
            "Ollama returned empty message.content "
            f"(done_reason={done_reason!r}, thinking_chars={thinking_chars}, "
            f"eval_count={eval_count!r})."
        )


class OllamaUnavailableError(RuntimeError):
    """Raised when the configured Ollama service cannot accept requests."""


class OllamaPrerequisiteError(RuntimeError):
    """Raised when Ollama is not ready before batch work starts."""


class OllamaReachabilityError(OllamaPrerequisiteError):
    """Raised when the Ollama API cannot be reached by the preflight check."""


# -------------------------------
# Logging
# -------------------------------
def _console_safe_line(line: str) -> str:
    """Return text that can be written to the current Windows console encoding."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return line.encode(encoding, errors="replace").decode(encoding, errors="replace")


def log(msg: str) -> None:
    """Append a timestamped message to the configured log and mirror it to stdout."""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    _append_to_ai_log(line + "\n")
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(_console_safe_line(line), flush=True)
    except Exception:
        pass

def _handle_sigint(signum, frame) -> None:
    """Force immediate process exit on Ctrl+C to avoid thread-pool hangs."""
    try:
        print("\n[abort] Ctrl+C received. Forcing exit now.", flush=True)
    finally:
        os._exit(130)


def build_llm() -> ChatOllama:
    """Create a ChatOllama client with the current runtime settings."""
    return ChatOllama(
        model=OLLAMA_MODEL,
        format="json",
        temperature=0.2,
        keep_alive=OLLAMA_KEEP_ALIVE,
        sync_client_kwargs={"timeout": AI_REQUEST_TIMEOUT_SEC},
        options={
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_NUM_PREDICT,
        },
    )


_thread_local = threading.local()


def get_thread_llm() -> ChatOllama:
    """Return a thread-local LLM client to avoid cross-thread client contention."""
    llm = getattr(_thread_local, "llm", None)
    if llm is None:
        llm = build_llm()
        _thread_local.llm = llm
    return llm


def _model_name_variants(model: str) -> Set[str]:
    """Return equivalent local model names, including Ollama's default tag."""
    normalized = (model or "").strip()
    if not normalized:
        return set()
    if normalized.endswith(":latest"):
        return {normalized, normalized[: -len(":latest")]}
    # A colon after the final slash is a tag. Earlier colons may belong to a
    # registry host/port and must not be treated as the model tag separator.
    if ":" not in normalized.rsplit("/", 1)[-1]:
        return {normalized, f"{normalized}:latest"}
    return {normalized}


def _read_ollama_tags() -> str:
    """Read Ollama's model list endpoint and distinguish connection failures."""
    tags_url = f"{OLLAMA_BASE_URL}/api/tags"
    req = urllib.request.Request(tags_url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_PREFLIGHT_TIMEOUT_SEC) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise OllamaPrerequisiteError(
            f"Ollama prerequisite check failed at {tags_url}: HTTP {exc.code}."
        ) from exc
    except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
        raise OllamaReachabilityError(
            f"Ollama is not reachable at {OLLAMA_BASE_URL}. "
            "Start it with 'ollama serve' and retry."
        ) from exc


def _start_ollama_serve() -> subprocess.Popen:
    """Start the Ollama server process without blocking this analyzer run."""
    kwargs: Dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        detached = getattr(subprocess, "DETACHED_PROCESS", 0)
        kwargs["creationflags"] = new_group | detached
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(["ollama", "serve"], **kwargs)


def _start_ollama_and_wait() -> None:
    """Best-effort start for a stopped local Ollama server."""
    log(
        f"[warn] Ollama is not reachable at {OLLAMA_BASE_URL}; "
        "starting 'ollama serve' and retrying preflight."
    )
    try:
        _start_ollama_serve()
    except FileNotFoundError as exc:
        raise OllamaPrerequisiteError(
            "Could not start Ollama with 'ollama serve' because 'ollama' "
            "was not found on PATH."
        ) from exc
    except OSError as exc:
        raise OllamaPrerequisiteError(
            f"Could not start Ollama with 'ollama serve': {exc}."
        ) from exc

    deadline = time.time() + OLLAMA_STARTUP_TIMEOUT_SEC
    last_error: Optional[OllamaReachabilityError] = None
    while True:
        try:
            _read_ollama_tags()
            return
        except OllamaReachabilityError as exc:
            last_error = exc

        if time.time() >= deadline:
            raise OllamaPrerequisiteError(
                f"Ollama is not reachable at {OLLAMA_BASE_URL}. "
                "Tried to start it with 'ollama serve', but the API did not "
                f"respond within {OLLAMA_STARTUP_TIMEOUT_SEC}s."
            ) from last_error
        time.sleep(OLLAMA_STARTUP_POLL_SEC)


def check_ollama_prerequisites() -> None:
    """Require a reachable Ollama API and the configured local model."""
    tags_url = f"{OLLAMA_BASE_URL}/api/tags"
    try:
        body = _read_ollama_tags()
    except OllamaReachabilityError:
        _start_ollama_and_wait()
        body = _read_ollama_tags()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OllamaPrerequisiteError(
            f"Ollama prerequisite check at {tags_url} returned invalid JSON."
        ) from exc

    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise OllamaPrerequisiteError(
            f"Ollama prerequisite check at {tags_url} returned an invalid model list."
        )

    installed: Set[str] = set()
    for item in models:
        if not isinstance(item, dict):
            continue
        for key in ("name", "model"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                installed.update(_model_name_variants(value))

    if not (_model_name_variants(OLLAMA_MODEL) & installed):
        raise OllamaPrerequisiteError(
            f"Ollama model '{OLLAMA_MODEL}' is not installed. "
            f"Run 'ollama pull {OLLAMA_MODEL}' and retry."
        )

    log(f"[ok] Ollama ready at {OLLAMA_BASE_URL} | model={OLLAMA_MODEL}")


def invoke_ollama_json(
    messages: list,
    num_predict: Optional[int] = None,
    think: Optional[Any] = None,
) -> str:
    """Call Ollama directly so socket timeout failures return to the worker."""
    wire_messages = []
    for msg in messages:
        role = "user"
        if isinstance(msg, SystemMessage):
            role = "system"
        wire_messages.append({"role": role, "content": str(msg.content)})

    effective_num_predict = num_predict or OLLAMA_NUM_PREDICT
    payload = {
        "model": OLLAMA_MODEL,
        "messages": wire_messages,
        "stream": False,
        "format": "json",
        "think": OLLAMA_THINK if think is None else think,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": effective_num_predict,
            "temperature": 0.2,
        },
    }
    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_REQUEST_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", "replace")
    except TimeoutError as exc:
        raise TimeoutError(f"Ollama request timed out after {AI_REQUEST_TIMEOUT_SEC}s") from exc
    except urllib.error.HTTPError as exc:
        if exc.code in {502, 503, 504}:
            raise OllamaUnavailableError(
                f"Ollama became unavailable at {OLLAMA_BASE_URL} (HTTP {exc.code})"
            ) from exc
        raise RuntimeError(f"Ollama request failed with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError(
                f"Ollama request timed out after {AI_REQUEST_TIMEOUT_SEC}s"
            ) from exc
        raise OllamaUnavailableError(
            f"Ollama became unavailable at {OLLAMA_BASE_URL}: {exc.reason}"
        ) from exc
    except (ConnectionError, OSError) as exc:
        raise OllamaUnavailableError(
            f"Ollama became unavailable at {OLLAMA_BASE_URL}: {exc}"
        ) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Ollama returned non-JSON response: {body[:200]}") from exc

    message = data.get("message") or {}
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"Ollama response missing message.content: {body[:200]}")
    if not content.strip():
        thinking = message.get("thinking")
        thinking_len = len(thinking) if isinstance(thinking, str) else 0
        done_reason = data.get("done_reason")
        raise OllamaEmptyContentError(
            done_reason=done_reason,
            thinking_chars=thinking_len,
            eval_count=data.get("eval_count"),
        )
    return content


# -------------------------------
# Prompt + utils
# -------------------------------
def clean_text(text: str) -> str:
    """Normalize scraped text into a single whitespace-collapsed line."""
    t = (text or "").replace("\\n", "\n")
    t = t.replace("\r", " ").replace("\t", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", t).strip()


def render_system_prompt(template_path: str = AI_SYSTEM_PROMPT_TEMPLATE_PATH) -> str:
    """Render the system prompt template with runtime prompt limits."""
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"AI system prompt template not found: {template_path}")

    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read().lstrip("\ufeff")

    return string.Template(template_text).substitute(
        FIT_SUMMARY_MAX_CHARS=FIT_SUMMARY_MAX_CHARS,
        KEYWORD_LIST_LIMIT=KEYWORD_LIST_LIMIT,
    )


def build_messages(resume_text: str, title: str, desc: str) -> list:
    """Build the LLM messages for one job-fit assessment.

    The job description is treated as untrusted data and placed in the human
    message, while the output contract stays in the system message. This does
    not eliminate prompt-injection risk, but it gives the model a clear priority
    order and the result is still validated before persistence.
    """
    sys_msg = render_system_prompt()
    user_msg = (
        "<resume>\n"
        f"{clean_text(resume_text)}\n"
        "</resume>\n\n"
        "<job_title>\n"
        f"{clean_text(title)}\n"
        "</job_title>\n\n"
        "<job_description>\n"
        f"{clean_text(desc)}\n"
        "</job_description>"
    )
    return [SystemMessage(content=sys_msg), HumanMessage(content=user_msg)]


def process_job_descriptions(
    job_descriptions: Dict[str, List[str]],
) -> Dict[str, List[str]]:
    """Group stored job descriptions by company and sentence-tokenize them.

    TODO: The old comment said this removed repeated boilerplate sentences, but
    the current implementation only tokenizes combined company descriptions.
    Callers should not rely on de-duplication until that behavior is implemented.
    """
    with SessionLocal() as s:
        ids = select_job_ids(s)
        total = len(ids)
        if total == 0:
            log("[ok] No jobs matched filter criteria.")
            return {}

        jobs = list(s.execute(select(Job).where(Job.id.in_(ids))).scalars())

        # Jobs grouped by company
        for i, j in enumerate(jobs, start=1):
            # Job currently has no company column in app.models; keep this safe
            # if the helper is called before that schema field exists.
            company = getattr(j, "company", None) or "Unknown"
            if company not in job_descriptions:
                job_descriptions[company] = []
            job_descriptions[company].append(j.desc or "")
            log(f"----- [Job {i}/XXX] {j.site}:{j.job_id} -----")

    processed = {}
    for company, descriptions in job_descriptions.items():
        combined = " ".join(descriptions)
        # As parse_job_description is not defined, fallback to splitting into sentences
        processed[company] = sent_tokenize(combined)
    return processed


def parse_json_strict(s: str) -> Optional[dict]:
    """Parse a model response as JSON, with one fallback for wrapped objects."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except Exception:
            return None


def require_json_keys(obj: Any, required: Iterable[str]) -> bool:
    """Return whether an object is a dict with every required key present."""
    return isinstance(obj, dict) and all(k in obj for k in required)


def _is_single_sentence(s: str) -> bool:
    """Apply a lightweight guard against list-like or multi-line summaries."""
    if "\n" in s or len(s) == 0:
        return False
    # allow one sentence; rudimentary check: <= 2 terminal punctuation marks
    return len(re.findall(r"[.!?]", s)) <= 2


def _coerce_int(x: Any) -> Optional[int]:
    """Coerce common model-produced integer forms such as '85%' to int."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int,)):
        return x
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, str):
        m = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*", x)
        if m:
            v = int(m.group(1))
            return v
    return None


def _coerce_float01(x: Any) -> Optional[float]:
    """Coerce model confidence-style values in the closed interval [0, 1]."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        if 0.0 <= v <= 1.0:
            return v
        return None
    if isinstance(x, str):
        try:
            v = float(x.strip())
            if 0.0 <= v <= 1.0:
                return v
        except Exception:
            return None
    return None


def _estimate_token_count(text: str) -> int:
    """Return a rough token estimate without requiring LangChain splitters."""
    return max(1, len(re.findall(r"\w+|[^\w\s]", text)))


def _token_like_chunks(text: str) -> List[str]:
    """Split text into word/punctuation/whitespace chunks for stable truncation."""
    return re.findall(r"\w+|[^\w\s]|\s+", text or "")


def _is_counted_token(chunk: str) -> bool:
    # Return whether a chunk should count against a rough token budget.
    return bool(chunk and not chunk.isspace())


def token_budget_label(max_tokens: int) -> str:
    """Return the startup-log label for an approximate prompt budget."""
    if max_tokens <= 0:
        return "full"
    return f"~{max_tokens} tokens"

def truncate_to_token_budget(text: str, max_tokens: int) -> Tuple[str, int, bool]:
    """Trim text only when max_tokens is positive; zero keeps full text."""
    cleaned = clean_text(text)
    if max_tokens <= 0:
        original_tokens = _estimate_token_count(cleaned)
        return cleaned, original_tokens, False

    chunks = _token_like_chunks(cleaned)
    original_tokens = sum(1 for chunk in chunks if _is_counted_token(chunk))
    if original_tokens <= max_tokens:
        return cleaned, original_tokens, False

    kept: List[str] = []
    count = 0
    for chunk in chunks:
        if _is_counted_token(chunk):
            if count >= max_tokens:
                break
            count += 1
        kept.append(chunk)
    return "".join(kept).strip(), original_tokens, True


def _normalize_str_list(arr: Any) -> Optional[List[str]]:
    """Validate, trim, and case-insensitively de-duplicate a list of strings."""
    if not isinstance(arr, list):
        return None
    out: List[str] = []
    seen = set()
    for item in arr:
        if not isinstance(item, str):
            return None
        t = item.strip()
        if not t:
            continue
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out


def _coerce_money(value: Any) -> Optional[int | float]:
    """Normalize a non-negative model-produced monetary number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    if number < 0:
        return None
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def validate_compensation(payload: Any) -> Tuple[bool, str]:
    """Validate and normalize the structured compensation object in place."""
    if not isinstance(payload, dict) or set(payload) != COMPENSATION_KEYS:
        return False, "compensation must contain exactly the required keys"

    for key in ("base_pay_low", "base_pay_high", "ote_low", "ote_high"):
        raw = payload.get(key)
        value = _coerce_money(raw)
        if raw is not None and value is None:
            return False, f"{key} must be a non-negative number or null"
        payload[key] = value

    for low_key, high_key in (
        ("base_pay_low", "base_pay_high"),
        ("ote_low", "ote_high"),
    ):
        low, high = payload.get(low_key), payload.get(high_key)
        if low is not None and high is not None and low > high:
            return False, f"{low_key} cannot exceed {high_key}"

    currency = payload.get("pay_currency")
    if currency is not None:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Za-z]{3}", currency.strip()):
            return False, "pay_currency must be a three-letter ISO code or null"
        payload["pay_currency"] = currency.strip().upper()

    period = payload.get("pay_period")
    if period is not None:
        if not isinstance(period, str) or period.strip().lower() not in PAY_PERIODS:
            return False, f"pay_period must be one of {sorted(PAY_PERIODS)} or null"
        payload["pay_period"] = period.strip().lower()

    has_money = any(
        payload.get(key) is not None
        for key in ("base_pay_low", "base_pay_high", "ote_low", "ote_high")
    )
    if has_money and not payload.get("pay_currency"):
        return False, "monetary values require pay_currency"

    for key in ("bonus_offered", "equity_offered", "commission_offered"):
        if payload.get(key) not in {True, None}:
            return False, f"{key} must be true or null"
    if not isinstance(payload.get("multiple_pay_ranges"), bool):
        return False, "multiple_pay_ranges must be boolean"

    for key in ("compensation_text", "compensation_notes"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            return False, f"{key} must be string or null"
        if isinstance(value, str):
            payload[key] = value.strip() or None
    return True, ""


def compensation_evidence_matches(payload: Dict[str, Any], description: str) -> bool:
    """Require disclosed compensation to carry an excerpt from the posting."""
    if not compensation_has_values(payload):
        return payload.get("compensation_text") is None
    evidence = clean_text(payload.get("compensation_text") or "")
    source = clean_text(description or "")
    return bool(evidence and evidence in source)


def validate_schema(payload: dict) -> Tuple[bool, str]:
    """Validate and normalize the LLM response before it is saved.

    Enforces the JSON contract from ``build_messages`` and mutates the payload
    in place where harmless normalization is possible.
    """
    if not isinstance(payload, dict):
        return False, "payload not an object"

    if "error" in payload:
        # Preserve only the exact fallback response the prompt allows.
        return payload == {
            "error": "Insufficient information provided"
        }, "error passthrough"

    if not require_json_keys(payload, REQUIRED_KEYS):
        return False, "required keys missing"

    # match_percentage
    mp_raw = payload.get("match_percentage")
    mp = _coerce_int(mp_raw)
    if mp is None or not (0 <= mp <= 100):
        return False, "match_percentage must be integer 0â€“100"
    payload["match_percentage"] = mp  # normalized

    # compensation
    ok, why = validate_compensation(payload.get("compensation"))
    if not ok:
        return False, why

    # fit_summary
    fs = payload.get("fit_summary")
    if not isinstance(fs, str):
        return False, "fit_summary must be string"
    fs = fs.strip()
    if len(fs) == 0 or len(fs) > FIT_SUMMARY_MAX_CHARS or not _is_single_sentence(fs):
        return False, (
            "fit_summary must be a short single sentence "
            f"(<= {FIT_SUMMARY_MAX_CHARS} chars)"
        )
    payload["fit_summary"] = fs

    # keywords_overlap
    ko = _normalize_str_list(payload.get("keywords_overlap"))
    if ko is None:
        return False, "keywords_overlap must be list[str]"
    payload["keywords_overlap"] = ko[:KEYWORD_LIST_LIMIT]

    # missing_keywords
    mk = _normalize_str_list(payload.get("missing_keywords"))
    if mk is None:
        return False, "missing_keywords must be list[str]"
    payload["missing_keywords"] = mk[:KEYWORD_LIST_LIMIT]

    # experience_match
    em = payload.get("experience_match")
    if not isinstance(em, str) or em not in EXP_ALLOWED:
        return False, f"experience_match must be one of {sorted(EXP_ALLOWED)}"

    # location_policy_match
    lm = payload.get("location_policy_match")
    if not isinstance(lm, str) or lm not in LOC_ALLOWED:
        return False, f"location_policy_match must be one of {sorted(LOC_ALLOWED)}"

    return True, ""


def repair_prompt(raw: str) -> str:
    """Build the one-shot correction prompt after invalid model output."""
    return (
        "Your previous reply was invalid. Respond again with ONLY a valid JSON object matching exactly: "
        '{"match_percentage":int,"compensation":object,"fit_summary":string,'
        '"keywords_overlap":[string],"missing_keywords":[string],'
        '"experience_match":"underqualified|qualified|overqualified","location_policy_match":"remote|hybrid|onsite|unknown"}. '
        "No markdown or extra text.\n\n"
        "Invalid previous reply:\n"
        f"{raw[:1200]}"
    )


def fresh_retry_prompt(previous_error: str) -> str:
    """Prompt used when an LLM call failed before returning usable content."""
    return (
        "Try again. Respond with ONLY one valid JSON object matching exactly: "
        '{"match_percentage":int,"compensation":object,"fit_summary":string,'
        '"keywords_overlap":[string],"missing_keywords":[string],'
        '"experience_match":"underqualified|qualified|overqualified",'
        '"location_policy_match":"remote|hybrid|onsite|unknown"}. '
        "No markdown or extra text.\n\n"
        f"Previous error: {previous_error[:500]}"
    )


def should_review_payload(payload: Any) -> bool:
    """Return whether a valid initial-analysis payload should get reviewed."""
    if not AI_SECOND_PASS_REVIEW or not isinstance(payload, dict):
        return False
    score = _coerce_int(payload.get("match_percentage"))
    if score is None:
        return False
    if AI_REVIEW_MIN_MATCH is None and AI_REVIEW_MAX_MATCH is None:
        return True
    lo_raw = 0 if AI_REVIEW_MIN_MATCH is None else AI_REVIEW_MIN_MATCH
    hi_raw = 100 if AI_REVIEW_MAX_MATCH is None else AI_REVIEW_MAX_MATCH
    lo = min(lo_raw, hi_raw)
    hi = max(lo_raw, hi_raw)
    return lo <= score <= hi


def review_prompt(candidate_json: str) -> str:
    """Build the private audit prompt for a valid initial-analysis result."""
    return (
        "Privately review the candidate JSON below against the original resume, job title, and job description. "
        "Audit score calibration, seniority fit, location policy, compensation accuracy, overlapping skills, missing critical skills, and unsupported keyword claims. "
        "If the candidate is already accurate, return the same values. If it is miscalibrated, return corrected values. "
        "Return ONLY one valid JSON object with EXACTLY the same seven keys and no extra fields, markdown, reasoning, notes, or explanations.\n\n"
        "Candidate JSON:\n"
        f"{candidate_json[:2000]}"
    )


def run_second_pass_review(
    base_msgs: List[Any], payload: Dict[str, Any]
) -> Tuple[Dict[str, Any], float, str]:
    """Review a valid initial payload and fall back to it if review fails."""
    if not should_review_payload(payload):
        return payload, 0.0, ""

    candidate_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    t0 = time.time()
    raw = invoke_ollama_json(base_msgs + [HumanMessage(content=review_prompt(candidate_json))])
    took = time.time() - t0
    reviewed = parse_json_strict(raw) or {}
    ok, why = validate_schema(reviewed)
    if ok:
        return reviewed, took, ""
    return payload, took, f"second-pass review ignored: {why}; {_payload_debug(reviewed, raw)}"


def _payload_debug(payload: Any, raw: str) -> str:
    if isinstance(payload, dict):
        keys = ",".join(sorted(str(k) for k in payload.keys()))
        return f"keys=[{keys}] raw={raw[:500]!r}"
    return f"payload_type={type(payload).__name__} raw={raw[:500]!r}"


def build_ai_update_params(
    payload: Dict[str, Any], analyzed_at: Optional[datetime] = None
) -> Dict[str, Any]:
    """Map a validated AI payload to DB update parameters."""
    analyzed_at = analyzed_at or utc_now_naive()
    compensation = payload.get("compensation") or {}
    salary = format_compensation_summary(compensation)
    return {
        "ai_match_percentage": payload.get("match_percentage"),
        "ai_salary": salary,
        "ai_fit_summary": payload.get("fit_summary"),
        "ai_keywords_overlap": json.dumps(
            payload.get("keywords_overlap") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "ai_missing_keywords": json.dumps(
            payload.get("missing_keywords") or [],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "ai_experience_match": payload.get("experience_match"),
        "ai_location_policy_match": payload.get("location_policy_match"),
        "ai_analyzed_at": analyzed_at,
        "base_pay_low": compensation.get("base_pay_low"),
        "base_pay_high": compensation.get("base_pay_high"),
        "pay_currency": compensation.get("pay_currency"),
        "pay_period": compensation.get("pay_period"),
        "ote_low": compensation.get("ote_low"),
        "ote_high": compensation.get("ote_high"),
        "bonus_offered": compensation.get("bonus_offered"),
        "equity_offered": compensation.get("equity_offered"),
        "commission_offered": compensation.get("commission_offered"),
        "multiple_pay_ranges": bool(compensation.get("multiple_pay_ranges")),
        "compensation_text": compensation.get("compensation_text"),
        "compensation_notes": compensation.get("compensation_notes"),
        "compensation_source": "ai",
        "compensation_analyzed_at": analyzed_at,
        "compensation_schema_version": COMPENSATION_SCHEMA_VERSION,
    }


def ai_changed_fields(update_params: Dict[str, Any]) -> str:
    fields = [
        "ai_analysis",
        "ai_match_percentage",
        "ai_salary",
        "ai_fit_summary",
        "ai_keywords_overlap",
        "ai_missing_keywords",
        "ai_experience_match",
        "ai_location_policy_match",
        "ai_analyzed_at",
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
    ]
    return ",".join(fields)


FIT_BRIEF_REQUIRED_KEYS = {
    "score_explanation",
    "strongest_matches",
    "concerns_or_gaps",
    "risk_flags",
    "interview_angle",
    "resume_bullet_matches",
}
FIT_BRIEF_MATCH_KEYS = {
    "resume_bullet",
    "job_requirement",
    "why_it_matters",
    "confidence",
}
FIT_BRIEF_CONFIDENCE = {"high", "medium", "low"}
APPLICATION_PREP_REQUIRED_KEYS = {
    "resume_improvements",
    "draft_resume_bullets",
    "evidence_notes",
    "fit_gaps",
    "positioning_summary",
}
APPLICATION_PREP_BULLET_KEYS = {
    "bullet",
    "source_resume_evidence",
    "job_requirement",
    "confidence",
}
APPLICATION_PREP_EVIDENCE_KEYS = {
    "draft_bullet",
    "note",
}


def stable_text_hash(text_value: str) -> str:
    """Return a stable sha256 for source text used by generated AI artifacts."""
    return hashlib.sha256((text_value or "").encode("utf-8", "ignore")).hexdigest()


def fit_brief_job_hash(task: FitBriefTask) -> str:
    """Use scraper content hash when available, otherwise hash core prompt input."""
    if task.content_hash:
        return task.content_hash
    return stable_text_hash("|".join([task.title or "", task.desc or "", task.ai_analysis or ""]))


def application_prep_job_hash(task: ApplicationPrepTask) -> str:
    if task.content_hash:
        return task.content_hash
    return stable_text_hash("|".join([task.title or "", task.desc or "", task.ai_analysis or ""]))


def render_fit_brief_system_prompt(
    template_path: str = FIT_BRIEF_SYSTEM_PROMPT_PATH,
) -> str:
    """Render the system prompt for applicant-facing fit briefs."""
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Fit Brief system prompt not found: {template_path}")
    with open(template_path, "r", encoding="utf-8") as handle:
        return handle.read().lstrip("\ufeff").strip()


def render_application_prep_system_prompt(
    template_path: str = APPLICATION_PREP_SYSTEM_PROMPT_PATH,
) -> str:
    """Render the system prompt for job-specific application prep."""
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Application Prep system prompt not found: {template_path}")
    with open(template_path, "r", encoding="utf-8") as handle:
        return handle.read().lstrip("\ufeff").strip()


def build_fit_brief_messages(resume_text: str, task: FitBriefTask) -> List[Any]:
    """Build the LLM messages for one high-match Fit Brief."""
    user_msg = (
        "<resume>\n"
        f"{clean_text(resume_text)}\n"
        "</resume>\n\n"
        "<job_title>\n"
        f"{clean_text(task.title)}\n"
        "</job_title>\n\n"
        "<job_description>\n"
        f"{clean_text(task.desc)}\n"
        "</job_description>\n\n"
        "<final_ai_analysis_json>\n"
        f"{clean_text(task.ai_analysis)}\n"
        "</final_ai_analysis_json>"
    )
    return [
        SystemMessage(content=render_fit_brief_system_prompt()),
        HumanMessage(content=user_msg),
    ]


def build_application_prep_messages(resume_text: str, task: ApplicationPrepTask) -> List[Any]:
    """Build the LLM messages for one move-forward application prep."""
    user_msg = (
        "<resume>\n"
        f"{clean_text(resume_text)}\n"
        "</resume>\n\n"
        "<job_title>\n"
        f"{clean_text(task.title)}\n"
        "</job_title>\n\n"
        "<job_description>\n"
        f"{clean_text(task.desc)}\n"
        "</job_description>\n\n"
        "<final_ai_analysis_json>\n"
        f"{clean_text(task.ai_analysis)}\n"
        "</final_ai_analysis_json>"
    )
    return [
        SystemMessage(content=render_application_prep_system_prompt()),
        HumanMessage(content=user_msg),
    ]


def _normalize_short_str_list(value: Any, limit: int = 8) -> Optional[List[str]]:
    items = _normalize_str_list(value)
    if items is None:
        return None
    return [item[:500].strip() for item in items if item.strip()][:limit]


def validate_application_prep_schema(payload: Any, resume_text: str) -> Tuple[bool, str]:
    """Validate and normalize Application Prep JSON before persistence."""
    if not isinstance(payload, dict):
        return False, "payload not an object"
    if set(payload.keys()) != APPLICATION_PREP_REQUIRED_KEYS:
        return False, "application prep keys must exactly match schema"

    positioning = payload.get("positioning_summary")
    if not isinstance(positioning, str) or not positioning.strip():
        return False, "positioning_summary must be a non-empty string"
    payload["positioning_summary"] = positioning.strip()[:1200]

    for key in ("resume_improvements", "fit_gaps"):
        items = _normalize_short_str_list(payload.get(key), limit=8)
        if items is None:
            return False, f"{key} must be list[str]"
        payload[key] = items

    bullets = payload.get("draft_resume_bullets")
    if not isinstance(bullets, list):
        return False, "draft_resume_bullets must be a list"
    normalized_bullets: List[Dict[str, str]] = []
    for item in bullets[:8]:
        if not isinstance(item, dict) or set(item.keys()) != APPLICATION_PREP_BULLET_KEYS:
            return False, "draft_resume_bullets entries must exactly match schema"
        bullet = str(item.get("bullet") or "").strip()
        source = str(item.get("source_resume_evidence") or "").strip()
        requirement = str(item.get("job_requirement") or "").strip()
        confidence = str(item.get("confidence") or "").strip().lower()
        if not bullet or not source or not requirement:
            return False, "draft bullet, source evidence, and job requirement must be non-empty"
        if source not in resume_text:
            return False, "source_resume_evidence must be exact text from resume"
        if confidence not in FIT_BRIEF_CONFIDENCE:
            return False, f"confidence must be one of {sorted(FIT_BRIEF_CONFIDENCE)}"
        normalized_bullets.append(
            {
                "bullet": bullet[:1000],
                "source_resume_evidence": source[:1500],
                "job_requirement": requirement[:1000],
                "confidence": confidence,
            }
        )
    payload["draft_resume_bullets"] = normalized_bullets

    evidence = payload.get("evidence_notes")
    if not isinstance(evidence, list):
        return False, "evidence_notes must be a list"
    normalized_evidence: List[Dict[str, str]] = []
    valid_bullets = {item["bullet"] for item in normalized_bullets}
    for item in evidence[:8]:
        if not isinstance(item, dict) or set(item.keys()) != APPLICATION_PREP_EVIDENCE_KEYS:
            return False, "evidence_notes entries must exactly match schema"
        draft_bullet = str(item.get("draft_bullet") or "").strip()
        note = str(item.get("note") or "").strip()
        if draft_bullet not in valid_bullets:
            return False, "evidence note draft_bullet must match a generated draft bullet"
        if not note:
            return False, "evidence note must be non-empty"
        normalized_evidence.append({"draft_bullet": draft_bullet, "note": note[:1000]})
    payload["evidence_notes"] = normalized_evidence
    return True, ""


def validate_fit_brief_schema(payload: Any, resume_text: str) -> Tuple[bool, str]:
    """Validate and normalize a Fit Brief before persistence."""
    if not isinstance(payload, dict):
        return False, "payload not an object"
    if set(payload.keys()) != FIT_BRIEF_REQUIRED_KEYS:
        return False, "fit brief keys must exactly match schema"

    for key in ("score_explanation", "interview_angle"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            return False, f"{key} must be a non-empty string"
        payload[key] = value.strip()[:1000]

    for key in ("strongest_matches", "concerns_or_gaps", "risk_flags"):
        items = _normalize_short_str_list(payload.get(key))
        if items is None:
            return False, f"{key} must be list[str]"
        payload[key] = items

    matches = payload.get("resume_bullet_matches")
    if not isinstance(matches, list):
        return False, "resume_bullet_matches must be a list"
    normalized_matches: List[Dict[str, str]] = []
    for item in matches[:7]:
        if not isinstance(item, dict) or set(item.keys()) != FIT_BRIEF_MATCH_KEYS:
            return False, "resume_bullet_matches entries must exactly match schema"
        bullet = str(item.get("resume_bullet") or "").strip()
        requirement = str(item.get("job_requirement") or "").strip()
        why = str(item.get("why_it_matters") or "").strip()
        confidence = str(item.get("confidence") or "").strip().lower()
        if not bullet or bullet not in resume_text:
            return False, "resume_bullet must be exact text from resume"
        if not requirement or not why:
            return False, "job_requirement and why_it_matters must be non-empty"
        if confidence not in FIT_BRIEF_CONFIDENCE:
            return False, f"confidence must be one of {sorted(FIT_BRIEF_CONFIDENCE)}"
        normalized_matches.append(
            {
                "resume_bullet": bullet,
                "job_requirement": requirement[:1000],
                "why_it_matters": why[:1000],
                "confidence": confidence,
            }
        )
    payload["resume_bullet_matches"] = normalized_matches
    return True, ""


def fit_brief_repair_prompt(raw: str) -> str:
    """Build the repair prompt for invalid Fit Brief JSON."""
    return (
        "Your previous Fit Brief response was invalid. Return ONLY one JSON object "
        "with exactly these keys: score_explanation, strongest_matches, "
        "concerns_or_gaps, risk_flags, interview_angle, resume_bullet_matches. "
        "Each resume_bullet_matches item must have exactly resume_bullet, "
        "job_requirement, why_it_matters, confidence. resume_bullet must be exact "
        "text copied from the resume. No markdown or extra text.\n\n"
        f"Invalid previous reply:\n{raw[:1500]}"
    )


def application_prep_repair_prompt(raw: str) -> str:
    """Build the repair prompt for invalid Application Prep JSON."""
    return (
        "Your previous Application Prep response was invalid. Return ONLY one JSON object "
        "with exactly these keys: resume_improvements, draft_resume_bullets, "
        "evidence_notes, fit_gaps, positioning_summary. draft_resume_bullets entries "
        "must have exactly bullet, source_resume_evidence, job_requirement, confidence. "
        "source_resume_evidence must be exact text copied from the resume. evidence_notes "
        "entries must have exactly draft_bullet and note, and draft_bullet must match one "
        "generated bullet exactly. No markdown or extra text.\n\n"
        f"Invalid previous reply:\n{raw[:1500]}"
    )


def analyze_application_prep_worker(
    resume_text: str, task: ApplicationPrepTask, index: int
) -> JobResult:
    """Generate and validate one job-specific application prep artifact."""
    if not task.title and not task.desc:
        return JobResult(task.id, task.site, task.job_id, "skip", None, None, 0.0, "Empty title+desc", index)

    msgs = build_application_prep_messages(resume_text, task)
    current_msgs = list(msgs)
    current_num_predict = APPLICATION_PREP_NUM_PREDICT
    took = 0.0
    attempts: List[str] = []
    payload: Dict[str, Any] = {}
    ok = False

    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        raw = ""
        t0 = time.time()
        try:
            raw = invoke_ollama_json(
                current_msgs,
                num_predict=current_num_predict,
                think=APPLICATION_PREP_THINK,
            )
            took += time.time() - t0
            parsed = parse_json_strict(raw) or {}
            ok, why = validate_application_prep_schema(parsed, resume_text)
            if ok:
                payload = parsed
                break
            attempts.append(f"attempt {attempt}: {why}; {_payload_debug(parsed, raw)}")
            current_msgs = msgs + [HumanMessage(content=application_prep_repair_prompt(raw))]
        except OllamaEmptyContentError as exc:
            took += time.time() - t0
            detail = f"attempt {attempt}: {exc}"
            if (
                exc.done_reason == "length"
                and exc.thinking_chars > 0
                and current_num_predict < APPLICATION_PREP_RETRY_NUM_PREDICT
                and attempt < AI_MAX_ATTEMPTS
            ):
                current_num_predict = APPLICATION_PREP_RETRY_NUM_PREDICT
                detail += f"; retrying with num_predict={current_num_predict}"
                attempts.append(detail)
                current_msgs = list(msgs)
                continue
            attempts.append(detail)
            current_msgs = msgs + [
                HumanMessage(content=application_prep_repair_prompt(raw or str(exc)))
            ]
        except OllamaUnavailableError as exc:
            took += time.time() - t0
            return JobResult(task.id, task.site, task.job_id, "ollama_unavailable", None, None, took, str(exc), index)
        except Exception as exc:
            took += time.time() - t0
            attempts.append(f"attempt {attempt}: {exc}")
            current_msgs = msgs + [HumanMessage(content=application_prep_repair_prompt(raw or str(exc)))]

    if not ok:
        return JobResult(
            task.id,
            task.site,
            task.job_id,
            "schema_error",
            None,
            None,
            took,
            " | ".join(attempts),
            index,
        )

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    update_params = {
        "prep_json": payload_json,
        "resume_hash": stable_text_hash(resume_text),
        "job_content_hash": application_prep_job_hash(task),
        "generated_at": utc_now_naive(),
        "schema_version": APPLICATION_PREP_SCHEMA_VERSION,
    }
    return JobResult(task.id, task.site, task.job_id, "ok", payload_json, update_params, took, "", index)


def analyze_fit_brief_worker(
    resume_text: str, task: FitBriefTask, index: int
) -> JobResult:
    """Generate and validate the applicant-facing Fit Brief for one job."""
    if not task.title and not task.desc:
        return JobResult(task.id, task.site, task.job_id, "skip", None, None, 0.0, "Empty title+desc", index)

    msgs = build_fit_brief_messages(resume_text, task)
    current_msgs = list(msgs)
    took = 0.0
    attempts: List[str] = []
    payload: Dict[str, Any] = {}
    ok = False

    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        raw = ""
        t0 = time.time()
        try:
            raw = invoke_ollama_json(current_msgs)
            took += time.time() - t0
            parsed = parse_json_strict(raw) or {}
            ok, why = validate_fit_brief_schema(parsed, resume_text)
            if ok:
                payload = parsed
                break
            attempts.append(f"attempt {attempt}: {why}; {_payload_debug(parsed, raw)}")
            current_msgs = msgs + [HumanMessage(content=fit_brief_repair_prompt(raw))]
        except OllamaUnavailableError as exc:
            took += time.time() - t0
            return JobResult(task.id, task.site, task.job_id, "ollama_unavailable", None, None, took, str(exc), index)
        except Exception as exc:
            took += time.time() - t0
            attempts.append(f"attempt {attempt}: {exc}")
            current_msgs = msgs + [HumanMessage(content=fit_brief_repair_prompt(raw or str(exc)))]

    if not ok:
        return JobResult(
            task.id,
            task.site,
            task.job_id,
            "schema_error",
            None,
            None,
            took,
            " | ".join(attempts),
            index,
        )

    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    update_params = {
        "brief_json": payload_json,
        "resume_hash": stable_text_hash(resume_text),
        "job_content_hash": fit_brief_job_hash(task),
        "generated_at": utc_now_naive(),
        "schema_version": FIT_BRIEF_SCHEMA_VERSION,
    }
    return JobResult(task.id, task.site, task.job_id, "ok", payload_json, update_params, took, "", index)



def should_expand_thinking_budget(
    exc: OllamaEmptyContentError, current_num_predict: int
) -> bool:
    """Return whether a reasoning-only response should get a larger retry."""
    return (
        exc.done_reason == "length"
        and exc.thinking_chars > 0
        and current_num_predict < AI_THINKING_RETRY_NUM_PREDICT
    )


def analyze_job_worker(resume_text: str, task: JobTask, index: int) -> JobResult:
    """Run LLM analysis for one job and return normalized worker output."""
    if not task.title and not task.desc:
        return JobResult(
            id=task.id,
            site=task.site,
            job_id=task.job_id,
            status="skip",
            payload_json=None,
            update_params=None,
            llm_seconds=0.0,
            error_text="Empty title+desc",
            index=index,
        )

    bounded_desc, tokenish, _ = truncate_to_token_budget(task.desc, MAX_JOB_DESC_TOKENS)
    deterministic = choose_deterministic_compensation("", task.desc)
    evidence = deterministic.get("compensation_text")
    if evidence and clean_text(evidence) not in bounded_desc:
        # Keep end-of-posting compensation visible even when the match-analysis
        # description budget otherwise keeps only the beginning of a long post.
        bounded_desc = f"{bounded_desc}\nCompensation evidence: {evidence}"
    msgs = build_messages(resume_text, task.title, bounded_desc)

    took = 0.0
    attempts: List[str] = []
    warnings: List[str] = []
    data: Dict[str, Any] = {}
    ok = False
    current_msgs = list(msgs)
    current_num_predict = OLLAMA_NUM_PREDICT

    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        raw = ""
        t0 = time.time()
        try:
            raw = invoke_ollama_json(current_msgs, num_predict=current_num_predict)
            took += time.time() - t0
            parsed = parse_json_strict(raw) or {}
            ok, why = validate_schema(parsed)
            if ok and not compensation_evidence_matches(parsed["compensation"], task.desc):
                ok, why = False, "compensation_text must be an exact excerpt from the job description"
            if ok:
                data = parsed
                break
            detail = f"attempt {attempt}: {why}; {_payload_debug(parsed, raw)}"
            attempts.append(detail)
            current_msgs = msgs + [HumanMessage(content=repair_prompt(raw))]
        except OllamaEmptyContentError as e:
            took += time.time() - t0
            detail = f"attempt {attempt}: {e}"
            if should_expand_thinking_budget(e, current_num_predict) and attempt < AI_MAX_ATTEMPTS:
                current_num_predict = AI_THINKING_RETRY_NUM_PREDICT
                detail += f"; retrying with num_predict={current_num_predict}"
                attempts.append(detail)
                warnings.append(detail)
                current_msgs = list(msgs)
                continue
            attempts.append(detail)
            current_msgs = msgs + [HumanMessage(content=fresh_retry_prompt(str(e)))]
        except OllamaUnavailableError as e:
            took += time.time() - t0
            return JobResult(
                id=task.id,
                site=task.site,
                job_id=task.job_id,
                status="ollama_unavailable",
                payload_json=None,
                update_params=None,
                llm_seconds=took,
                error_text=str(e),
                index=index,
            )
        except Exception as e:
            took += time.time() - t0
            detail = f"attempt {attempt}: {e}"
            attempts.append(detail)
            current_msgs = msgs + [HumanMessage(content=fresh_retry_prompt(str(e)))]
    if not ok:
        error_text = " | ".join(attempts)
        return JobResult(
            id=task.id,
            site=task.site,
            job_id=task.job_id,
            status="schema_error",
            payload_json=None,
            update_params=None,
            llm_seconds=took,
            error_text=error_text,
            index=index,
        )

    try:
        data, review_took, review_warning = run_second_pass_review(msgs, data)
        took += review_took
        if review_warning:
            warnings.append(review_warning)
    except OllamaUnavailableError as e:
        return JobResult(
            id=task.id,
            site=task.site,
            job_id=task.job_id,
            status="ollama_unavailable",
            payload_json=None,
            update_params=None,
            llm_seconds=took,
            error_text=str(e),
            index=index,
        )
    except Exception as e:
        warnings.append(f"second-pass review failed: {e}")

    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    update_params = build_ai_update_params(data)
    why = ""
    if tokenish > TOKEN_THRESHOLD:
        warnings.append(f"Long desc (~{tokenish} tokens)")
    if warnings:
        why = " | ".join(warnings)

    return JobResult(
        id=task.id,
        site=task.site,
        job_id=task.job_id,
        status="ok",
        payload_json=compact,
        update_params=update_params,
        llm_seconds=took,
        error_text=why,
        index=index,
    )


# -------------------------------
# Query helpers
# -------------------------------
def select_job_ids(session: Session, redo: Optional[int] = None) -> List[int]:
    """Return candidate job primary keys based on filters and optional redo mode."""
    where = ["is_active = 1"]
    params: Dict[str, Any] = {}
    if ONLY_EMPTY and redo is None:
        where.append("(ai_analysis IS NULL OR ai_analysis = '')")
    if SITE_FILTER:
        sites = [s.strip() for s in SITE_FILTER.split(",") if s.strip()]
        if sites:
            in_parts = []
            for i, val in enumerate(sites):
                pname = f"site{i}"
                params[pname] = val
                in_parts.append(f":{pname}")
            where.append(f"site IN ({', '.join(in_parts)})")
    where_sql = " AND ".join(where) if where else "1=1"
    if redo is not None:
        params["redo"] = redo
        limit_sql = " ORDER BY discovery_date DESC, id DESC LIMIT :redo"
    else:
        limit_sql = f" LIMIT {LIMIT}" if LIMIT > 0 else ""
    # SITE_FILTER values are bound parameters; LIMIT is parsed as int at import.
    sql = f"SELECT id FROM jobs WHERE {where_sql}{limit_sql}"
    rows = session.execute(sa_text(sql), params).fetchall()
    return [r[0] for r in rows]


def build_compensation_messages(title: str, desc: str) -> List[Any]:
    """Build a focused prompt from compensation-bearing description lines."""
    with open(COMPENSATION_SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as handle:
        system_prompt = handle.read().strip()
    if not system_prompt:
        raise RuntimeError("compensation system prompt is empty")

    # Compensation is commonly near the beginning or end. Keep those portions
    # plus every sentence with an explicit compensation signal.
    cleaned = clean_text(desc)
    signal = re.compile(
        r"[$€£Ł]|\b(?:salary|compensation|pay range|hourly|OTE|on-target|bonus|equity|commission)\b",
        re.IGNORECASE,
    )
    signal_parts = [
        sentence
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", cleaned)
        if signal.search(sentence)
    ]
    # Put explicit signals first so the configured token cap cannot trim them
    # behind general role text.
    parts = [*signal_parts, cleaned[:1200], cleaned[-4000:]]
    seen = set()
    selected = []
    for part in parts:
        item = part.strip()
        if item and item not in seen:
            seen.add(item)
            selected.append(item)
    context, _, _ = truncate_to_token_budget("\n".join(selected), MAX_JOB_DESC_TOKENS)
    user = (
        "<job_title>\n"
        f"{clean_text(title)}\n"
        "</job_title>\n\n"
        "<job_description>\n"
        f"{context}\n"
        "</job_description>"
    )
    return [SystemMessage(content=system_prompt), HumanMessage(content=user)]


def analyze_compensation_worker(task: JobTask, index: int) -> JobResult:
    """Extract and validate only compensation for one job."""
    if not task.title and not task.desc:
        return JobResult(
            id=task.id,
            site=task.site,
            job_id=task.job_id,
            status="skip",
            payload_json=None,
            update_params=None,
            llm_seconds=0.0,
            error_text="Empty title+desc",
            index=index,
        )
    messages = build_compensation_messages(task.title, task.desc)
    attempts = []
    took = 0.0
    current = list(messages)
    for attempt in range(1, AI_MAX_ATTEMPTS + 1):
        started = time.time()
        raw = ""
        try:
            raw = invoke_ollama_json(current)
            took += time.time() - started
            payload = parse_json_strict(raw) or {}
            ok, why = validate_compensation(payload)
            if ok and not compensation_evidence_matches(payload, task.desc):
                ok, why = False, "compensation_text must be an exact excerpt from the job description"
            if ok:
                analyzed_at = utc_now_naive()
                update = {
                    **payload,
                    "ai_salary": format_compensation_summary(payload),
                    "compensation_source": "ai",
                    "compensation_analyzed_at": analyzed_at,
                    "compensation_schema_version": COMPENSATION_SCHEMA_VERSION,
                }
                return JobResult(
                    id=task.id,
                    site=task.site,
                    job_id=task.job_id,
                    status="ok",
                    payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    update_params=update,
                    llm_seconds=took,
                    error_text="",
                    index=index,
                )
            attempts.append(f"attempt {attempt}: {why}")
            current = messages + [
                HumanMessage(
                    content="Your previous response was invalid. Return only the exact compensation JSON object."
                )
            ]
        except OllamaUnavailableError as exc:
            took += time.time() - started
            return JobResult(
                id=task.id,
                site=task.site,
                job_id=task.job_id,
                status="ollama_unavailable",
                payload_json=None,
                update_params=None,
                llm_seconds=took,
                error_text=str(exc),
                index=index,
            )
        except Exception as exc:
            took += time.time() - started
            attempts.append(f"attempt {attempt}: {exc}")
            current = list(messages)
    return JobResult(
        id=task.id,
        site=task.site,
        job_id=task.job_id,
        status="schema_error",
        payload_json=None,
        update_params=None,
        llm_seconds=took,
        error_text=" | ".join(attempts),
        index=index,
    )


def select_compensation_jobs(session: Session, args: argparse.Namespace) -> List[Job]:
    """Select active jobs while honoring resume/version controls."""
    stmt = select(Job).where(Job.is_active.is_(True))
    if not args.force:
        stmt = stmt.where(
            (Job.compensation_schema_version.is_(None))
            | (Job.compensation_schema_version != COMPENSATION_SCHEMA_VERSION)
        )
    if SITE_FILTER:
        sites = [value.strip() for value in SITE_FILTER.split(",") if value.strip()]
        if sites:
            stmt = stmt.where(Job.site.in_(sites))
    jobs = list(
        session.execute(stmt.order_by(Job.site, Job.discovery_date.desc(), Job.id.desc()))
        .scalars()
        .all()
    )
    if args.sample_per_site:
        counts: Dict[str, int] = {}
        sample = []
        for job in jobs:
            site = job.site or ""
            if counts.get(site, 0) >= args.sample_per_site:
                continue
            counts[site] = counts.get(site, 0) + 1
            sample.append(job)
        return sample
    return jobs


def _reference_compensation_values(job: Job) -> List[str]:
    try:
        refs = json.loads(job.reference_fields or "{}")
    except Exception:
        return []
    if not isinstance(refs, dict):
        return []
    return [
        str(value)
        for key, value in refs.items()
        if any(part in str(key).lower() for part in ("salary", "pay", "compensation"))
    ]


def _content_hash_with_pay(job: Job, pay: str) -> str:
    """Keep the scraper's canonical hash aligned after deterministic Pay cleanup."""
    canon = "|".join(
        [
            job.title or "",
            job.url or "",
            job.desc or "",
            job.keywords or "",
            job.level or "",
            pay,
            job.reference_fields or "",
        ]
    ).encode("utf-8", "ignore")
    return hashlib.sha256(canon).hexdigest()


def run_compensation_backfill(args: argparse.Namespace) -> None:
    """Run the resumable compensation-only cleanup and commit one job at a time."""
    init_db()
    with SessionLocal() as session:
        jobs = select_compensation_jobs(session, args)
        tasks = [
            JobTask(
                id=job.id,
                site=job.site or "",
                job_id=job.job_id or "",
                title=job.title or "",
                desc=job.desc or "",
                pay=job.pay or "",
                run_id=job.run_id,
                content_hash=job.content_hash,
            )
            for job in jobs
        ]
        jobs_by_id = {job.id: job for job in jobs}

        totals = {
            "processed": 0,
            "no_compensation": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
            "unsubmitted": 0,
        }
        log(
            f"Compensation-only jobs={len(tasks)} dry_run={args.dry_run} "
            f"force={args.force} schema_version={COMPENSATION_SCHEMA_VERSION}"
        )

        if not tasks:
            log(f"Compensation-only complete: {totals}")
            return

        try:
            check_ollama_prerequisites()
        except OllamaPrerequisiteError as exc:
            log(f"[fatal] {exc}")
            raise SystemExit(1) from exc

        futures: Set[Future] = set()
        future_to_task: Dict[Future, JobTask] = {}
        submitted = 0
        aborted = False

        with ThreadPoolExecutor(max_workers=AI_CONCURRENCY) as executor:
            while (not aborted and submitted < len(tasks)) or futures:
                while (
                    not aborted
                    and submitted < len(tasks)
                    and len(futures) < AI_MAX_INFLIGHT
                ):
                    task = tasks[submitted]
                    index = submitted + 1
                    log(
                        f"[submit] [Job {index}/{len(tasks)}] "
                        f"{task.site}:{task.job_id} title={task.title[:80]!r}"
                    )
                    future = executor.submit(analyze_compensation_worker, task, index)
                    futures.add(future)
                    future_to_task[future] = task
                    submitted += 1

                done, _ = wait(
                    futures,
                    timeout=AI_WAIT_HEARTBEAT_SEC,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    log(
                        f"[wait] compensation-only submitted={submitted}/{len(tasks)} "
                        f"inflight={len(futures)}"
                    )
                    continue

                for future in done:
                    futures.remove(future)
                    task = future_to_task.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = JobResult(
                            id=task.id,
                            site=task.site,
                            job_id=task.job_id,
                            status="llm_error",
                            payload_json=None,
                            update_params=None,
                            llm_seconds=0.0,
                            error_text=f"worker crash: {exc}",
                            index=0,
                        )

                    if result.status == "ollama_unavailable":
                        totals["failed"] += 1
                        if not aborted:
                            aborted = True
                            log(f"[fatal] {result.error_text}; aborting batch")
                            # Futures that have not started can be cancelled.
                            # Running requests are left to finish and are drained.
                            for pending in list(futures):
                                if pending.cancel():
                                    futures.remove(pending)
                                    future_to_task.pop(pending, None)
                                    totals["cancelled"] += 1
                        continue

                    if result.status == "skip":
                        totals["skipped"] += 1
                        log(f"[skip] {result.site}:{result.job_id} {result.error_text}")
                        continue
                    if result.status != "ok" or result.update_params is None:
                        totals["failed"] += 1
                        log(f"[err] {result.site}:{result.job_id} {result.error_text}")
                        continue

                    job = jobs_by_id[result.id]
                    ai_data = result.update_params
                    session.refresh(job)
                    if not job.is_active:
                        totals["skipped"] += 1
                        log(
                            f"[skip] {job.site}:{job.job_id} became inactive before compensation write"
                        )
                        continue
                    deterministic = choose_deterministic_compensation(
                        "",
                        job.desc or "",
                        _reference_compensation_values(job),
                    )
                    new_pay = format_compensation_summary(deterministic) or "Unknown"
                    if args.dry_run:
                        label = ai_data.get("ai_salary") or "No compensation"
                        log(
                            f"[dry-run] {job.site}:{job.job_id} "
                            f"Pay={new_pay!r} AI Salary={label!r}"
                        )
                    else:
                        before = {"pay": job.pay, "ai_salary": job.ai_salary}
                        old_hash = job.content_hash
                        job.pay = new_pay
                        job.ai_salary = ai_data.get("ai_salary")
                        for field in COMPENSATION_KEYS:
                            setattr(job, field, ai_data.get(field))
                        job.compensation_source = ai_data["compensation_source"]
                        job.compensation_analyzed_at = ai_data["compensation_analyzed_at"]
                        job.compensation_schema_version = ai_data[
                            "compensation_schema_version"
                        ]
                        job.content_hash = _content_hash_with_pay(job, new_pay)
                        changed = {
                            "fields": [
                                "pay",
                                "ai_salary",
                                *sorted(COMPENSATION_KEYS),
                                "compensation_source",
                                "compensation_analyzed_at",
                                "compensation_schema_version",
                            ],
                            "before": before,
                            "after": {"pay": job.pay, "ai_salary": job.ai_salary},
                        }
                        session.add(
                            JobChange(
                                run_id=job.run_id,
                                job_id_text=job.job_id,
                                site=job.site,
                                job_pk=job.id,
                                change_type="update",
                                change_source="ai",
                                old_hash=old_hash,
                                new_hash=job.content_hash,
                                changed_fields=json.dumps(
                                    changed,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            )
                        )
                        try:
                            session.commit()
                        except Exception as exc:
                            session.rollback()
                            totals["failed"] += 1
                            log(
                                f"[err] {job.site}:{job.job_id} "
                                f"DB commit failed: {exc}"
                            )
                            continue
                    totals["processed"] += 1
                    if not compensation_has_values(ai_data):
                        totals["no_compensation"] += 1

        totals["unsubmitted"] = len(tasks) - submitted
        log(f"Compensation-only complete: {totals}")
        if aborted:
            raise SystemExit(1)


def select_fit_brief_jobs(
    session: Session, resume_text: str, *, force: bool = False
) -> List[Tuple[Job, Optional[JobFitBrief]]]:
    """Select active reviewed matches that need an applicant-facing Fit Brief."""
    resume_hash = stable_text_hash(resume_text)
    stmt = (
        select(Job, JobFitBrief)
        .outerjoin(JobFitBrief, JobFitBrief.job_pk == Job.id)
        .where(Job.is_active.is_(True))
        .where(Job.ai_match_percentage.is_not(None))
        .where(Job.ai_match_percentage >= AI_FIT_BRIEF_MIN_MATCH)
        .where(or_(Job.title != "", Job.desc != ""))
        .order_by(Job.ai_match_percentage.desc(), Job.discovery_date.desc(), Job.id.desc())
    )
    rows = list(session.execute(stmt).all())
    if force:
        return rows

    selected: List[Tuple[Job, Optional[JobFitBrief]]] = []
    for job, brief in rows:
        current_job_hash = job.content_hash or stable_text_hash(
            "|".join([job.title or "", job.desc or "", job.ai_analysis or ""])
        )
        if brief is None:
            selected.append((job, brief))
            continue
        if (
            brief.resume_hash != resume_hash
            or brief.job_content_hash != current_job_hash
            or brief.schema_version != FIT_BRIEF_SCHEMA_VERSION
        ):
            selected.append((job, brief))
    return selected


def run_fit_brief_generation(
    resume_text: str,
    *,
    force: bool = False,
    preflight: bool = True,
) -> Dict[str, int]:
    """Run Stage 3 Fit Brief generation for eligible high-match jobs."""
    init_db()
    totals = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "cancelled": 0,
        "unsubmitted": 0,
    }
    with SessionLocal() as session:
        rows = select_fit_brief_jobs(session, resume_text, force=force)
        tasks = [
            FitBriefTask(
                id=job.id,
                site=job.site or "",
                job_id=job.job_id or "",
                title=job.title or "",
                desc=job.desc or "",
                ai_analysis=job.ai_analysis or "",
                ai_match_percentage=job.ai_match_percentage,
                content_hash=job.content_hash,
                existing_resume_hash=getattr(brief, "resume_hash", None),
                existing_job_content_hash=getattr(brief, "job_content_hash", None),
            )
            for job, brief in rows
        ]
        log(
            f"Stage 3 Fit Brief jobs={len(tasks)} min_match={AI_FIT_BRIEF_MIN_MATCH} "
            f"force={force} schema_version={FIT_BRIEF_SCHEMA_VERSION}"
        )
        if not tasks:
            log(f"Stage 3 Fit Brief complete: {totals}")
            return totals

        if preflight:
            try:
                check_ollama_prerequisites()
            except OllamaPrerequisiteError as exc:
                log(f"[fatal] {exc}")
                raise SystemExit(1) from exc

        futures: Set[Future] = set()
        future_to_task: Dict[Future, FitBriefTask] = {}
        submitted = 0
        aborted = False

        with ThreadPoolExecutor(max_workers=AI_CONCURRENCY) as executor:
            while (not aborted and submitted < len(tasks)) or futures:
                while (
                    not aborted
                    and submitted < len(tasks)
                    and len(futures) < AI_MAX_INFLIGHT
                ):
                    task = tasks[submitted]
                    index = submitted + 1
                    log(
                        f"[submit] [Fit Brief {index}/{len(tasks)}] "
                        f"{task.site}:{task.job_id} title={task.title[:80]!r}"
                    )
                    future = executor.submit(analyze_fit_brief_worker, resume_text, task, index)
                    futures.add(future)
                    future_to_task[future] = task
                    submitted += 1

                done, _ = wait(
                    futures,
                    timeout=AI_WAIT_HEARTBEAT_SEC,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    log(
                        f"[wait] fit-briefs submitted={submitted}/{len(tasks)} "
                        f"inflight={len(futures)}"
                    )
                    continue

                for future in done:
                    futures.remove(future)
                    task = future_to_task.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = JobResult(
                            id=task.id,
                            site=task.site,
                            job_id=task.job_id,
                            status="llm_error",
                            payload_json=None,
                            update_params=None,
                            llm_seconds=0.0,
                            error_text=f"worker crash: {exc}",
                            index=0,
                        )

                    if result.status == "ollama_unavailable":
                        totals["failed"] += 1
                        if not aborted:
                            aborted = True
                            log(f"[fatal] {result.error_text}; aborting fit-brief batch")
                            for pending in list(futures):
                                if pending.cancel():
                                    futures.remove(pending)
                                    future_to_task.pop(pending, None)
                                    totals["cancelled"] += 1
                        continue
                    if result.status == "skip":
                        totals["skipped"] += 1
                        log(f"[skip] {result.site}:{result.job_id} {result.error_text}")
                        continue
                    if result.status != "ok" or result.update_params is None:
                        totals["failed"] += 1
                        log(f"[err] Fit Brief {result.site}:{result.job_id} {result.error_text}")
                        continue

                    brief = (
                        session.execute(
                            select(JobFitBrief).where(JobFitBrief.job_pk == result.id)
                        )
                        .scalars()
                        .one_or_none()
                    )
                    if brief is None:
                        brief = JobFitBrief(job_pk=result.id, brief_json="{}")
                        session.add(brief)
                    brief.brief_json = result.update_params["brief_json"]
                    brief.resume_hash = result.update_params["resume_hash"]
                    brief.job_content_hash = result.update_params["job_content_hash"]
                    brief.generated_at = result.update_params["generated_at"]
                    brief.schema_version = result.update_params["schema_version"]
                    try:
                        session.commit()
                    except Exception as exc:
                        session.rollback()
                        totals["failed"] += 1
                        log(f"[err] Fit Brief DB commit failed: {exc}")
                        continue
                    totals["processed"] += 1
                    log(f"[ok] Fit Brief committed {result.site}:{result.job_id} in {result.llm_seconds:.1f}s")

        totals["unsubmitted"] = len(tasks) - submitted
        log(f"Stage 3 Fit Brief complete: {totals}")
        if aborted:
            raise SystemExit(1)
        return totals


def select_application_prep_jobs(
    session: Session, resume_text: str, *, force: bool = False
) -> List[Tuple[Job, JobApplicationPrep]]:
    """Select liked move-forward jobs that need application-prep generation."""
    resume_hash = stable_text_hash(resume_text)
    rows = list(
        session.execute(
            select(Job, JobApplicationPrep)
            .join(JobApplicationPrep, JobApplicationPrep.job_pk == Job.id)
            .join(JobSwipe, JobSwipe.job_pk == Job.id)
            .where(JobSwipe.action == "like")
            .where(or_(Job.title != "", Job.desc != ""))
            .order_by(JobApplicationPrep.queued_at.desc(), Job.id.desc())
        ).all()
    )
    if force:
        return rows

    selected: List[Tuple[Job, JobApplicationPrep]] = []
    for job, prep in rows:
        current_job_hash = job.content_hash or stable_text_hash(
            "|".join([job.title or "", job.desc or "", job.ai_analysis or ""])
        )
        status = (prep.status or "queued").strip().lower()
        if status in {"queued", "stale"}:
            selected.append((job, prep))
            continue
        if (
            status == "done"
            and (
                prep.resume_hash != resume_hash
                or prep.job_content_hash != current_job_hash
                or prep.schema_version != APPLICATION_PREP_SCHEMA_VERSION
            )
        ):
            prep.status = "stale"
            selected.append((job, prep))
    if selected:
        session.commit()
    return selected


def run_application_prep_generation(
    *,
    resume_text: Optional[str] = None,
    force: bool = False,
    preflight: bool = True,
) -> Dict[str, int]:
    """Run high-priority application prep for liked move-forward jobs."""
    init_db()
    if resume_text is None:
        if not os.path.exists(RESUME_PATH):
            log(f"[err] Resume not found: {RESUME_PATH}")
            raise SystemExit(1)
        with open(RESUME_PATH, "r", encoding="utf-8", errors="ignore") as f:
            resume_text = f.read()

    totals = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "cancelled": 0,
        "unsubmitted": 0,
    }
    with SessionLocal() as session:
        rows = select_application_prep_jobs(session, resume_text, force=force)
        tasks: List[ApplicationPrepTask] = []
        now = utc_now_naive()
        for job, prep in rows:
            prep.status = "running"
            prep.started_at = now
            prep.error_text = None
            tasks.append(
                ApplicationPrepTask(
                    id=job.id,
                    prep_id=prep.id,
                    site=job.site or "",
                    job_id=job.job_id or "",
                    title=job.title or "",
                    desc=job.desc or "",
                    ai_analysis=job.ai_analysis or "",
                    ai_match_percentage=job.ai_match_percentage,
                    content_hash=job.content_hash,
                )
            )
        if tasks:
            session.commit()

        log(
            f"Application Prep jobs={len(tasks)} force={force} "
            f"schema_version={APPLICATION_PREP_SCHEMA_VERSION}"
        )
        if not tasks:
            log(f"Application Prep complete: {totals}")
            return totals

        if preflight:
            try:
                check_ollama_prerequisites()
            except OllamaPrerequisiteError as exc:
                for task in tasks:
                    prep = session.get(JobApplicationPrep, task.prep_id)
                    if prep is not None:
                        prep.status = "failed"
                        prep.error_text = str(exc)
                session.commit()
                log(f"[fatal] {exc}")
                raise SystemExit(1) from exc

        futures: Set[Future] = set()
        future_to_task: Dict[Future, ApplicationPrepTask] = {}
        submitted = 0
        aborted = False

        with ThreadPoolExecutor(max_workers=AI_CONCURRENCY) as executor:
            while (not aborted and submitted < len(tasks)) or futures:
                while (
                    not aborted
                    and submitted < len(tasks)
                    and len(futures) < AI_MAX_INFLIGHT
                ):
                    task = tasks[submitted]
                    index = submitted + 1
                    log(
                        f"[submit] [Application Prep {index}/{len(tasks)}] "
                        f"{task.site}:{task.job_id} title={task.title[:80]!r}"
                    )
                    future = executor.submit(analyze_application_prep_worker, resume_text, task, index)
                    futures.add(future)
                    future_to_task[future] = task
                    submitted += 1

                done, _ = wait(
                    futures,
                    timeout=AI_WAIT_HEARTBEAT_SEC,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    log(
                        f"[wait] application-prep submitted={submitted}/{len(tasks)} "
                        f"inflight={len(futures)}"
                    )
                    continue

                for future in done:
                    futures.remove(future)
                    task = future_to_task.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = JobResult(
                            id=task.id,
                            site=task.site,
                            job_id=task.job_id,
                            status="llm_error",
                            payload_json=None,
                            update_params=None,
                            llm_seconds=0.0,
                            error_text=f"worker crash: {exc}",
                            index=0,
                        )

                    prep = session.get(JobApplicationPrep, task.prep_id)
                    if prep is None:
                        totals["failed"] += 1
                        log(f"[err] Application Prep row missing for {task.site}:{task.job_id}")
                        continue

                    if result.status == "ollama_unavailable":
                        totals["failed"] += 1
                        prep.status = "failed"
                        prep.error_text = result.error_text
                        session.commit()
                        if not aborted:
                            aborted = True
                            log(f"[fatal] {result.error_text}; aborting application-prep batch")
                            for pending in list(futures):
                                if pending.cancel():
                                    futures.remove(pending)
                                    future_to_task.pop(pending, None)
                                    totals["cancelled"] += 1
                        continue
                    if result.status == "skip":
                        totals["skipped"] += 1
                        prep.status = "failed"
                        prep.error_text = result.error_text
                        session.commit()
                        log(f"[skip] {result.site}:{result.job_id} {result.error_text}")
                        continue
                    if result.status != "ok" or result.update_params is None:
                        totals["failed"] += 1
                        prep.status = "failed"
                        prep.error_text = result.error_text
                        session.commit()
                        log(f"[err] Application Prep {result.site}:{result.job_id} {result.error_text}")
                        continue

                    prep.prep_json = result.update_params["prep_json"]
                    prep.resume_hash = result.update_params["resume_hash"]
                    prep.job_content_hash = result.update_params["job_content_hash"]
                    prep.generated_at = result.update_params["generated_at"]
                    prep.schema_version = result.update_params["schema_version"]
                    prep.status = "done"
                    prep.error_text = None
                    try:
                        session.commit()
                    except Exception as exc:
                        session.rollback()
                        totals["failed"] += 1
                        log(f"[err] Application Prep DB commit failed: {exc}")
                        continue
                    totals["processed"] += 1
                    log(f"[ok] Application Prep committed {result.site}:{result.job_id} in {result.llm_seconds:.1f}s")

        totals["unsubmitted"] = len(tasks) - submitted
        log(f"Application Prep complete: {totals}")
        if aborted:
            raise SystemExit(1)
        return totals


# -------------------------------
# Main
# -------------------------------
def main() -> None:
    """Run the batch analysis workflow and persist valid model responses."""
    args = parse_args()
    signal.signal(signal.SIGINT, _handle_sigint)
    if getattr(args, "compensation_only", False):
        run_compensation_backfill(args)
        return
    # Regular analysis also writes the structured compensation columns.
    init_db()
    start = datetime.now()
    log("------------------------------------------------------")
    log(f"AI Job Analysis started {start.strftime('%Y-%m-%d %H:%M:%S')}")
    log(
        f"Model={OLLAMA_MODEL} | ONLY_EMPTY={ONLY_EMPTY} | SITE_FILTER='{SITE_FILTER}' | LIMIT={LIMIT} | REDO={args.redo if args.redo is not None else 'off'}"
    )
    log(
        f"Prompt budgets: resume={token_budget_label(MAX_RESUME_TOKENS)} | job_desc={token_budget_label(MAX_JOB_DESC_TOKENS)}"
    )
    log(
        f"Ollama options: think={OLLAMA_THINK!r} | num_ctx={OLLAMA_NUM_CTX} | num_predict={OLLAMA_NUM_PREDICT} | thinking_retry_num_predict={AI_THINKING_RETRY_NUM_PREDICT} | keep_alive={OLLAMA_KEEP_ALIVE}"
    )
    log(
        f"Parallelism: workers={AI_CONCURRENCY} | max_inflight={AI_MAX_INFLIGHT} | progress_every={AI_BATCH_LOG_EVERY}"
    )
    log(
        f"Timeouts: preflight={OLLAMA_PREFLIGHT_TIMEOUT_SEC}s | "
        f"request_timeout={AI_REQUEST_TIMEOUT_SEC}s | heartbeat={AI_WAIT_HEARTBEAT_SEC}s"
    )
    log("------------------------------------------------------")

    if not os.path.exists(RESUME_PATH):
        log(f"[err] Resume not found: {RESUME_PATH}")
        sys.exit(1)

    with open(RESUME_PATH, "r", encoding="utf-8", errors="ignore") as f:
        resume_text = f.read()

    bounded_resume_text, resume_tokens, resume_truncated = truncate_to_token_budget(
        resume_text, MAX_RESUME_TOKENS
    )
    if resume_truncated:
        log(
            f"[warn] Resume truncated from ~{resume_tokens} to ~{MAX_RESUME_TOKENS} tokens for prompt budget"
        )

    if getattr(args, "fit_briefs_only", False):
        run_fit_brief_generation(
            resume_text,
            force=bool(getattr(args, "force", False)),
            preflight=True,
        )
        return
    if getattr(args, "application_prep_only", False):
        run_application_prep_generation(
            resume_text=resume_text,
            force=bool(getattr(args, "force", False)),
            preflight=True,
        )
        return

    processed = 0
    failures = 0
    skipped = 0
    cancelled = 0
    completed = 0
    llm_seconds_total = 0.0
    aborted = False

    with SessionLocal() as s:
        inspector = sa_inspect(s.bind)
        job_changes_cols = {c["name"] for c in inspector.get_columns("job_changes")}
        has_change_source = "change_source" in job_changes_cols

        ids = select_job_ids(s, redo=args.redo)
        total = len(ids)
        if total == 0:
            log("[ok] No jobs matched filter criteria.")
            run_application_prep_generation(
                resume_text=resume_text,
                force=False,
                preflight=True,
            )
            run_fit_brief_generation(resume_text, force=False, preflight=True)
            return

        jobs = list(s.execute(select(Job).where(Job.id.in_(ids))).scalars())
        tasks: List[JobTask] = [
            JobTask(
                id=j.id,
                site=j.site or "",
                job_id=j.job_id or "",
                title=j.title or "",
                desc=j.desc or "",
                pay=j.pay or "",
                run_id=j.run_id,
                content_hash=j.content_hash,
            )
            for j in jobs
        ]

        try:
            check_ollama_prerequisites()
        except OllamaPrerequisiteError as exc:
            log(f"[fatal] {exc}")
            raise SystemExit(1) from exc

        run_application_prep_generation(
            resume_text=resume_text,
            force=False,
            preflight=False,
        )

        upd = sa_text(
            """
            UPDATE jobs
            SET ai_analysis = :val,
                ai_match_percentage = :ai_match_percentage,
                ai_salary = :ai_salary,
                ai_fit_summary = :ai_fit_summary,
                ai_keywords_overlap = :ai_keywords_overlap,
                ai_missing_keywords = :ai_missing_keywords,
                ai_experience_match = :ai_experience_match,
                ai_location_policy_match = :ai_location_policy_match,
                ai_analyzed_at = :ai_analyzed_at,
                base_pay_low = :base_pay_low,
                base_pay_high = :base_pay_high,
                pay_currency = :pay_currency,
                pay_period = :pay_period,
                ote_low = :ote_low,
                ote_high = :ote_high,
                bonus_offered = :bonus_offered,
                equity_offered = :equity_offered,
                commission_offered = :commission_offered,
                multiple_pay_ranges = :multiple_pay_ranges,
                compensation_text = :compensation_text,
                compensation_notes = :compensation_notes,
                compensation_source = :compensation_source,
                compensation_analyzed_at = :compensation_analyzed_at,
                compensation_schema_version = :compensation_schema_version
            WHERE id = :id
              AND is_active = 1
            """
        )
        if has_change_source:
            ins_ai_change = sa_text(
                """
                INSERT INTO job_changes (
                    run_id,
                    job_id_text,
                    site,
                    job_pk,
                    change_type,
                    change_source,
                    old_hash,
                    new_hash,
                    changed_fields
                )
                VALUES (
                    :run_id,
                    :job_id_text,
                    :site,
                    :job_pk,
                    'update',
                    'ai',
                    :old_hash,
                    :new_hash,
                    :changed_fields
                )
                """
            )
        else:
            ins_ai_change = sa_text(
                """
                INSERT INTO job_changes (
                    run_id,
                    job_id_text,
                    site,
                    job_pk,
                    change_type,
                    old_hash,
                    new_hash,
                    changed_fields
                )
                VALUES (
                    :run_id,
                    :job_id_text,
                    :site,
                    :job_pk,
                    'update',
                    :old_hash,
                    :new_hash,
                    :changed_fields
                )
                """
            )
            log("[warn] job_changes.change_source column not found; writing AI changes without source tag")
        task_by_id = {task.id: task for task in tasks}
        futures: Set[Future] = set()
        future_to_task: Dict[Future, JobTask] = {}
        future_started_at: Dict[Future, float] = {}
        submitted = 0

        def handle_result(result: JobResult) -> None:
            nonlocal processed, failures, skipped, completed, llm_seconds_total, aborted
            completed += 1
            llm_seconds_total += result.llm_seconds
            log(f"----- [Job {result.index}/{total}] {result.site}:{result.job_id} -----")

            if result.status == "ollama_unavailable":
                failures += 1
                aborted = True
                log(f"[fatal] {result.error_text}; aborting batch")
                return

            if result.status == "skip":
                skipped += 1
                log("[skip] Empty title+desc")
                return

            if (
                result.status == "ok"
                and result.payload_json is not None
                and result.update_params is not None
            ):
                try:
                    params = {
                        **result.update_params,
                        "val": result.payload_json,
                        "id": result.id,
                    }
                    update_result = s.execute(upd, params)
                    if update_result.rowcount == 0:
                        s.rollback()
                        skipped += 1
                        log("[skip] Job became inactive before AI write")
                        return
                    task = task_by_id[result.id]
                    s.execute(
                        ins_ai_change,
                        {
                            "run_id": task.run_id,
                            "job_id_text": task.job_id,
                            "site": task.site,
                            "job_pk": task.id,
                            "old_hash": task.content_hash,
                            "new_hash": task.content_hash,
                            "changed_fields": ai_changed_fields(result.update_params),
                        },
                    )
                    s.commit()
                    processed += 1
                    log(f"[ok] committed in {result.llm_seconds:.1f}s")
                    if result.update_params.get("ai_salary") is not None:
                        log(f"[ok] AI Salary={result.update_params['ai_salary']}")
                    if result.error_text:
                        log(f"[warn] {result.error_text}")
                except Exception as e:
                    s.rollback()
                    failures += 1
                    log(f"[err] DB commit failed: {e}")
                return

            failures += 1
            if result.status == "llm_error":
                log(f"[err] LLM call failed: {result.error_text}")
            else:
                log(f"[err] Invalid after retry: {result.error_text}")

        with ThreadPoolExecutor(max_workers=AI_CONCURRENCY) as executor:
            while (not aborted and submitted < total) or futures:
                while (
                    not aborted
                    and submitted < total
                    and len(futures) < AI_MAX_INFLIGHT
                ):
                    task = tasks[submitted]
                    idx = submitted + 1
                    log(f"[submit] [Job {idx}/{total}] {task.site}:{task.job_id} title={task.title[:80]!r}")
                    fut = executor.submit(analyze_job_worker, bounded_resume_text, task, idx)
                    futures.add(fut)
                    future_to_task[fut] = task
                    future_started_at[fut] = time.time()
                    submitted += 1

                done, _ = wait(
                    futures,
                    timeout=AI_WAIT_HEARTBEAT_SEC,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    now = time.time()
                    oldest = (
                        max(
                            0.0,
                            now - min(future_started_at.get(f, now) for f in futures),
                        )
                        if futures
                        else 0.0
                    )
                    elapsed = (datetime.now() - start).total_seconds()
                    rate = completed / elapsed if elapsed > 0 else 0.0
                    log(
                        f"[wait] no completion in {AI_WAIT_HEARTBEAT_SEC}s | completed={completed}/{total} submitted={submitted}/{total} inflight={len(futures)} oldest_inflight={oldest:.1f}s rate={rate:.2f}/s"
                    )
                    continue
                for fut in done:
                    futures.remove(fut)
                    task = future_to_task.pop(fut)
                    future_started_at.pop(fut, None)
                    try:
                        result = fut.result()
                    except Exception as e:
                        result = JobResult(
                            id=task.id,
                            site=task.site,
                            job_id=task.job_id,
                            status="llm_error",
                            payload_json=None,
                            update_params=None,
                            llm_seconds=0.0,
                            error_text=f"worker crash: {e}",
                            index=completed + 1,
                        )
                    handle_result(result)

                    if aborted:
                        # Cancel work that has not started. Already-running
                        # requests are retained in the set and drained.
                        for pending in list(futures):
                            if pending.cancel():
                                futures.remove(pending)
                                future_to_task.pop(pending, None)
                                future_started_at.pop(pending, None)
                                cancelled += 1

                    remaining = total - completed
                    if completed % AI_BATCH_LOG_EVERY == 0 or remaining == 0:
                        elapsed = (datetime.now() - start).total_seconds()
                        rate = completed / elapsed if elapsed > 0 else 0.0
                        avg_llm = llm_seconds_total / completed if completed > 0 else 0.0
                        log(
                            f"[batch] completed={completed}/{total} processed={processed} failed={failures} skipped={skipped} inflight={len(futures)} rate={rate:.2f}/s avg_llm={avg_llm:.2f}s"
                        )

    run_application_prep_generation(
        resume_text=resume_text,
        force=False,
        preflight=False,
    )
    run_fit_brief_generation(resume_text, force=False, preflight=False)

    end = datetime.now()
    log("------------------------------------------------------")
    elapsed_total = (end - start).total_seconds()
    throughput = completed / elapsed_total if elapsed_total > 0 else 0.0
    avg_llm_final = llm_seconds_total / completed if completed > 0 else 0.0
    unsubmitted = total - submitted
    log(
        f"Completed {end.strftime('%Y-%m-%d %H:%M:%S')}  |  total={completed}  "
        f"processed={processed}  failed={failures}  skipped={skipped}  "
        f"cancelled={cancelled}  unsubmitted={unsubmitted}  "
        f"throughput={throughput:.2f}/s  avg_llm={avg_llm_final:.2f}s"
    )
    log(f"Total runtime: {(end-start).total_seconds()/60:.2f} min")
    log("------------------------------------------------------")
    if aborted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

