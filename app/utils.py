from __future__ import annotations

import re
from collections import Counter

import nltk


_NLTK_RESOURCES = [
    ("tokenizers/punkt", "punkt"),
    ("tokenizers/punkt_tab/english", "punkt_tab"),
]


def _ensure_nltk_resource(resource_path: str, download_name: str, verbose: bool) -> None:
    try:
        nltk.data.find(resource_path)
        if verbose:
            print(f"NLTK resource ready: {download_name}")
        return
    except LookupError:
        if verbose:
            print(f"Downloading NLTK resource: {download_name}")

    ok = nltk.download(download_name, quiet=not verbose)
    if not ok:
        raise RuntimeError(f"NLTK download failed for resource: {download_name}")

    try:
        nltk.data.find(resource_path)
    except LookupError as exc:
        raise RuntimeError(
            f"NLTK resource {download_name!r} was downloaded but still cannot be found. "
            f"Try: python -m nltk.downloader {download_name}"
        ) from exc

    if verbose:
        print(f"NLTK resource ready: {download_name}")


def ensure_nltk(verbose: bool = False):
    for resource_path, download_name in _NLTK_RESOURCES:
        _ensure_nltk_resource(resource_path, download_name, verbose)
    try:
        # NLTK 3.8+ uses this language-specific tagger name.
        nltk.data.find("taggers/averaged_perceptron_tagger_eng")
        if verbose:
            print("NLTK resource ready: averaged_perceptron_tagger_eng")
    except LookupError:
        try:
            if verbose:
                print("Downloading NLTK resource: averaged_perceptron_tagger_eng")
            ok = nltk.download(
                "averaged_perceptron_tagger_eng", quiet=not verbose
            )
            if not ok:
                raise RuntimeError(
                    "NLTK download failed for resource: averaged_perceptron_tagger_eng"
                )
        except Exception:
            if verbose:
                print("Downloading NLTK resource: averaged_perceptron_tagger")
            ok = nltk.download("averaged_perceptron_tagger", quiet=not verbose)
            if not ok:
                raise RuntimeError(
                    "NLTK download failed for resource: averaged_perceptron_tagger"
                )
        if verbose:
            print("NLTK resource ready: averaged_perceptron_tagger")


def extract_keywords(job_description: str, top_n: int = 10) -> list[str]:
    cleaned = re.sub(r"[^a-zA-Z\s]", " ", job_description or "")
    tokens = nltk.word_tokenize(cleaned.lower())
    pos_tags = nltk.pos_tag(tokens)
    words = [w for (w, pos) in pos_tags if pos.startswith("NN") or pos.startswith("JJ")]
    counts = Counter(words)
    return [w for (w, _) in counts.most_common(top_n)]


# Helper: match level patterns like:
# "Engineer II", "Engineer 2", "L2", "Level 2", "IC2", "P2", roman numerals
_LEVEL_PATTERNS = [
    (re.compile(r"\b(intern)\b", re.I), "Junior"),
    (re.compile(r"\b(entry|entry[-\s]?level|graduate)\b", re.I), "Junior"),
    # Explicit numeric levels (broad): L1/L2/L3, Level 1/2/3, IC1/2/3, P1/2/3, SDE1/2/3
    (re.compile(r"\b(level\s*)?(l|ic|p)\s*1\b|\bsde\s*1\b", re.I), "Junior"),
    (re.compile(r"\b(level\s*)?(l|ic|p)\s*2\b|\bsde\s*2\b", re.I), "Mid"),
    (re.compile(r"\b(level\s*)?(l|ic|p)\s*[3-9]\b|\bsde\s*[3-9]\b", re.I), "Senior"),
    # Roman numerals, anchored to avoid false hits in words
    (re.compile(r"(^|[\s\-\(\[,])i([\s\-\)\],]|$)", re.I), "Junior"),
    (re.compile(r"(^|[\s\-\(\[,])ii([\s\-\)\],]|$)", re.I), "Mid"),
    (
        re.compile(r"(^|[\s\-\(\[,])iii|iv|v|vi|vii|viii|ix|x([\s\-\)\],]|$)", re.I),
        "Senior",
    ),
]


