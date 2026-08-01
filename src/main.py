from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

try:
    from .bot_service import HotDealService
    from .category_filters import (
        format_excluded_categories_for_display,
        format_filterable_categories_for_help,
        get_excluded_categories_autocomplete_choices,
        parse_excluded_categories_input,
    )
    from .site_filters import (
        format_excluded_sites_for_display,
        format_filterable_sites_for_help,
        get_excluded_sites_autocomplete_choices,
        parse_excluded_sites_input,
    )
    from .error_logging import get_audit_logger, get_error_logger, get_runtime_logger
    from .storage import (
        close_db,
        get_meta_value,
        set_meta_value,
        validate_db_file_path_or_raise,
    )
    from .startup_lock import StartupLock, StartupLockError
except ImportError:
    from bot_service import HotDealService
    from category_filters import (
        format_excluded_categories_for_display,
        format_filterable_categories_for_help,
        get_excluded_categories_autocomplete_choices,
        parse_excluded_categories_input,
    )
    from site_filters import (
        format_excluded_sites_for_display,
        format_filterable_sites_for_help,
        get_excluded_sites_autocomplete_choices,
        parse_excluded_sites_input,
    )
    from error_logging import get_audit_logger, get_error_logger, get_runtime_logger
    from storage import (
        close_db,
        get_meta_value,
        set_meta_value,
        validate_db_file_path_or_raise,
    )
    from startup_lock import StartupLock, StartupLockError

############################
# Load environment variables
############################
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_FILE)
TOKEN = os.getenv("DISCORD_BOT_TOKEN") or "YOUR_DISCORD_BOT_TOKEN_HERE"
LOCK_FILE = BASE_DIR / "bot_startup.lock"
error_logger = get_error_logger(BASE_DIR)
audit_logger = get_audit_logger(BASE_DIR)
runtime_logger = get_runtime_logger(BASE_DIR)

FILTER_CATEGORY_INPUT_MAX_LEN = 200
SITE_FILTER_INPUT_MAX_LEN = 200
ALERT_KEYWORD_INPUT_MAX_LEN = 200
ALERT_KEYWORD_MAX_ITEMS_PER_REQUEST = 20
STARTUP_COMMAND_SYNC_TIMEOUT_SECONDS = 45.0
COMMAND_TREE_HASH_META_KEY = "command_tree_hash"

MODE_VALUE_ALIASES = {
    "view": "조회",
    "set": "설정",
    "add": "추가",
    "remove": "삭제",
    "clear": "초기화",
    "보기": "조회",
    "조회": "조회",
    "설정": "설정",
    "추가": "추가",
    "제거": "삭제",
    "삭제": "삭제",
    "초기화": "초기화",
}
VALID_MODE_VALUES = {"조회", "설정", "추가", "삭제", "초기화"}

############################
# Discord Setup
############################
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = False
bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
)
service = HotDealService(bot)


def validate_token_or_raise(token: str) -> str:
    normalized = str(token or "").strip()
    if not normalized or normalized == "YOUR_DISCORD_BOT_TOKEN_HERE":
        raise RuntimeError(
            "DISCORD_BOT_TOKEN is missing. Set a valid token in your environment."
        )
    if " " in normalized:
        raise RuntimeError("DISCORD_BOT_TOKEN looks invalid (contains spaces).")
    return normalized


def validate_environment_paths_or_raise() -> None:
    validate_db_file_path_or_raise()

    if LOCK_FILE.resolve().parent != BASE_DIR.resolve():
        raise RuntimeError(
            "Lock file path is outside project base directory. "
            f"lock_file={LOCK_FILE.resolve()}, base_dir={BASE_DIR.resolve()}"
        )

    src_env = (BASE_DIR / "src" / ".env").resolve()
    if src_env.exists():
        error_logger.error(
            "Detected nested env file under src/.env. "
            "Use project root .env only to avoid config confusion."
        )


