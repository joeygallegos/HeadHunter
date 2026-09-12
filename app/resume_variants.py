from __future__ import annotations

import hashlib
import json
import socket
from typing import Any, Dict, List, Tuple

from .json_utils import safe_json_loads


RESUME_SOURCE_MODE_RESUME_TXT = "resume_txt"
RESUME_SOURCE_MODE_GOOGLE_DOC = "google_doc"
RESUME_SOURCE_MODES = {RESUME_SOURCE_MODE_RESUME_TXT, RESUME_SOURCE_MODE_GOOGLE_DOC}

RESUME_VARIANT_STATUS_DRAFT = "draft"
RESUME_VARIANT_STATUS_CREATING = "creating"
RESUME_VARIANT_STATUS_DONE = "done"
RESUME_VARIANT_STATUS_FAILED = "failed"
RESUME_VARIANT_STATUS_STALE = "stale"
RESUME_VARIANT_STATUSES = {
    RESUME_VARIANT_STATUS_DRAFT,
    RESUME_VARIANT_STATUS_CREATING,
    RESUME_VARIANT_STATUS_DONE,
    RESUME_VARIANT_STATUS_FAILED,
    RESUME_VARIANT_STATUS_STALE,
}

RESUME_VARIANT_STAGE_ANALYSIS = "analysis"
RESUME_VARIANT_STAGE_MATCHING = "matching"
RESUME_VARIANT_STAGE_REVIEW = "review"
RESUME_VARIANT_STAGE_CREATING = "creating"
RESUME_VARIANT_STAGE_COMPLETE = "complete"
RESUME_VARIANT_STAGES = {
    RESUME_VARIANT_STAGE_ANALYSIS,
    RESUME_VARIANT_STAGE_MATCHING,
    RESUME_VARIANT_STAGE_REVIEW,
    RESUME_VARIANT_STAGE_CREATING,
    RESUME_VARIANT_STAGE_COMPLETE,
}

MAX_SWAP_CANDIDATES = 4
CONFIDENCE_VALUES = {"high", "medium", "low"}
EVIDENCE_SOURCE_TYPES = {"resume", "responsibilities_inventory"}

SWAP_ANALYSIS_KEYS = {
    "baseline_candidates",
    "replacement_candidates",
    "suggested_pairs",
    "coverage_summary",
    "remaining_gaps",
}
BASELINE_CANDIDATE_KEYS = {
    "anchor_id",
    "bullet",
    "section",
    "value_band",
    "confidence",
    "lower_value_rationale",
}
REPLACEMENT_CANDIDATE_KEYS = {
    "candidate_id",
    "bullet",
    "evidence_sources",
    "job_requirement",
    "confidence",
    "value_rationale",
}
EVIDENCE_SOURCE_KEYS = {"source", "evidence"}
SUGGESTED_PAIR_KEYS = {"anchor_id", "candidate_id", "comparative_rationale"}
APPROVED_PAIR_KEYS = {
    "anchor_id",
    "candidate_id",
    "approved_bullet",
    "edited",
    "grounding_acknowledged",
}


def stable_json_hash(payload: Any) -> str:
    """Hash generated JSON contracts without depending on key order."""
    body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8", "ignore")).hexdigest()


def resume_variant_host_id() -> str:
    """Default per-host settings key; callers may override with an env value."""
    return socket.gethostname().strip().lower() or "localhost"


def _short_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _validate_exact_keys(item: Any, keys: set[str], name: str) -> Tuple[bool, str]:
    if not isinstance(item, dict):
        return False, f"{name} must be an object"
    if set(item.keys()) != keys:
        return False, f"{name} keys must exactly match contract"
    return True, ""


def _normalize_confidence(value: Any, name: str) -> Tuple[str, str]:
    confidence = str(value or "").strip().lower()
    if confidence not in CONFIDENCE_VALUES:
        return "", f"{name} confidence must be one of {sorted(CONFIDENCE_VALUES)}"
    return confidence, ""


