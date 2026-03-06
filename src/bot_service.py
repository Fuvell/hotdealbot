import asyncio
import os
import re
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import discord
from discord.ext import commands
from pytz import timezone

try:
    from .crawlers.arca_crawler import fetch_hot_deals_arca
    from .crawlers.eomisae_crawler import fetch_hot_deals_eomisae
    from .crawlers.fmkorea_crawler import fetch_hot_deals_fmkorea
    from .crawlers.ppomppu_crawler import fetch_hot_deals_ppomppu
    from .category_filters import (
        get_deal_category_for_display,
        is_deal_excluded_for_norms,
        normalize_loaded_excluded_categories_by_guild,
    )
    from .site_filters import (
        is_deal_site_excluded_for_codes,
        normalize_loaded_excluded_sites_by_guild,
    )
    from .deal_key import (
        compact_existing_key,
        get_lookup_keys_for_deal,
        get_storage_key_for_deal,
    )
    from .crawlers.quasarzone_crawler import fetch_hot_deals
    from .error_logging import get_error_logger
    from .storage import (
        init_db,
        load_excluded_categories_by_guild,
        load_excluded_sites_by_guild,
        load_registered_channels,
        load_sent_deal_ids,
        mark_deal_sent,
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
    )
except ImportError:
    from crawlers.arca_crawler import fetch_hot_deals_arca
    from crawlers.eomisae_crawler import fetch_hot_deals_eomisae
    from crawlers.fmkorea_crawler import fetch_hot_deals_fmkorea
    from crawlers.ppomppu_crawler import fetch_hot_deals_ppomppu
    from category_filters import (
        get_deal_category_for_display,
        is_deal_excluded_for_norms,
        normalize_loaded_excluded_categories_by_guild,
    )
    from site_filters import (
        is_deal_site_excluded_for_codes,
        normalize_loaded_excluded_sites_by_guild,
    )
    from deal_key import (
        compact_existing_key,
        get_lookup_keys_for_deal,
        get_storage_key_for_deal,
    )
    from crawlers.quasarzone_crawler import fetch_hot_deals
    from error_logging import get_error_logger
    from storage import (
        init_db,
        load_excluded_categories_by_guild,
        load_excluded_sites_by_guild,
        load_registered_channels,
        load_sent_deal_ids,
        mark_deal_sent,
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
    )

error_logger = get_error_logger()
PROJECT_BASE_DIR = Path(__file__).resolve().parent.parent


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


