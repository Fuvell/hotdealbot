from __future__ import annotations

import re
from typing import Any, Mapping

from discord import app_commands

COMMON_FILTERABLE_CATEGORIES = [
    "식품",
    "생활용품",
    "게임",
    "PC",
    "전자제품",
    "가전",
    "의류",
    "화장품",
    "상품권",
    "기타",
]

UNCATEGORIZED_FILTER_TOKEN = "__uncategorized__"
UNCATEGORIZED_FILTER_DISPLAY = "미분류/없음"


def normalize_category_token(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


UNCATEGORIZED_ALIAS_NORMS = {
    normalize_category_token(UNCATEGORIZED_FILTER_DISPLAY),
    normalize_category_token("unknown"),
    normalize_category_token("uncategorized"),
    normalize_category_token("미분류"),
    normalize_category_token("없음"),
    normalize_category_token("nocategory"),
    normalize_category_token("no-category"),
}

CLEAR_ALL_FILTER_INPUT_NORMS = {
    normalize_category_token("none"),
    normalize_category_token("null"),
    normalize_category_token("clear"),
    normalize_category_token("없음"),
    normalize_category_token("초기화"),
}

DISPLAY_CATEGORY_BY_NORM = {
    normalize_category_token(category): category
    for category in COMMON_FILTERABLE_CATEGORIES
}

# User input aliases for common category labels.
CATEGORY_ALIAS_BY_NORM = {
    normalize_category_token("electronics"): normalize_category_token("전자제품"),
    normalize_category_token("전자"): normalize_category_token("전자제품"),
    normalize_category_token("전자기기"): normalize_category_token("전자제품"),
    normalize_category_token("가전제품"): normalize_category_token("가전"),
    normalize_category_token("쿠폰"): normalize_category_token("상품권"),
}


def format_filterable_categories_for_help() -> str:
    return ", ".join([*COMMON_FILTERABLE_CATEGORIES, UNCATEGORIZED_FILTER_DISPLAY])


def canonicalize_category_input_token(token: str) -> str | None:
    if str(token).strip() == UNCATEGORIZED_FILTER_TOKEN:
        return UNCATEGORIZED_FILTER_TOKEN

    token_norm = normalize_category_token(token)

    canonical_category = DISPLAY_CATEGORY_BY_NORM.get(token_norm)
    if canonical_category is not None:
        return normalize_category_token(canonical_category)

    alias_norm = CATEGORY_ALIAS_BY_NORM.get(token_norm)
    if alias_norm is not None:
        return alias_norm

    if token_norm in UNCATEGORIZED_ALIAS_NORMS:
        return UNCATEGORIZED_FILTER_TOKEN

    return None


def format_excluded_categories_for_display(category_norms: set[str]) -> str:
    if not category_norms:
        return "없음"

    display_values: list[str] = []
    for category in COMMON_FILTERABLE_CATEGORIES:
        category_norm = normalize_category_token(category)
        if category_norm in category_norms:
            display_values.append(category)
    if UNCATEGORIZED_FILTER_TOKEN in category_norms:
        display_values.append(UNCATEGORIZED_FILTER_DISPLAY)
    return ", ".join(display_values) if display_values else "없음"


def parse_excluded_categories_input(raw: str | None) -> tuple[set[str], list[str]]:
    if raw is None:
        return set(), []

    raw_text = raw.strip()
    if not raw_text:
        return set(), []

    if normalize_category_token(raw_text) in CLEAR_ALL_FILTER_INPUT_NORMS:
        return set(), []

    tokens = [token.strip() for token in re.split(r"[,;\n]+", raw_text) if token.strip()]
    if not tokens:
        return set(), []

    parsed_norms: set[str] = set()
    unknown_tokens: list[str] = []
    for token in tokens:
        canonical_norm = canonicalize_category_input_token(token)
        if canonical_norm is None:
            unknown_tokens.append(token)
            continue
        parsed_norms.add(canonical_norm)

    return parsed_norms, unknown_tokens


def get_excluded_categories_autocomplete_choices(
    current: str,
) -> list[app_commands.Choice[str]]:
    current_text = (current or "").strip()
    raw_parts = [part.strip() for part in current_text.split(",")]
    fixed_parts = [part for part in raw_parts[:-1] if part]
    prefix = raw_parts[-1] if raw_parts else ""
    prefix_norm = normalize_category_token(prefix)

    selected_norms: set[str] = set()
    for token in fixed_parts:
        canonical_norm = canonicalize_category_input_token(token)
        if canonical_norm is not None:
            selected_norms.add(canonical_norm)

    choices: list[app_commands.Choice[str]] = []
    for category in COMMON_FILTERABLE_CATEGORIES:
        category_norm = normalize_category_token(category)
        if category_norm in selected_norms:
            continue
        if prefix and prefix_norm not in category_norm:
            continue
        value = ", ".join([*fixed_parts, category])
        if len(value) > 100:
            continue
        choices.append(app_commands.Choice(name=category, value=value))

    if UNCATEGORIZED_FILTER_TOKEN not in selected_norms:
        uncategorized_norm = normalize_category_token(UNCATEGORIZED_FILTER_DISPLAY)
        if not prefix or prefix_norm in uncategorized_norm:
            value = ", ".join([*fixed_parts, UNCATEGORIZED_FILTER_DISPLAY])
            if len(value) <= 100:
                choices.append(
                    app_commands.Choice(
                        name=UNCATEGORIZED_FILTER_DISPLAY,
                        value=value,
                    )
                )

    if (
        not current_text
        or "none".startswith(prefix_norm)
        or normalize_category_token("없음").startswith(prefix_norm)
    ):
        choices.append(app_commands.Choice(name="없음 (전체 허용)", value="없음"))

    return choices[:25]


def normalize_excluded_category_norms(category_norms: set[str]) -> set[str]:
    normalized: set[str] = set()
    for raw_token in category_norms:
        canonical_norm = canonicalize_category_input_token(raw_token)
        if canonical_norm is not None:
            normalized.add(canonical_norm)
    return normalized


def normalize_loaded_excluded_categories_by_guild(
    loaded: dict[int, set[str]],
) -> dict[int, set[str]]:
    normalized_data: dict[int, set[str]] = {}
    for guild_id, raw_norms in loaded.items():
        normalized_norms = normalize_excluded_category_norms(raw_norms)
        if not normalized_norms:
            continue
        normalized_data[guild_id] = normalized_norms
    return normalized_data


def get_deal_category_token(deal: Mapping[str, Any]) -> str:
    """
    Canonical filter token for a deal's category, computed once per deal so
    per-guild exclusion checks reduce to a set-membership test.
    """
    deal_category_raw = str(deal.get("category", "") or "").strip()
    if not deal_category_raw:
        return UNCATEGORIZED_FILTER_TOKEN

    canonical_deal_norm = canonicalize_category_input_token(deal_category_raw)
    if canonical_deal_norm is not None:
        return canonical_deal_norm

    deal_category_norm = normalize_category_token(deal_category_raw)
    if deal_category_norm in UNCATEGORIZED_ALIAS_NORMS:
        return UNCATEGORIZED_FILTER_TOKEN
    return deal_category_norm


def is_deal_excluded_for_norms(excluded_norms: set[str], deal: Mapping[str, Any]) -> bool:
    if not excluded_norms:
        return False
    return get_deal_category_token(deal) in excluded_norms


def get_deal_category_for_display(deal: Mapping[str, Any]) -> str:
    category = str(deal.get("category", "") or "").strip()
    return category if category else "미분류"
