from __future__ import annotations

import re
from typing import Any, Mapping

from discord import app_commands

ARCALIVE_DISPLAY = "\uc544\uce74\ub77c\uc774\ube0c"
QUASARZONE_DISPLAY = "\ud018\uc774\uc0ac\uc874"
FMKOREA_DISPLAY = "FM\ucf54\ub9ac\uc544"
PPOMPPU_DISPLAY = "\ubf50\ubfcc"
EOMISAE_DISPLAY = "\uc5b4\ubbf8\uc0c8"
NONE_DISPLAY = "\uc5c6\uc74c"

FILTERABLE_SITES: list[tuple[str, str]] = [
    ("arcalive", ARCALIVE_DISPLAY),
    ("quasarzone", QUASARZONE_DISPLAY),
    ("fmkorea", FMKOREA_DISPLAY),
    ("ppomppu", PPOMPPU_DISPLAY),
    ("eomisae", EOMISAE_DISPLAY),
]

SITE_DISPLAY_BY_CODE = {code: display for code, display in FILTERABLE_SITES}


def normalize_site_token(value: str) -> str:
    return re.sub(r"\s+", "", str(value)).casefold()


SITE_CODE_BY_DISPLAY_NORM = {
    normalize_site_token(display): code
    for code, display in FILTERABLE_SITES
}

SITE_ALIAS_BY_NORM = {
    normalize_site_token("arca"): "arcalive",
    normalize_site_token(ARCALIVE_DISPLAY): "arcalive",
    normalize_site_token("quasar"): "quasarzone",
    normalize_site_token("qz"): "quasarzone",
    normalize_site_token("\ud018\uc0ac"): "quasarzone",
    normalize_site_token("\ud018\uc874"): "quasarzone",
    normalize_site_token("\ud018\uc774\uc0ac"): "quasarzone",
    normalize_site_token(QUASARZONE_DISPLAY): "quasarzone",
    normalize_site_token("fm"): "fmkorea",
    normalize_site_token("\uc5d0\ud38c"): "fmkorea",
    normalize_site_token("\uc5d0\ud38c\ucf54\ub9ac\uc544"): "fmkorea",
    normalize_site_token(FMKOREA_DISPLAY): "fmkorea",
    normalize_site_token("pp"): "ppomppu",
    normalize_site_token(PPOMPPU_DISPLAY): "ppomppu",
    normalize_site_token("eomi"): "eomisae",
    normalize_site_token(EOMISAE_DISPLAY): "eomisae",
}

CLEAR_ALL_FILTER_INPUT_NORMS = {
    normalize_site_token("none"),
    normalize_site_token("null"),
    normalize_site_token("clear"),
    normalize_site_token(NONE_DISPLAY),
    normalize_site_token("\ucd08\uae30\ud654"),
}

# Tokens that expand to EVERY filterable site (\uc804\uccb4 \uc81c\uc678 \u2014 the opposite of
# \uc5c6\uc74c/\uc804\uccb4 \ud5c8\uc6a9).
SELECT_ALL_FILTER_INPUT_NORMS = {
    normalize_site_token("\uc804\uccb4"),
    normalize_site_token("\uc804\ubd80"),
    normalize_site_token("\ubaa8\ub450"),
    normalize_site_token("all"),
    normalize_site_token("everything"),
}
ALL_SITE_CODES = {code for code, _ in FILTERABLE_SITES}
SELECT_ALL_DISPLAY = "\uc804\uccb4"


def format_filterable_sites_for_help() -> str:
    return ", ".join(display for _, display in FILTERABLE_SITES)


def canonicalize_site_input_token(token: str) -> str | None:
    normalized = normalize_site_token(token)
    if not normalized:
        return None

    if normalized in SITE_DISPLAY_BY_CODE:
        return normalized

    from_display = SITE_CODE_BY_DISPLAY_NORM.get(normalized)
    if from_display is not None:
        return from_display

    from_alias = SITE_ALIAS_BY_NORM.get(normalized)
    if from_alias is not None:
        return from_alias

    return None