class HotDealService:
    ALERT_KEYWORD_MAX_COUNT = 5
    ALERT_KEYWORD_MIN_LEN = 2
    ALERT_KEYWORD_MAX_LEN = 15
    SENT_DEAL_RETENTION_DAYS = 60
    CRAWLER_TIMEOUT_SECONDS = 20.0
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
        if compacted_sent_keys != self.posted_deal_ids:
            replace_sent_deal_ids(compacted_sent_keys)
            self.posted_deal_ids = compacted_sent_keys
            print(
                "Compacted sent deal keys: "
                f"{before_compact_count} rows -> {len(compacted_sent_keys)} rows"
            )

        print(
            "Loaded state from SQLite: "
            f"channels={len(self.registered_channels)}, "
            f"category-filter-configs={len(self.excluded_categories_by_guild)}, "
            f"site-filter-configs={len(self.excluded_sites_by_guild)}, "
            f"alert-users={len(self.alert_rules_by_user)}, "
            f"sent_ids={len(self.posted_deal_ids)}"
        )
        if purged_rows:
            print(
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

    def check_alert_update_cooldown(self, user_id: int) -> tuple[bool, float]:
        user_id_int = int(user_id)
        now = monotonic()
        last_update = self.alert_last_update_monotonic_by_user.get(user_id_int, 0.0)
        elapsed = now - last_update
        if elapsed >= self.ALERT_UPDATE_COOLDOWN_SECONDS:
            return True, 0.0
        return False, max(0.0, self.ALERT_UPDATE_COOLDOWN_SECONDS - elapsed)

    def touch_alert_update(self, user_id: int) -> None:
        self.alert_last_update_monotonic_by_user[int(user_id)] = monotonic()

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
                "\ubb34\ub8cc",
                "\ud56b\ub51c",
                "\ud2b9\uac00",
                "\uc138\uc77c",
                "\ud560\uc778",
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

    async def send_deal_embed_with_retry(
        self,
        channel: discord.abc.MessageableChannel,
        deal: Mapping[str, Any],
        channel_id: int,
    ) -> DeliveryResult:
        source_url = str(deal.get("url", "") or "").strip()
        if not self.is_source_url_allowed(source_url):
            error_logger.error(
                "Blocked deal send due to source-domain allowlist. "
                f"channel_id={channel_id}, url={source_url}"
            )
            return DeliveryResult(
                success=False,
                retryable_failure=False,
                channel_stale=False,
                reason="source-url-disallowed",
            )

        delays = (0.0, *self.DISCORD_SEND_RETRY_DELAYS_SECONDS)

        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                await self.send_deal_embed(channel, deal)
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

            files: list[discord.File] = []
            try:
                embed, files = self.build_deal_embed_payload(deal)
                send_kwargs: dict[str, Any] = {
                    "embed": embed,
                    "allowed_mentions": discord.AllowedMentions.none(),
                }
                if files:
                    send_kwargs["files"] = files
                await user.send(**send_kwargs)
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
            finally:
                for file in files:
                    file.close()

        return DeliveryResult(
            success=False,
            retryable_failure=True,
            reason="unknown-dm-failure",
        )

    def get_deal_keys(self, deal: Mapping[str, object]) -> list[str]:
        return get_lookup_keys_for_deal(deal)

    def maybe_purge_old_sent_deals(self, min_interval_seconds: float = 3600.0) -> int:
        now = monotonic()
        if now - self.last_sent_deals_purge_monotonic < min_interval_seconds:
            return 0

        self.last_sent_deals_purge_monotonic = now
        purged_rows = purge_sent_deals_older_than(self.SENT_DEAL_RETENTION_DAYS)
        if purged_rows:
            self.posted_deal_ids = load_sent_deal_ids()
            print(
                "Periodic sent-deal purge removed rows: "
                f"{purged_rows} (retention={self.SENT_DEAL_RETENTION_DAYS}d)"
            )
        return purged_rows

    def mark_deal_as_seen(self, deal: Mapping[str, object]) -> str | None:
        storage_key = get_storage_key_for_deal(deal)
        if not storage_key:
            return None

        if storage_key not in self.posted_deal_ids:
            self.posted_deal_ids.add(storage_key)
            mark_deal_sent(storage_key)

        return storage_key

    def is_deal_excluded_for_guild(self, guild_id: int, deal: Mapping[str, Any]) -> bool:
        excluded_norms = self.get_excluded_category_norms_for_guild(guild_id)
        return is_deal_excluded_for_norms(excluded_norms, deal)

    def is_deal_site_excluded_for_guild(self, guild_id: int, deal: Mapping[str, Any]) -> bool:
        excluded_site_codes = self.get_excluded_site_codes_for_guild(guild_id)
        return is_deal_site_excluded_for_codes(excluded_site_codes, deal)

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
            print(
                "Timed out while checking channel staleness. "
                f"channel_id={channel_id}"
            )
            error_logger.error(
                "Timed out while checking channel staleness. "
                f"channel_id={channel_id}, timeout={self.DISCORD_API_CHECK_TIMEOUT_SECONDS}s"
            )
            return False
        except (discord.NotFound, discord.Forbidden):
            return True
        except discord.HTTPException as e:
            print(f"Transient channel check error for {channel_id}: {e}")
            error_logger.error(
                "Transient channel check error. "
                f"channel_id={channel_id}, error={e}"
            )
            return False
        except Exception as e:
            print(f"Unexpected channel check error for {channel_id}: {e}")
            error_logger.exception(
                "Unexpected channel check error. "
                f"channel_id={channel_id}"
            )
            return False

    async def prune_stale_registered_channels(self, reason: str) -> int:
        removed_count = 0
        for guild_id, channel_id in list(self.registered_channels.items()):
            if not await self.is_registered_channel_stale(channel_id):
                continue
            if self.unregister_channel_by_channel_id(channel_id):
                removed_count += 1
                print(
                    f"Pruned stale channel {channel_id} from guild {guild_id}. "
                    f"reason={reason}"
                )

        if removed_count:
            print(f"Pruned {removed_count} stale registered channel(s). reason={reason}")
        return removed_count

    def _is_crawler_circuit_open(self, crawler_name: str) -> bool:
        open_until = self.crawler_circuit_open_until.get(crawler_name, 0.0)
        return open_until > monotonic()

    def _record_crawler_success(self, crawler_name: str, deals_count: int) -> None:
        self.crawler_consecutive_failures[crawler_name] = 0
        self.crawler_circuit_open_until.pop(crawler_name, None)
        self.crawler_last_success_at[crawler_name] = datetime.now(timezone("UTC"))
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
            print(
                "Crawler circuit is open. Skipping fetch this cycle. "
                f"crawler={crawler_name}, retry_after={seconds_left}s"
            )
            return []

        delays = (0.0, *self.CRAWLER_RETRY_DELAYS_SECONDS)
        last_error_reason = "unknown"

        for attempt_index, delay in enumerate(delays, start=1):
            if delay > 0:
                await asyncio.sleep(delay)

            try:
                loop = asyncio.get_running_loop()
                raw_deals = await asyncio.wait_for(
                    loop.run_in_executor(None, fetch_func),
                    timeout=self.CRAWLER_TIMEOUT_SECONDS,
                )
                if raw_deals is None:
                    deals: list[dict] = []
                else:
                    deals = list(raw_deals)

                self._record_crawler_success(crawler_name, len(deals))
                return deals
            except asyncio.TimeoutError:
                last_error_reason = (
                    f"timeout>{self.CRAWLER_TIMEOUT_SECONDS}s at attempt={attempt_index}"
                )
                print(
                    "Crawler timeout. "
                    f"crawler={crawler_name}, attempt={attempt_index}/{len(delays)}"
                )
            except Exception as e:
                last_error_reason = f"{type(e).__name__}: {e}"
                print(
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

    async def send_deal_embed(
        self,
        channel: discord.abc.MessageableChannel,
        deal: Mapping[str, Any],
    ) -> None:
        embed, files = self.build_deal_embed_payload(deal)
        try:
            send_kwargs: dict[str, Any] = {
                "embed": embed,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if files:
                send_kwargs["files"] = files
            await channel.send(**send_kwargs)
        finally:
            for file in files:
                file.close()

    @staticmethod
    def _looks_like_web_url(value: str | None) -> bool:
        text = str(value or "").strip().lower()
        return text.startswith("http://") or text.startswith("https://")

    @staticmethod
    def _resolve_local_asset_file(raw_path: str | None) -> Path | None:
        text = str(raw_path or "").strip()
        if not text:
            return None
        if HotDealService._looks_like_web_url(text):
            return None

        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = PROJECT_BASE_DIR / candidate

        try:
            resolved = candidate.resolve()
            resolved.relative_to(PROJECT_BASE_DIR.resolve())
        except Exception:
            return None

        if not resolved.is_file():
            return None
        return resolved

    def build_deal_embed_payload(
        self,
        deal: Mapping[str, Any],
    ) -> tuple[discord.Embed, list[discord.File]]:
        embed = self.build_deal_embed(deal)
        files: list[discord.File] = []

        logo_path = self._resolve_local_asset_file(str(deal.get("logo", "") or ""))
        if logo_path is not None:
            filename = f"logo_{logo_path.stem}{logo_path.suffix.lower()}"
            files.append(discord.File(str(logo_path), filename=filename))
            embed.set_author(
                name=str(deal.get("site_name", "알 수 없음") or "알 수 없음"),
                icon_url=f"attachment://{filename}",
            )

        return embed, files

    def build_deal_embed(self, deal: Mapping[str, Any]) -> discord.Embed:
        embed = discord.Embed(
            title=deal.get("title", "핫딜"),
            url=deal.get("url", ""),
            color=self.parse_embed_color(str(deal.get("site_color"))),
        )
        raw_thumbnail = str(deal.get("image_url", "") or "").strip()
        thumbnail_url = (
            raw_thumbnail
            if self._looks_like_web_url(raw_thumbnail)
            else "https://via.placeholder.com/150"
        )
        raw_logo = str(deal.get("logo", "") or "").strip()
        logo_url = (
            raw_logo
            if self._looks_like_web_url(raw_logo)
            else "https://via.placeholder.com/150"
        )
        embed.set_thumbnail(url=thumbnail_url)
        embed.set_author(
            name=deal.get("site_name", "알 수 없음"),
            icon_url=logo_url,
        )
        site_code = str(deal.get("site_code", "") or "").strip().lower()
        if site_code != "eomisae":
            embed.add_field(
                name=f"> **{deal.get('price', '가격 정보 없음')}**",
                value="\u200b",
                inline=True,
            )
        category_display = get_deal_category_for_display(deal)
        embed.add_field(name=f"`[ {category_display} ]`", value="\u200b", inline=True)
        korea_time = datetime.now(timezone("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S")
        embed.set_footer(text=korea_time)
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