def validate_swap_analysis(payload: Any) -> Tuple[bool, str]:
    """Validate and normalize a resume bullet swap analysis payload in place."""
    ok, why = _validate_exact_keys(payload, SWAP_ANALYSIS_KEYS, "swap analysis")
    if not ok:
        return False, why

    baseline_items = payload.get("baseline_candidates")
    replacement_items = payload.get("replacement_candidates")
    suggested_pairs = payload.get("suggested_pairs")
    if not isinstance(baseline_items, list):
        return False, "baseline_candidates must be a list"
    if not isinstance(replacement_items, list):
        return False, "replacement_candidates must be a list"
    if not isinstance(suggested_pairs, list):
        return False, "suggested_pairs must be a list"
    remaining_gaps = payload.get("remaining_gaps")
    if not isinstance(remaining_gaps, list):
        return False, "remaining_gaps must be a list"

    if len(baseline_items) > MAX_SWAP_CANDIDATES:
        return False, "baseline_candidates cannot contain more than 4 items"
    if len(replacement_items) > MAX_SWAP_CANDIDATES:
        return False, "replacement_candidates cannot contain more than 4 items"

    anchors: set[str] = set()
    normalized_baseline: List[Dict[str, str]] = []
    for item in baseline_items:
        ok, why = _validate_exact_keys(item, BASELINE_CANDIDATE_KEYS, "baseline candidate")
        if not ok:
            return False, why
        anchor_id = _short_text(item.get("anchor_id"), 120)
        bullet = _short_text(item.get("bullet"), 1000)
        rationale = _short_text(item.get("lower_value_rationale"), 1000)
        confidence, why = _normalize_confidence(item.get("confidence"), "baseline candidate")
        if why:
            return False, why
        if not anchor_id or not bullet or not rationale:
            return False, "baseline candidate anchor, bullet, and rationale are required"
        if anchor_id in anchors:
            return False, "baseline candidate anchor_id values must be unique"
        anchors.add(anchor_id)
        normalized_baseline.append(
            {
                "anchor_id": anchor_id,
                "bullet": bullet,
                "section": _short_text(item.get("section"), 200),
                "value_band": _short_text(item.get("value_band"), 80),
                "confidence": confidence,
                "lower_value_rationale": rationale,
            }
        )

    candidates: set[str] = set()
    normalized_replacements: List[Dict[str, Any]] = []
    for item in replacement_items:
        ok, why = _validate_exact_keys(item, REPLACEMENT_CANDIDATE_KEYS, "replacement candidate")
        if not ok:
            return False, why
        candidate_id = _short_text(item.get("candidate_id"), 120)
        bullet = _short_text(item.get("bullet"), 1000)
        requirement = _short_text(item.get("job_requirement"), 1000)
        rationale = _short_text(item.get("value_rationale"), 1000)
        confidence, why = _normalize_confidence(item.get("confidence"), "replacement candidate")
        if why:
            return False, why
        if not candidate_id or not bullet or not requirement or not rationale:
            return False, "replacement candidate id, bullet, requirement, and rationale are required"
        if candidate_id in candidates:
            return False, "replacement candidate_id values must be unique"
        sources = item.get("evidence_sources")
        if not isinstance(sources, list) or not sources or len(sources) > 2:
            return False, "replacement evidence_sources must contain one or two sources"
        seen_sources: set[str] = set()
        normalized_sources: List[Dict[str, str]] = []
        for source in sources:
            ok, why = _validate_exact_keys(source, EVIDENCE_SOURCE_KEYS, "evidence source")
            if not ok:
                return False, why
            source_type = _short_text(source.get("source"), 80)
            evidence = _short_text(source.get("evidence"), 1500)
            if source_type not in EVIDENCE_SOURCE_TYPES or not evidence:
                return False, "evidence source and text must be valid and non-empty"
            if source_type in seen_sources:
                return False, "evidence_sources cannot repeat a source"
            seen_sources.add(source_type)
            normalized_sources.append({"source": source_type, "evidence": evidence})
        candidates.add(candidate_id)
        normalized_replacements.append(
            {
                "candidate_id": candidate_id,
                "bullet": bullet,
                "evidence_sources": normalized_sources,
                "job_requirement": requirement,
                "confidence": confidence,
                "value_rationale": rationale,
            }
        )

    normalized_pairs: List[Dict[str, str]] = []
    seen_suggestions: set[tuple[str, str]] = set()
    for item in suggested_pairs:
        ok, why = _validate_exact_keys(item, SUGGESTED_PAIR_KEYS, "suggested pair")
        if not ok:
            return False, why
        anchor_id = _short_text(item.get("anchor_id"), 120)
        candidate_id = _short_text(item.get("candidate_id"), 120)
        rationale = _short_text(item.get("comparative_rationale"), 1000)
        if anchor_id not in anchors or candidate_id not in candidates or not rationale:
            return False, "suggested pairs must reference known candidates with rationale"
        pair_key = (anchor_id, candidate_id)
        if pair_key in seen_suggestions:
            return False, "suggested pairs must be unique"
        seen_suggestions.add(pair_key)
        normalized_pairs.append(
            {
                "anchor_id": anchor_id,
                "candidate_id": candidate_id,
                "comparative_rationale": rationale,
            }
        )

    payload["baseline_candidates"] = normalized_baseline
    payload["replacement_candidates"] = normalized_replacements
    payload["suggested_pairs"] = normalized_pairs
    payload["coverage_summary"] = _short_text(payload.get("coverage_summary"), 1200)
    payload["remaining_gaps"] = [
        _short_text(item, 500)
        for item in remaining_gaps
        if _short_text(item, 500)
    ][:8]
    return True, ""


