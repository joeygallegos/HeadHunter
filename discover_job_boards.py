"""Discover candidate job boards and career pages with Ollama-guided triage.

Python owns all network access. Ollama receives bounded page/link evidence and
decides which links look worth inspecting and which pages deserve a
``steps.json`` suggestion.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup

from app.event_log import append_event, read_event_log, reset_event_log
from app.file_utils import read_json_file, read_text, write_json
from app.steps_suggestions import merge_steps_suggestions

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
PROMPT_DIR = os.path.join(BASE_DIR, "prompts")
DEFAULT_RESUME_PATH = os.path.join(BASE_DIR, "resume.txt")
DEFAULT_SEEDS_PATH = os.path.join(BASE_DIR, "config", "job_discovery_seeds.json")
DEFAULT_SUGGESTIONS_PATH = os.path.join(OUTPUT_DIR, "steps_suggestions.json")
DEFAULT_DISCOVERY_PATH = os.path.join(OUTPUT_DIR, "job_board_discovery.json")
DEFAULT_EVENTS_PATH = os.path.join(OUTPUT_DIR, "job_board_discovery_events.jsonl")
DEFAULT_STEPS_PATH = os.path.join(BASE_DIR, "steps.json")

DEFAULT_SEARCH_TEMPLATES = [
    "{role} remote careers",
    "{role} {location} careers",
    "{role} {location} jobs",
    "{role} jobs greenhouse",
    "{role} jobs lever",
    "{role} jobs workday",
    "{skill} remote jobs",
    "{skill} {location} careers",
]

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-r1:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "4096"))
DISCOVERY_NUM_PREDICT = int(os.getenv("JOB_DISCOVERY_NUM_PREDICT", "800"))
DISCOVERY_TIMEOUT_SEC = max(10, int(os.getenv("JOB_DISCOVERY_TIMEOUT_SEC", "60")))
FETCH_TIMEOUT_SEC = max(5, int(os.getenv("JOB_DISCOVERY_FETCH_TIMEOUT_SEC", "20")))
USER_AGENT = os.getenv(
    "JOB_DISCOVERY_USER_AGENT",
    "JobScrapeDiscovery/1.0 (+local research; contact owner)",
)

SUGGESTION_STATES = {"pending", "approved", "rejected", "applied"}


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int]
    text: str
    links: List[Dict[str, str]]
    error: str = ""


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover job boards and career pages with Ollama-guided triage."
    )
    parser.add_argument("--resume", default=DEFAULT_RESUME_PATH)
    parser.add_argument("--seeds", default=DEFAULT_SEEDS_PATH)
    parser.add_argument("--max-pages", type=int, default=None)
    parser.add_argument("--max-depth", type=int, default=None)
    parser.add_argument("--max-search-queries", type=int, default=None)
    parser.add_argument("--max-search-results", type=int, default=None)
    parser.add_argument("--events", default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--no-events", action="store_true")
    parser.add_argument("--no-search", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def load_prompt(name: str) -> str:
    return read_text(os.path.join(PROMPT_DIR, name)).lstrip("\ufeff")


def parse_json_object(text: str) -> Dict[str, Any]:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return obj if isinstance(obj, dict) else {}


def invoke_ollama_json(system_prompt: str, user_payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(user_payload, ensure_ascii=False, indent=2),
            },
        ],
        "stream": False,
        "format": "json",
        "think": False,
        "keep_alive": OLLAMA_KEEP_ALIVE,
        "options": {
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": DISCOVERY_NUM_PREDICT,
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
        with urllib.request.urlopen(req, timeout=DISCOVERY_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama request failed: {exc}") from exc

    data = parse_json_object(body)
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Ollama response missing content: {body[:200]}")
    return parse_json_object(content)


def normalize_str_list(value: Any, limit: int = 20) -> List[str]:
    if not isinstance(value, list):
        return []
    out: List[str] = []
    seen = set()
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
        if len(out) >= limit:
            break
    return out


def validate_criteria(payload: Dict[str, Any]) -> Dict[str, Any]:
    criteria = {
        "target_roles": normalize_str_list(payload.get("target_roles"), 12),
        "skills": normalize_str_list(payload.get("skills"), 20),
        "industries": normalize_str_list(payload.get("industries"), 12),
        "seniority": str(payload.get("seniority") or "unknown").strip() or "unknown",
        "remote_preference": str(payload.get("remote_preference") or "remote_or_hybrid")
        .strip()
        .lower(),
        "location_city": str(payload.get("location_city") or "").strip(),
        "location_region": str(payload.get("location_region") or "").strip(),
    }
    if criteria["remote_preference"] not in {
        "remote",
        "remote_or_hybrid",
        "hybrid",
        "onsite",
        "unknown",
    }:
        criteria["remote_preference"] = "remote_or_hybrid"
    return criteria


def fallback_criteria_from_resume(resume_text: str) -> Dict[str, Any]:
    location_city = ""
    location_region = ""
    match = re.search(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*,\s*(Texas|TX)\b", resume_text)
    if match:
        location_city = match.group(1)
        location_region = "Texas"
    return {
        "target_roles": ["Security Engineer", "Cloud Security Engineer"],
        "skills": ["Cloud Security", "Vulnerability Management", "Python Automation"],
        "industries": [],
        "seniority": "senior",
        "remote_preference": "remote_or_hybrid",
        "location_city": location_city,
        "location_region": location_region,
    }


def extract_resume_criteria(resume_text: str) -> Dict[str, Any]:
    prompt = load_prompt("job_discovery_criteria_system.txt")
    try:
        criteria = validate_criteria(
            invoke_ollama_json(prompt, {"resume_text": resume_text[:12000]})
        )
    except Exception as exc:
        log(f"[warn] criteria extraction failed; using fallback: {exc}")
        criteria = fallback_criteria_from_resume(resume_text)

    fallback = fallback_criteria_from_resume(resume_text)
    for key in ("target_roles", "skills"):
        if not criteria.get(key):
            criteria[key] = fallback[key]
    if not criteria.get("location_city"):
        criteria["location_city"] = fallback["location_city"]
        criteria["location_region"] = fallback["location_region"]
    return criteria


def normalize_url(base_url: str, candidate: str) -> Optional[str]:
    candidate = str(candidate or "").strip().replace("\n", "").replace("\r", "")
    if not candidate or candidate.startswith("#"):
        return None
    if candidate.lower().startswith(("mailto:", "tel:", "javascript:", "data:", "blob:")):
        return None
    absolute = urllib.parse.urljoin(base_url, candidate)
    parsed = urllib.parse.urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    clean = parsed._replace(fragment="")
    return urllib.parse.urlunparse(clean)


def normalized_domain(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_excluded_url(url: str, excluded_domains: Iterable[str]) -> bool:
    domain = normalized_domain(url)
    for excluded in excluded_domains:
        item = str(excluded or "").strip().lower()
        if not item:
            continue
        item = item[4:] if item.startswith("www.") else item
        if domain == item or domain.endswith(f".{item}"):
            return True
    return False


def criteria_location_text(criteria: Dict[str, Any]) -> str:
    city = str(criteria.get("location_city") or "").strip()
    region = str(criteria.get("location_region") or "").strip()
    return " ".join(part for part in [city, region] if part).strip()


def build_search_queries(criteria: Dict[str, Any], config: Dict[str, Any]) -> List[str]:
    templates = config.get("search_templates") or DEFAULT_SEARCH_TEMPLATES
    if not isinstance(templates, list):
        templates = DEFAULT_SEARCH_TEMPLATES

    max_queries = max(0, int(config.get("max_search_queries") or 12))
    roles = normalize_str_list(criteria.get("target_roles"), 6) or ["security engineer"]
    skills = normalize_str_list(criteria.get("skills"), 8) or roles
    location = criteria_location_text(criteria) or "remote"

    queries: List[str] = []
    seen = set()
    formatter = string.Formatter()
    for template in templates:
        template_text = str(template or "").strip()
        if not template_text:
            continue
        fields = {field for _, field, _, _ in formatter.parse(template_text) if field}
        role_values = roles if "role" in fields else [roles[0]]
        skill_values = skills if "skill" in fields else [skills[0]]
        for role in role_values:
            for skill in skill_values:
                try:
                    query = template_text.format(
                        role=role,
                        skill=skill,
                        location=location,
                        remote_preference=criteria.get("remote_preference") or "",
                        seniority=criteria.get("seniority") or "",
                    )
                except KeyError:
                    continue
                query = re.sub(r"\s+", " ", query).strip()
                key = query.lower()
                if not query or key in seen:
                    continue
                seen.add(key)
                queries.append(query)
                if len(queries) >= max_queries:
                    return queries
    return queries


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _xml_local_name(child.tag) == name:
            return "".join(child.itertext()).strip()
    return ""


def parse_bing_rss_results(xml_text: str, limit: int) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    results: List[Dict[str, str]] = []
    for item in root.iter():
        if _xml_local_name(item.tag) != "item":
            continue
        title = _xml_child_text(item, "title")
        link = _xml_child_text(item, "link")
        description = _xml_child_text(item, "description")
        url = normalize_url("", link)
        if not url:
            continue
        results.append(
            {
                "title": title,
                "url": url,
                "description": re.sub(r"\s+", " ", description).strip()[:500],
            }
        )
        if len(results) >= limit:
            break
    return results


def fetch_bing_rss_results(query: str, limit: int) -> List[Dict[str, str]]:
    params = urllib.parse.urlencode({"format": "rss", "q": query})
    url = f"https://www.bing.com/search?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
        raw = resp.read(500_000)
    return parse_bing_rss_results(raw.decode("utf-8", "replace"), limit=limit)


def discover_search_urls(
    criteria: Dict[str, Any],
    config: Dict[str, Any],
    events_path: Optional[str] = None,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    if not bool(config.get("enable_search", True)):
        append_event(
            events_path,
            "search_disabled",
            "Search expansion is disabled; using configured seed URLs only.",
        )
        return [], []

    provider = str(config.get("search_provider") or "bing_rss").strip().lower()
    if provider != "bing_rss":
        append_event(
            events_path,
            "search_failed",
            "Unsupported search provider configured.",
            provider=provider,
        )
        log(f"[warn] unsupported search provider {provider}; skipping search expansion")
        return [], []

    queries = build_search_queries(criteria, config)
    max_results = max(1, int(config.get("max_search_results_per_query") or 8))
    excluded = config.get("excluded_domains") or []
    queued_urls: List[str] = []
    seen_urls = set()
    records: List[Dict[str, Any]] = []

    append_event(
        events_path,
        "search_started",
        "Starting search-result expansion before crawl.",
        provider=provider,
        query_count=len(queries),
        max_results_per_query=max_results,
    )

    for query in queries:
        append_event(
            events_path,
            "search_query_started",
            "Fetching search results.",
            provider=provider,
            query=query,
        )
        try:
            results = fetch_bing_rss_results(query, limit=max_results)
            append_event(
                events_path,
                "search_query_finished",
                "Search results fetched.",
                provider=provider,
                query=query,
                result_count=len(results),
            )
        except Exception as exc:
            log(f"[warn] search failed for {query!r}: {exc}")
            append_event(
                events_path,
                "search_query_failed",
                "Search query failed; continuing with remaining queries.",
                provider=provider,
                query=query,
                error=str(exc),
            )
            results = []

        records.append({"query": query, "results": results})
        for result in results:
            url = normalize_url("", result.get("url", ""))
            if not url:
                continue
            if is_excluded_url(url, excluded):
                append_event(
                    events_path,
                    "search_result_skipped",
                    "Search result skipped because its domain is excluded.",
                    query=query,
                    url=url,
                )
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            queued_urls.append(url)
            append_event(
                events_path,
                "search_result_queued",
                "Search result queued as a crawl start URL.",
                query=query,
                url=url,
                title=result.get("title", ""),
            )

    append_event(
        events_path,
        "search_finished",
        "Search expansion finished.",
        query_count=len(queries),
        queued_url_count=len(queued_urls),
    )
    return queued_urls, records


def fetch_page(url: str, max_links: int) -> FetchResult:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:
            status = getattr(resp, "status", None)
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(1_000_000)
    except Exception as exc:
        return FetchResult(url=url, ok=False, status=None, text="", links=[], error=str(exc))

    if "html" not in content_type and "text" not in content_type:
        return FetchResult(
            url=url,
            ok=False,
            status=status,
            text="",
            links=[],
            error=f"unsupported content type: {content_type}",
        )

    html = raw.decode("utf-8", "replace")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()[:8000]
    links: List[Dict[str, str]] = []
    seen = set()
    for a in soup.select("a[href]"):
        href = normalize_url(url, a.get("href", ""))
        if not href or href in seen:
            continue
        seen.add(href)
        label = re.sub(r"\s+", " ", a.get_text(" ", strip=True)).strip()[:160]
        links.append({"url": href, "text": label})
        if len(links) >= max_links:
            break
    return FetchResult(url=url, ok=True, status=status, text=text, links=links)


def detect_platform(url: str, text: str = "") -> str:
    combined = f"{url} {text}".lower()
    checks = [
        ("workday", ["myworkdayjobs.com", "workdayjobs.com", "workday"]),
        ("greenhouse", ["greenhouse.io", "greenhouse"]),
        ("lever", ["lever.co", "lever"]),
        ("ashby", ["ashbyhq.com", "jobs.ashbyhq.com", "ashby"]),
        ("smartrecruiters", ["smartrecruiters.com", "smartrecruiters"]),
        ("icims", ["icims.com", "icims"]),
    ]
    for platform, needles in checks:
        if any(needle in combined for needle in needles):
            return platform
    return "unknown"


def site_key_from_url(url: str, platform: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = normalized_domain(url).split(":")[0]
    parts = [p for p in host.split(".") if p not in {"jobs", "careers", "www", "boards"}]
    if platform == "workday":
        first = (parsed.path.strip("/").split("/") or [""])[0]
        if first:
            parts = [first]
    base = parts[0] if parts else host
    key = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return key or "discovered_site"


def build_steps_template(url: str, platform: str) -> Tuple[List[Dict[str, Any]], bool, str]:
    manual_note = "Generated selector template should be test-run before regular scraping."
    if platform == "workday":
        steps = [
            {"action": "load_url", "url": url},
            {
                "action": "data_extract",
                "focus_scope": "section[data-automation-id='jobResults']>ul[role='list']>li",
                "extract_steps": [
                    {
                        "action": "extract",
                        "as_column": "JobID",
                        "xpath": "ul[data-automation-id='subtitle']",
                        "attr_target": None,
                    },
                    {
                        "action": "extract",
                        "as_column": "JobTitle",
                        "xpath": "a[data-automation-id='jobTitle']",
                        "attr_target": None,
                    },
                    {
                        "action": "extract",
                        "data_type": "url",
                        "as_column": "JobUrl",
                        "xpath": "a[data-automation-id='jobTitle']",
                        "attr_target": "href",
                    },
                    {"action": "redirect", "using_column": "JobUrl"},
                    {"action": "sleep"},
                    {
                        "action": "extract",
                        "as_column": "JobDesc",
                        "xpath": "div[data-automation-id='jobPostingDescription']",
                        "attr_target": None,
                    },
                    {"action": "next"},
                ],
            },
        ]
        return steps, False, manual_note

    if platform == "greenhouse":
        steps = [
            {"action": "load_url", "url": url},
            {
                "action": "data_extract",
                "focus_scope": "div.opening, section.opening, tr.job-post",
                "extract_steps": [
                    {"action": "extract", "as_column": "JobTitle", "xpath": "a", "attr_target": None},
                    {
                        "action": "extract",
                        "data_type": "url",
                        "as_column": "JobUrl",
                        "xpath": "a",
                        "attr_target": "href",
                    },
                    {"action": "regex_extract", "using_column": "JobUrl", "as_column": "JobID", "regex_pattern": r"(\d+|[^/]+)$"},
                    {"action": "redirect", "using_column": "JobUrl"},
                    {"action": "sleep"},
                    {"action": "extract", "as_column": "JobDesc", "xpath": "#content, main, body", "attr_target": None},
                    {"action": "next"},
                ],
            },
        ]
        return steps, False, manual_note

    if platform == "lever":
        steps = [
            {"action": "load_url", "url": url},
            {
                "action": "data_extract",
                "focus_scope": ".posting, .posting-title, a[href*='/jobs/']",
                "extract_steps": [
                    {"action": "extract", "as_column": "JobTitle", "xpath": "a, h5", "attr_target": None},
                    {
                        "action": "extract",
                        "data_type": "url",
                        "as_column": "JobUrl",
                        "xpath": "a",
                        "attr_target": "href",
                    },
                    {"action": "regex_extract", "using_column": "JobUrl", "as_column": "JobID", "regex_pattern": r"([^/]+)$"},
                    {"action": "redirect", "using_column": "JobUrl"},
                    {"action": "sleep"},
                    {"action": "extract", "as_column": "JobDesc", "xpath": ".content, main, body", "attr_target": None},
                    {"action": "next"},
                ],
            },
        ]
        return steps, False, manual_note

    steps = [
        {"action": "load_url", "url": url},
        {
            "action": "data_extract",
            "focus_scope": "REVIEW_SELECTOR_FOR_JOB_CARD",
            "extract_steps": [
                {"action": "extract", "as_column": "JobID", "xpath": "REVIEW_SELECTOR_FOR_JOB_ID", "attr_target": None},
                {"action": "extract", "as_column": "JobTitle", "xpath": "REVIEW_SELECTOR_FOR_JOB_TITLE", "attr_target": None},
                {
                    "action": "extract",
                    "data_type": "url",
                    "as_column": "JobUrl",
                    "xpath": "REVIEW_SELECTOR_FOR_JOB_LINK",
                    "attr_target": "href",
                },
                {"action": "redirect", "using_column": "JobUrl"},
                {"action": "sleep"},
                {"action": "extract", "as_column": "JobDesc", "xpath": "REVIEW_SELECTOR_FOR_JOB_DESC", "attr_target": None},
                {"action": "next"},
            ],
        },
    ]
    return steps, True, "Unknown/custom board; selectors must be reviewed before applying."


def coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        return 0.0
    return max(0.0, min(confidence, 1.0))


def build_suggestion(candidate: Dict[str, Any], source_url: str) -> Dict[str, Any]:
    url = str(candidate.get("url") or source_url).strip()
    platform = detect_platform(url, " ".join(str(candidate.get(k, "")) for k in ("reason", "evidence")))
    if candidate.get("platform"):
        platform = str(candidate.get("platform")).strip().lower() or platform
    steps, manual_review, note = build_steps_template(url, platform)
    site_key = str(candidate.get("site_key") or "").strip() or site_key_from_url(url, platform)
    return {
        "id": stable_suggestion_id(url),
        "state": "pending",
        "site_key": site_key,
        "url": url,
        "platform": platform,
        "confidence": coerce_confidence(candidate.get("confidence", 0.5)),
        "reason": str(candidate.get("reason") or "").strip(),
        "remote_evidence": str(candidate.get("remote_evidence") or "").strip(),
        "location_evidence": str(candidate.get("location_evidence") or "").strip(),
        "manual_review": bool(candidate.get("manual_review", manual_review)) or manual_review,
        "notes": str(candidate.get("notes") or note).strip(),
        "steps": steps,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def stable_suggestion_id(url: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-")
    return cleaned[:120] or f"suggestion-{int(time.time())}"


def validate_triage(payload: Dict[str, Any]) -> Dict[str, Any]:
    next_urls = normalize_str_list(payload.get("next_urls"), 50)
    candidates_raw = payload.get("candidates")
    candidates = candidates_raw if isinstance(candidates_raw, list) else []
    return {"next_urls": next_urls, "candidates": [c for c in candidates if isinstance(c, dict)]}


def fallback_triage(page: FetchResult) -> Dict[str, Any]:
    useful_words = ("career", "jobs", "job", "security", "remote", "workday", "greenhouse", "lever")
    next_urls = [
        item["url"]
        for item in page.links
        if any(word in f"{item.get('text','')} {item.get('url','')}".lower() for word in useful_words)
    ][:10]
    candidates: List[Dict[str, Any]] = []
    platform = detect_platform(page.url, page.text)
    if platform != "unknown" or re.search(r"\b(careers?|jobs?|open positions)\b", page.text, re.I):
        candidates.append(
            {
                "url": page.url,
                "platform": platform,
                "confidence": 0.55 if platform != "unknown" else 0.35,
                "reason": "Heuristic match for a careers or job-board page.",
                "remote_evidence": "remote" if "remote" in page.text.lower() else "",
                "location_evidence": "",
            }
        )
    return {"next_urls": next_urls, "candidates": candidates}


def triage_page(
    page: FetchResult,
    criteria: Dict[str, Any],
    events_path: Optional[str] = None,
) -> Dict[str, Any]:
    prompt = load_prompt("job_discovery_triage_system.txt")
    payload = {
        "criteria": criteria,
        "page": {
            "url": page.url,
            "status": page.status,
            "text": page.text[:5000],
            "links": page.links[:60],
        },
    }
    append_event(
        events_path,
        "ollama_triage_started",
        "Asking Ollama to triage fetched page.",
        url=page.url,
        link_count=len(page.links),
    )
    try:
        triage = validate_triage(invoke_ollama_json(prompt, payload))
        append_event(
            events_path,
            "ollama_triage_finished",
            "Ollama selected next links and candidates.",
            url=page.url,
            next_url_count=len(triage["next_urls"]),
            candidate_count=len(triage["candidates"]),
            next_urls=triage["next_urls"][:10],
        )
    except Exception as exc:
        log(f"[warn] triage failed for {page.url}; using fallback: {exc}")
        append_event(
            events_path,
            "ollama_triage_failed",
            "Ollama triage failed; using heuristic fallback.",
            url=page.url,
            error=str(exc),
        )
        triage = fallback_triage(page)
    return triage


def default_seed_config() -> Dict[str, Any]:
    example_path = os.path.join(BASE_DIR, "config", "job_discovery_seeds.example.json")
    return read_json_file(example_path, {"seed_urls": [], "excluded_domains": []})


def load_seed_config(path: str) -> Dict[str, Any]:
    config = read_json_file(path, None)
    if config is None:
        config = default_seed_config()
        log(f"[warn] seed config not found at {path}; using example defaults")
    config.setdefault("seed_urls", [])
    config.setdefault("excluded_domains", [])
    config.setdefault("max_pages", 25)
    config.setdefault("max_depth", 2)
    config.setdefault("max_links_per_page", 80)
    config.setdefault("enable_search", True)
    config.setdefault("search_provider", "bing_rss")
    config.setdefault("max_search_queries", 12)
    config.setdefault("max_search_results_per_query", 8)
    config.setdefault("search_templates", DEFAULT_SEARCH_TEMPLATES)
    return config


def crawl(
    config: Dict[str, Any],
    criteria: Dict[str, Any],
    max_pages: int,
    max_depth: int,
    extra_seed_urls: Optional[List[str]] = None,
    events_path: Optional[str] = None,
) -> Dict[str, Any]:
    max_links = max(1, int(config.get("max_links_per_page") or 80))
    excluded = config.get("excluded_domains") or []
    queue: List[Tuple[str, int]] = []
    seen_start_urls = set()
    start_urls = [(seed, "seed") for seed in (config.get("seed_urls") or [])]
    start_urls.extend((seed, "search") for seed in (extra_seed_urls or []))
    for seed, source in start_urls:
        url = normalize_url("", seed)
        if not url or url in seen_start_urls or is_excluded_url(url, excluded):
            continue
        seen_start_urls.add(url)
        queue.append((url, 0))
        append_event(
            events_path,
            "seed_queued",
            "Start URL queued for inspection.",
            url=url,
            depth=0,
            source=source,
        )

    visited = set()
    pages: List[Dict[str, Any]] = []
    suggestions_by_id: Dict[str, Dict[str, Any]] = {}

    while queue and len(visited) < max_pages:
        url, depth = queue.pop(0)
        if url in visited or depth > max_depth or is_excluded_url(url, excluded):
            continue
        visited.add(url)
        log(f"[fetch] depth={depth} {url}")
        append_event(
            events_path,
            "fetch_started",
            "Fetching page.",
            url=url,
            depth=depth,
            visited_count=len(visited),
            queued_count=len(queue),
        )
        page = fetch_page(url, max_links=max_links)
        pages.append(
            {
                "url": url,
                "depth": depth,
                "ok": page.ok,
                "status": page.status,
                "error": page.error,
                "text_sample": page.text[:800],
                "link_count": len(page.links),
            }
        )
        if not page.ok:
            append_event(
                events_path,
                "fetch_failed",
                "Page fetch failed or returned unsupported content.",
                url=url,
                depth=depth,
                status=page.status,
                error=page.error,
            )
            continue

        append_event(
            events_path,
            "fetch_finished",
            "Fetched page and extracted links.",
            url=url,
            depth=depth,
            status=page.status,
            link_count=len(page.links),
            text_chars=len(page.text),
        )

        triage = triage_page(page, criteria, events_path=events_path)
        for candidate in triage["candidates"]:
            suggestion = build_suggestion(candidate, source_url=page.url)
            if suggestion["id"] not in suggestions_by_id:
                suggestions_by_id[suggestion["id"]] = suggestion
                append_event(
                    events_path,
                    "suggestion_created",
                    "Created a steps.json suggestion.",
                    url=suggestion["url"],
                    site_key=suggestion["site_key"],
                    platform=suggestion["platform"],
                    confidence=suggestion["confidence"],
                    manual_review=suggestion["manual_review"],
                    reason=suggestion["reason"],
                )

        if depth >= max_depth:
            continue
        for next_url in triage["next_urls"]:
            normalized = normalize_url(page.url, next_url)
            if not normalized or normalized in visited or is_excluded_url(normalized, excluded):
                continue
            queue.append((normalized, depth + 1))
            append_event(
                events_path,
                "next_url_queued",
                "Ollama-selected URL queued for inspection.",
                from_url=page.url,
                url=normalized,
                depth=depth + 1,
            )

    result = {
        "criteria": criteria,
        "pages": pages,
        "suggestions": list(suggestions_by_id.values()),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    append_event(
        events_path,
        "crawl_finished",
        "Discovery crawl finished.",
        page_count=len(pages),
        suggestion_count=len(result["suggestions"]),
        visited_count=len(visited),
        queued_count=len(queue),
    )
    return result


def run_discovery(args: argparse.Namespace) -> Dict[str, Any]:
    events_path = None if args.no_events or args.dry_run else args.events
    reset_event_log(events_path)
    append_event(
        events_path,
        "run_started",
        "Job board discovery run started.",
        resume_path=args.resume,
        seeds_path=args.seeds,
        model=OLLAMA_MODEL,
    )
    resume_text = read_text(args.resume)
    append_event(
        events_path,
        "resume_loaded",
        "Loaded resume text for criteria extraction.",
        resume_path=args.resume,
        resume_chars=len(resume_text),
    )
    config = load_seed_config(args.seeds)
    if args.no_search:
        config["enable_search"] = False
    if args.max_search_queries is not None:
        config["max_search_queries"] = args.max_search_queries
    if args.max_search_results is not None:
        config["max_search_results_per_query"] = args.max_search_results
    max_pages = max(1, int(args.max_pages or config.get("max_pages") or 25))
    max_depth = max(0, int(args.max_depth if args.max_depth is not None else config.get("max_depth") or 2))
    append_event(
        events_path,
        "seeds_loaded",
        "Loaded discovery seed configuration.",
        seed_count=len(config.get("seed_urls") or []),
        excluded_domain_count=len(config.get("excluded_domains") or []),
        max_pages=max_pages,
        max_depth=max_depth,
    )

    append_event(
        events_path,
        "criteria_extraction_started",
        "Asking Ollama to extract discovery criteria from the resume.",
    )
    criteria = extract_resume_criteria(resume_text)
    append_event(
        events_path,
        "criteria_extracted",
        "Discovery criteria extracted.",
        criteria=criteria,
    )
    search_urls, search_records = discover_search_urls(
        criteria,
        config,
        events_path=events_path,
    )
    append_event(
        events_path,
        "crawl_started",
        "Starting web crawl.",
        max_pages=max_pages,
        max_depth=max_depth,
        seed_count=len(config.get("seed_urls") or []),
        search_start_url_count=len(search_urls),
    )
    result = crawl(
        config,
        criteria,
        max_pages=max_pages,
        max_depth=max_depth,
        extra_seed_urls=search_urls,
        events_path=events_path,
    )
    result["search"] = {
        "enabled": bool(config.get("enable_search", True)),
        "provider": str(config.get("search_provider") or "bing_rss"),
        "queued_urls": search_urls,
        "queries": search_records,
    }
    suggestions_doc = {
        "generated_at": result["generated_at"],
        "criteria": criteria,
        "search": result["search"],
        "suggestions": result["suggestions"],
    }
    if not args.dry_run:
        write_json(DEFAULT_DISCOVERY_PATH, result)
        write_json(DEFAULT_SUGGESTIONS_PATH, suggestions_doc)
        append_event(
            events_path,
            "outputs_written",
            "Discovery output files written.",
            discovery_path=DEFAULT_DISCOVERY_PATH,
            suggestions_path=DEFAULT_SUGGESTIONS_PATH,
        )
    return result


def main() -> None:
    args = parse_args()
    result = run_discovery(args)
    log(
        f"[done] pages={len(result['pages'])} suggestions={len(result['suggestions'])}"
    )
    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
