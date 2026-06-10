from __future__ import annotations
import nltk, re
from collections import Counter


def ensure_nltk():
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt")
    try:
        nltk.data.find("taggers/averaged_perceptron_tagger_eng")
    except LookupError:
        try:
            nltk.download("averaged_perceptron_tagger_eng")
        except Exception:
            nltk.download("averaged_perceptron_tagger")


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
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        nltk.download("punkt")
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
