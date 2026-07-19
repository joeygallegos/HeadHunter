from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional


# Compensation numbers must have either a currency marker/code or nearby pay
# language. This deliberately excludes generic values such as "5 years".
_CURRENCY_TOKEN = r"(?:USD|CAD|AUD|EUR|GBP|\$|€|£|Ł)"
_NUMBER_TOKEN = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?\s*[kK]|\d+(?:\.\d+)?)"
_RANGE_RE = re.compile(
    rf"(?P<c1>{_CURRENCY_TOKEN})?\s*(?P<low>{_NUMBER_TOKEN})\s*"
    rf"(?:-|–|—|to)\s*(?P<c2>{_CURRENCY_TOKEN})?\s*(?P<high>{_NUMBER_TOKEN})"
    rf"(?:\s*(?P<c3>USD|CAD|AUD|EUR|GBP))?",
    re.IGNORECASE,
)
_SINGLE_RE = re.compile(
    rf"(?P<c1>{_CURRENCY_TOKEN})\s*(?P<value>{_NUMBER_TOKEN})"
    rf"(?:\s*(?P<c2>USD|CAD|AUD|EUR|GBP))?",
    re.IGNORECASE,
)
_PAY_CONTEXT_RE = re.compile(
    r"\b(?:base salary|base pay|salary range|pay range|compensation range|"
    r"hourly rate|annual salary|annual pay|on[- ]target earnings?|OTE)\b",
    re.IGNORECASE,
)
_OTE_RE = re.compile(r"\b(?:OTE|on[- ]target earnings?)\b", re.IGNORECASE)
_BONUS_RE = re.compile(
    r"\b(?:eligible|eligibility) for (?:an? )?bonuses?\b|"
    r"\b(?:annual|target|performance|cash|sign[- ]on) bonus\b|"
    r"\bplus (?:a )?bonus\b",
    re.IGNORECASE,
)
_EQUITY_RE = re.compile(
    r"\b(?:equity grants?|equity awards?|stock options?|restricted stock|RSUs?)\b|"
    r"\bplus equity\b",
    re.IGNORECASE,
)
_COMMISSION_RE = re.compile(
    r"\b(?:eligible|eligibility) for commissions?\b|"
    r"\bcommission (?:plan|structure|target|opportunity|rate)\b",
    re.IGNORECASE,
)


def _decimal(value: Any) -> Optional[Decimal]:
    """Normalize a model/parser number without converting through float."""
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", "")
    multiplier = Decimal("1000") if raw.lower().endswith("k") else Decimal("1")
    if multiplier != 1:
        raw = raw[:-1].strip()
    try:
        number = Decimal(raw) * multiplier
    except (InvalidOperation, ValueError):
        return None
    return number if number >= 0 else None


def _currency(*tokens: Optional[str]) -> Optional[str]:
    for token in tokens:
        value = (token or "").strip().upper()
        if not value:
            continue
        if value == "$":
            return "USD"
        if value == "€":
            return "EUR"
        if value in {"£", "Ł"}:
            return "GBP"
        if value in {"USD", "CAD", "AUD", "EUR", "GBP"}:
            return value
    return None


def _period(text: str) -> Optional[str]:
    low = text.lower()
    if re.search(r"(?:/\s*(?:hr|hour)\b|\bper hour\b|\bhourly\b)", low):
        return "hour"
    if re.search(r"\b(?:per year|a year|annual(?:ly)?|per annum)\b", low):
        return "year"
    if re.search(r"\bper month\b|/\s*month\b", low):
        return "month"
    if re.search(r"\bper week\b|/\s*week\b", low):
        return "week"
    if re.search(r"\bper day\b|/\s*day\b", low):
        return "day"
    return None


def _is_ote_amount(text: str, start: int, end: int) -> bool:
    """Classify one amount without letting a nearby OTE swallow labeled base pay."""
    immediate = text[max(0, start - 25) : min(len(text), end + 25)]
    if re.search(r"\bbase(?: salary| pay)?\b", immediate, re.IGNORECASE):
        return False
    nearby = text[max(0, start - 80) : min(len(text), end + 80)]
    return bool(_OTE_RE.search(nearby))


def _empty_compensation() -> Dict[str, Any]:
    return {
        "base_pay_low": None,
        "base_pay_high": None,
        "pay_currency": None,
        "pay_period": None,
        "ote_low": None,
        "ote_high": None,
        "bonus_offered": None,
        "equity_offered": None,
        "commission_offered": None,
        "multiple_pay_ranges": False,
        "compensation_text": None,
        "compensation_notes": None,
    }


