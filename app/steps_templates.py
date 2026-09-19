"""Reusable starter recipes for common applicant-tracking systems.

These are intentionally starter templates, not platform detection or a promise
that every tenant has identical markup. Users should test each generated recipe
before adding it to scheduled scraping.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List
from urllib.parse import urlparse


_TEMPLATE_METADATA = {
    "workday": {
        "id": "workday",
        "label": "Workday",
        "description": "Workday results with up to 10 pages of DOM pagination.",
    },
    "greenhouse": {
        "id": "greenhouse",
        "label": "Greenhouse",
        "description": "Greenhouse-style job list and detail-page starter.",
    },
    "lever": {
        "id": "lever",
        "label": "Lever",
        "description": "Lever-style job list and detail-page starter.",
    },
}


def list_steps_templates() -> List[Dict[str, str]]:
    """Return display-safe metadata for every supported starter template."""
    return [deepcopy(_TEMPLATE_METADATA[key]) for key in _TEMPLATE_METADATA]


def get_steps_template_metadata(template_id: str) -> Dict[str, str]:
    """Return metadata for one template or raise a user-facing validation error."""
    normalized_id = template_id.strip().lower() if isinstance(template_id, str) else ""
    template = _TEMPLATE_METADATA.get(normalized_id)
    if not template:
        raise ValueError("Unsupported template_id. Choose Workday, Greenhouse, or Lever.")
    return deepcopy(template)


def _validate_starting_url(url: str) -> str:
    candidate = url.strip() if isinstance(url, str) else ""
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http:// or https:// URL")
    return candidate


def build_steps_template(template_id: str, url: str) -> List[Dict[str, Any]]:
    """Build a fresh schema-compatible recipe for a supported ATS template."""
    template_id = get_steps_template_metadata(template_id)["id"]
    url = _validate_starting_url(url)

    if template_id == "workday":
        return [
            {"action": "load_url", "url": url},
            {
                "action": "data_extract",
                "focus_scope": "section[data-automation-id='jobResults']>ul[role='list']>li",
                # Collect list pages before visiting detail pages so Workday's
                # client-side pagination is not reset during detail extraction.
                "pagination": {
                    "mode": "click_next",
                    "max_pages": 10,
                    "current_page_css": "nav[aria-label='pagination'] button[aria-current='page']",
                    "next_page_css": "nav[aria-label='pagination'] button[aria-label='next']",
                    "next_disabled_css": "button[aria-label='next'][disabled], button[aria-label='next'][aria-disabled='true']",
                    "page_wait_ms": 1200,
                    "page_as_column": "__page",
                },
                "extract_steps": [
                    {"action": "extract", "as_column": "JobID", "xpath": "ul[data-automation-id='subtitle']", "attr_target": None},
                    {"action": "extract", "as_column": "JobTitle", "xpath": "a[data-automation-id='jobTitle']", "attr_target": None},
                    {"action": "extract", "data_type": "url", "as_column": "JobUrl", "xpath": "a[data-automation-id='jobTitle']", "attr_target": "href"},
                    {"action": "redirect", "using_column": "JobUrl"},
                    {"action": "sleep"},
                    {"action": "extract", "as_column": "JobDesc", "xpath": "div[data-automation-id='jobPostingDescription']", "attr_target": None},
                    # Workday frequently serves relative links; preserve the
                    # resolved detail URL after the browser has navigated there.
                    {"action": "extract", "data_type": "current_url", "as_column": "JobUrl"},
                    {"action": "next"},
                ],
            },
        ]

    if template_id == "greenhouse":
        return [
            {"action": "load_url", "url": url},
            {
                "action": "data_extract",
                "focus_scope": "div.opening, section.opening, tr.job-post",
                "extract_steps": [
                    {"action": "extract", "as_column": "JobTitle", "xpath": "a", "attr_target": None},
                    {"action": "extract", "data_type": "url", "as_column": "JobUrl", "xpath": "a", "attr_target": "href"},
                    {"action": "regex_extract", "using_column": "JobUrl", "as_column": "JobID", "regex_pattern": r"(\d+|[^/]+)$"},
                    {"action": "redirect", "using_column": "JobUrl"},
                    {"action": "sleep"},
                    {"action": "extract", "as_column": "JobDesc", "xpath": "#content, main, body", "attr_target": None},
                    {"action": "extract", "data_type": "current_url", "as_column": "JobUrl"},
                    {"action": "next"},
                ],
            },
        ]

    # Template ID validation above means the remaining supported value is Lever.
    return [
        {"action": "load_url", "url": url},
        {
            "action": "data_extract",
            "focus_scope": ".posting, .posting-title, a[href*='/jobs/']",
            "extract_steps": [
                {"action": "extract", "as_column": "JobTitle", "xpath": "a, h5", "attr_target": None},
                {"action": "extract", "data_type": "url", "as_column": "JobUrl", "xpath": "a", "attr_target": "href"},
                {"action": "regex_extract", "using_column": "JobUrl", "as_column": "JobID", "regex_pattern": r"([^/]+)$"},
                {"action": "redirect", "using_column": "JobUrl"},
                {"action": "sleep"},
                {"action": "extract", "as_column": "JobDesc", "xpath": ".content, main, body", "attr_target": None},
                {"action": "extract", "data_type": "current_url", "as_column": "JobUrl"},
                {"action": "next"},
            ],
        },
    ]