def format_excluded_sites_for_display(site_codes: set[str]) -> str:
    if not site_codes:
        return NONE_DISPLAY

    display_values = [
        SITE_DISPLAY_BY_CODE[code]
        for code, _ in FILTERABLE_SITES
        if code in site_codes
    ]
    return ", ".join(display_values) if display_values else NONE_DISPLAY


def parse_excluded_sites_input(raw: str | None) -> tuple[set[str], list[str]]:
    if raw is None:
        return set(), []

    raw_text = raw.strip()
    if not raw_text:
        return set(), []

    if normalize_site_token(raw_text) in CLEAR_ALL_FILTER_INPUT_NORMS:
        return set(), []

    tokens = [token.strip() for token in re.split(r"[,;\n]+", raw_text) if token.strip()]
    if not tokens:
        return set(), []

    parsed_codes: set[str] = set()
    unknown_tokens: list[str] = []

    for token in tokens:
        if normalize_site_token(token) in SELECT_ALL_FILTER_INPUT_NORMS:
            parsed_codes |= ALL_SITE_CODES
            continue
        canonical_code = canonicalize_site_input_token(token)
        if canonical_code is None:
            unknown_tokens.append(token)
            continue
        parsed_codes.add(canonical_code)

    return parsed_codes, unknown_tokens


def get_excluded_sites_autocomplete_choices(
    current: str,
) -> list[app_commands.Choice[str]]:
    current_text = (current or "").strip()
    raw_parts = [part.strip() for part in current_text.split(",")]
    fixed_parts = [part for part in raw_parts[:-1] if part]
    prefix = raw_parts[-1] if raw_parts else ""
    prefix_norm = normalize_site_token(prefix)

    selected_codes: set[str] = set()
    for token in fixed_parts:
        canonical_code = canonicalize_site_input_token(token)
        if canonical_code is not None:
            selected_codes.add(canonical_code)

    choices: list[app_commands.Choice[str]] = []
    for code, display in FILTERABLE_SITES:
        if code in selected_codes:
            continue

        display_norm = normalize_site_token(display)
        if prefix and prefix_norm not in display_norm and prefix_norm not in code:
            continue

        value = ", ".join([*fixed_parts, display])
        if len(value) > 100:
            continue
        choices.append(app_commands.Choice(name=display, value=value))

    if (
        not current_text
        or "all".startswith(prefix_norm)
        or normalize_site_token(SELECT_ALL_DISPLAY).startswith(prefix_norm)
    ):
        choices.append(
            app_commands.Choice(
                name="\uc804\uccb4 (\ubaa8\ub4e0 \uc0ac\uc774\ud2b8 \uc81c\uc678)",
                value=SELECT_ALL_DISPLAY,
            )
        )

    if (
        not current_text
        or "none".startswith(prefix_norm)
        or normalize_site_token(NONE_DISPLAY).startswith(prefix_norm)
    ):
        choices.append(
            app_commands.Choice(
                name=f"{NONE_DISPLAY} (\uc804\uccb4 \ud5c8\uc6a9)",
                value=NONE_DISPLAY,
            )
        )

    return choices[:25]


def normalize_excluded_site_codes(site_codes: set[str]) -> set[str]:
    normalized: set[str] = set()
    for raw_token in site_codes:
        canonical_code = canonicalize_site_input_token(raw_token)
        if canonical_code is not None:
            normalized.add(canonical_code)
    return normalized


def normalize_loaded_excluded_sites_by_guild(
    loaded: dict[int, set[str]],
) -> dict[int, set[str]]:
    normalized_data: dict[int, set[str]] = {}
    for guild_id, raw_codes in loaded.items():
        normalized_codes = normalize_excluded_site_codes(raw_codes)
        if not normalized_codes:
            continue
        normalized_data[guild_id] = normalized_codes
    return normalized_data


def get_deal_site_code(deal: Mapping[str, Any]) -> str | None:
    """Canonical site code for a deal, computed once per deal."""
    return canonicalize_site_input_token(str(deal.get("site_code", "") or ""))


def is_deal_site_excluded_for_codes(
    excluded_site_codes: set[str],
    deal: Mapping[str, Any],
) -> bool:
    if not excluded_site_codes:
        return False

    site_code = get_deal_site_code(deal)
    if site_code is None:
        return False
    return site_code in excluded_site_codes