def get_job_level(job_title: str) -> str:
    t = (job_title or "").strip()
    tl = t.lower()

    # 1) level indicator wins
    for rx, label in _LEVEL_PATTERNS:
        if rx.search(t):
            return label

    # 2) keywords
    if any(
        k in tl for k in ["vp", "vice president", "director", "head", "chief", "cxo"]
    ):
        return "Leader"
    if "manager" in tl:
        # Optional: treat "manager" as leader; if you want people-managers only, tighten this.
        return "Leader"
    if any(k in tl for k in ["architect", "principal", "distinguished"]):
        return "Architect"

    # Senior keywords
    if any(k in tl for k in ["senior", "sr", "lead", "leader", "expert"]):
        return "Senior"

    # Mid keywords (your staff decision)
    if any(
        k in tl for k in ["staff", "intermediate", "mid", "mid-level", "journeyman"]
    ):
        return "Mid"

    # Junior keywords
    if any(k in tl for k in ["junior", "jr", "associate", "trainee", "apprentice"]):
        return "Junior"

    # 3) plain English fallback (role-based defaults)
    # Junior-ish roles
    if any(
        k in tl
        for k in [
            "sales development representative",
            "sdr",
            "coordinator",
            "assistant",
            "clerk",
            "technician",
            "intern",
        ]
    ):
        return "Junior"

    # Mid-ish common roles (most “plain titles” land here)
    if any(
        k in tl
        for k in [
            "engineer",
            "developer",
            "analyst",
            "specialist",
            "administrator",
            "account executive",
            "sales engineer",
            "producer",
            "designer",
            "investigator",
        ]
    ):
        return "Mid"

    return "Unknown"


def scan_for_pay_range(text: str) -> list[str]:
    currency = r"(\$|€|£|USD|EUR|GBP)?"
    num = r"([\d,]+(?:\.\d+)?)"
    range_patterns = [rf"{currency}\s*{num}\s*(?:-|–|to)\s*{currency}\s*{num}"]
    single_patterns = [
        rf"{currency}\s*{num}\s*(?:per year|per annum|year|annum|annual)\b",
        rf"{currency}\s*{num}\s*(?:per hour|hour|hr|hourly)\b",
        rf"{currency}\s*{num}\b",
    ]
    for pattern in range_patterns:
        hits = [
            m.group(0).strip() for m in re.finditer(pattern, text or "", re.IGNORECASE)
        ]
        if hits:

            def max_end(s: str) -> float:
                nums = re.findall(r"[\d,]+(?:\.\d+)?", s)
                return float(nums[-1].replace(",", "")) if nums else 0.0

            return [sorted(hits, key=max_end, reverse=True)[0]]
    singles = []
    for pattern in single_patterns:
        for m in re.finditer(pattern, text or "", re.IGNORECASE):
            raw = m.group(0).strip()
            if raw and raw not in singles:
                singles.append(raw)
    return singles


def remove_canned_text(jobs: list[dict]) -> list[dict]:
    descs = [j.get("JobDesc", "") for j in jobs if j.get("JobDesc")]
    if len(descs) < 2:
        return jobs
    ensure_nltk()
    sent_lists = [nltk.sent_tokenize(d) for d in descs]
    all_sents = [s.strip() for lst in sent_lists for s in lst]
    counts = Counter(all_sents)
    n = len(sent_lists)
    recurring = {
        s for s, c in counts.items() if c >= n and all(s in lst for lst in sent_lists)
    }
    if not recurring:
        return jobs
    for j in jobs:
        d = j.get("JobDesc", "")
        if not d:
            continue
        sents = nltk.sent_tokenize(d)
        j["JobDesc"] = " ".join(
            [s for s in sents if s.strip() not in recurring]
        ).strip()
    return jobs
