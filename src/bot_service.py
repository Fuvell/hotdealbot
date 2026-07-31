from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import discord
from discord.ext import commands

try:
    from .crawlers.arca_crawler import fetch_hot_deals_arca
    from .crawlers.eomisae_crawler import enrich_deal_eomisae, fetch_hot_deals_eomisae
    from .crawlers.fmkorea_crawler import enrich_deal_fmkorea, fetch_hot_deals_fmkorea
    from .crawlers.ppomppu_crawler import fetch_hot_deals_ppomppu
    from .category_filters import (
        get_deal_category_for_display,
        get_deal_category_token,
        is_deal_excluded_for_norms,
        normalize_loaded_excluded_categories_by_guild,
    )
    from .site_filters import (
        get_deal_site_code,
        is_deal_site_excluded_for_codes,
        normalize_loaded_excluded_sites_by_guild,
    )
    from .deal_key import (
        compact_existing_key,
        get_lookup_keys_for_deal,
        get_storage_key_for_deal,
    )
    from .crawlers.quasarzone_crawler import fetch_hot_deals
    from .error_logging import get_error_logger, get_runtime_logger
    from .storage import (
        init_db,
        load_excluded_categories_by_guild,
        load_excluded_sites_by_guild,
        load_registered_channels,
        load_sent_deal_ids,
        mark_deals_sent,
        purge_sent_deals_older_than,
        remove_registered_channel_by_channel_id,
        remove_registered_channel_by_guild_id,
        clear_user_alert_keywords,
        load_user_alert_keywords,
        remove_user_alert_keyword,
        replace_excluded_categories_for_guild,
        replace_excluded_sites_for_guild,
        replace_sent_deal_ids,
        upsert_user_alert_keyword,
        upsert_registered_channel,
        vacuum_db,
    )
except ImportError:
    from crawlers.arca_crawler import fetch_hot_deals_arca
    from crawlers.eomisae_crawler import enrich_deal_eomisae, fetch_hot_deals_eomisae
    from crawlers.fmkorea_crawler import enrich_deal_fmkorea, fetch_hot_deals_fmkorea
    from crawlers.ppomppu_crawler import fetch_hot_deals_ppomppu
    from category_filters import (
        get_deal_category_for_display,
        get_deal_category_token,
        is_deal_excluded_for_norms,
        normalize_loaded_excluded_categories_by_guild,
    )
    from site_filters import (
        get_deal_site_code,
        is_deal_site_excluded_for_codes,
        normalize_loaded_excluded_sites_by_guild,
    )
    from deal_key import (
        compact_existing_key,
        get_lookup_keys_for_deal,
        get_storage_key_for_deal,
    )
    from crawlers.quasarzone_crawler import fetch_hot_deals
    from error_logging import get_error_logger, get_runtime_logger
    from storage import (
        init_db,
        load_excluded_categories_by_guild,
        load_excluded_sites_by_guild,
        load_registered_channels,
        load_sent_deal_ids,
        mark_deals_sent,
        purge_sent_deals_older_than,
        remove_registered_channel_by_channel_id,
        remove_registered_channel_by_guild_id,
        clear_user_alert_keywords,
        load_user_alert_keywords,
        remove_user_alert_keyword,
        replace_excluded_categories_for_guild,
        replace_excluded_sites_for_guild,
        replace_sent_deal_ids,
        upsert_user_alert_keyword,
        upsert_registered_channel,
        vacuum_db,
    )

error_logger = get_error_logger()
runtime_logger = get_runtime_logger()
PROJECT_BASE_DIR = Path(__file__).resolve().parent.parent

# Detail-page enrichment runs only for NEW deals (post-dedup), keyed by site.
DEAL_ENRICHERS: dict[str, Callable[[dict], None]] = {
    "fmkorea": enrich_deal_fmkorea,
    "eomisae": enrich_deal_eomisae,
}


@dataclass(frozen=True)
class AlertKeywordRule:
    keyword_norm: str
    keyword_raw: str
    keyword_tokens: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    retryable_failure: bool = False
    channel_stale: bool = False
    reason: str = ""


@dataclass
class PlannedDeal:
    deal: dict[str, Any]
    lookup_keys: list[str]
    storage_key: str | None
    channel_targets: list[tuple[int, int]]  # (guild_id, channel_id)
    alert_matches: dict[int, list[str]]


@dataclass
class DeliveryPlan:
    items: list[PlannedDeal] = field(default_factory=list)


@dataclass
class DealOutcome:
    planned: PlannedDeal
    delivered: bool = False
    retryable_failures: int = 0
    permanent_failures: int = 0
    reasons: list[str] = field(default_factory=list)