def validate_approved_pairs(pairs: Any, analysis: Dict[str, Any]) -> Tuple[bool, str, List[Dict[str, Any]]]:
    """Validate the user-approved left-to-right bullet swaps for a variant draft."""
    if not isinstance(pairs, list):
        return False, "approved pairs must be a list", []
    if len(pairs) > MAX_SWAP_CANDIDATES:
        return False, "approved pairs cannot contain more than 4 items", []

    baseline_by_id = {
        item["anchor_id"]: item
        for item in analysis.get("baseline_candidates", [])
        if isinstance(item, dict) and item.get("anchor_id")
    }
    replacement_by_id = {
        item["candidate_id"]: item
        for item in analysis.get("replacement_candidates", [])
        if isinstance(item, dict) and item.get("candidate_id")
    }

    seen_anchors: set[str] = set()
    seen_candidates: set[str] = set()
    normalized: List[Dict[str, Any]] = []
    for item in pairs:
        ok, why = _validate_exact_keys(item, APPROVED_PAIR_KEYS, "approved pair")
        if not ok:
            return False, why, []
        anchor_id = _short_text(item.get("anchor_id"), 120)
        candidate_id = _short_text(item.get("candidate_id"), 120)
        if anchor_id not in baseline_by_id:
            return False, "approved pair references an unknown baseline anchor", []
        if candidate_id not in replacement_by_id:
            return False, "approved pair references an unknown replacement candidate", []
        if anchor_id in seen_anchors:
            return False, "a baseline bullet can only be swapped once", []
        if candidate_id in seen_candidates:
            return False, "a replacement bullet can only be used once", []
        approved_bullet = _short_text(item.get("approved_bullet"), 1000)
        original_bullet = replacement_by_id[candidate_id]["bullet"]
        edited = bool(item.get("edited")) or approved_bullet != original_bullet
        if not approved_bullet:
            return False, "approved bullet text is required", []
        if edited and approved_bullet != original_bullet and not bool(item.get("grounding_acknowledged")):
            return False, "edited bullets require grounding acknowledgment", []
        seen_anchors.add(anchor_id)
        seen_candidates.add(candidate_id)
        normalized.append(
            {
                "anchor_id": anchor_id,
                "candidate_id": candidate_id,
                "original_bullet": baseline_by_id[anchor_id]["bullet"],
                "approved_bullet": approved_bullet,
                "edited": edited,
                "grounding_acknowledged": bool(item.get("grounding_acknowledged")),
            }
        )
    return True, "", normalized


def serialize_resume_source_settings(settings: Any) -> Dict[str, Any]:
    """Return the small settings payload the Application Prep UI needs."""
    mode = _short_text(getattr(settings, "source_mode", ""), 32) or RESUME_SOURCE_MODE_RESUME_TXT
    return {
        "host_id": _short_text(getattr(settings, "host_id", ""), 255),
        "source_mode": mode if mode in RESUME_SOURCE_MODES else RESUME_SOURCE_MODE_RESUME_TXT,
        "google_document_id": _short_text(getattr(settings, "google_document_id", ""), 255),
        "google_document_name": _short_text(getattr(settings, "google_document_name", ""), 512),
        "google_document_url": _short_text(getattr(settings, "google_document_url", ""), 1024),
        "baseline_revision": _short_text(getattr(settings, "baseline_revision", ""), 255),
        "baseline_hash": _short_text(getattr(settings, "baseline_hash", ""), 64),
        "last_synced_at": getattr(settings, "last_synced_at", None),
        "can_apply": mode == RESUME_SOURCE_MODE_GOOGLE_DOC and bool(getattr(settings, "baseline_hash", "")),
    }


def serialize_resume_variant(variant: Any) -> Dict[str, Any]:
    """Serialize draft/generated variant state without exposing filesystem roots."""
    return {
        "id": getattr(variant, "id", None),
        "job_pk": getattr(variant, "job_pk", None),
        "host_id": _short_text(getattr(variant, "host_id", ""), 255),
        "status": _short_text(getattr(variant, "status", ""), 16),
        "stage": _short_text(getattr(variant, "stage", ""), 32),
        "error_text": _short_text(getattr(variant, "error_text", ""), 2000),
        "source_mode": _short_text(getattr(variant, "source_mode", ""), 32),
        "baseline_document_name": _short_text(getattr(variant, "baseline_document_name", ""), 512),
        "baseline_document_url": _short_text(getattr(variant, "baseline_document_url", ""), 1024),
        "baseline_revision": _short_text(getattr(variant, "baseline_revision", ""), 255),
        "baseline_hash": _short_text(getattr(variant, "baseline_hash", ""), 64),
        "application_prep_hash": _short_text(getattr(variant, "application_prep_hash", ""), 64),
        "analysis": safe_json_loads(getattr(variant, "analysis_json", None)) or None,
        "replacements": safe_json_loads(getattr(variant, "replacements_json", None)) or [],
        "copied_document_url": _short_text(getattr(variant, "copied_document_url", ""), 1024),
        "has_pdf": bool(getattr(variant, "pdf_relative_path", "")),
        "pdf_sha256": _short_text(getattr(variant, "pdf_sha256", ""), 64),
        "baseline_page_count": getattr(variant, "baseline_page_count", None),
        "pdf_page_count": getattr(variant, "pdf_page_count", None),
        "page_count_warning": bool(getattr(variant, "page_count_warning", False)),
        "created_at": getattr(variant, "created_at", None),
        "updated_at": getattr(variant, "updated_at", None),
        "generated_at": getattr(variant, "generated_at", None),
    }