def compute_command_tree_hash() -> str | None:
    """Stable hash of slash-command definitions, to skip redundant syncs."""
    try:
        serialized = []
        for command in bot.tree.get_commands():
            try:
                serialized.append(command.to_dict(bot.tree))
            except TypeError:
                serialized.append(command.to_dict())  # older discord.py signature
        payload = json.dumps(serialized, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:
        error_logger.exception("Failed to compute command tree hash.")
        return None


def parse_alert_keywords_input(
    raw: str | None,
    *,
    max_items: int = ALERT_KEYWORD_MAX_ITEMS_PER_REQUEST,
) -> tuple[list[str], bool]:
    if raw is None:
        return [], False

    tokens = [token.strip() for token in re.split(r"[,;\n]+", raw) if token.strip()]
    if len(tokens) <= max_items:
        return tokens, False
    return tokens[:max_items], True


def format_alert_keywords_for_display(keywords: list[str]) -> str:
    if not keywords:
        return "없음"
    return ", ".join(sorted(keywords, key=lambda value: value.casefold()))


def build_alert_examples_for_display(limit: int = 3) -> str:
    examples = ["아이폰", "에어팟", "RTX 4080"]
    return ", ".join(examples[: max(1, int(limit))])


def build_alert_response_embed(
    title: str,
    current_keywords: list[str] | None = None,
    *,
    extra_lines: list[str] | None = None,
    is_error: bool = False,
) -> discord.Embed:
    description_lines: list[str] = []

    if current_keywords is not None:
        remaining_slots = max(0, service.ALERT_KEYWORD_MAX_COUNT - len(current_keywords))
        description_lines.extend(
            [
                f"키워드: {format_alert_keywords_for_display(current_keywords)}",
                f"개수: {len(current_keywords)}/{service.ALERT_KEYWORD_MAX_COUNT}",
                f"남은 슬롯: {remaining_slots}",
            ]
        )

    if extra_lines:
        for value in extra_lines:
            text = str(value).strip()
            if text:
                description_lines.append(text)

    description = "\u200b"
    if description_lines:
        description = "\n".join(f"- {line}" for line in description_lines)

    color_hex = "ff4d4f" if is_error else "ff9900"
    return discord.Embed(
        title=title,
        description=description,
        color=service.parse_embed_color(color_hex),
    )


def normalize_mode_value(raw_mode: str | None) -> str:
    mode_key = str(raw_mode or "").strip().lower()
    return MODE_VALUE_ALIASES.get(mode_key, str(raw_mode or "").strip())


def log_admin_audit_event(
    interaction: discord.Interaction,
    *,
    action: str,
    details: str = "",
) -> None:
    guild_id = getattr(interaction.guild, "id", None)
    channel_id = getattr(interaction.channel, "id", None)
    user_id = getattr(interaction.user, "id", None)

    audit_logger.info(
        "action=%s guild_id=%s channel_id=%s user_id=%s details=%s",
        action,
        guild_id,
        channel_id,
        user_id,
        details,
    )


async def safe_defer_interaction(
    interaction: discord.Interaction,
    *,
    ephemeral: bool = True,
) -> bool:
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except Exception:
        error_logger.exception("Failed to defer interaction response.")
        return False


async def safe_send_interaction_message(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    ephemeral: bool = True,
) -> bool:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(
                content=content,
                embed=embed,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await interaction.response.send_message(
                content=content,
                embed=embed,
                ephemeral=ephemeral,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return True
    except Exception:
        error_logger.exception("Failed to send interaction response.")
        return False


def build_help_embed(
    interaction: discord.Interaction,
    bot_user: discord.ClientUser | None,
) -> discord.Embed:
    embed = discord.Embed(
        title="명령어 안내",
        description="아래 명령어로 봇을 사용할 수 있습니다.",
        color=service.parse_embed_color("ff9900"),
    )

    author_name = "핫딜봇"
    icon_url: str | None = None
    if bot_user is not None:
        icon_url = bot_user.display_avatar.url
        author_name = str(bot_user)

    if icon_url:
        embed.set_author(name=author_name, icon_url=icon_url)
        embed.set_thumbnail(url=icon_url)
    else:
        embed.set_author(name=author_name)

    embed.add_field(
        name="`/도움말`",
        value="명령어 도움말을 확인합니다.",
        inline=False,
    )
    embed.add_field(
        name="`/알림`",
        value=(
            "등록한 키워드가 포함된 핫딜을 DM으로 알림받습니다.\n"
            "사용법: `/알림 모드:<조회|설정|추가|삭제|초기화> 키워드:<키워드 목록>`\n"
            "예시: `/알림 모드:추가 키워드:아이폰,에어팟`"
        ),
        inline=False,
    )

    can_manage_guild = False
    if interaction.guild is not None and isinstance(interaction.user, discord.Member):
        can_manage_guild = bool(interaction.user.guild_permissions.manage_guild)

    if can_manage_guild:
        embed.add_field(
            name="`/채널등록`",
            value=(
                "현재 채널을 핫딜 수신 채널로 등록합니다.\n"
                "예시: `/채널등록`"
            ),
            inline=False,
        )
        embed.add_field(
            name="`/채널해제`",
            value=(
                "현재 서버의 핫딜 수신 채널 등록을 해제합니다.\n"
                "예시: `/채널해제`"
            ),
            inline=False,
        )
        embed.add_field(
            name="`/필터`",
            value=(
                "핫딜 수신에서 제외할 카테고리 목록을 설정합니다.\n"
                "사용법: `/필터 모드:<조회|설정|추가|삭제|초기화> 카테고리:<카테고리 목록>`\n"
                "예시: `/필터 모드:설정 카테고리:식품,게임`"
            ),
            inline=False,
        )
        embed.add_field(
            name="`/사이트필터`",
            value=(
                "핫딜 수신에서 제외할 사이트 목록을 설정합니다.\n"
                "사용법: `/사이트필터 모드:<조회|설정|추가|삭제|초기화> 사이트:<사이트 목록>`\n"
                "예시: `/사이트필터 모드:추가 사이트:아카라이브,뽐뿌`"
            ),
            inline=False,
        )

    embed.add_field(
        name="도움이 필요하신가요?",
        value="[클릭](https://discord.gg/FAU8fhYVKz)",
        inline=False,
    )

    embed.set_footer(text="핫딜봇 베타 (ver. 2.2)")
    return embed


def build_welcome_embed() -> discord.Embed:
    embed = discord.Embed(
        title="핫딜봇을 추가해줘서 고마워요!",
        description=(
            "- 우선, 원하는 채널에서 `/채널등록` 명령어를 사용하여 핫딜봇을 가동시켜주세요!\n"
            "- `/도움말` 명령어를 통해 핫딜봇이 지원하는 여러 기능도 확인해보세요.\n"
            "- `/필터`, `/사이트필터`로 원하지 않는 카테고리/사이트를 제외할 수 있어요.\n"
            "- `/알림`으로 키워드가 포함된 핫딜을 DM으로 받아볼 수도 있어요."
        ),
        color=service.parse_embed_color("ff9900"),
    )

    if bot.user is not None:
        icon_url = bot.user.display_avatar.url
        embed.set_author(name=str(bot.user), icon_url=icon_url)
        embed.set_thumbnail(url=icon_url)

    embed.add_field(
        name="도움이 필요하신가요?",
        value="[클릭](https://discord.gg/FAU8fhYVKz)",
        inline=False,
    )
    embed.set_footer(text="핫딜봇 베타 (ver. 2.2)")
    return embed


############################
# Bot Events
############################
@bot.event
async def on_guild_join(guild: discord.Guild):
    runtime_logger.info(f"Joined guild {guild.id} ({guild.name}).")
    try:
        embed = build_welcome_embed()

        target_channel = None
        me = guild.me
        if me is not None:
            system_channel = guild.system_channel
            if (
                system_channel is not None
                and system_channel.permissions_for(me).send_messages
            ):
                target_channel = system_channel
            else:
                # No usable system channel: fall back to the first text
                # channel the bot is allowed to write in.
                for channel in guild.text_channels:
                    if channel.permissions_for(me).send_messages:
                        target_channel = channel
                        break

        if target_channel is None:
            runtime_logger.info(
                f"No writable channel for welcome message. guild_id={guild.id}"
            )
            return

        await target_channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        runtime_logger.info(
            f"Sent welcome message to channel {target_channel.id} "
            f"for guild {guild.id}."
        )
    except Exception:
        error_logger.exception(f"Failed to send welcome message. guild_id={guild.id}")



@bot.event
async def on_ready():
    runtime_logger.info("[startup] on_ready begin")
    runtime_logger.info(
        f"{bot.user} (ID: {bot.user.id}) is online, searching for deals."
    )

    try:
        await bot.change_presence(
            status=discord.Status.online,
            activity=discord.Game(name="핫딜봇 베타 /도움말"),
        )
        runtime_logger.info("[startup] presence updated")
    except Exception:
        error_logger.exception("Failed to set bot presence.")

    if not service.commands_synced:
        tree_hash = compute_command_tree_hash()
        stored_hash = None
        try:
            stored_hash = get_meta_value(COMMAND_TREE_HASH_META_KEY)
        except Exception:
            error_logger.exception("Failed to read stored command tree hash.")

        if tree_hash is not None and stored_hash == tree_hash:
            service.commands_synced = True
            runtime_logger.info(
                "[startup] command definitions unchanged; skipping slash sync"
            )
        else:
            try:
                runtime_logger.info("[startup] syncing slash commands...")
                synced = await asyncio.wait_for(
                    bot.tree.sync(),
                    timeout=STARTUP_COMMAND_SYNC_TIMEOUT_SECONDS,
                )
                service.commands_synced = True
                runtime_logger.info(f"Synced {len(synced)} slash command(s).")
                if tree_hash is not None:
                    try:
                        set_meta_value(COMMAND_TREE_HASH_META_KEY, tree_hash)
                    except Exception:
                        error_logger.exception("Failed to store command tree hash.")
            except asyncio.TimeoutError:
                error_logger.error(
                    "Slash command sync timed out during startup. "
                    f"timeout={STARTUP_COMMAND_SYNC_TIMEOUT_SECONDS}s"
                )
            except Exception:
                error_logger.exception("Failed to sync slash commands.")

    try:
        async with service.deal_state_lock:
            service.refresh_runtime_config()
        runtime_logger.info(
            "[startup] runtime config loaded: "
            f"channels={len(service.registered_channels)}, "
            f"filter-guilds={len(service.excluded_categories_by_guild)}, "
            f"site-filter-guilds={len(service.excluded_sites_by_guild)}"
        )
    except Exception:
        error_logger.exception("Failed to refresh runtime config during startup.")

    if not check_hot_deals.is_running():
        check_hot_deals.start()
        runtime_logger.info("[startup] check_hot_deals task started")
    else:
        runtime_logger.info("[startup] check_hot_deals task already running")

    runtime_logger.info("[startup] on_ready complete")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    async with service.deal_state_lock:
        if service.unregister_channel_by_channel_id(channel.id):
            runtime_logger.info(f"Removed deleted channel from DB: {channel.id}")


@bot.event
async def on_guild_remove(guild: discord.Guild):
    removed_count = 0
    async with service.deal_state_lock:
        removed_channel_id = service.unregister_channel_by_guild_id(guild.id)
        if removed_channel_id is not None:
            removed_count += 1
        else:
            removed_count += await service.prune_stale_registered_channels(
                f"guild_remove:{guild.id}"
            )

    if removed_count:
        runtime_logger.info(
            f"Removed {removed_count} channel(s) after guild removal: {guild.id}"
        )


############################
# Background Task
############################
@tasks.loop(minutes=1)
async def check_hot_deals():
    """
    Delivery cycle in three phases:
      1) plan  (lock): dedup fetched deals, compute recipients per new deal
      2) send  (no lock): enrich new deals, deliver batched embeds + DMs
      3) settle (lock): batch-mark successes, drop stale channels, cap retries
    """
    try:
        all_deals = await service.fetch_all_deals()

        async with service.deal_state_lock:
            service.maybe_purge_old_sent_deals()
            plan = service.build_delivery_plan(all_deals)

        if not plan.items:
            return

        await service.enrich_new_deals(plan.items)
        outcomes, stale_channel_ids = await service.deliver_plan(plan)

        async with service.deal_state_lock:
            service.finalize_deliveries(outcomes, stale_channel_ids)
    except Exception:
        error_logger.exception("Error while fetching or sending deals.")


############################
# Commands
############################
@bot.tree.command(
    name="도움말",
    description="명령어 목록을 표시합니다.",
)
async def help_command(interaction: discord.Interaction):
    try:
        embed = build_help_embed(interaction, bot.user)
        await safe_send_interaction_message(
            interaction,
            embed=embed,
            ephemeral=True,
        )
    except Exception:
        error_logger.exception("Failed to run /help command.")
        await safe_send_interaction_message(
            interaction,
            content="`/도움말` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )


@bot.tree.command(
    name="채널등록",
    description="현재 채널을 핫딜 수신 채널로 등록합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def set_channel(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "이 명령어는 서버 채널에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    channel_id = interaction.channel.id

    try:
        async with service.deal_state_lock:
            existing_channel_id = service.registered_channels.get(guild_id)
            if existing_channel_id == channel_id:
                service.update_registered_guild_name(guild_id, interaction.guild.name)
                log_admin_audit_event(
                    interaction,
                    action="setchannel.noop",
                    details=f"guild_id={guild_id}, channel_id={channel_id}, reason=already_registered",
                )
                await safe_send_interaction_message(
                    interaction,
                    content="이미 이 채널이 등록되어 있습니다.",
                    ephemeral=True,
                )
                return
            if existing_channel_id is not None:
                log_admin_audit_event(
                    interaction,
                    action="setchannel.rejected",
                    details=(
                        f"guild_id={guild_id}, channel_id={channel_id}, "
                        f"reason=existing_channel:{existing_channel_id}"
                    ),
                )
                await safe_send_interaction_message(
                    interaction,
                    content=(
                        "이 서버에는 이미 등록된 채널이 있습니다 "
                        f"(<#{existing_channel_id}>). 먼저 해당 채널에서 `/채널해제`를 실행한 뒤, "
                        "원하는 채널에서 `/채널등록`을 실행해 주세요."
                    ),
                    ephemeral=True,
                )
                return

            service.register_channel(guild_id, channel_id, interaction.guild.name)
            log_admin_audit_event(
                interaction,
                action="setchannel.success",
                details=f"guild_id={guild_id}, channel_id={channel_id}",
            )

        await safe_send_interaction_message(
            interaction,
            content=(
                "현재 채널을 수신 채널로 등록했습니다. 기존에 수집된 핫딜은 다시 전송하지 않습니다.\n"
                "필요하면 `/필터`, `/사이트필터`로 제외 대상을 설정해 주세요."
            ),
            ephemeral=True,
        )
        runtime_logger.info(f"Channel {channel_id} registered for guild {guild_id}.")
    except Exception as e:
        error_logger.exception(
            "Error registering channel with /setchannel. "
            f"guild_id={guild_id}, channel_id={channel_id}"
        )
        log_admin_audit_event(
            interaction,
            action="setchannel.error",
            details=f"guild_id={guild_id}, channel_id={channel_id}, error={type(e).__name__}",
        )
        await safe_send_interaction_message(
            interaction,
            content="채널 등록 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )


@bot.tree.command(
    name="필터",
    description="이 서버의 제외 카테고리를 설정합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    mode="모드 (조회, 설정, 추가, 삭제, 초기화)",
    category="설정/추가/삭제에서 사용할 카테고리 목록 (쉼표 구분)",
)
@app_commands.rename(mode="모드", category="카테고리")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="조회", value="조회"),
        app_commands.Choice(name="설정", value="설정"),
        app_commands.Choice(name="추가", value="추가"),
        app_commands.Choice(name="삭제", value="삭제"),
        app_commands.Choice(name="초기화", value="초기화"),
    ]
)
async def filter_command(
    interaction: discord.Interaction,
    mode: str,
    category: app_commands.Range[str, 1, FILTER_CATEGORY_INPUT_MAX_LEN] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "이 명령어는 서버 채널에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    mode_value = normalize_mode_value(mode)
    if mode_value not in VALID_MODE_VALUES:
        log_admin_audit_event(
            interaction,
            action="filter.rejected",
            details=f"guild_id={guild_id}, mode={mode_value}, reason=invalid_mode",
        )
        await interaction.response.send_message(
            "잘못된 모드 값입니다. 사용 가능한 값: 조회, 설정, 추가, 삭제, 초기화.",
            ephemeral=True,
        )
        return

    parsed_norms: set[str] = set()
    if mode_value in {"설정", "추가", "삭제"}:
        if category is None or not category.strip():
            log_admin_audit_event(
                interaction,
                action="filter.rejected",
                details=f"guild_id={guild_id}, mode={mode_value}, reason=missing_category",
            )
            await interaction.response.send_message(
                "해당 모드에서는 `카테고리` 입력이 필요합니다. "
                f"사용 가능한 카테고리: {format_filterable_categories_for_help()}",
                ephemeral=True,
            )
            return

        parsed_norms, unknown_values = parse_excluded_categories_input(category)
        if unknown_values:
            log_admin_audit_event(
                interaction,
                action="filter.rejected",
                details=(
                    f"guild_id={guild_id}, mode={mode_value}, "
                    f"reason=unknown_categories:{','.join(unknown_values)}"
                ),
            )
            await interaction.response.send_message(
                "알 수 없는 카테고리: "
                f"{', '.join(unknown_values)}\n"
                f"사용 가능한 카테고리: {format_filterable_categories_for_help()}",
                ephemeral=True,
            )
            return

    deferred = await safe_defer_interaction(interaction, ephemeral=True)
    if not deferred:
        await safe_send_interaction_message(
            interaction,
            content="`/필터` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    async with service.deal_state_lock:
        if guild_id not in service.registered_channels:
            log_admin_audit_event(
                interaction,
                action="filter.rejected",
                details=f"guild_id={guild_id}, mode={mode_value}, reason=no_registered_channel",
            )
            await safe_send_interaction_message(
                interaction,
                content=(
                    "이 서버에는 등록된 수신 채널이 없습니다. "
                    "먼저 `/채널등록`을 실행해 주세요."
                ),
                ephemeral=True,
            )
            return

        current_norms = set(service.get_excluded_category_norms_for_guild(guild_id))
        if mode_value == "설정":
            current_norms = set(parsed_norms)
            service.replace_excluded_categories_for_guild(guild_id, current_norms)
        elif mode_value == "추가":
            current_norms = current_norms | set(parsed_norms)
            service.replace_excluded_categories_for_guild(guild_id, current_norms)
        elif mode_value == "삭제":
            current_norms = current_norms - set(parsed_norms)
            service.replace_excluded_categories_for_guild(guild_id, current_norms)
        elif mode_value == "초기화":
            current_norms = set()
            service.replace_excluded_categories_for_guild(guild_id, current_norms)

        if mode_value != "조회":
            log_admin_audit_event(
                interaction,
                action="filter.success",
                details=(
                    f"guild_id={guild_id}, mode={mode_value}, "
                    f"excluded_count={len(current_norms)}"
                ),
            )

    if mode_value == "조회":
        header = "현재 제외 카테고리입니다."
    elif mode_value == "설정":
        header = "제외 카테고리를 설정했습니다."
    elif mode_value == "추가":
        header = "제외 카테고리를 추가했습니다."
    elif mode_value == "삭제":
        header = "제외 카테고리를 제거했습니다."
    else:
        header = "제외 카테고리를 모두 초기화했습니다."

    await safe_send_interaction_message(
        interaction,
        content=(
            f"{header}\n"
            f"- 제외 카테고리: {format_excluded_categories_for_display(current_norms)}"
        ),
        ephemeral=True,
    )


@filter_command.autocomplete("category")
async def filter_category_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        return get_excluded_categories_autocomplete_choices(current)
    except Exception:
        error_logger.exception("Autocomplete error for /filter category.")
        return []


@bot.tree.command(
    name="사이트필터",
    description="이 서버의 제외 사이트를 설정합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(
    mode="모드 (조회, 설정, 추가, 삭제, 초기화)",
    site="설정/추가/삭제에서 사용할 사이트 목록 (쉼표 구분)",
)
@app_commands.rename(mode="모드", site="사이트")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="조회", value="조회"),
        app_commands.Choice(name="설정", value="설정"),
        app_commands.Choice(name="추가", value="추가"),
        app_commands.Choice(name="삭제", value="삭제"),
        app_commands.Choice(name="초기화", value="초기화"),
    ]
)
async def site_filter_command(
    interaction: discord.Interaction,
    mode: str,
    site: app_commands.Range[str, 1, SITE_FILTER_INPUT_MAX_LEN] | None = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "이 명령어는 서버 채널에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    mode_value = normalize_mode_value(mode)
    if mode_value not in VALID_MODE_VALUES:
        log_admin_audit_event(
            interaction,
            action="sitefilter.rejected",
            details=f"guild_id={guild_id}, mode={mode_value}, reason=invalid_mode",
        )
        await interaction.response.send_message(
            "잘못된 모드 값입니다. 사용 가능한 값: 조회, 설정, 추가, 삭제, 초기화.",
            ephemeral=True,
        )
        return

    parsed_site_codes: set[str] = set()
    if mode_value in {"설정", "추가", "삭제"}:
        if site is None or not site.strip():
            log_admin_audit_event(
                interaction,
                action="sitefilter.rejected",
                details=f"guild_id={guild_id}, mode={mode_value}, reason=missing_site",
            )
            await interaction.response.send_message(
                "해당 모드에서는 `사이트` 입력이 필요합니다. "
                f"사용 가능한 사이트: {format_filterable_sites_for_help()}",
                ephemeral=True,
            )
            return

        parsed_site_codes, unknown_values = parse_excluded_sites_input(site)
        if unknown_values:
            log_admin_audit_event(
                interaction,
                action="sitefilter.rejected",
                details=(
                    f"guild_id={guild_id}, mode={mode_value}, "
                    f"reason=unknown_sites:{','.join(unknown_values)}"
                ),
            )
            await interaction.response.send_message(
                "알 수 없는 사이트: "
                f"{', '.join(unknown_values)}\n"
                f"사용 가능한 사이트: {format_filterable_sites_for_help()}",
                ephemeral=True,
            )
            return

    deferred = await safe_defer_interaction(interaction, ephemeral=True)
    if not deferred:
        await safe_send_interaction_message(
            interaction,
            content="`/사이트필터` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
            ephemeral=True,
        )
        return

    async with service.deal_state_lock:
        if guild_id not in service.registered_channels:
            log_admin_audit_event(
                interaction,
                action="sitefilter.rejected",
                details=f"guild_id={guild_id}, mode={mode_value}, reason=no_registered_channel",
            )
            await safe_send_interaction_message(
                interaction,
                content=(
                    "이 서버에는 등록된 수신 채널이 없습니다. "
                    "먼저 `/채널등록`을 실행해 주세요."
                ),
                ephemeral=True,
            )
            return

        current_site_codes = set(service.get_excluded_site_codes_for_guild(guild_id))
        if mode_value == "설정":
            current_site_codes = set(parsed_site_codes)
            service.replace_excluded_sites_for_guild(guild_id, current_site_codes)
        elif mode_value == "추가":
            current_site_codes = current_site_codes | set(parsed_site_codes)
            service.replace_excluded_sites_for_guild(guild_id, current_site_codes)
        elif mode_value == "삭제":
            current_site_codes = current_site_codes - set(parsed_site_codes)
            service.replace_excluded_sites_for_guild(guild_id, current_site_codes)
        elif mode_value == "초기화":
            current_site_codes = set()
            service.replace_excluded_sites_for_guild(guild_id, current_site_codes)

        if mode_value != "조회":
            log_admin_audit_event(
                interaction,
                action="sitefilter.success",
                details=(
                    f"guild_id={guild_id}, mode={mode_value}, "
                    f"excluded_site_count={len(current_site_codes)}"
                ),
            )

    if mode_value == "조회":
        header = "현재 제외 사이트입니다."
    elif mode_value == "설정":
        header = "제외 사이트를 설정했습니다."
    elif mode_value == "추가":
        header = "제외 사이트를 추가했습니다."
    elif mode_value == "삭제":
        header = "제외 사이트를 제거했습니다."
    else:
        header = "제외 사이트를 모두 초기화했습니다."

    await safe_send_interaction_message(
        interaction,
        content=(
            f"{header}\n"
            f"- 제외 사이트: {format_excluded_sites_for_display(current_site_codes)}"
        ),
        ephemeral=True,
    )


@site_filter_command.autocomplete("site")
async def site_filter_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        return get_excluded_sites_autocomplete_choices(current)
    except Exception:
        error_logger.exception("Autocomplete error for /site-filter site.")
        return []


@bot.tree.command(
    name="알림",
    description="키워드 알림 DM을 설정합니다.",
)
@app_commands.describe(
    mode="모드 (조회, 설정, 추가, 삭제, 초기화)",
    keyword="설정/추가/삭제에서 사용할 키워드 목록 (쉼표 구분)",
)
@app_commands.rename(mode="모드", keyword="키워드")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="조회", value="조회"),
        app_commands.Choice(name="설정", value="설정"),
        app_commands.Choice(name="추가", value="추가"),
        app_commands.Choice(name="삭제", value="삭제"),
        app_commands.Choice(name="초기화", value="초기화"),
    ]
)
async def alert_command(
    interaction: discord.Interaction,
    mode: str,
    keyword: app_commands.Range[str, 1, ALERT_KEYWORD_INPUT_MAX_LEN] | None = None,
):
    user_id = interaction.user.id
    mode_value = normalize_mode_value(mode)
    if mode_value not in VALID_MODE_VALUES:
        await interaction.response.send_message(
            embed=build_alert_response_embed(
                "잘못된 모드 값입니다.",
                extra_lines=["사용 가능한 값: 조회, 설정, 추가, 삭제, 초기화"],
                is_error=True,
            ),
            ephemeral=True,
        )
        return

    if mode_value in {"설정", "추가", "삭제"} and (keyword is None or not keyword.strip()):
        await interaction.response.send_message(
            embed=build_alert_response_embed(
                "`키워드` 입력이 필요합니다.",
                extra_lines=["쉼표(,)로 여러 개를 입력할 수 있습니다."],
                is_error=True,
            ),
            ephemeral=True,
        )
        return

    parsed_keyword_rules: dict[str, str] = {}
    if mode_value in {"설정", "추가", "삭제"}:
        parsed_keywords, was_truncated = parse_alert_keywords_input(keyword)
        if was_truncated:
            await interaction.response.send_message(
                embed=build_alert_response_embed(
                    "한 번에 너무 많은 키워드를 입력했습니다.",
                    extra_lines=[f"최대 개수: {ALERT_KEYWORD_MAX_ITEMS_PER_REQUEST}개"],
                    is_error=True,
                ),
                ephemeral=True,
            )
            return

        if not parsed_keywords:
            await interaction.response.send_message(
                embed=build_alert_response_embed(
                    "유효한 키워드가 없어 처리할 수 없습니다.",
                    is_error=True,
                ),
                ephemeral=True,
            )
            return

        if mode_value == "삭제":
            # Deletion only needs normalization — stored keywords that are
            # invalid under CURRENT add-rules must still be removable.
            for raw_keyword in parsed_keywords:
                keyword_norm = service.normalize_alert_text(raw_keyword)
                if keyword_norm:
                    parsed_keyword_rules[keyword_norm] = raw_keyword

            if not parsed_keyword_rules:
                await interaction.response.send_message(
                    embed=build_alert_response_embed(
                        "유효한 키워드가 없어 처리할 수 없습니다.",
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return
        else:
            too_short_or_long: list[str] = []
            invalid_tokens: list[str] = []
            abuse_tokens: list[str] = []
            for raw_keyword in parsed_keywords:
                keyword_len = len(raw_keyword)
                if (
                    keyword_len < service.ALERT_KEYWORD_MIN_LEN
                    or keyword_len > service.ALERT_KEYWORD_MAX_LEN
                ):
                    too_short_or_long.append(raw_keyword)
                    continue

                rule = service.build_alert_rule(raw_keyword)
                if rule is None:
                    invalid_tokens.append(raw_keyword)
                    continue

                abuse_reason = service.get_alert_keyword_abuse_reason(rule)
                if abuse_reason is not None:
                    abuse_tokens.append(f"{raw_keyword} ({abuse_reason})")
                    continue

                parsed_keyword_rules[rule.keyword_norm] = rule.keyword_raw

            if too_short_or_long:
                await interaction.response.send_message(
                    embed=build_alert_response_embed(
                        "키워드 길이 제한을 확인해 주세요.",
                        extra_lines=[
                            (
                                f"허용 범위: "
                                f"{service.ALERT_KEYWORD_MIN_LEN}~{service.ALERT_KEYWORD_MAX_LEN}자"
                            ),
                            f"제한 위반: {', '.join(too_short_or_long)}",
                        ],
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return

            if invalid_tokens:
                await interaction.response.send_message(
                    embed=build_alert_response_embed(
                        "일부 키워드는 형식이 올바르지 않습니다.",
                        extra_lines=[f"잘못된 값: {', '.join(invalid_tokens)}"],
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return

            if abuse_tokens:
                await interaction.response.send_message(
                    embed=build_alert_response_embed(
                        "일부 키워드는 과도하게 넓은 매칭으로 차단되었습니다.",
                        extra_lines=[f"차단됨: {', '.join(abuse_tokens)}"],
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return

            if mode_value == "설정" and len(parsed_keyword_rules) > service.ALERT_KEYWORD_MAX_COUNT:
                await interaction.response.send_message(
                    embed=build_alert_response_embed(
                        "설정하려는 키워드 수가 제한을 초과합니다.",
                        extra_lines=[f"최대 허용 개수: {service.ALERT_KEYWORD_MAX_COUNT}"],
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return

    # Consume the cooldown only after validation passed, atomically.
    if mode_value in {"설정", "추가", "삭제", "초기화"}:
        can_update, retry_after = service.try_alert_update(user_id)
        if not can_update:
            await interaction.response.send_message(
                embed=build_alert_response_embed(
                    "요청이 너무 빠릅니다.",
                    extra_lines=[f"{retry_after:.1f}초 후 다시 시도해 주세요."],
                    is_error=True,
                ),
                ephemeral=True,
            )
            return

    deferred = await safe_defer_interaction(interaction, ephemeral=True)
    if not deferred:
        await safe_send_interaction_message(
            interaction,
            embed=build_alert_response_embed(
                "`/알림` 실행 중 오류가 발생했습니다.",
                extra_lines=["잠시 후 다시 시도해 주세요."],
                is_error=True,
            ),
            ephemeral=True,
        )
        return

    async with service.deal_state_lock:
        if mode_value == "조회":
            current_keywords = service.get_alert_keywords_for_user(user_id)
            await safe_send_interaction_message(
                interaction,
                embed=build_alert_response_embed(
                    "현재 알림 설정입니다.",
                    current_keywords,
                    extra_lines=[
                        f"예시: /알림 모드:추가 키워드:{build_alert_examples_for_display()}",
                    ],
                ),
                ephemeral=True,
            )
            return

        if mode_value == "초기화":
            removed_count = service.clear_alert_keywords_for_user(user_id)
            current_keywords = service.get_alert_keywords_for_user(user_id)
            await safe_send_interaction_message(
                interaction,
                embed=build_alert_response_embed(
                    "알림 키워드를 모두 초기화했습니다.",
                    current_keywords,
                    extra_lines=[f"초기화된 개수: {removed_count}개"],
                ),
                ephemeral=True,
            )
            return

        if mode_value == "추가":
            current_count = service.get_alert_keyword_count_for_user(user_id)
            newly_added_count = 0
            for keyword_norm in parsed_keyword_rules:
                if not service.has_alert_keyword_norm_for_user(user_id, keyword_norm):
                    newly_added_count += 1

            if current_count + newly_added_count > service.ALERT_KEYWORD_MAX_COUNT:
                await safe_send_interaction_message(
                    interaction,
                    embed=build_alert_response_embed(
                        "추가하려는 키워드 수가 제한을 초과합니다.",
                        extra_lines=[
                            f"현재 개수: {current_count}",
                            f"신규 추가 수: {newly_added_count}",
                            f"최대 허용 개수: {service.ALERT_KEYWORD_MAX_COUNT}",
                        ],
                        is_error=True,
                    ),
                    ephemeral=True,
                )
                return

            inserted_count = 0
            for keyword_raw in parsed_keyword_rules.values():
                was_new = service.add_alert_rule_for_user(user_id, keyword_raw)
                if was_new:
                    inserted_count += 1

            current_keywords = service.get_alert_keywords_for_user(user_id)
            await safe_send_interaction_message(
                interaction,
                embed=build_alert_response_embed(
                    "알림 키워드를 추가했습니다.",
                    current_keywords,
                    extra_lines=[f"추가된 개수: {inserted_count}개"],
                ),
                ephemeral=True,
            )
            return

        if mode_value == "설정":
            service.clear_alert_keywords_for_user(user_id)
            for keyword_raw in parsed_keyword_rules.values():
                service.add_alert_rule_for_user(user_id, keyword_raw)

            current_keywords = service.get_alert_keywords_for_user(user_id)
            await safe_send_interaction_message(
                interaction,
                embed=build_alert_response_embed(
                    "알림 키워드를 설정했습니다.",
                    current_keywords,
                    extra_lines=[f"적용된 개수: {len(current_keywords)}개"],
                ),
                ephemeral=True,
            )
            return

        removed_count = 0
        for keyword_norm in parsed_keyword_rules:
            if service.remove_alert_keyword_norm_for_user(user_id, keyword_norm):
                removed_count += 1

        current_keywords = service.get_alert_keywords_for_user(user_id)
        await safe_send_interaction_message(
            interaction,
            embed=build_alert_response_embed(
                "알림 키워드를 제거했습니다.",
                current_keywords,
                extra_lines=[f"제거된 개수: {removed_count}개"],
            ),
            ephemeral=True,
        )


@alert_command.autocomplete("keyword")
async def alert_keyword_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    try:
        mode_value = normalize_mode_value(getattr(interaction.namespace, "mode", ""))
        if mode_value != "삭제":
            return []

        async with service.deal_state_lock:
            user_keywords = service.get_alert_keywords_for_user(interaction.user.id)

        current_norm = str(current or "").strip().casefold()
        choices: list[app_commands.Choice[str]] = []
        for keyword in user_keywords:
            if current_norm and current_norm not in keyword.casefold():
                continue
            choices.append(app_commands.Choice(name=keyword, value=keyword))

        return choices[:25]
    except Exception:
        error_logger.exception("Autocomplete error for /alert keyword.")
        return []


@bot.tree.command(
    name="채널해제",
    description="현재 서버의 수신 채널 등록을 해제합니다.",
)
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_channel(interaction: discord.Interaction):
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "이 명령어는 서버 채널에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    command_channel_id = interaction.channel.id
    remove_status = "none"
    removed_channel_id: int | None = None
    registered_channel_id: int | None = None
    async with service.deal_state_lock:
        registered_channel_id = service.registered_channels.get(guild_id)
        if registered_channel_id is None:
            remove_status = "none"
        elif registered_channel_id != command_channel_id:
            remove_status = "wrong_channel"
        else:
            remove_status = "removed"
            removed_channel_id = service.unregister_channel_by_guild_id(guild_id)

    if remove_status == "wrong_channel":
        log_admin_audit_event(
            interaction,
            action="removechannel.rejected",
            details=(
                f"guild_id={guild_id}, command_channel_id={command_channel_id}, "
                f"reason=wrong_channel:{registered_channel_id}"
            ),
        )
        await interaction.response.send_message(
            "등록된 채널은 "
            f"<#{registered_channel_id}>. "
            "해당 채널에서 `/채널해제`를 실행해 주세요.",
            ephemeral=True,
        )
    elif remove_status == "removed" and removed_channel_id is not None:
        log_admin_audit_event(
            interaction,
            action="removechannel.success",
            details=f"guild_id={guild_id}, removed_channel_id={removed_channel_id}",
        )
        await interaction.response.send_message(
            "현재 채널의 등록을 해제했습니다. "
            "이제 이 서버로는 핫딜을 전송하지 않습니다.",
            ephemeral=True,
        )
        runtime_logger.info(
            f"Guild {guild_id} channel unregistered (channel_id={removed_channel_id})."
        )
    else:
        log_admin_audit_event(
            interaction,
            action="removechannel.noop",
            details=f"guild_id={guild_id}, command_channel_id={command_channel_id}, reason=none_registered",
        )
        await interaction.response.send_message(
            "이 서버에 등록된 채널이 없습니다.",
            ephemeral=True,
        )


@help_command.error
async def help_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    error_logger.error(f"Unhandled /help error: {error}")
    await safe_send_interaction_message(
        interaction,
        content="`/도움말` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        ephemeral=True,
    )


@set_channel.error
async def set_channel_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        log_admin_audit_event(
            interaction,
            action="setchannel.denied",
            details="reason=missing_manage_guild_permission",
        )
        message = "이 명령어는 `서버 관리` 권한이 필요합니다."
    else:
        message = "`/채널등록` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        error_logger.error(f"Unhandled /setchannel error: {error}")

    await safe_send_interaction_message(
        interaction,
        content=message,
        ephemeral=True,
    )


@filter_command.error
async def filter_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        log_admin_audit_event(
            interaction,
            action="filter.denied",
            details="reason=missing_manage_guild_permission",
        )
        message = "이 명령어는 `서버 관리` 권한이 필요합니다."
    else:
        message = "`/필터` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        error_logger.error(f"Unhandled /filter error: {error}")

    await safe_send_interaction_message(
        interaction,
        content=message,
        ephemeral=True,
    )


@site_filter_command.error
async def site_filter_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        log_admin_audit_event(
            interaction,
            action="sitefilter.denied",
            details="reason=missing_manage_guild_permission",
        )
        message = "이 명령어는 `서버 관리` 권한이 필요합니다."
    else:
        message = "`/사이트필터` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        error_logger.error(f"Unhandled /site-filter error: {error}")

    await safe_send_interaction_message(
        interaction,
        content=message,
        ephemeral=True,
    )


@alert_command.error
async def alert_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    error_logger.error(f"Unhandled /alert error: {error}")

    await safe_send_interaction_message(
        interaction,
        embed=build_alert_response_embed(
            "`/알림` 실행 중 오류가 발생했습니다.",
            extra_lines=["잠시 후 다시 시도해 주세요."],
            is_error=True,
        ),
        ephemeral=True,
    )


@remove_channel.error
async def remove_channel_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.errors.MissingPermissions):
        log_admin_audit_event(
            interaction,
            action="removechannel.denied",
            details="reason=missing_manage_guild_permission",
        )
        message = "이 명령어는 `서버 관리` 권한이 필요합니다."
    else:
        message = "`/채널해제` 실행 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
        error_logger.error(f"Unhandled /removechannel error: {error}")

    await safe_send_interaction_message(
        interaction,
        content=message,
        ephemeral=True,
    )


############################
# Run the Bot
############################
def main() -> None:
    try:
        validated_token = validate_token_or_raise(TOKEN)
        validate_environment_paths_or_raise()
        with StartupLock(LOCK_FILE):
            try:
                service.initialize_state()
                bot.run(validated_token, log_level=logging.WARN)
            finally:
                close_db()
    except StartupLockError as e:
        runtime_logger.info(str(e))
        error_logger.error(f"Startup lock error: {e}")
    except discord.LoginFailure as e:
        runtime_logger.info(
            "Discord rejected the bot token. Check DISCORD_BOT_TOKEN in .env."
        )
        error_logger.error(f"Discord login failure: {e}")
    except sqlite3.Error as e:
        runtime_logger.info(f"Database error during startup: {e}")
        error_logger.exception("Database error during startup.")
    except RuntimeError as e:
        runtime_logger.info(str(e))
        error_logger.error(f"Startup configuration error: {e}")


if __name__ == "__main__":
    main()