def extract_structured_compensation(
    text: str, *, assume_pay_context: bool = False
) -> Dict[str, Any]:
    """Extract conservative compensation facts from one source text."""
    raw = text or ""
    result = _empty_compensation()
    candidates = []

    for match in _RANGE_RE.finditer(raw):
        start, end = match.span()
        context = raw[max(0, start - 100) : min(len(raw), end + 120)]
        currency = _currency(match.group("c1"), match.group("c2"), match.group("c3"))
        low = _decimal(match.group("low"))
        high = _decimal(match.group("high"))
        if low is None or high is None or low > high:
            continue
        # Bare ranges need explicit context and salary-scale values. Hourly
        # ranges should carry a currency marker, which avoids level/tenure hits.
        if currency is None and (
            not _PAY_CONTEXT_RE.search(context) or low < 1000 or high < 1000
        ):
            continue
        candidates.append(
            {
                "low": low,
                "high": high,
                "currency": currency,
                "period": _period(context),
                "ote": _is_ote_amount(raw, start, end),
                "text": context.strip(),
                "start": start,
            }
        )

    # A single amount is useful only when an explicit compensation phrase is close.
    if not candidates:
        for match in _SINGLE_RE.finditer(raw):
            start, end = match.span()
            context = raw[max(0, start - 100) : min(len(raw), end + 120)]
            if not assume_pay_context and not _PAY_CONTEXT_RE.search(context):
                continue
            value = _decimal(match.group("value"))
            if value is None:
                continue
            candidates.append(
                {
                    "low": value,
                    "high": value,
                    "currency": _currency(match.group("c1"), match.group("c2")),
                    "period": _period(context),
                    "ote": _is_ote_amount(raw, start, end),
                    "text": context.strip(),
                    "start": start,
                }
            )

    # Repeated boilerplate can include the same range more than once. It is not
    # a multi-band posting unless the disclosed numeric band is actually distinct.
    unique = []
    seen_ranges = set()
    for item in candidates:
        key = (
            item["low"],
            item["high"],
            item["currency"],
            item["period"],
            item["ote"],
        )
        if key not in seen_ranges:
            seen_ranges.add(key)
            unique.append(item)
    base = [item for item in unique if not item["ote"]]
    ote = [item for item in unique if item["ote"]]
    primary = base or ote
    if primary:
        result["pay_currency"] = next(
            (item["currency"] for item in primary if item["currency"]), None
        )
        result["pay_period"] = next(
            (item["period"] for item in primary if item["period"]), None
        )
        result["compensation_text"] = primary[0]["text"]
        result["multiple_pay_ranges"] = len(primary) > 1
    if base:
        result["base_pay_low"] = min(item["low"] for item in base)
        result["base_pay_high"] = max(item["high"] for item in base)
    if ote:
        result["ote_low"] = min(item["low"] for item in ote)
        result["ote_high"] = max(item["high"] for item in ote)
        if result["pay_currency"] is None:
            result["pay_currency"] = next(
                (item["currency"] for item in ote if item["currency"]), None
            )
        if result["pay_period"] is None:
            result["pay_period"] = next(
                (item["period"] for item in ote if item["period"]), None
            )

    result["bonus_offered"] = True if _BONUS_RE.search(raw) else None
    result["equity_offered"] = True if _EQUITY_RE.search(raw) else None
    result["commission_offered"] = True if _COMMISSION_RE.search(raw) else None
    notes = []
    if result["bonus_offered"]:
        notes.append("Bonus offered")
    if result["equity_offered"]:
        notes.append("Equity offered")
    if result["commission_offered"]:
        notes.append("Commission offered")
    split = re.search(r"\b\d{1,2}\s*/\s*\d{1,2}\s+split\b", raw, re.IGNORECASE)
    if split:
        notes.append(split.group(0))
    result["compensation_notes"] = "; ".join(notes) or None
    return result


def choose_deterministic_compensation(
    explicit_pay: str, description: str, reference_values: Iterable[str] = ()
) -> Dict[str, Any]:
    """Use explicit salary data first, then references, then the description."""
    sources = [(explicit_pay, True)]
    sources.extend((value, True) for value in reference_values)
    sources.append((description, False))
    for value, assume_pay_context in sources:
        parsed = extract_structured_compensation(
            value or "", assume_pay_context=assume_pay_context
        )
        if any(
            parsed[key] is not None
            for key in ("base_pay_low", "base_pay_high", "ote_low", "ote_high")
        ):
            return parsed
    return _empty_compensation()


def _format_amount(value: Any, currency: Optional[str]) -> str:
    number = _decimal(value)
    if number is None:
        return ""
    if number == number.to_integral_value():
        rendered = f"{int(number):,}"
    else:
        rendered = f"{number:,.2f}".rstrip("0").rstrip(".")
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "")
    return f"{symbol}{rendered}" if symbol else f"{rendered} {currency or ''}".strip()


def format_compensation_summary(data: Dict[str, Any]) -> Optional[str]:
    """Render a compact display string from validated normalized values."""
    currency = data.get("pay_currency")
    period = data.get("pay_period")
    pieces = []
    for label, low_key, high_key in (
        ("", "base_pay_low", "base_pay_high"),
        ("OTE ", "ote_low", "ote_high"),
    ):
        low = data.get(low_key)
        high = data.get(high_key)
        if low is None and high is None:
            continue
        low = low if low is not None else high
        high = high if high is not None else low
        left = _format_amount(low, currency)
        right = _format_amount(high, currency)
        rendered = left if left == right else f"{left} - {right}"
        if period:
            rendered += f" per {period}"
        pieces.append(f"{label}{rendered}")
    return "; ".join(pieces) or None


def compensation_has_values(data: Dict[str, Any]) -> bool:
    """Return whether a structured result contains any disclosed compensation."""
    return any(
        data.get(key) is not None
        for key in (
            "base_pay_low",
            "base_pay_high",
            "ote_low",
            "ote_high",
            "bonus_offered",
            "equity_offered",
            "commission_offered",
        )
    )
