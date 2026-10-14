"""Typed, environment-backed Ollama settings for each AI processing pass."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping


def parse_ollama_think(value: Any) -> Any:
    """Normalize Ollama's boolean or level-based thinking control."""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"low", "medium", "high", "max"}:
        return normalized
    raise ValueError(
        "Ollama think settings must be one of false, true, low, medium, high, or max"
    )


@dataclass(frozen=True)
class OllamaProfile:
    """Resolved inference controls for one logical AI pass."""

    name: str
    model: str
    think: Any
    num_ctx: int
    num_predict: int
    retry_num_predict: int
    temperature: float
    timeout_sec: int
    max_attempts: int

    def fingerprint(self) -> str:
        """Return a stable digest suitable for cache and artifact freshness checks."""
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _override(prefix: str, suffix: str, baseline: Any) -> Any:
    value = os.getenv(f"{prefix}_{suffix}")
    return baseline if value in {None, ""} else value


def load_ollama_profile(
    name: str,
    prefix: str,
    baseline: Mapping[str, Any],
) -> OllamaProfile:
    """Resolve a pass profile while retaining the supplied legacy settings as defaults."""
    num_predict = max(1, int(_override(prefix, "NUM_PREDICT", baseline["num_predict"])))
    retry_num_predict = max(
        num_predict,
        int(_override(prefix, "RETRY_NUM_PREDICT", baseline["retry_num_predict"])),
    )
    return OllamaProfile(
        name=name,
        model=str(_override(prefix, "MODEL", baseline["model"])).strip(),
        think=parse_ollama_think(_override(prefix, "THINK", baseline["think"])),
        num_ctx=max(1, int(_override(prefix, "NUM_CTX", baseline["num_ctx"]))),
        num_predict=num_predict,
        retry_num_predict=retry_num_predict,
        temperature=float(_override(prefix, "TEMPERATURE", baseline["temperature"])),
        timeout_sec=max(
            10, int(_override(prefix, "TIMEOUT_SEC", baseline["timeout_sec"]))
        ),
        max_attempts=max(
            1, int(_override(prefix, "MAX_ATTEMPTS", baseline["max_attempts"]))
        ),
    )


def generation_config_hash(
    profile: OllamaProfile,
    prompt_path: str,
    schema_version: int,
) -> str:
    """Fingerprint every behavior-changing input behind a generated artifact."""
    try:
        with open(prompt_path, "r", encoding="utf-8") as handle:
            prompt = handle.read().lstrip("\ufeff").strip()
    except OSError:
        prompt = ""
    payload = json.dumps(
        [schema_version, prompt, profile.fingerprint()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_job_profile(name: str) -> OllamaProfile:
    """Resolve a job-processing profile without importing the analyzer module."""
    model = os.getenv("OLLAMA_MODEL", "deepseek-r1:8b")
    think = parse_ollama_think(os.getenv("OLLAMA_THINK", "false"))
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "2048"))
    num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "300"))
    retry_num_predict = max(
        num_predict,
        int(os.getenv("AI_THINKING_RETRY_NUM_PREDICT", str(num_predict * 3))),
    )
    temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0.2"))
    timeout_sec = max(10, int(os.getenv("AI_REQUEST_TIMEOUT_SEC", "120")))
    max_attempts = max(1, int(os.getenv("AI_MAX_ATTEMPTS", "4")))
    baseline = {
        "model": model,
        "think": think,
        "num_ctx": num_ctx,
        "num_predict": num_predict,
        "retry_num_predict": retry_num_predict,
        "temperature": temperature,
        "timeout_sec": timeout_sec,
        "max_attempts": max_attempts,
    }
    application_num_predict = max(
        num_predict, int(os.getenv("APPLICATION_PREP_NUM_PREDICT", "4096"))
    )
    definitions = {
        "screen": (
            "AI_SCREEN",
            {
                **baseline,
                "think": False,
                "num_predict": max(
                    64, int(os.getenv("AI_SCREEN_NUM_PREDICT", "512"))
                ),
                "retry_num_predict": max(
                    max(64, int(os.getenv("AI_SCREEN_NUM_PREDICT", "512"))),
                    int(os.getenv("AI_SCREEN_RETRY_NUM_PREDICT", "2048")),
                ),
            },
        ),
        "detail": ("AI_DETAIL", baseline),
        "review": ("AI_REVIEW", baseline),
        "compensation": ("AI_COMPENSATION", baseline),
        "fit_brief": ("AI_FIT_BRIEF", baseline),
        "application_prep": (
            "APPLICATION_PREP",
            {
                **baseline,
                "think": parse_ollama_think(
                    os.getenv("APPLICATION_PREP_THINK", "false")
                ),
                "num_ctx": max(
                    num_ctx, int(os.getenv("APPLICATION_PREP_NUM_CTX", "65536"))
                ),
                "num_predict": application_num_predict,
                "retry_num_predict": max(
                    retry_num_predict,
                    application_num_predict,
                    int(
                        os.getenv(
                            "APPLICATION_PREP_RETRY_NUM_PREDICT",
                            str(application_num_predict * 2),
                        )
                    ),
                ),
            },
        ),
    }
    try:
        prefix, pass_baseline = definitions[name]
    except KeyError as exc:
        raise ValueError(f"Unknown AI profile: {name}") from exc
    return load_ollama_profile(name, prefix, pass_baseline)
