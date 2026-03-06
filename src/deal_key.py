import hashlib
import re
from typing import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
BOARD_RE = re.compile(r"^[a-z0-9_]+$")
CANONICAL_KEY_RE = re.compile(r"^([a-z][a-z0-9_]{0,15})-([a-z0-9_]+)-(\d+)$")
LEGACY_AL_KEY_RE = re.compile(r"^al-(\d+)$")
LEGACY_QZ_KEY_RE = re.compile(r"^qz-(\d+)$")

URL_DECODERS: Sequence[tuple[str, re.Pattern[str]]] = (
    (
        "al",
        re.compile(r"^https?://(?:www\.)?arca\.live/b/([a-z0-9_]+)/(\d+)/?.*$"),
    ),
    (
        "qr",
        re.compile(
            r"^https?://(?:www\.)?quasarzone\.com/bbs/([a-z0-9_]+)/views/(\d+)/?.*$"
        ),
    ),
    (
        "fm",
        re.compile(r"^https?://(?:www\.)?fmkorea\.com/(hotdeal)/(\d+)/?.*$"),
    ),
    (
        "eo",
        re.compile(r"^https?://(?:www\.)?eomisae\.co\.kr/(fs)/(\d+)/?.*$"),
    ),
)

LEGACY_ALIAS_BY_SOURCE = {
    "al": "al",
    "qr": "qz",
}


def encode_deal_key(source: str, board_id: str, article_id: int | str) -> str:
    src = source.strip().lower()
    board = board_id.strip().lower()
    aid = str(article_id).strip()

    if not SOURCE_RE.match(src):
        raise ValueError(f"Invalid source id: {src}")
    if not BOARD_RE.match(board):
        raise ValueError(f"Invalid board id: {board}")
    if not aid.isdigit():
        raise ValueError(f"Invalid article id: {aid}")

    return f"{src}-{board}-{aid}"


def decode_deal_key(key: str) -> tuple[str, str, int] | None:
    m = CANONICAL_KEY_RE.match(key.strip().lower())
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def normalize_deal_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    if not path:
        path = "/"

    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, "", ""))


def decode_key_from_url(url: str) -> tuple[str, str, int] | None:
    normalized = normalize_deal_url(url)
    if not normalized:
        return None

    for source, pattern in URL_DECODERS:
        m = pattern.match(normalized)
        if m:
            return source, m.group(1), int(m.group(2))

    return None


def canonical_deal_key_from_deal(deal: Mapping[str, object]) -> str | None:
    raw_id = str(deal.get("id", "")).strip().lower()
    deal_url = str(deal.get("url", ""))

    decoded = decode_deal_key(raw_id)
    if decoded:
        return encode_deal_key(*decoded)

    legacy_al = LEGACY_AL_KEY_RE.match(raw_id)
    if legacy_al:
        return encode_deal_key("al", "hotdeal", legacy_al.group(1))

    legacy_qz = LEGACY_QZ_KEY_RE.match(raw_id)
    if legacy_qz:
        parsed = decode_key_from_url(deal_url)
        if parsed and parsed[0] == "qr":
            return encode_deal_key("qr", parsed[1], legacy_qz.group(1))
        return None

    parsed_from_url = decode_key_from_url(deal_url)
    if parsed_from_url:
        return encode_deal_key(*parsed_from_url)

    return None


def compact_url_hash_key(deal: Mapping[str, object]) -> str | None:
    normalized = normalize_deal_url(str(deal.get("url", "")))
    if not normalized:
        return None
    digest = hashlib.blake2s(normalized.encode("utf-8"), digest_size=8).hexdigest()
    return f"uh-{digest}"


def get_lookup_keys_for_deal(deal: Mapping[str, object]) -> list[str]:
    keys: list[str] = []

    canonical = canonical_deal_key_from_deal(deal)
    if canonical:
        keys.append(canonical)
        decoded = decode_deal_key(canonical)
        if decoded:
            source, _, article_id = decoded
            legacy_prefix = LEGACY_ALIAS_BY_SOURCE.get(source)
            if legacy_prefix:
                keys.append(f"{legacy_prefix}-{article_id}")

    raw_id = str(deal.get("id", "")).strip().lower()
    if raw_id:
        keys.append(raw_id)

    # URL-based fallback key is only useful when canonical/raw IDs are unavailable.
    # For query-only URLs (e.g., view.php?id=...&no=...), normalized paths may collide.
    if not canonical and not raw_id:
        normalized = normalize_deal_url(str(deal.get("url", "")))
        if normalized:
            keys.append(f"url:{normalized}")

    deduped: list[str] = []
    for key in keys:
        if key and key not in deduped:
            deduped.append(key)
    return deduped


def get_storage_key_for_deal(deal: Mapping[str, object]) -> str | None:
    canonical = canonical_deal_key_from_deal(deal)
    if canonical:
        return canonical

    raw_id = str(deal.get("id", "")).strip().lower()
    if raw_id:
        return raw_id

    return compact_url_hash_key(deal)


def compact_existing_key(raw_key: str) -> str | None:
    key = raw_key.strip().lower()
    if not key:
        return None

    decoded = decode_deal_key(key)
    if decoded:
        return encode_deal_key(*decoded)

    legacy_al = LEGACY_AL_KEY_RE.match(key)
    if legacy_al:
        return encode_deal_key("al", "hotdeal", legacy_al.group(1))

    legacy_qz = LEGACY_QZ_KEY_RE.match(key)
    if legacy_qz:
        # Board id is ambiguous without URL context. Preserve key instead of guessing.
        return key

    if key.startswith("url:"):
        parsed = decode_key_from_url(key[4:])
        if parsed:
            return encode_deal_key(*parsed)
        return compact_url_hash_key({"url": key[4:]})

    return key
