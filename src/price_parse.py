from __future__ import annotations

import re

# "2.9만원", "29만" — 만(10,000) units, optionally followed by 원.
_MANWON_RE = re.compile(r"(\d+(?:\.\d+)?)\s*만\s*원?")
# "22,900원", "7200원"
_WON_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*원")
# "₩15,938" / "￦15,938" (quasarzone v2 layout)
_WON_SYMBOL_RE = re.compile(r"[₩￦]\s*(\d[\d,]*(?:\.\d+)?)")
_USD_RE = re.compile(r"[$＄]\s*(\d[\d,]*(?:\.\d+)?)")
_EUR_RE = re.compile(r"[€]\s*(\d[\d,]*(?:\.\d+)?)")
_JPY_RE = re.compile(r"[¥￥]\s*(\d[\d,]*(?:\.\d+)?)")


def parse_price(raw: str | None) -> tuple[float | None, str]:
    """
    Parse a crawled price string into (amount, currency).

    Returns (None, "") when no confident numeric price is present.
    Deliberately does NOT interpret 무료/무배/공짜 — those usually describe
    shipping, not the product price; the raw text is kept alongside anyway.
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""

    match = _MANWON_RE.search(text)
    if match:
        return float(match.group(1)) * 10000, "KRW"

    match = _WON_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "")), "KRW"

    match = _WON_SYMBOL_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "")), "KRW"

    match = _USD_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "")), "USD"

    match = _EUR_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "")), "EUR"

    match = _JPY_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "")), "JPY"

    return None, ""