class HotDealService:
    ALERT_KEYWORD_MAX_COUNT = 5
    # NOTE: minimum is 3 because the anti-abuse check rejects any keyword
    # whose normalized form is 2 characters or fewer.
    ALERT_KEYWORD_MIN_LEN = 3
    ALERT_KEYWORD_MAX_LEN = 15
    SENT_DEAL_RETENTION_DAYS = 60
    CRAWLER_TIMEOUT_SECONDS = 20.0
    CRAWLER_TIMEOUT_SECONDS_BY_NAME = {
        # fmkorea may fall back to Playwright; eomisae may need detail pages.
        "fmkorea": 60.0,
        "eomisae": 30.0,
    }
    CRAWLER_RETRY_DELAYS_SECONDS = (1.0, 2.0)
    CRAWLER_CIRCUIT_FAIL_THRESHOLD = 3
    CRAWLER_CIRCUIT_OPEN_SECONDS = 300.0
    DISCORD_SEND_RETRY_DELAYS_SECONDS = (1.0, 2.0)
    ALERT_DM_RATE_LIMIT_WINDOW_SECONDS = 3600.0
    ALERT_DM_RATE_LIMIT_MAX_PER_WINDOW = 20
    ALERT_DM_SUPPRESS_LOG_COOLDOWN_SECONDS = 300.0
    ALERT_UPDATE_COOLDOWN_SECONDS = 4.0
    CRAWLER_PARSE_ANOMALY_THRESHOLD = 3
    CRAWLER_MIN_EXPECTED_COUNT = 1
    DISCORD_API_CHECK_TIMEOUT_SECONDS = 10.0
    ENRICH_TIMEOUT_SECONDS = 15.0
    EMBEDS_PER_MESSAGE = 10
    # Flood guard: max new deals accepted per site per cycle; the overflow is
    # marked seen without posting (protects channels after outages/first runs).
    MAX_NEW_DEALS_PER_SITE_PER_CYCLE = 10
    # Give-up thresholds for deals that keep failing to deliver.
    PERMANENT_FAILURE_GIVE_UP_CYCLES = 3
    ANY_FAILURE_GIVE_UP_CYCLES = 10

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.registered_channels: dict[int, int] = {}
        self.excluded_categories_by_guild: dict[int, set[str]] = {}
        self.excluded_sites_by_guild: dict[int, set[str]] = {}
        self.alert_rules_by_user: dict[int, dict[str, AlertKeywordRule]] = {}
        self.posted_deal_ids: set[str] = set()
        self.commands_synced = False
        self.deal_state_lock = asyncio.Lock()
        self.alert_dm_timestamps_by_user: dict[int, deque[float]] = defaultdict(deque)
        self.alert_dm_suppress_log_until_by_user: dict[int, float] = {}
        self.crawler_consecutive_failures: dict[str, int] = defaultdict(int)
        self.crawler_circuit_open_until: dict[str, float] = {}
        self.crawler_parse_anomaly_counts: dict[str, int] = defaultdict(int)
        self.crawler_last_success_at: dict[str, datetime] = {}
        self.crawler_last_deal_count: dict[str, int] = {}
        self.runtime_metrics: dict[str, int] = defaultdict(int)
        self.alert_last_update_monotonic_by_user: dict[int, float] = {}
        self.delivery_failure_counts: dict[str, int] = {}
        self.last_sent_deals_purge_monotonic = 0.0

        allowlist_raw = os.getenv(
            "SOURCE_DOMAIN_ALLOWLIST",
            "arca.live,quasarzone.com,fmkorea.com,ppomppu.co.kr,eomisae.co.kr",
        )
        self.allowed_source_domains = {
            domain.strip().lower()
            for domain in allowlist_raw.split(",")
            if domain.strip()
        }
        self.enforce_source_domain_allowlist = (
            str(os.getenv("ENFORCE_SOURCE_DOMAIN_ALLOWLIST", "1")).strip() != "0"
        )

    ############################
    # State loading / config
    ############################
    def initialize_state(self) -> None:
        init_db()
        purged_rows = purge_sent_deals_older_than(self.SENT_DEAL_RETENTION_DAYS)
        self.refresh_runtime_config()
        self.refresh_alert_keywords()
        self.posted_deal_ids = load_sent_deal_ids()
        before_compact_count = len(self.posted_deal_ids)
        compacted_sent_keys = {
            key for key in (compact_existing_key(k) for k in self.posted_deal_ids) if key
        }
        compacted = compacted_sent_keys != self.posted_deal_ids
        if compacted:
            replace_sent_deal_ids(compacted_sent_keys)
            self.posted_deal_ids = compacted_sent_keys
            runtime_logger.info(
                "Compacted sent deal keys: "
                f"{before_compact_count} rows -> {len(compacted_sent_keys)} rows"
            )

        if purged_rows or compacted:
            try:
                vacuum_db()
            except Exception:
                error_logger.exception("Failed to VACUUM after startup purge.")

        runtime_logger.info(
            "Loaded state from SQLite: "
            f"channels={len(self.registered_channels)}, "
            f"category-filter-configs={len(self.excluded_categories_by_guild)}, "
            f"site-filter-configs={len(self.excluded_sites_by_guild)}, "
            f"alert-users={len(self.alert_rules_by_user)}, "
            f"sent_ids={len(self.posted_deal_ids)}"
        )
        if purged_rows:
            runtime_logger.info(
                "Purged old sent-deal rows: "
                f"{purged_rows} rows older than {self.SENT_DEAL_RETENTION_DAYS} days"
            )

    def refresh_runtime_config(self) -> None:
        self.registered_channels = load_registered_channels()
        loaded_excluded_categories = load_excluded_categories_by_guild()
        loaded_excluded_sites = load_excluded_sites_by_guild()
        self.excluded_categories_by_guild = normalize_loaded_excluded_categories_by_guild(
            loaded_excluded_categories
        )
        self.excluded_sites_by_guild = normalize_loaded_excluded_sites_by_guild(
            loaded_excluded_sites
        )
        for guild_id in self.registered_channels:
            raw_norms = loaded_excluded_categories.get(guild_id, set())
            normalized_norms = self.excluded_categories_by_guild.get(guild_id, set())
            if set(raw_norms) != set(normalized_norms):
                replace_excluded_categories_for_guild(guild_id, normalized_norms)
            raw_sites = loaded_excluded_sites.get(guild_id, set())
            normalized_sites = self.excluded_sites_by_guild.get(guild_id, set())
            if set(raw_sites) != set(normalized_sites):
                replace_excluded_sites_for_guild(guild_id, normalized_sites)

    def get_excluded_category_norms_for_guild(self, guild_id: int) -> set[str]:
        return self.excluded_categories_by_guild.get(guild_id, set())

    def get_excluded_site_codes_for_guild(self, guild_id: int) -> set[str]:
        return self.excluded_sites_by_guild.get(guild_id, set())

    def set_excluded_category_norms_for_guild(
        self,
        guild_id: int,
        category_norms: set[str],
    ) -> None:
        if category_norms:
            self.excluded_categories_by_guild[guild_id] = set(category_norms)
            return
        self.excluded_categories_by_guild.pop(guild_id, None)

    def set_excluded_site_codes_for_guild(
        self,
        guild_id: int,
        site_codes: set[str],
    ) -> None:
        if site_codes:
            self.excluded_sites_by_guild[guild_id] = set(site_codes)
            return
        self.excluded_sites_by_guild.pop(guild_id, None)

    def replace_excluded_categories_for_guild(
        self,
        guild_id: int,
        category_norms: set[str],
    ) -> None:
        replace_excluded_categories_for_guild(guild_id, category_norms)
        self.set_excluded_category_norms_for_guild(guild_id, category_norms)

    def replace_excluded_sites_for_guild(
        self,
        guild_id: int,
        site_codes: set[str],
    ) -> None:
        replace_excluded_sites_for_guild(guild_id, site_codes)
        self.set_excluded_site_codes_for_guild(guild_id, site_codes)

    def register_channel(
        self,
        guild_id: int,
        channel_id: int,
        guild_name: str = "",
    ) -> None:
        upsert_registered_channel(guild_id, channel_id, guild_name)
        self.registered_channels[guild_id] = channel_id
        self.replace_excluded_categories_for_guild(guild_id, set())
        self.replace_excluded_sites_for_guild(guild_id, set())

    def update_registered_guild_name(self, guild_id: int, guild_name: str) -> bool:
        channel_id = self.registered_channels.get(guild_id)
        if channel_id is None:
            return False
        upsert_registered_channel(guild_id, channel_id, guild_name)
        return True

    ############################
    # Alert keywords
    ############################
    @staticmethod
    def normalize_alert_text(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.sub(r"[\W_]+", "", normalized)

    def build_alert_rule(self, keyword_raw: str) -> AlertKeywordRule | None:
        normalized_raw = str(keyword_raw or "").strip()
        if not normalized_raw:
            return None

        keyword_norm = self.normalize_alert_text(normalized_raw)
        if not keyword_norm:
            return None

        normalized_for_tokens = unicodedata.normalize("NFKC", normalized_raw).casefold()
        keyword_tokens: list[str] = []
        for chunk in re.split(r"\s+", normalized_for_tokens):
            chunk_norm = self.normalize_alert_text(chunk)
            if not chunk_norm or chunk_norm in keyword_tokens:
                continue
            keyword_tokens.append(chunk_norm)

        if keyword_norm not in keyword_tokens:
            keyword_tokens.append(keyword_norm)

        return AlertKeywordRule(
            keyword_norm=keyword_norm,
            keyword_raw=normalized_raw,
            keyword_tokens=tuple(keyword_tokens),
        )

    def refresh_alert_keywords(self) -> None:
        loaded_keywords = load_user_alert_keywords()
        loaded_rules: dict[int, dict[str, AlertKeywordRule]] = {}

        for user_id, keyword_map in loaded_keywords.items():
            user_rules: dict[str, AlertKeywordRule] = {}
            for keyword_raw in keyword_map.values():
                rule = self.build_alert_rule(keyword_raw)
                if rule is None:
                    continue
                user_rules[rule.keyword_norm] = rule

            if user_rules:
                loaded_rules[int(user_id)] = user_rules

        self.alert_rules_by_user = loaded_rules

    def get_alert_keywords_for_user(self, user_id: int) -> list[str]:
        user_rules = self.alert_rules_by_user.get(int(user_id), {})
        return [rule.keyword_raw for rule in user_rules.values()]

    def get_alert_keyword_count_for_user(self, user_id: int) -> int:
        return len(self.alert_rules_by_user.get(int(user_id), {}))

    def try_alert_update(self, user_id: int) -> tuple[bool, float]:
        """Atomically check the cooldown and, if allowed, consume it."""
        user_id_int = int(user_id)
        now = monotonic()
        last_update = self.alert_last_update_monotonic_by_user.get(user_id_int, 0.0)
        elapsed = now - last_update
        if elapsed >= self.ALERT_UPDATE_COOLDOWN_SECONDS:
            self.alert_last_update_monotonic_by_user[user_id_int] = now
            return True, 0.0
        return False, max(0.0, self.ALERT_UPDATE_COOLDOWN_SECONDS - elapsed)

    def get_alert_keyword_abuse_reason(self, rule: AlertKeywordRule) -> str | None:
        keyword_norm = rule.keyword_norm

        if len(keyword_norm) <= 2:
            return "keyword is too broad (2 chars or fewer after normalization)"

        if len(set(keyword_norm)) == 1 and len(keyword_norm) >= 3:
            return "keyword repeats the same character too much"

        if re.fullmatch(r"[a-z]+", keyword_norm) and len(keyword_norm) <= 3:
            return "very short alphabetic keyword is too broad"

        generic_norms = {
            self.normalize_alert_text(v)
            for v in (
                "deal",
                "sale",
                "hotdeal",
                "discount",
                "무료",
                "핫딜",
                "특가",
                "세일",
                "할인",
            )
        }
        if keyword_norm in generic_norms:
            return "generic keyword is too broad"

        return None

    def has_alert_keyword_norm_for_user(self, user_id: int, keyword_norm: str) -> bool:
        user_rules = self.alert_rules_by_user.get(int(user_id), {})
        return str(keyword_norm) in user_rules

    def add_alert_rule_for_user(self, user_id: int, keyword_raw: str) -> bool:
        rule = self.build_alert_rule(keyword_raw)
        if rule is None:
            return False

        user_id_int = int(user_id)
        user_rules = self.alert_rules_by_user.setdefault(user_id_int, {})
        was_new = rule.keyword_norm not in user_rules
        user_rules[rule.keyword_norm] = rule
        upsert_user_alert_keyword(user_id_int, rule.keyword_norm, rule.keyword_raw)
        return was_new

    def remove_alert_keyword_norm_for_user(self, user_id: int, keyword_norm: str) -> bool:
        user_id_int = int(user_id)
        keyword_norm_text = str(keyword_norm)
        removed_in_memory = False

        user_rules = self.alert_rules_by_user.get(user_id_int)
        if user_rules and keyword_norm_text in user_rules:
            user_rules.pop(keyword_norm_text, None)
            removed_in_memory = True
            if not user_rules:
                self.alert_rules_by_user.pop(user_id_int, None)

        removed_in_db = remove_user_alert_keyword(user_id_int, keyword_norm_text)
        return removed_in_memory or removed_in_db

    def clear_alert_keywords_for_user(self, user_id: int) -> int:
        user_id_int = int(user_id)
        removed_in_db = clear_user_alert_keywords(user_id_int)
        removed_in_memory = len(self.alert_rules_by_user.get(user_id_int, {}))
        self.alert_rules_by_user.pop(user_id_int, None)
        return max(removed_in_db, removed_in_memory)

    def get_alert_matches_for_deal(self, deal: Mapping[str, Any]) -> dict[int, list[str]]:
        if not self.alert_rules_by_user:
            return {}

        searchable_text = " ".join(
            [
                str(deal.get("title", "") or ""),
                str(deal.get("category", "") or ""),
                str(deal.get("site_name", "") or ""),
            ]
        )
        normalized_text = self.normalize_alert_text(searchable_text)
        if not normalized_text:
            return {}

        matches_by_user: dict[int, list[str]] = {}
        for user_id, user_rules in self.alert_rules_by_user.items():
            matched_keywords: list[str] = []
            for rule in user_rules.values():
                if rule.keyword_norm and rule.keyword_norm in normalized_text:
                    matched_keywords.append(rule.keyword_raw)
                    continue
                if len(rule.keyword_tokens) > 1 and all(
                    token in normalized_text for token in rule.keyword_tokens
                ):
                    matched_keywords.append(rule.keyword_raw)

            if matched_keywords:
                matches_by_user[user_id] = matched_keywords

        return matches_by_user

    ############################
    # DM rate limiting
    ############################
    def _prune_alert_dm_window(self, user_id: int, now: float) -> None:
        timestamps = self.alert_dm_timestamps_by_user.get(user_id)
        if timestamps is None:
            return

        window = self.ALERT_DM_RATE_LIMIT_WINDOW_SECONDS
        while timestamps and (now - timestamps[0]) > window:
            timestamps.popleft()

        if not timestamps:
            self.alert_dm_timestamps_by_user.pop(user_id, None)

    def _can_send_alert_dm(self, user_id: int) -> bool:
        now = monotonic()
        self._prune_alert_dm_window(user_id, now)
        timestamps = self.alert_dm_timestamps_by_user.setdefault(user_id, deque())

        if len(timestamps) < self.ALERT_DM_RATE_LIMIT_MAX_PER_WINDOW:
            return True

        suppress_until = self.alert_dm_suppress_log_until_by_user.get(user_id, 0.0)
        if now >= suppress_until:
            self.alert_dm_suppress_log_until_by_user[user_id] = (
                now + self.ALERT_DM_SUPPRESS_LOG_COOLDOWN_SECONDS
            )
            error_logger.error(
                "Alert DM rate limit reached. "
                f"user_id={user_id}, window={self.ALERT_DM_RATE_LIMIT_WINDOW_SECONDS}s, "
                f"max={self.ALERT_DM_RATE_LIMIT_MAX_PER_WINDOW}"
            )
        return False

    def _record_alert_dm_sent(self, user_id: int) -> None:
        now = monotonic()
        timestamps = self.alert_dm_timestamps_by_user.setdefault(user_id, deque())
        timestamps.append(now)
        self._prune_alert_dm_window(user_id, now)

    ############################
    # Source URL allowlist
    ############################
    @staticmethod
    def _is_retryable_http_exception(error: discord.HTTPException) -> bool:
        status = getattr(error, "status", None)
        if status is None:
            return True
        return int(status) == 429 or int(status) >= 500

    def is_source_url_allowed(self, url: str | None) -> bool:
        if not self.enforce_source_domain_allowlist:
            return True

        raw_url = str(url or "").strip()
        if not raw_url:
            return False

        try:
            parsed = urlparse(raw_url)
        except Exception:
            return False

        if parsed.scheme not in {"http", "https"}:
            return False

        host = str(parsed.hostname or "").lower().strip(".")
        if not host:
            return False

        return any(
            host == domain or host.endswith(f".{domain}")
            for domain in self.allowed_source_domains
        )

    ############################
    # Delivery pipeline
    ############################
    def get_deal_keys(self, deal: Mapping[str, object]) -> list[str]:
        return get_lookup_keys_for_deal(deal)

    def build_delivery_plan(self, all_deals: list[dict]) -> DeliveryPlan:
        """
        Phase 1 of the delivery cycle (caller must hold deal_state_lock):
        dedup fetched deals, apply the per-site burst cap, and compute each
        new deal's eligible channels and alert matches from a single snapshot.
        Deals with no recipients are marked seen in one batch.
        """
        plan = DeliveryPlan()
        seen_in_cycle: set[str] = set()
        new_count_by_site: dict[str, int] = defaultdict(int)
        burst_skipped_by_site: dict[str, int] = defaultdict(int)
        keys_to_mark: list[str] = []

        for deal in all_deals:
            lookup_keys = self.get_deal_keys(deal)
            if not lookup_keys:
                continue

            if any(key in seen_in_cycle for key in lookup_keys):
                continue
            seen_in_cycle.update(lookup_keys)

            if any(key in self.posted_deal_ids for key in lookup_keys):
                continue

            storage_key = get_storage_key_for_deal(deal)
            site_code = get_deal_site_code(deal) or str(deal.get("site_code", "") or "")
            category_token = get_deal_category_token(deal)

            new_count_by_site[site_code] += 1
            if new_count_by_site[site_code] > self.MAX_NEW_DEALS_PER_SITE_PER_CYCLE:
                burst_skipped_by_site[site_code] += 1
                if storage_key and storage_key not in self.posted_deal_ids:
                    self.posted_deal_ids.add(storage_key)
                    keys_to_mark.append(storage_key)
                continue

            alert_matches = self.get_alert_matches_for_deal(deal)

            channel_targets: list[tuple[int, int]] = []
            for guild_id, channel_id in self.registered_channels.items():
                if category_token in self.excluded_categories_by_guild.get(guild_id, set()):
                    continue
                if site_code and site_code in self.excluded_sites_by_guild.get(guild_id, set()):
                    continue
                channel_targets.append((guild_id, channel_id))

            if not channel_targets and not alert_matches:
                if storage_key and storage_key not in self.posted_deal_ids:
                    self.posted_deal_ids.add(storage_key)
                    keys_to_mark.append(storage_key)
                continue

            plan.items.append(
                PlannedDeal(
                    deal=deal,
                    lookup_keys=lookup_keys,
                    storage_key=storage_key,
                    channel_targets=channel_targets,
                    alert_matches=alert_matches,
                )
            )

        if keys_to_mark:
            mark_deals_sent(keys_to_mark)

        for site, skipped_count in burst_skipped_by_site.items():
            runtime_logger.info(
                f"Burst cap: suppressed {skipped_count} extra new deal(s) from "
                f"site '{site}' this cycle."
            )

        return plan

    async def enrich_new_deals(self, items: list[PlannedDeal]) -> None:
        """Fetch detail-page image/price for new deals only (best-effort)."""
        loop = asyncio.get_running_loop()

        async def _enrich_one(item: PlannedDeal) -> None:
            site_code = str(item.deal.get("site_code", "") or "")
            enricher = DEAL_ENRICHERS.get(site_code)
            if enricher is None:
                return
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, enricher, item.deal),
                    timeout=self.ENRICH_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                runtime_logger.info(
                    f"Deal enrichment timed out. site={site_code}, "
                    f"id={item.deal.get('id', 'unknown')}"
                )
            except Exception:
                error_logger.exception(
                    f"Deal enrichment failed. site={site_code}, "
                    f"id={item.deal.get('id', 'unknown')}"
                )

        if items:
            await asyncio.gather(*(_enrich_one(item) for item in items))

    async def deliver_plan(
        self,
        plan: DeliveryPlan,
    ) -> tuple[list[DealOutcome], set[int]]:
        """
        Phase 2 of the delivery cycle (no lock held): send embeds in batches
        of up to EMBEDS_PER_MESSAGE per channel, all channels concurrently,
        plus keyword-alert DMs grouped per user.
        """
        outcomes: dict[int, DealOutcome] = {
            id(item): DealOutcome(planned=item) for item in plan.items
        }
        stale_channel_ids: set[int] = set()

        by_channel: dict[int, tuple[int, list[PlannedDeal]]] = {}
        for item in plan.items:
            for guild_id, channel_id in item.channel_targets:
                if channel_id not in by_channel:
                    by_channel[channel_id] = (guild_id, [])
                by_channel[channel_id][1].append(item)

        async def deliver_channel(
            channel_id: int,
            guild_id: int,
            items: list[PlannedDeal],
        ) -> None:
            async with self.deal_state_lock:
                if self.registered_channels.get(guild_id) != channel_id:
                    return

            channel = await self.resolve_channel(channel_id)
            if channel is None:
                for item in items:
                    outcome = outcomes[id(item)]
                    outcome.retryable_failures += 1
                    outcome.reasons.append(f"channel:{channel_id}:unresolved")
                if await self.is_registered_channel_stale(channel_id):
                    stale_channel_ids.add(channel_id)
                return

            if not isinstance(channel, discord.abc.Messageable):
                stale_channel_ids.add(channel_id)
                for item in items:
                    outcome = outcomes[id(item)]
                    outcome.permanent_failures += 1
                    outcome.reasons.append(f"channel:{channel_id}:not-messageable")
                return

            sendable: list[PlannedDeal] = []
            for item in items:
                if not self.is_source_url_allowed(str(item.deal.get("url", "") or "")):
                    outcome = outcomes[id(item)]
                    outcome.permanent_failures += 1
                    outcome.reasons.append(f"channel:{channel_id}:source-url-disallowed")
                    error_logger.error(
                        "Blocked deal send due to source-domain allowlist. "
                        f"channel_id={channel_id}, url={item.deal.get('url', '')}"
                    )
                    continue
                sendable.append(item)

            batch_size = self.EMBEDS_PER_MESSAGE
            for start in range(0, len(sendable), batch_size):
                chunk = sendable[start:start + batch_size]
                embeds = [self.build_deal_embed(item.deal) for item in chunk]
                result = await self.send_embeds_with_retry(channel, embeds, channel_id)

                if result.success:
                    for item in chunk:
                        outcomes[id(item)].delivered = True
                    continue

                if result.channel_stale:
                    stale_channel_ids.add(channel_id)
                    for item in chunk:
                        outcome = outcomes[id(item)]
                        outcome.permanent_failures += 1
                        outcome.reasons.append(f"channel:{channel_id}:{result.reason}")
                    return

                if not result.retryable_failure and len(chunk) > 1:
                    # A batch-level 400 usually means one bad embed; isolate it
                    # by falling back to individual sends.
                    for item, embed in zip(chunk, embeds):
                        single = await self.send_embeds_with_retry(
                            channel, [embed], channel_id
                        )
                        outcome = outcomes[id(item)]
                        if single.success:
                            outcome.delivered = True
                        elif single.channel_stale:
                            stale_channel_ids.add(channel_id)
                            outcome.permanent_failures += 1
                            outcome.reasons.append(
                                f"channel:{channel_id}:{single.reason}"
                            )
                            return
                        elif single.retryable_failure:
                            outcome.retryable_failures += 1
                            outcome.reasons.append(
                                f"channel:{channel_id}:{single.reason}"
                            )
                        else:
                            outcome.permanent_failures += 1
                            outcome.reasons.append(
                                f"channel:{channel_id}:{single.reason}"
                            )
                    continue

                for item in chunk:
                    outcome = outcomes[id(item)]
                    if result.retryable_failure:
                        outcome.retryable_failures += 1
                    else:
                        outcome.permanent_failures += 1
                    outcome.reasons.append(f"channel:{channel_id}:{result.reason}")

        async def deliver_dms() -> None:
            deals_by_user: dict[int, list[tuple[PlannedDeal, list[str]]]] = {}
            for item in plan.items:
                for user_id, matched_keywords in item.alert_matches.items():
                    deals_by_user.setdefault(user_id, []).append(
                        (item, matched_keywords)
                    )

            async def dm_user(
                user_id: int,
                entries: list[tuple[PlannedDeal, list[str]]],
            ) -> None:
                for item, matched_keywords in entries:
                    try:
                        result = await self.send_alert_dm(
                            user_id, item.deal, matched_keywords
                        )
                    except Exception as e:
                        error_logger.exception(
                            "Unexpected error while sending keyword alert DM. "
                            f"user_id={user_id}"
                        )
                        outcome = outcomes[id(item)]
                        outcome.retryable_failures += 1
                        outcome.reasons.append(f"dm:{user_id}:{type(e).__name__}")
                        continue

                    outcome = outcomes[id(item)]
                    if result.success:
                        outcome.delivered = True
                    elif result.retryable_failure:
                        outcome.retryable_failures += 1
                        outcome.reasons.append(f"dm:{user_id}:{result.reason}")
                    else:
                        outcome.permanent_failures += 1
                        outcome.reasons.append(f"dm:{user_id}:{result.reason}")

            if deals_by_user:
                await asyncio.gather(
                    *(dm_user(uid, entries) for uid, entries in deals_by_user.items())
                )

        channel_tasks = [
            deliver_channel(channel_id, guild_id, items)
            for channel_id, (guild_id, items) in by_channel.items()
        ]
        await asyncio.gather(*channel_tasks, deliver_dms())

        return list(outcomes.values()), stale_channel_ids

    def finalize_deliveries(
        self,
        outcomes: list[DealOutcome],
        stale_channel_ids: set[int],
    ) -> None:
        """
        Phase 3 of the delivery cycle (caller must hold deal_state_lock):
        batch-mark delivered deals as seen, unregister stale channels, and
        give up on deals that keep failing so they never retry forever.
        """
        for stale_channel_id in stale_channel_ids:
            if self.unregister_channel_by_channel_id(stale_channel_id):
                runtime_logger.info(
                    f"Removed stale channel from DB during send: {stale_channel_id}"
                )

        keys_to_mark: list[str] = []
        for outcome in outcomes:
            storage_key = outcome.planned.storage_key

            if outcome.delivered:
                if storage_key:
                    if storage_key not in self.posted_deal_ids:
                        self.posted_deal_ids.add(storage_key)
                        keys_to_mark.append(storage_key)
                    self.delivery_failure_counts.pop(storage_key, None)
                continue

            if storage_key is None:
                continue

            failure_count = self.delivery_failure_counts.get(storage_key, 0) + 1
            self.delivery_failure_counts[storage_key] = failure_count
            permanent_only = (
                outcome.permanent_failures > 0 and outcome.retryable_failures == 0
            )
            give_up = (
                permanent_only
                and failure_count >= self.PERMANENT_FAILURE_GIVE_UP_CYCLES
            ) or failure_count >= self.ANY_FAILURE_GIVE_UP_CYCLES

            if give_up:
                self.delivery_failure_counts.pop(storage_key, None)
                if storage_key not in self.posted_deal_ids:
                    self.posted_deal_ids.add(storage_key)
                    keys_to_mark.append(storage_key)
                error_logger.error(
                    "Giving up on undeliverable deal after repeated failures. "
                    f"key={storage_key}, cycles={failure_count}, "
                    f"reasons={';'.join(outcome.reasons[:6])}"
                )
            else:
                runtime_logger.info(
                    f"No successful deliveries for {storage_key}; retrying later. "
                    f"attempt={failure_count}, "
                    f"retryable={outcome.retryable_failures}, "
                    f"permanent={outcome.permanent_failures}, "
                    f"reasons={';'.join(outcome.reasons[:6])}"
                )

        if keys_to_mark:
            mark_deals_sent(keys_to_mark)

    ############################
    # Discord send primitives
    ############################
    async def send_embeds_with_retry(
        self,
        channel: Any,
        embeds: list[discord.Embed],
        channel_id: int,
    ) -> DeliveryResult:
        delays = (0.0, *self.DISCORD_SEND_RETRY_DELAYS_SECONDS)

        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                await channel.send(
                    embeds=embeds,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return DeliveryResult(success=True, reason="sent")
            except (discord.NotFound, discord.Forbidden) as e:
                error_logger.error(
                    "Channel became unavailable while sending deal. "
                    f"channel_id={channel_id}, error={e}"
                )
                return DeliveryResult(
                    success=False,
                    retryable_failure=False,
                    channel_stale=True,
                    reason=f"{type(e).__name__}:{e}",
                )
            except discord.HTTPException as e:
                retryable = self._is_retryable_http_exception(e)
                if retryable and attempt_index < len(delays):
                    continue
                return DeliveryResult(
                    success=False,
                    retryable_failure=retryable,
                    channel_stale=False,
                    reason=f"HTTPException(status={getattr(e, 'status', 'n/a')}):{e}",
                )
            except Exception as e:
                if attempt_index < len(delays):
                    continue
                return DeliveryResult(
                    success=False,
                    retryable_failure=True,
                    channel_stale=False,
                    reason=f"{type(e).__name__}:{e}",
                )

        return DeliveryResult(
            success=False,
            retryable_failure=True,
            channel_stale=False,
            reason="unknown-send-failure",
        )

    async def send_alert_dm(
        self,
        user_id: int,
        deal: Mapping[str, Any],
        matched_keywords: list[str],
    ) -> DeliveryResult:
        user_id_int = int(user_id)

        if not self._can_send_alert_dm(user_id_int):
            return DeliveryResult(
                success=False,
                retryable_failure=False,
                channel_stale=False,
                reason="dm-rate-limited",
            )

        if not self.is_source_url_allowed(str(deal.get("url", "") or "")):
            return DeliveryResult(
                success=False,
                retryable_failure=False,
                reason="source-url-disallowed",
            )

        user = self.bot.get_user(user_id_int)
        if user is None:
            delays = (0.0, *self.DISCORD_SEND_RETRY_DELAYS_SECONDS)
            for attempt_index, delay in enumerate(delays, start=1):
                if delay > 0:
                    await asyncio.sleep(delay)
                try:
                    user = await self.bot.fetch_user(user_id_int)
                    break
                except discord.NotFound as e:
                    error_logger.error(
                        "Could not fetch alert user for DM (not found). "
                        f"user_id={user_id_int}, error={e}"
                    )
                    return DeliveryResult(
                        success=False,
                        retryable_failure=False,
                        reason=f"NotFound:{e}",
                    )
                except discord.HTTPException as e:
                    retryable = self._is_retryable_http_exception(e)
                    if retryable and attempt_index < len(delays):
                        continue
                    error_logger.error(
                        "Could not fetch alert user for DM (HTTPException). "
                        f"user_id={user_id_int}, error={e}"
                    )
                    return DeliveryResult(
                        success=False,
                        retryable_failure=retryable,
                        reason=f"HTTPException(status={getattr(e, 'status', 'n/a')}):{e}",
                    )
                except Exception as e:
                    if attempt_index < len(delays):
                        continue
                    error_logger.error(
                        "Could not fetch alert user for DM (unexpected). "
                        f"user_id={user_id_int}, error={e}"
                    )
                    return DeliveryResult(
                        success=False,
                        retryable_failure=True,
                        reason=f"{type(e).__name__}:{e}",
                    )

        if user is None:
            return DeliveryResult(
                success=False,
                retryable_failure=True,
                reason="fetch-user-returned-none",
            )

        delays = (0.0, *self.DISCORD_SEND_RETRY_DELAYS_SECONDS)
        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                embed = self.build_deal_embed(deal)
                await user.send(
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                self._record_alert_dm_sent(user_id_int)
                return DeliveryResult(success=True, reason="dm-sent")
            except (discord.Forbidden, discord.NotFound) as e:
                error_logger.error(
                    "Could not DM user for keyword alert (forbidden/not-found). "
                    f"user_id={user_id_int}, error={e}"
                )
                return DeliveryResult(
                    success=False,
                    retryable_failure=False,
                    reason=f"{type(e).__name__}:{e}",
                )
            except discord.HTTPException as e:
                retryable = self._is_retryable_http_exception(e)
                if retryable and attempt_index < len(delays):
                    continue
                error_logger.error(
                    "HTTP error while DMing user for keyword alert. "
                    f"user_id={user_id_int}, error={e}"
                )
                return DeliveryResult(
                    success=False,
                    retryable_failure=retryable,
                    reason=f"HTTPException(status={getattr(e, 'status', 'n/a')}):{e}",
                )
            except Exception as e:
                if attempt_index < len(delays):
                    continue
                error_logger.error(
                    "Unexpected error while DMing user for keyword alert. "
                    f"user_id={user_id_int}, error={e}"
                )
                return DeliveryResult(
                    success=False,
                    retryable_failure=True,
                    reason=f"{type(e).__name__}:{e}",
                )

        return DeliveryResult(
            success=False,
            retryable_failure=True,
            reason="unknown-dm-failure",
        )

    ############################
    # Seen-key bookkeeping
    ############################
    def maybe_purge_old_sent_deals(self, min_interval_seconds: float = 3600.0) -> int:
        now = monotonic()
        if now - self.last_sent_deals_purge_monotonic < min_interval_seconds:
            return 0

        self.last_sent_deals_purge_monotonic = now
        purged_rows = purge_sent_deals_older_than(self.SENT_DEAL_RETENTION_DAYS)
        if purged_rows:
            self.posted_deal_ids = load_sent_deal_ids()
            runtime_logger.info(
                "Periodic sent-deal purge removed rows: "
                f"{purged_rows} (retention={self.SENT_DEAL_RETENTION_DAYS}d)"
            )

        self._prune_transient_user_state(now)
        return purged_rows

    def _prune_transient_user_state(self, now: float) -> None:
        """Keep the small per-user bookkeeping dicts from growing forever."""
        stale_cooldown_age = self.ALERT_UPDATE_COOLDOWN_SECONDS * 10
        for user_id in list(self.alert_last_update_monotonic_by_user):
            if now - self.alert_last_update_monotonic_by_user[user_id] > stale_cooldown_age:
                self.alert_last_update_monotonic_by_user.pop(user_id, None)

        for user_id in list(self.alert_dm_suppress_log_until_by_user):
            if now >= self.alert_dm_suppress_log_until_by_user[user_id]:
                self.alert_dm_suppress_log_until_by_user.pop(user_id, None)

    def mark_deal_as_seen(self, deal: Mapping[str, object]) -> str | None:
        storage_key = get_storage_key_for_deal(deal)
        if not storage_key:
            return None

        if storage_key not in self.posted_deal_ids:
            self.posted_deal_ids.add(storage_key)
            mark_deals_sent([storage_key])

        return storage_key

    def mark_deals_as_seen(self, deals: list[Mapping[str, object]]) -> int:
        keys_to_mark: list[str] = []
        for deal in deals:
            storage_key = get_storage_key_for_deal(deal)
            if storage_key and storage_key not in self.posted_deal_ids:
                self.posted_deal_ids.add(storage_key)
                keys_to_mark.append(storage_key)
        if keys_to_mark:
            mark_deals_sent(keys_to_mark)
        return len(keys_to_mark)

    def is_deal_excluded_for_guild(self, guild_id: int, deal: Mapping[str, Any]) -> bool:
        excluded_norms = self.get_excluded_category_norms_for_guild(guild_id)
        return is_deal_excluded_for_norms(excluded_norms, deal)

    def is_deal_site_excluded_for_guild(self, guild_id: int, deal: Mapping[str, Any]) -> bool:
        excluded_site_codes = self.get_excluded_site_codes_for_guild(guild_id)
        return is_deal_site_excluded_for_codes(excluded_site_codes, deal)

    ############################
    # Channel registry upkeep
    ############################
    def unregister_channel_by_channel_id(self, channel_id: int) -> bool:
        removed = remove_registered_channel_by_channel_id(channel_id)
        if removed:
            for guild_id, registered_channel_id in list(self.registered_channels.items()):
                if registered_channel_id == channel_id:
                    self.registered_channels.pop(guild_id, None)
                    self.excluded_categories_by_guild.pop(guild_id, None)
                    self.excluded_sites_by_guild.pop(guild_id, None)
        return removed

    def unregister_channel_by_guild_id(self, guild_id: int) -> int | None:
        removed_channel_id = remove_registered_channel_by_guild_id(guild_id)
        if removed_channel_id is not None:
            self.registered_channels.pop(guild_id, None)
            self.excluded_categories_by_guild.pop(guild_id, None)
            self.excluded_sites_by_guild.pop(guild_id, None)
        return removed_channel_id

    async def is_registered_channel_stale(self, channel_id: int) -> bool:
        if self.bot.get_channel(channel_id) is not None:
            return False

        try:
            await asyncio.wait_for(
                self.bot.fetch_channel(channel_id),
                timeout=self.DISCORD_API_CHECK_TIMEOUT_SECONDS,
            )
            return False
        except asyncio.TimeoutError:
            error_logger.error(
                "Timed out while checking channel staleness. "
                f"channel_id={channel_id}, timeout={self.DISCORD_API_CHECK_TIMEOUT_SECONDS}s"
            )
            return False
        except (discord.NotFound, discord.Forbidden):
            return True
        except discord.HTTPException as e:
            error_logger.error(
                "Transient channel check error. "
                f"channel_id={channel_id}, error={e}"
            )
            return False
        except Exception:
            error_logger.exception(
                "Unexpected channel check error. "
                f"channel_id={channel_id}"
            )
            return False

    async def prune_stale_registered_channels(self, reason: str) -> int:
        channels_snapshot = list(self.registered_channels.items())

        async def check_one(guild_id: int, channel_id: int) -> int | None:
            if await self.is_registered_channel_stale(channel_id):
                return channel_id
            return None

        results = await asyncio.gather(
            *(check_one(guild_id, channel_id) for guild_id, channel_id in channels_snapshot)
        )

        removed_count = 0
        for stale_channel_id in results:
            if stale_channel_id is None:
                continue
            if self.unregister_channel_by_channel_id(stale_channel_id):
                removed_count += 1
                runtime_logger.info(
                    f"Pruned stale channel {stale_channel_id}. reason={reason}"
                )

        if removed_count:
            runtime_logger.info(
                f"Pruned {removed_count} stale registered channel(s). reason={reason}"
            )
        return removed_count

    ############################
    # Crawling
    ############################
    def _is_crawler_circuit_open(self, crawler_name: str) -> bool:
        open_until = self.crawler_circuit_open_until.get(crawler_name, 0.0)
        return open_until > monotonic()

    def _record_crawler_success(self, crawler_name: str, deals_count: int) -> None:
        self.crawler_consecutive_failures[crawler_name] = 0
        self.crawler_circuit_open_until.pop(crawler_name, None)
        self.crawler_last_success_at[crawler_name] = datetime.now(timezone.utc)
        self.crawler_last_deal_count[crawler_name] = int(deals_count)

        self.runtime_metrics["crawler_fetch_success_total"] += 1
        self.runtime_metrics["crawler_deals_fetched_total"] += int(max(0, deals_count))

        if deals_count < self.CRAWLER_MIN_EXPECTED_COUNT:
            anomaly_count = self.crawler_parse_anomaly_counts.get(crawler_name, 0) + 1
            self.crawler_parse_anomaly_counts[crawler_name] = anomaly_count
            if anomaly_count >= self.CRAWLER_PARSE_ANOMALY_THRESHOLD:
                error_logger.error(
                    "Possible parser breakage: low result count persisted. "
                    f"crawler={crawler_name}, count={deals_count}, "
                    f"anomaly_streak={anomaly_count}"
                )
                self.runtime_metrics["crawler_parse_anomaly_alerts_total"] += 1
        else:
            self.crawler_parse_anomaly_counts[crawler_name] = 0

    def _record_crawler_failure(self, crawler_name: str, reason: str) -> None:
        fail_count = self.crawler_consecutive_failures.get(crawler_name, 0) + 1
        self.crawler_consecutive_failures[crawler_name] = fail_count
        self.runtime_metrics["crawler_fetch_failure_total"] += 1

        if fail_count >= self.CRAWLER_CIRCUIT_FAIL_THRESHOLD:
            open_until = monotonic() + self.CRAWLER_CIRCUIT_OPEN_SECONDS
            self.crawler_circuit_open_until[crawler_name] = open_until
            error_logger.error(
                "Crawler circuit opened after consecutive failures. "
                f"crawler={crawler_name}, failures={fail_count}, reason={reason}"
            )
            return

        error_logger.error(
            "Crawler failure. "
            f"crawler={crawler_name}, failures={fail_count}, reason={reason}"
        )

    async def _fetch_site_with_retry(
        self,
        crawler_name: str,
        fetch_func: Callable[[], list[dict]],
    ) -> list[dict]:
        if self._is_crawler_circuit_open(crawler_name):
            seconds_left = int(
                max(0.0, self.crawler_circuit_open_until.get(crawler_name, 0.0) - monotonic())
            )
            self.runtime_metrics["crawler_circuit_skips_total"] += 1
            runtime_logger.info(
                "Crawler circuit is open. Skipping fetch this cycle. "
                f"crawler={crawler_name}, retry_after={seconds_left}s"
            )
            return []

        timeout_seconds = self.CRAWLER_TIMEOUT_SECONDS_BY_NAME.get(
            crawler_name, self.CRAWLER_TIMEOUT_SECONDS
        )
        delays = (0.0, *self.CRAWLER_RETRY_DELAYS_SECONDS)
        last_error_reason = "unknown"

        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                loop = asyncio.get_running_loop()
                raw_deals = await asyncio.wait_for(
                    loop.run_in_executor(None, fetch_func),
                    timeout=timeout_seconds,
                )
                if raw_deals is None:
                    deals: list[dict] = []
                else:
                    deals = list(raw_deals)

                self._record_crawler_success(crawler_name, len(deals))
                return deals
            except asyncio.TimeoutError:
                last_error_reason = (
                    f"timeout>{timeout_seconds}s at attempt={attempt_index}"
                )
                runtime_logger.info(
                    "Crawler timeout. "
                    f"crawler={crawler_name}, attempt={attempt_index}/{len(delays)}"
                )
            except Exception as e:
                last_error_reason = f"{type(e).__name__}: {e}"
                runtime_logger.info(
                    "Crawler fetch error. "
                    f"crawler={crawler_name}, attempt={attempt_index}/{len(delays)}, error={e}"
                )

        self._record_crawler_failure(crawler_name, last_error_reason)
        return []

    async def fetch_all_deals(self) -> list[dict]:
        quasar_future = self._fetch_site_with_retry("quasarzone", fetch_hot_deals)
        arca_future = self._fetch_site_with_retry("arcalive", fetch_hot_deals_arca)
        fmkorea_future = self._fetch_site_with_retry("fmkorea", fetch_hot_deals_fmkorea)
        ppomppu_future = self._fetch_site_with_retry("ppomppu", fetch_hot_deals_ppomppu)
        eomisae_future = self._fetch_site_with_retry("eomisae", fetch_hot_deals_eomisae)
        quasar_deals, arca_deals, fmkorea_deals, ppomppu_deals, eomisae_deals = (
            await asyncio.gather(
                quasar_future,
                arca_future,
                fmkorea_future,
                ppomppu_future,
                eomisae_future,
            )
        )
        return quasar_deals + arca_deals + fmkorea_deals + ppomppu_deals + eomisae_deals

    ############################
    # Embeds
    ############################
    @staticmethod
    def _looks_like_web_url(value: str | None) -> bool:
        text = str(value or "").strip().lower()
        return text.startswith("http://") or text.startswith("https://")

    @staticmethod
    def _clamp_text(value: Any, limit: int) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)] + "…"

    def build_deal_embed(self, deal: Mapping[str, Any]) -> discord.Embed:
        title = self._clamp_text(deal.get("title") or "핫딜", 256)
        raw_url = str(deal.get("url", "") or "").strip()

        embed = discord.Embed(
            title=title,
            url=raw_url if self._looks_like_web_url(raw_url) else None,
            color=self.parse_embed_color(str(deal.get("site_color"))),
            timestamp=datetime.now(timezone.utc),
        )

        raw_thumbnail = str(deal.get("image_url", "") or "").strip()
        if self._looks_like_web_url(raw_thumbnail):
            embed.set_thumbnail(url=raw_thumbnail)

        raw_logo = str(deal.get("logo", "") or "").strip()
        author_name = self._clamp_text(deal.get("site_name") or "알 수 없음", 256)
        if self._looks_like_web_url(raw_logo):
            embed.set_author(name=author_name, icon_url=raw_logo)
        else:
            embed.set_author(name=author_name)

        site_code = str(deal.get("site_code", "") or "").strip().lower()
        if site_code != "eomisae":
            price = str(deal.get("price", "") or "").strip() or "가격 정보 없음"
            embed.add_field(
                name=self._clamp_text(f"> **{price}**", 256),
                value="\u200b",
                inline=True,
            )
        category_display = get_deal_category_for_display(deal)
        embed.add_field(
            name=self._clamp_text(f"`[ {category_display} ]`", 256),
            value="\u200b",
            inline=True,
        )
        return embed

    async def resolve_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel

        delays = (0.0, *self.DISCORD_SEND_RETRY_DELAYS_SECONDS)
        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
            except discord.HTTPException as e:
                retryable = self._is_retryable_http_exception(e)
                if retryable and attempt_index < len(delays):
                    continue
                error_logger.error(
                    "Could not fetch channel (HTTPException). "
                    f"channel_id={channel_id}, error={e}"
                )
                return None
            except Exception as e:
                if attempt_index < len(delays):
                    continue
                error_logger.error(
                    "Could not fetch channel (unexpected). "
                    f"channel_id={channel_id}, error={e}"
                )
                return None

        return None

    @staticmethod
    def parse_embed_color(raw_color: str | None, default_hex: str = "ff0000") -> int:
        raw = str(raw_color or "").strip().lower().lstrip("#")
        try:
            return int(raw, 16)
        except ValueError:
            return int(default_hex, 16)
