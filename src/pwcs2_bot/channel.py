"""Framework-neutral QQ group application with lazy legacy transport compatibility."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from contextlib import suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from loguru import logger
from pydantic import AwareDatetime, Field, model_validator

from pwcs2_bot.ai_chat import (
    DEFAULT_CHAT_SYSTEM_PROMPT,
    DEFAULT_MAIN_MODEL,
    DEFAULT_VISION_MODEL,
    AIChatError,
    AIChatService,
)
from pwcs2_bot.ai_group_data import AIGroupDataService
from pwcs2_bot.auto_prediction import AutoPredictionService
from pwcs2_bot.chat_profiles import GroupPersona
from pwcs2_bot.chemistry import ChemistryNetworkSnapshot, format_chemistry_network
from pwcs2_bot.commands import CommandService, ParsedCommand
from pwcs2_bot.community import (
    CommunityRepository,
    GroupPlayService,
    HistorySource,
    QuizQuestion,
    QuizReveal,
    format_quiz_question,
    format_quiz_reveal,
)
from pwcs2_bot.content_relay import DEFAULT_BLOCKED_KEYWORDS, ContentRelayService
from pwcs2_bot.daily_admin import DailyAdminService, format_daily_admin_announcement
from pwcs2_bot.daily_grenade_quiz import DailyGrenadeQuizService
from pwcs2_bot.daily_summary import DailySummaryService
from pwcs2_bot.daily_word_cloud import DailyWordCloudService, WordCloudSnapshot
from pwcs2_bot.database import BindingRepository, DailyAdminRun
from pwcs2_bot.group_ai_memory import GroupAIMemoryService
from pwcs2_bot.group_card_lock import GroupCardLockReconciler
from pwcs2_bot.group_companion import COMPANION_SYSTEM_PROMPT, GroupCompanion
from pwcs2_bot.group_level_reminder import (
    LEVEL_REASON,
    UNBOUND_REASON,
    ComplianceReminder,
    GroupLevelReminderService,
)
from pwcs2_bot.highlight_commentary import generate_highlight_commentary
from pwcs2_bot.highlight_delivery import (
    HighlightPreDispatchError,
    HighlightVideoDelivery,
)
from pwcs2_bot.highlight_jobs import HighlightJobStore
from pwcs2_bot.highlight_protocol import HighlightProtocolError
from pwcs2_bot.keyword_reaction import (
    is_xh_reaction_trigger,
    xh_reaction_image_path,
)
from pwcs2_bot.match_commentary import MatchCommentaryService
from pwcs2_bot.match_formatting import MatchSnapshot, format_match_snapshot
from pwcs2_bot.match_notifier import MatchNotifier
from pwcs2_bot.member_memory import MemberMemoryService
from pwcs2_bot.membership_sync import MembershipReconciler
from pwcs2_bot.native_voice import NativeVoice, NativeVoiceError
from pwcs2_bot.party import PartyService
from pwcs2_bot.perfectworld import PerfectWorldClient
from pwcs2_bot.skin_prices import (
    DailySkinPriceService,
    SkinPriceClient,
    SkinPriceReport,
)
from pwcs2_bot.stats_card import StatsCardRenderer, StatsCardRenderError
from pwcs2_bot.steam_presence import SteamPresenceClient
from pwcs2_bot.stream_live import LivePlatformClient, StreamerTarget
from pwcs2_bot.stream_live_monitor import StreamLiveMonitor
from pwcs2_bot.teamspeak import TeamSpeakStatusClient
from pwcs2_bot.training_game import (
    DEFAULT_TRAINING_GAME_URL,
    TRAINING_GAME_FILE_NAME,
    TRAINING_GAME_MAX_FILE_BYTES,
    TrainingGameRateLimiter,
    build_training_game_system_prompt,
    format_training_game_intro,
    is_training_game_direct_request,
    is_training_game_file_request,
    is_training_game_trigger,
    training_game_html_path,
)
from pwcs2_bot.transport_contract import OutboundMessage, QQTransportConfig
from pwcs2_bot.transport_health import TransportHealthMonitor
from pwcs2_bot.unbound_guard import UnboundMessageDecision, UnboundMessageGuard
from pwcs2_bot.valorant import ValorantClient
from pwcs2_bot.watch_stage import WatchStageClient
from pwcs2_bot.weekly_chemistry import WeeklyChemistryService
from pwcs2_bot.weekly_report import (
    WeeklyReportService,
    WeeklyReportSnapshot,
    format_weekly_report,
)

_AI_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_DATA_IMAGE_PREFIX = "data:image/"
_ONEBOT_TRUE_VALUES = {"1", "true", "yes"}
_ONEBOT_FALSE_VALUES = {"0", "false", "no"}


def _parse_onebot_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in _ONEBOT_TRUE_VALUES:
        return True
    if normalized in _ONEBOT_FALSE_VALUES:
        return False
    raise ValueError(f"invalid OneBot boolean: {value!r}")


def split_outbound_text(text: str, limit: int = 3500) -> tuple[str, ...]:
    remaining = text.strip()
    if not remaining:
        return ()
    chunks: list[str] = []
    while len(remaining) > limit:
        cut = max(
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind("。", 0, limit + 1),
            remaining.rfind("！", 0, limit + 1),
            remaining.rfind("？", 0, limit + 1),
        )
        if cut < limit // 2:
            cut = limit
        elif remaining[cut] != "\n":
            cut += 1
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return tuple(chunks)


_EXPLICIT_REPLY: ContextVar[bool] = ContextVar("pwcs2_explicit_reply", default=False)


class PWCS2Config(QQTransportConfig):
    """NapCat transport plus Perfect World and command settings."""

    pw_token: str = ""
    pw_app_version: str = "3.5.4.172"
    pw_base_url: str = "https://api.wmpvp.com"
    pw_my_steam_id: str = ""
    database_path: str = "/data/bindings.db"
    command_prefix: str = "#"
    allowed_groups: list[str] = Field(default_factory=list)
    ai_chat_only_groups: list[str] = Field(default_factory=list)
    ai_mention_only_groups: list[str] = Field(default_factory=list)
    ai_group_personas: dict[str, GroupPersona] = Field(default_factory=dict)
    ai_upstream_extensions_enabled: bool = False
    private_replies_enabled: bool = True
    automatic_quiet_until: AwareDatetime | None = None
    message_replay_ignore_before: AwareDatetime | None = None
    stream_live_enabled: bool = False
    stream_live_targets: list[StreamerTarget] = Field(
        default_factory=lambda: [
            StreamerTarget(
                platform="douyu",
                name="玩机器",
                room_id="6979222",
                url="https://www.douyu.com/6657",
            ),
            StreamerTarget(
                platform="huya",
                name="DANK1NG",
                room_id="10188",
                url="https://www.huya.com/dank1ng",
            ),
            StreamerTarget(
                platform="douyin",
                name="MasTurbaT0r",
                room_id="59901412180",
                url="https://live.douyin.com/59901412180",
                expected_sec_uid=(
                    "MS4wLjABAAAAd0oHxHUvBQGLCc5xbYs1pnCtsojy7MBXZCYcjJ1kxuOmrdL0ZSuyDfZYoxVReCBD"
                ),
            ),
        ]
    )
    stream_live_poll_interval_seconds: int = Field(default=60, ge=30, le=3600)
    stream_live_initial_delay_seconds: int = Field(default=20, ge=0, le=300)
    stream_live_timeout_seconds: float = Field(default=10, gt=0, le=30)
    stream_live_confirmation_polls: int = Field(default=2, ge=1, le=5)
    xh_reaction_enabled: bool = True
    training_game_enabled: bool = True
    training_game_url: str = DEFAULT_TRAINING_GAME_URL
    training_game_group_cooldown_seconds: float = Field(
        default=1800, ge=0, le=86400
    )
    training_game_user_cooldown_seconds: float = Field(
        default=60, ge=0, le=3600
    )
    training_game_file_upload_enabled: bool = True
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    cache_ttl_seconds: int = Field(default=60, ge=0, le=3600)
    query_cooldown_seconds: float = Field(default=5.0, ge=0, le=300)
    stats_image_enabled: bool = True
    stats_image_timeout_seconds: float = Field(default=45.0, gt=0, le=120)
    valorant_enabled: bool = True
    valorant_query_url: str = "https://wgval.cloud/public-query"
    valorant_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    valorant_queue_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    valorant_cache_ttl_seconds: float = Field(default=600.0, ge=0, le=86400)
    valorant_stale_ttl_seconds: float = Field(default=86400.0, ge=0, le=604800)
    valorant_failure_threshold: int = Field(default=3, ge=1, le=20)
    valorant_failure_cooldown_seconds: float = Field(default=1800.0, ge=0, le=86400)
    watch_stage_enabled: bool = True
    watch_stage_base_url: str = "https://appactivity.wmpvp.com"
    watch_stage_timeout_seconds: float = Field(default=8.0, gt=0, le=30)
    match_notifications_enabled: bool = True
    match_poll_interval_seconds: int = Field(default=180, ge=30, le=3600)
    match_poll_initial_delay_seconds: int = Field(default=15, ge=0, le=300)
    match_poll_page_size: int = Field(default=20, ge=1, le=50)
    match_notifications_per_poll: int = Field(default=3, ge=1, le=10)
    match_ai_commentary_enabled: bool = True
    match_ai_commentary_max_tokens: int = Field(default=500, ge=128, le=1200)
    demo_sources_enabled: bool = False
    demo_storage_path: str = "/data/demos"
    demo_import_root: str = "/data/demo-import"
    demo_max_file_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1024 * 1024,
        le=2 * 1024 * 1024 * 1024,
    )
    highlight_enabled: bool = False
    highlight_data_path: str = "/data/highlights"
    highlight_intent_database_path: str = "/data/highlights/intents.db"
    highlight_delivery_poll_seconds: int = Field(default=30, ge=5, le=3600)
    highlight_delivery_initial_delay_seconds: int = Field(default=20, ge=0, le=3600)
    highlight_delivery_timeout_seconds: float = Field(default=120.0, ge=30, le=300)
    highlight_unknown_retention_days: int = Field(default=14, ge=3, le=90)
    highlight_inline_video_max_bytes: int = Field(
        default=24 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024,
    )
    highlight_napcat_media_root: str = ""
    highlight_shared_output_gid: int = Field(default=-1, ge=-1, le=65535)
    party_expiration_hours: int = Field(default=6, ge=1, le=24)
    community_initial_balance: int = Field(default=100, ge=0, le=10000)
    community_manager_qq_ids: list[str] = Field(default_factory=list)
    prediction_enabled: bool = False
    prediction_duration_minutes: int = Field(default=30, ge=5, le=1440)
    auto_prediction_enabled: bool = False
    auto_prediction_expiry_hours: int = Field(default=4, ge=1, le=24)
    auto_prediction_poll_interval_seconds: int = Field(default=60, ge=15, le=300)
    auto_prediction_initial_delay_seconds: int = Field(default=30, ge=0, le=300)
    auto_prediction_watch_concurrency: int = Field(default=4, ge=1, le=10)
    auto_prediction_max_total_score: int = Field(default=3, ge=0, le=10)
    steam_web_api_key: str = ""
    steam_presence_base_url: str = "https://api.steampowered.com"
    steam_presence_proxy_url: str = ""
    steam_presence_timeout_seconds: float = Field(default=6, gt=0, le=30)
    steam_presence_cache_ttl_seconds: float = Field(default=45, ge=0, le=3600)
    steam_presence_stale_ttl_seconds: float = Field(default=900, ge=0, le=86400)
    steam_presence_failure_cooldown_seconds: float = Field(default=15, ge=0, le=300)
    grenade_quiz_enabled: bool = True
    grenade_quiz_question_time: str = Field(
        default="19:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    grenade_quiz_reveal_time: str = Field(
        default="21:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    grenade_quiz_timezone: str = "Asia/Shanghai"
    grenade_quiz_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    grenade_quiz_initial_delay_seconds: int = Field(default=45, ge=0, le=300)
    grenade_quiz_correct_reward: int = Field(default=20, ge=1, le=1000)
    grenade_quiz_fastest_bonus: int = Field(default=10, ge=0, le=1000)
    weekly_report_enabled: bool = True
    weekly_report_time: str = Field(
        default="22:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    weekly_report_weekday: int = Field(default=6, ge=0, le=6)
    weekly_report_timezone: str = "Asia/Shanghai"
    weekly_report_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    weekly_report_initial_delay_seconds: int = Field(default=90, ge=0, le=300)
    weekly_report_match_page_size: int = Field(default=50, ge=10, le=50)
    weekly_chemistry_enabled: bool = True
    weekly_chemistry_time: str = Field(
        default="21:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    weekly_chemistry_weekday: int = Field(default=6, ge=0, le=6)
    weekly_chemistry_timezone: str = "Asia/Shanghai"
    weekly_chemistry_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    weekly_chemistry_initial_delay_seconds: int = Field(default=60, ge=0, le=300)
    weekly_chemistry_match_page_size: int = Field(default=50, ge=10, le=50)
    membership_sync_enabled: bool = True
    membership_sync_interval_seconds: int = Field(default=21600, ge=300, le=86400)
    membership_sync_initial_delay_seconds: int = Field(default=45, ge=0, le=300)
    unbound_guard_enabled: bool = False
    unbound_daily_message_limit: int = Field(default=10, ge=1, le=100)
    unbound_mute_duration_seconds: int = Field(default=600, ge=60, le=2592000)
    unbound_guard_timezone: str = "Asia/Shanghai"
    daily_summary_enabled: bool = True
    daily_summary_time: str = Field(
        default="23:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    daily_summary_timezone: str = "Asia/Shanghai"
    daily_summary_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    daily_summary_initial_delay_seconds: int = Field(default=30, ge=0, le=300)
    daily_summary_match_page_size: int = Field(default=50, ge=1, le=50)
    daily_admin_enabled: bool = True
    daily_admin_time: str = Field(
        default="00:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    daily_admin_timezone: str = "Asia/Shanghai"
    daily_admin_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    daily_admin_initial_delay_seconds: int = Field(default=60, ge=0, le=300)
    group_card_lock_enabled: bool = True
    group_card_lock_reconcile_interval_seconds: int = Field(
        default=300, ge=30, le=3600
    )
    group_card_lock_reconcile_initial_delay_seconds: int = Field(
        default=15, ge=0, le=300
    )
    admin_ban_guard_enabled: bool = True
    admin_ban_revoke_threshold_seconds: int = Field(default=600, ge=60, le=86400)
    daily_word_cloud_enabled: bool = True
    daily_word_cloud_time: str = Field(
        default="23:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    daily_word_cloud_timezone: str = "Asia/Shanghai"
    daily_word_cloud_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    daily_word_cloud_initial_delay_seconds: int = Field(default=30, ge=0, le=300)
    daily_word_cloud_max_words: int = Field(default=40, ge=10, le=40)
    daily_word_cloud_retention_days: int = Field(default=14, ge=1, le=90)
    skin_price_enabled: bool = True
    skin_price_time: str = Field(
        default="12:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    skin_price_timezone: str = "Asia/Shanghai"
    skin_price_poll_interval_seconds: int = Field(default=300, ge=30, le=3600)
    skin_price_initial_delay_seconds: int = Field(default=45, ge=0, le=300)
    skin_price_max_items_per_group: int = Field(default=20, ge=1, le=50)
    skin_price_retention_days: int = Field(default=90, ge=7, le=365)
    skin_price_request_timeout_seconds: float = Field(default=20, gt=0, le=60)
    skin_price_cache_ttl_seconds: int = Field(default=3600, ge=60, le=21600)
    skin_price_localization_cache_ttl_seconds: int = Field(
        default=86400, ge=3600, le=604800
    )
    skin_price_data_url: str = "https://prices.csgotrader.app/latest/buff163.json"
    skin_price_exchange_url: str = "https://open.er-api.com/v6/latest/USD"
    skin_price_skins_en_url: str = (
        "https://gcore.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/skins.json"
    )
    skin_price_skins_zh_url: str = (
        "https://gcore.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN/skins.json"
    )
    group_level_reminder_enabled: bool = True
    group_level_reminder_time: str = Field(
        default="20:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$"
    )
    group_level_reminder_timezone: str = "Asia/Shanghai"
    group_level_reminder_target_level: int = Field(default=1, ge=0, le=100)
    group_level_reminder_include_unbound: bool = True
    group_level_removal_grace_days: int = Field(default=15, ge=1, le=90)
    group_level_removal_max_per_day: int = Field(default=10, ge=1, le=50)
    group_level_reminder_poll_interval_seconds: int = Field(
        default=300, ge=30, le=3600
    )
    group_level_reminder_initial_delay_seconds: int = Field(
        default=30, ge=0, le=300
    )
    content_relay_enabled: bool = False
    content_relay_source_groups: list[str] = Field(default_factory=list)
    content_relay_target_group: str = ""
    content_relay_daily_limit: int = Field(default=5, ge=1, le=50)
    content_relay_cooldown_seconds: int = Field(default=3600, ge=0, le=86400)
    content_relay_priority_source_groups: list[str] = Field(default_factory=list)
    content_relay_priority_cooldown_seconds: int = Field(
        default=900, ge=0, le=86400
    )
    content_relay_secondary_daily_limit: int = Field(default=3, ge=0, le=50)
    content_relay_timezone: str = "Asia/Shanghai"
    content_relay_max_text_chars: int = Field(default=120, ge=0, le=1000)
    content_relay_max_media: int = Field(default=4, ge=1, le=10)
    content_relay_blocked_keywords: list[str] = Field(
        default_factory=lambda: list(DEFAULT_BLOCKED_KEYWORDS)
    )
    ai_enabled: bool = True
    ai_companion_enabled: bool = False
    ai_companion_groups: list[str] = Field(default_factory=list)
    ai_companion_proactive_enabled: bool = False
    ai_companion_use_upstream_limits: bool = False
    ai_companion_responsive_groups: list[str] = Field(default_factory=list)
    ai_companion_reply_target: float = Field(default=0.85, ge=0, le=1)
    ai_companion_min_interval_seconds: int = Field(default=1800, ge=300, le=86400)
    ai_companion_hourly_limit: int = Field(default=2, ge=1, le=4)
    ai_companion_daily_limit: int = Field(default=8, ge=1, le=24)
    ai_voice_enabled: bool = False
    ai_voice_reply_enabled: bool = False
    ai_voice_character: str = ""
    ai_data_tools_enabled: bool = True
    ai_api_key: str = ""
    ai_base_url: str = "https://api.llm.ustc.edu.cn/v1"
    ai_proxy_url: str = ""
    ai_model: str = DEFAULT_MAIN_MODEL
    ai_vision_model: str = DEFAULT_VISION_MODEL
    ai_vision_max_tokens: int = Field(default=1024, ge=64, le=4096)
    ai_timeout_seconds: float | None = Field(default=60, gt=0, le=180)
    ai_temperature: float = Field(default=0.3, ge=0, le=2)
    ai_max_tokens: int = Field(default=1024, ge=64, le=8192)
    ai_cooldown_seconds: float = Field(default=8, ge=0, le=300)
    ai_max_history_messages: int = Field(default=12, ge=0, le=40)
    ai_max_input_chars: int = Field(default=18000, ge=4000, le=60000)
    ai_max_concurrent_requests: int = Field(default=20, ge=1, le=20)
    ai_group_shared_context_enabled: bool = True
    ai_allow_concurrent_same_chat: bool = True
    ai_multimodal_enabled: bool = True
    ai_max_images: int = Field(default=2, ge=0, le=4)
    ai_image_max_bytes: int = Field(default=4_000_000, ge=1024, le=8_000_000)
    ai_image_timeout_seconds: float = Field(default=12.0, gt=0, le=60)
    ai_member_memory_enabled: bool = True
    ai_member_memory_refresh_interval_seconds: int = Field(
        default=21600, ge=300, le=86400
    )
    ai_member_memory_initial_delay_seconds: int = Field(
        default=75, ge=0, le=300
    )
    ai_member_memory_max_members: int = Field(default=50, ge=1, le=200)
    ai_member_memory_max_context_chars: int = Field(default=12000, ge=1000, le=30000)
    ai_group_memory_enabled: bool = True
    ai_group_memory_max_entries: int = Field(default=20, ge=1, le=100)
    ai_group_memory_max_entries_per_member: int = Field(default=5, ge=1, le=20)
    ai_group_memory_max_content_chars: int = Field(default=200, ge=20, le=1000)
    ai_group_memory_max_context_chars: int = Field(default=6000, ge=500, le=20000)
    forward_unmatched: bool = False
    welcome_new_members: bool = False
    ts_enabled: bool = True
    ts_query_host: str = "teamspeak-query"
    ts_query_port: int = Field(default=10011, ge=1, le=65535)
    ts_query_username: str = "serveradmin"
    ts_query_password_file: str = "/run/secrets/ts3_query_password"
    ts_query_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    ts_cache_ttl_seconds: float = Field(default=10.0, ge=0, le=300)
    ts_public_address: str = "ts.example.com"

    @model_validator(mode="after")
    def validate_cache_ttls(self) -> PWCS2Config:
        if self.steam_presence_stale_ttl_seconds < self.steam_presence_cache_ttl_seconds:
            raise ValueError(
                "steamPresenceStaleTtlSeconds must be greater than or equal to "
                "steamPresenceCacheTtlSeconds"
            )
        if self.valorant_stale_ttl_seconds < self.valorant_cache_ttl_seconds:
            raise ValueError(
                "valorantStaleTtlSeconds must be greater than or equal to "
                "valorantCacheTtlSeconds"
            )
        if self.ai_group_memory_max_entries_per_member > self.ai_group_memory_max_entries:
            raise ValueError(
                "aiGroupMemoryMaxEntriesPerMember must not exceed "
                "aiGroupMemoryMaxEntries"
            )
        training_url = urlparse(self.training_game_url.strip())
        if (
            training_url.scheme not in {"http", "https"}
            or not training_url.hostname
            or training_url.username is not None
            or training_url.password is not None
        ):
            raise ValueError("trainingGameUrl must be a public HTTP(S) URL")
        self.training_game_url = self.training_game_url.strip()
        live_keys = [
            (target.platform, target.room_id)
            for target in self.stream_live_targets
            if target.enabled
        ]
        if len(live_keys) != len(set(live_keys)):
            raise ValueError("streamLiveTargets must not contain duplicate rooms")
        if self.highlight_enabled:
            highlight_root = Path(self.highlight_data_path).expanduser().resolve()
            intent_database = (
                Path(self.highlight_intent_database_path).expanduser().resolve()
            )
            if intent_database.parent != highlight_root:
                raise ValueError(
                    "highlightIntentDatabasePath must be directly inside "
                    "highlightDataPath"
                )
        return self


def _highlight_caption(item: Any) -> str:
    context = item.context if isinstance(getattr(item, "context", None), dict) else {}
    highlight = context.get("highlight")
    if not isinstance(highlight, dict):
        highlight = {}
    raw_tags = highlight.get("tags")
    tags = tuple(str(tag) for tag in raw_tags) if isinstance(raw_tags, list) else ()
    lowlight_names = {
        "lowlight_close_whiff": "近战描边",
        "lowlight_self_flash": "自闪暴毙",
        "lowlight_self_damage": "自残暴毙",
        "lowlight_knife_fail": "刀人失败",
        "lowlight_advantage_throw": "优势残局送掉",
        "lowlight_teamkill": "痛击队友",
        "lowlight_world_death": "离谱暴毙",
    }
    lowlight_labels = [lowlight_names[tag] for tag in tags if tag in lowlight_names]
    steam_id = str(getattr(item, "steam_id", ""))
    match_id = str(getattr(item, "match_id", ""))[:80]
    if lowlight_labels:
        detail = " · ".join(lowlight_labels)
        return f"🍚 下饭时刻｜{detail}｜SteamID {steam_id}｜比赛 {match_id}"
    return f"🎬 Demo 自动高光｜SteamID {steam_id}｜比赛 {match_id}"


class PWCS2Application:
    """CS/group application shared by transports; does not start a gateway itself."""

    name = "pwcs2"
    display_name = "Perfect World CS2 (QQ/NapCat)"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return PWCS2Config().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: Any = None) -> None:
        parsed = PWCS2Config.model_validate(config)
        super().__init__(parsed, bus)
        self.config: PWCS2Config = parsed
        self.companion = GroupCompanion(
            enabled=parsed.ai_companion_enabled,
            groups=parsed.ai_companion_groups + parsed.ai_chat_only_groups,
            proactive=parsed.ai_companion_proactive_enabled,
            min_interval=parsed.ai_companion_min_interval_seconds,
            hourly_limit=parsed.ai_companion_hourly_limit,
            daily_limit=parsed.ai_companion_daily_limit,
            state_path=Path(parsed.database_path).parent / 'runtime' / 'companion-rate.json',
        )
        self.native_voice = NativeVoice(
            lambda action, params, **kwargs: self._call_action(action, params, **kwargs),
            enabled=parsed.ai_voice_enabled,
            reply_enabled=parsed.ai_voice_reply_enabled,
            character=parsed.ai_voice_character,
        )
        self.transport_health = TransportHealthMonitor(
            path=Path(parsed.database_path).parent / "runtime" / "napcat-health.json",
            connection=lambda: self._ws,
            probe=lambda: self._call_action("get_status", {}, timeout=5),
        )
        self._transport_health_task: asyncio.Task[None] | None = None
        self.training_game_limiter = TrainingGameRateLimiter(
            group_cooldown_seconds=(
                parsed.training_game_group_cooldown_seconds
            ),
            user_cooldown_seconds=parsed.training_game_user_cooldown_seconds,
        )
        self.repository = BindingRepository(parsed.database_path)
        self.highlight_intent_repository = (
            BindingRepository(parsed.highlight_intent_database_path)
            if parsed.highlight_enabled
            else None
        )
        self.stream_live = StreamLiveMonitor(
            self.repository,
            LivePlatformClient(timeout_seconds=parsed.stream_live_timeout_seconds),
            self._send_stream_live_notification,
            targets=parsed.stream_live_targets,
            allowed_groups=parsed.allowed_groups,
            enabled=parsed.stream_live_enabled,
            poll_interval_seconds=parsed.stream_live_poll_interval_seconds,
            initial_delay_seconds=parsed.stream_live_initial_delay_seconds,
            confirmation_polls=parsed.stream_live_confirmation_polls,
        )
        self.perfectworld = PerfectWorldClient(
            parsed.pw_token,
            app_version=parsed.pw_app_version,
            base_url=parsed.pw_base_url,
            my_steam_id=parsed.pw_my_steam_id,
            timeout_seconds=parsed.request_timeout_seconds,
            cache_ttl_seconds=parsed.cache_ttl_seconds,
        )
        self.watch_stage = WatchStageClient(
            enabled=parsed.watch_stage_enabled,
            base_url=parsed.watch_stage_base_url,
            timeout_seconds=parsed.watch_stage_timeout_seconds,
        )
        teamspeak: TeamSpeakStatusClient | None = None
        if parsed.ts_enabled:
            try:
                ts_query_password = Path(parsed.ts_query_password_file).read_text(
                    encoding="utf-8"
                ).strip()
                teamspeak = TeamSpeakStatusClient(
                    host=parsed.ts_query_host,
                    port=parsed.ts_query_port,
                    username=parsed.ts_query_username,
                    password=ts_query_password,
                    timeout_seconds=parsed.ts_query_timeout_seconds,
                    cache_ttl_seconds=parsed.ts_cache_ttl_seconds,
                )
            except (OSError, ValueError) as exc:
                logger.warning("pwcs2: TeamSpeak status query disabled: {}", exc)

        skin_price_client = SkinPriceClient(
            price_url=parsed.skin_price_data_url,
            exchange_url=parsed.skin_price_exchange_url,
            skins_en_url=parsed.skin_price_skins_en_url,
            skins_zh_url=parsed.skin_price_skins_zh_url,
            timeout_seconds=parsed.skin_price_request_timeout_seconds,
            cache_ttl_seconds=parsed.skin_price_cache_ttl_seconds,
            localization_cache_ttl_seconds=(
                parsed.skin_price_localization_cache_ttl_seconds
            ),
        )
        self.skin_prices = DailySkinPriceService(
            self.repository,
            skin_price_client,
            self._send_skin_price_report,
            enabled=parsed.skin_price_enabled,
            send_time=parsed.skin_price_time,
            timezone_name=parsed.skin_price_timezone,
            poll_interval_seconds=parsed.skin_price_poll_interval_seconds,
            initial_delay_seconds=parsed.skin_price_initial_delay_seconds,
            max_items_per_group=parsed.skin_price_max_items_per_group,
            retention_days=parsed.skin_price_retention_days,
            allowed_groups=parsed.allowed_groups,
        )
        self.party = PartyService(
            self.repository,
            teamspeak_address=parsed.ts_public_address,
            expiration_hours=parsed.party_expiration_hours,
        )
        self.community_repository = CommunityRepository(
            parsed.database_path,
            initial_balance=parsed.community_initial_balance,
        )
        self.group_play = GroupPlayService(
            self.community_repository,
            self.repository,
            prediction_enabled=parsed.prediction_enabled,
            prediction_duration_minutes=parsed.prediction_duration_minutes,
            manager_qq_ids=parsed.community_manager_qq_ids,
        )
        self.group_ai_memory = GroupAIMemoryService(
            self.repository,
            enabled=parsed.ai_group_memory_enabled,
            max_entries=parsed.ai_group_memory_max_entries,
            max_entries_per_member=parsed.ai_group_memory_max_entries_per_member,
            max_content_chars=parsed.ai_group_memory_max_content_chars,
            max_context_chars=parsed.ai_group_memory_max_context_chars,
            manager_qq_ids=parsed.community_manager_qq_ids,
        )
        self.steam_presence = SteamPresenceClient(
            parsed.steam_web_api_key,
            base_url=parsed.steam_presence_base_url,
            proxy_url=parsed.steam_presence_proxy_url,
            timeout_seconds=parsed.steam_presence_timeout_seconds,
            cache_ttl_seconds=parsed.steam_presence_cache_ttl_seconds,
            stale_ttl_seconds=parsed.steam_presence_stale_ttl_seconds,
            failure_cooldown_seconds=parsed.steam_presence_failure_cooldown_seconds,
        )
        self.valorant = (
            ValorantClient(
                query_url=parsed.valorant_query_url,
                timeout_seconds=parsed.valorant_timeout_seconds,
                queue_timeout_seconds=parsed.valorant_queue_timeout_seconds,
                failure_threshold=parsed.valorant_failure_threshold,
                failure_cooldown_seconds=parsed.valorant_failure_cooldown_seconds,
            )
            if parsed.valorant_enabled
            else None
        )
        self.auto_prediction = AutoPredictionService(
            self.community_repository,
            self.repository,
            self.perfectworld,
            self.steam_presence,
            self._send_auto_prediction,
            enabled=parsed.prediction_enabled and parsed.auto_prediction_enabled,
            duration_minutes=parsed.prediction_duration_minutes,
            expiry_hours=parsed.auto_prediction_expiry_hours,
            poll_interval_seconds=parsed.auto_prediction_poll_interval_seconds,
            initial_delay_seconds=parsed.auto_prediction_initial_delay_seconds,
            allowed_groups=parsed.allowed_groups,
            watch_stage=self.watch_stage,
            watch_stage_concurrency=parsed.auto_prediction_watch_concurrency,
            max_total_score=parsed.auto_prediction_max_total_score,
        )
        self.commands = CommandService(
            self.repository,
            self.perfectworld,
            prefix=parsed.command_prefix,
            query_cooldown_seconds=parsed.query_cooldown_seconds,
            teamspeak=teamspeak,
            teamspeak_address=parsed.ts_public_address,
            skin_prices=self.skin_prices,
            party=self.party,
            group_play=self.group_play,
            watch_stage=self.watch_stage,
            steam_presence=self.steam_presence,
            valorant=self.valorant,
            group_ai_memory=self.group_ai_memory,
            valorant_cache_ttl_seconds=parsed.valorant_cache_ttl_seconds,
            valorant_stale_ttl_seconds=parsed.valorant_stale_ttl_seconds,
        )
        self.stats_renderer = StatsCardRenderer(
            enabled=parsed.stats_image_enabled,
            timeout_seconds=parsed.stats_image_timeout_seconds,
        )
        self.ai_group_data = AIGroupDataService(self.commands)
        self.ai = AIChatService(
            parsed.ai_api_key,
            enabled=parsed.ai_enabled,
            base_url=parsed.ai_base_url,
            proxy_url=parsed.ai_proxy_url or os.environ.get("USTC_LLM_PROXY_URL", ""),
            model=parsed.ai_model,
            vision_model=parsed.ai_vision_model,
            vision_max_tokens=parsed.ai_vision_max_tokens,
            timeout_seconds=parsed.ai_timeout_seconds,
            temperature=parsed.ai_temperature,
            max_tokens=parsed.ai_max_tokens,
            cooldown_seconds=parsed.ai_cooldown_seconds,
            max_history_messages=parsed.ai_max_history_messages,
            max_input_chars=parsed.ai_max_input_chars,
            max_concurrent_requests=parsed.ai_max_concurrent_requests,
            allow_concurrent_same_chat=parsed.ai_allow_concurrent_same_chat,
            system_prompt=(
                (DEFAULT_CHAT_SYSTEM_PROMPT
                 + build_training_game_system_prompt(parsed.training_game_url)
                 if parsed.training_game_enabled else DEFAULT_CHAT_SYSTEM_PROMPT)
                + (COMPANION_SYSTEM_PROMPT if parsed.ai_companion_enabled else "")
                + (
                    (
                        "\n已启用QQ原生语音转文字；只有QQ提供可用音色时才能语音回复，"
                        "不能保证当前有可用音色。"
                        if parsed.ai_voice_reply_enabled
                        else "\n已启用QQ原生语音转文字，目前只用文字回答，不声称能发送语音。"
                    )
                    if parsed.ai_voice_enabled else ""
                )
            ),
        )
        self.match_commentary = MatchCommentaryService(
            self.repository,
            self.ai,
            enabled=parsed.match_ai_commentary_enabled,
            max_tokens=parsed.match_ai_commentary_max_tokens,
        )
        self.member_memory = MemberMemoryService(
            self.repository,
            self.perfectworld,
            enabled=parsed.ai_member_memory_enabled,
            refresh_interval_seconds=parsed.ai_member_memory_refresh_interval_seconds,
            initial_delay_seconds=parsed.ai_member_memory_initial_delay_seconds,
            max_context_members=parsed.ai_member_memory_max_members,
            max_context_chars=parsed.ai_member_memory_max_context_chars,
            allowed_groups=parsed.allowed_groups,
        )
        self.match_notifier = MatchNotifier(
            self.repository,
            self.perfectworld,
            self._send_match_notification,
            poll_interval_seconds=parsed.match_poll_interval_seconds,
            initial_delay_seconds=parsed.match_poll_initial_delay_seconds,
            page_size=parsed.match_poll_page_size,
            max_notifications_per_poll=parsed.match_notifications_per_poll,
            allowed_groups=parsed.allowed_groups,
            observe_matches=self.auto_prediction.observe_matches,
        )
        self.weekly_chemistry = WeeklyChemistryService(
            self.repository,
            self.perfectworld,
            self._send_weekly_chemistry,
            enabled=parsed.weekly_chemistry_enabled,
            send_time=parsed.weekly_chemistry_time,
            weekday=parsed.weekly_chemistry_weekday,
            timezone_name=parsed.weekly_chemistry_timezone,
            poll_interval_seconds=parsed.weekly_chemistry_poll_interval_seconds,
            initial_delay_seconds=parsed.weekly_chemistry_initial_delay_seconds,
            match_page_size=parsed.weekly_chemistry_match_page_size,
            allowed_groups=parsed.allowed_groups,
        )
        self.grenade_quiz = DailyGrenadeQuizService(
            self.community_repository,
            self.repository,
            self._send_grenade_question,
            self._send_grenade_reveal,
            enabled=parsed.grenade_quiz_enabled,
            question_time=parsed.grenade_quiz_question_time,
            reveal_time=parsed.grenade_quiz_reveal_time,
            timezone_name=parsed.grenade_quiz_timezone,
            poll_interval_seconds=parsed.grenade_quiz_poll_interval_seconds,
            initial_delay_seconds=parsed.grenade_quiz_initial_delay_seconds,
            correct_reward=parsed.grenade_quiz_correct_reward,
            fastest_bonus=parsed.grenade_quiz_fastest_bonus,
            allowed_groups=parsed.allowed_groups,
        )
        self.weekly_report = WeeklyReportService(
            self.community_repository,
            self.repository,
            self.perfectworld,
            self._send_weekly_report,
            enabled=parsed.weekly_report_enabled,
            send_time=parsed.weekly_report_time,
            weekday=parsed.weekly_report_weekday,
            timezone_name=parsed.weekly_report_timezone,
            poll_interval_seconds=parsed.weekly_report_poll_interval_seconds,
            initial_delay_seconds=parsed.weekly_report_initial_delay_seconds,
            match_page_size=parsed.weekly_report_match_page_size,
            allowed_groups=parsed.allowed_groups,
        )
        self.daily_summary = DailySummaryService(
            self.repository,
            self.perfectworld,
            self.ai,
            self._send_daily_summary,
            enabled=parsed.daily_summary_enabled,
            send_time=parsed.daily_summary_time,
            timezone_name=parsed.daily_summary_timezone,
            poll_interval_seconds=parsed.daily_summary_poll_interval_seconds,
            initial_delay_seconds=parsed.daily_summary_initial_delay_seconds,
            match_page_size=parsed.daily_summary_match_page_size,
            allowed_groups=parsed.allowed_groups,
        )
        self.daily_admin = DailyAdminService(
            self.repository,
            self.daily_summary,
            self._fetch_group_members,
            self._apply_daily_admin,
            self._send_daily_admin,
            lambda: str(self._self_id or ""),
            enabled=parsed.daily_admin_enabled,
            run_time=parsed.daily_admin_time,
            timezone_name=parsed.daily_admin_timezone,
            poll_interval_seconds=parsed.daily_admin_poll_interval_seconds,
            initial_delay_seconds=parsed.daily_admin_initial_delay_seconds,
            allowed_groups=parsed.allowed_groups,
        )
        self.daily_word_cloud = DailyWordCloudService(
            self.repository,
            self._send_daily_word_cloud,
            enabled=parsed.daily_word_cloud_enabled,
            send_time=parsed.daily_word_cloud_time,
            timezone_name=parsed.daily_word_cloud_timezone,
            poll_interval_seconds=parsed.daily_word_cloud_poll_interval_seconds,
            initial_delay_seconds=parsed.daily_word_cloud_initial_delay_seconds,
            max_words=parsed.daily_word_cloud_max_words,
            retention_days=parsed.daily_word_cloud_retention_days,
            allowed_groups=parsed.allowed_groups,
        )
        self.membership_reconciler = MembershipReconciler(
            self.repository,
            self._fetch_group_member_ids,
            self.community_repository.remove_member,
            interval_seconds=parsed.membership_sync_interval_seconds,
            initial_delay_seconds=parsed.membership_sync_initial_delay_seconds,
            allowed_groups=parsed.allowed_groups,
        )
        self.group_level_reminder = GroupLevelReminderService(
            self.repository,
            self._fetch_group_members,
            self._send_group_level_reminder,
            self._remove_noncompliant_members,
            enabled=parsed.group_level_reminder_enabled,
            send_time=parsed.group_level_reminder_time,
            timezone_name=parsed.group_level_reminder_timezone,
            target_level=parsed.group_level_reminder_target_level,
            include_unbound=parsed.group_level_reminder_include_unbound,
            removal_grace_days=parsed.group_level_removal_grace_days,
            removal_max_per_day=parsed.group_level_removal_max_per_day,
            poll_interval_seconds=parsed.group_level_reminder_poll_interval_seconds,
            initial_delay_seconds=parsed.group_level_reminder_initial_delay_seconds,
            allowed_groups=parsed.allowed_groups,
        )
        self.content_relay = ContentRelayService(
            self.repository,
            self._send_content_relay,
            enabled=parsed.content_relay_enabled,
            source_groups=parsed.content_relay_source_groups,
            target_group=parsed.content_relay_target_group,
            daily_limit=parsed.content_relay_daily_limit,
            cooldown_seconds=parsed.content_relay_cooldown_seconds,
            priority_source_groups=parsed.content_relay_priority_source_groups,
            priority_cooldown_seconds=parsed.content_relay_priority_cooldown_seconds,
            secondary_daily_limit=parsed.content_relay_secondary_daily_limit,
            timezone_name=parsed.content_relay_timezone,
            max_text_chars=parsed.content_relay_max_text_chars,
            max_media=parsed.content_relay_max_media,
            blocked_keywords=parsed.content_relay_blocked_keywords,
        )
        self.unbound_guard = UnboundMessageGuard(
            self.repository,
            daily_limit=parsed.unbound_daily_message_limit,
            mute_duration_seconds=parsed.unbound_mute_duration_seconds,
            timezone_name=parsed.unbound_guard_timezone,
        )
        self.group_card_reconciler = GroupCardLockReconciler(
            self.repository,
            self._fetch_group_members,
            self._restore_locked_group_card,
            lambda: str(self._self_id or ""),
            interval_seconds=parsed.group_card_lock_reconcile_interval_seconds,
            initial_delay_seconds=parsed.group_card_lock_reconcile_initial_delay_seconds,
            allowed_groups=parsed.allowed_groups,
        )
        self.highlight_store = (
            HighlightJobStore(
                parsed.highlight_data_path,
                max_video_bytes=parsed.highlight_inline_video_max_bytes,
                shared_root_gid=(
                    parsed.highlight_shared_output_gid
                    if parsed.highlight_shared_output_gid >= 0
                    else None
                ),
                shared_output_gid=(
                    parsed.highlight_shared_output_gid
                    if parsed.highlight_shared_output_gid >= 0
                    else None
                ),
            )
            if parsed.highlight_enabled
            else None
        )
        self.highlight_delivery = (
            HighlightVideoDelivery(
                lambda action, params: self._call_action(
                    action,
                    params,
                    timeout=parsed.highlight_delivery_timeout_seconds,
                ),
                allowed_groups=set(parsed.allowed_groups),
                max_inline_bytes=parsed.highlight_inline_video_max_bytes,
                ready_check=lambda: self._ws is not None,
                napcat_media_root=parsed.highlight_napcat_media_root,
            )
            if parsed.highlight_enabled
            else None
        )
        self._match_notifier_task: asyncio.Task[None] | None = None
        self._daily_summary_task: asyncio.Task[None] | None = None
        self._daily_admin_task: asyncio.Task[None] | None = None
        self._membership_sync_task: asyncio.Task[None] | None = None
        self._group_level_reminder_task: asyncio.Task[None] | None = None
        self._member_memory_task: asyncio.Task[None] | None = None
        self._daily_word_cloud_task: asyncio.Task[None] | None = None
        self._skin_price_task: asyncio.Task[None] | None = None
        self._weekly_chemistry_task: asyncio.Task[None] | None = None
        self._grenade_quiz_task: asyncio.Task[None] | None = None
        self._weekly_report_task: asyncio.Task[None] | None = None
        self._auto_prediction_task: asyncio.Task[None] | None = None
        self._stream_live_task: asyncio.Task[None] | None = None
        self._group_card_reconcile_task: asyncio.Task[None] | None = None
        self._highlight_delivery_task: asyncio.Task[None] | None = None
        self._admin_ban_guard_lock = asyncio.Lock()
        self._group_card_operation_lock = asyncio.Lock()
        self._pending_group_card_restores: set[tuple[str, str]] = set()

    async def start(self) -> None:
        await self.start_background_services()
        try:
            await super().start()
        finally:
            await self.stop_background_services()

    async def start_background_services(self) -> None:
        if not self.config.prediction_enabled:
            cancelled, refunded = self.community_repository.cancel_all_open_predictions()
            if cancelled:
                logger.info(
                    "pwcs2: prediction feature disabled; cancelled {} open "
                    "prediction(s) and refunded {} bettor(s)",
                    cancelled,
                    refunded,
                )
        if self.config.match_notifications_enabled and self._match_notifier_task is None:
            self._match_notifier_task = asyncio.create_task(
                self.match_notifier.run(),
                name="pwcs2-match-notifier",
            )
        if self.highlight_store is not None and self._highlight_delivery_task is None:
            self._highlight_delivery_task = asyncio.create_task(
                self._run_highlight_delivery(),
                name="pwcs2-highlight-delivery",
            )
        if self.stream_live.enabled and self._stream_live_task is None:
            self._stream_live_task = asyncio.create_task(
                self.stream_live.run(),
                name="pwcs2-stream-live",
            )
        if (
            self.config.prediction_enabled
            and self.config.auto_prediction_enabled
            and self._auto_prediction_task is None
        ):
            self._auto_prediction_task = asyncio.create_task(
                self.auto_prediction.run(),
                name="pwcs2-auto-prediction",
            )
        if self.config.daily_summary_enabled and self._daily_summary_task is None:
            self._daily_summary_task = asyncio.create_task(
                self.daily_summary.run(),
                name="pwcs2-daily-summary",
            )
        if self.config.daily_admin_enabled and self._daily_admin_task is None:
            self._daily_admin_task = asyncio.create_task(
                self.daily_admin.run(),
                name="pwcs2-daily-admin",
            )
        if self.config.membership_sync_enabled and self._membership_sync_task is None:
            self._membership_sync_task = asyncio.create_task(
                self.membership_reconciler.run(),
                name="pwcs2-membership-sync",
            )
        if (
            self.config.group_card_lock_enabled
            and self._group_card_reconcile_task is None
        ):
            self._group_card_reconcile_task = asyncio.create_task(
                self.group_card_reconciler.run(),
                name="pwcs2-group-card-reconcile",
            )
        if (
            self.config.group_level_reminder_enabled
            and self._group_level_reminder_task is None
        ):
            self._group_level_reminder_task = asyncio.create_task(
                self.group_level_reminder.run(),
                name="pwcs2-group-level-reminder",
            )
        if self.config.ai_member_memory_enabled and self._member_memory_task is None:
            self._member_memory_task = asyncio.create_task(
                self.member_memory.run(),
                name="pwcs2-member-memory",
            )
        if self.config.daily_word_cloud_enabled and self._daily_word_cloud_task is None:
            self._daily_word_cloud_task = asyncio.create_task(
                self.daily_word_cloud.run(),
                name="pwcs2-daily-word-cloud",
            )
        if self.config.skin_price_enabled and self._skin_price_task is None:
            self._skin_price_task = asyncio.create_task(
                self.skin_prices.run(),
                name="pwcs2-daily-skin-prices",
            )
        if self.config.weekly_chemistry_enabled and self._weekly_chemistry_task is None:
            self._weekly_chemistry_task = asyncio.create_task(
                self.weekly_chemistry.run(),
                name="pwcs2-weekly-chemistry",
            )
        if self.config.grenade_quiz_enabled and self._grenade_quiz_task is None:
            self._grenade_quiz_task = asyncio.create_task(
                self.grenade_quiz.run(),
                name="pwcs2-daily-grenade-quiz",
            )
        if self.config.weekly_report_enabled and self._weekly_report_task is None:
            self._weekly_report_task = asyncio.create_task(
                self.weekly_report.run(),
                name="pwcs2-weekly-report",
            )
        if self._transport_health_task is None:
            self._transport_health_task = asyncio.create_task(
                self.transport_health.run(), name="pwcs2-transport-health"
            )

    async def stop_background_services(self) -> None:
        await self._stop_transport_health()
        await self._stop_match_notifier()
        await self._stop_daily_summary()
        await self._stop_daily_admin()
        await self._stop_membership_sync()
        await self._stop_group_card_reconciler()
        await self._stop_group_level_reminder()
        await self._stop_member_memory()
        await self._stop_daily_word_cloud()
        await self._stop_skin_prices()
        await self._stop_weekly_chemistry()
        await self._stop_grenade_quiz()
        await self._stop_weekly_report()
        await self._stop_auto_prediction()
        await self._stop_stream_live()
        await self._stop_highlight_delivery()

    async def stop(self) -> None:
        await self.stop_background_services()
        await self.stats_renderer.close()
        await self.skin_prices.close()
        await self.ai.close()
        await self.steam_presence.close()
        if self.valorant is not None:
            await self.valorant.close()
        await self.watch_stage.close()
        await self.stream_live.close()
        await self.perfectworld.close()
        await super().stop()

    async def _stop_transport_health(self) -> None:
        task = self._transport_health_task
        if task is not None:
            self._transport_health_task = None
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _stop_match_notifier(self) -> None:
        self.match_notifier.stop()
        task = self._match_notifier_task
        if task is None:
            return
        self._match_notifier_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_auto_prediction(self) -> None:
        self.auto_prediction.stop()
        task = self._auto_prediction_task
        if task is None:
            return
        self._auto_prediction_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_stream_live(self) -> None:
        self.stream_live.stop()
        task = self._stream_live_task
        if task is None:
            return
        self._stream_live_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_highlight_delivery(self) -> None:
        task = self._highlight_delivery_task
        if task is None:
            return
        self._highlight_delivery_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_daily_summary(self) -> None:
        self.daily_summary.stop()
        task = self._daily_summary_task
        if task is None:
            return
        self._daily_summary_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_daily_admin(self) -> None:
        self.daily_admin.stop()
        task = self._daily_admin_task
        if task is None:
            return
        self._daily_admin_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_membership_sync(self) -> None:
        self.membership_reconciler.stop()
        task = self._membership_sync_task
        if task is None:
            return
        self._membership_sync_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_group_card_reconciler(self) -> None:
        self.group_card_reconciler.stop()
        task = self._group_card_reconcile_task
        if task is None:
            return
        self._group_card_reconcile_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_group_level_reminder(self) -> None:
        self.group_level_reminder.stop()
        task = self._group_level_reminder_task
        if task is None:
            return
        self._group_level_reminder_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_member_memory(self) -> None:
        self.member_memory.stop()
        task = self._member_memory_task
        if task is None:
            return
        self._member_memory_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_daily_word_cloud(self) -> None:
        self.daily_word_cloud.stop()
        task = self._daily_word_cloud_task
        if task is None:
            return
        self._daily_word_cloud_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_skin_prices(self) -> None:
        self.skin_prices.stop()
        task = self._skin_price_task
        if task is None:
            return
        self._skin_price_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_weekly_chemistry(self) -> None:
        self.weekly_chemistry.stop()
        task = self._weekly_chemistry_task
        if task is None:
            return
        self._weekly_chemistry_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_grenade_quiz(self) -> None:
        self.grenade_quiz.stop()
        task = self._grenade_quiz_task
        if task is None:
            return
        self._grenade_quiz_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _stop_weekly_report(self) -> None:
        self.weekly_report.stop()
        task = self._weekly_report_task
        if task is None:
            return
        self._weekly_report_task = None
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _on_notice(self, event: dict[str, Any]) -> None:
        if (
            event.get("notice_type") == "group_ban"
            and self.config.admin_ban_guard_enabled
        ):
            await self._enforce_admin_ban_policy(event)
            return
        if (
            event.get("notice_type") == "group_card"
            and self.config.group_card_lock_enabled
        ):
            await self._enforce_group_card_lock(event)
            return
        if event.get("notice_type") == "group_decrease":
            group_id = str(event.get("group_id") or "")
            user_id = str(event.get("user_id") or "")
            if not group_id or not user_id or user_id == str(self._self_id or ""):
                return
            if self.config.allowed_groups and group_id not in self.config.allowed_groups:
                return
            self.member_memory.forget_member(group_id, user_id)
            self.repository.clear_unbound_message_usage(group_id, user_id)
            self.repository.clear_group_member_compliance(group_id, user_id)
            self.repository.clear_match_commentary_preference(group_id, user_id)
            self.repository.clear_group_card_lock(group_id, user_id)
            self.community_repository.remove_member(group_id, user_id)
            self.repository.remove_member_from_all_parties(group_id, user_id)
            steam_removed = self.repository.unbind(group_id, user_id)
            valorant_removed = self.repository.unbind_valorant(group_id, user_id)
            if steam_removed or valorant_removed:
                logger.info(
                    "pwcs2: removed bindings after member left group={} qq={} "
                    "steam={} valorant={}",
                    group_id,
                    user_id,
                    steam_removed,
                    valorant_removed,
                )
            return
        await super()._on_notice(event)

    async def _enforce_admin_ban_policy(self, event: dict[str, Any]) -> None:
        """Undo administrator mutes and revoke the role for overlong punishments."""
        if str(event.get("sub_type") or "").casefold() != "ban":
            return
        group_id = str(event.get("group_id") or "")
        operator_qq_id = str(event.get("operator_id") or "")
        target_qq_id = str(event.get("user_id") or "")
        try:
            duration_seconds = int(event.get("duration") or 0)
        except (TypeError, ValueError):
            return
        bot_id = str(self._self_id or "")
        if (
            not group_id.isdecimal()
            or not operator_qq_id.isdecimal()
            or not target_qq_id.isdecimal()
            or not bot_id.isdecimal()
            or duration_seconds <= 0
            or operator_qq_id == bot_id
            or operator_qq_id == target_qq_id
            or (self.config.allowed_groups and group_id not in self.config.allowed_groups)
        ):
            return

        async with self._admin_ban_guard_lock:
            try:
                bot_response = await self._call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(bot_id),
                        "no_cache": True,
                    },
                )
                operator_response = await self._call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(operator_qq_id),
                        "no_cache": True,
                    },
                )
                bot = bot_response.get("data")
                operator = operator_response.get("data")
                if not isinstance(bot, dict) or not isinstance(operator, dict):
                    raise RuntimeError("NapCat returned invalid member data")
                if str(bot.get("user_id") or "") != bot_id:
                    raise RuntimeError("NapCat returned mismatched bot data")
                if str(operator.get("user_id") or "") != operator_qq_id:
                    raise RuntimeError("NapCat returned mismatched operator data")
                if str(bot.get("role") or "").strip().casefold() != "owner":
                    logger.warning(
                        "pwcs2: cannot enforce admin ban policy because bot is not owner "
                        "group={}",
                        group_id,
                    )
                    return
                if str(operator.get("role") or "").strip().casefold() != "admin":
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: failed to verify admin ban notice group={} operator={}: {}",
                    group_id,
                    operator_qq_id,
                    exc,
                )
                return

            unmuted = False
            admin_revoked = False
            try:
                await self._call_action(
                    "set_group_ban",
                    {
                        "group_id": int(group_id),
                        "user_id": int(target_qq_id),
                        "duration": 0,
                    },
                )
                unmuted = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "pwcs2: failed to undo admin mute group={} operator={} target={}: {}",
                    group_id,
                    operator_qq_id,
                    target_qq_id,
                    exc,
                )
            if duration_seconds > self.config.admin_ban_revoke_threshold_seconds:
                try:
                    await self._call_action(
                        "set_group_admin",
                        {
                            "group_id": int(group_id),
                            "user_id": int(operator_qq_id),
                            "enable": False,
                        },
                    )
                    admin_revoked = True
                    self.repository.clear_daily_admin_if(group_id, operator_qq_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "pwcs2: failed to revoke abusive admin group={} operator={}: {}",
                        group_id,
                        operator_qq_id,
                        exc,
                    )
            self.repository.record_admin_ban_guard_event(
                group_id,
                operator_qq_id,
                target_qq_id,
                duration_seconds,
                unmuted=unmuted,
                admin_revoked=admin_revoked,
            )

            minutes = duration_seconds / 60
            threshold_minutes = self.config.admin_ban_revoke_threshold_seconds / 60
            if not unmuted:
                content = (
                    f"管理员 QQ {operator_qq_id} 对 QQ {target_qq_id} 发起禁言，"
                    "机器人解除失败，已记录告警，请群主立即检查。"
                )
                if admin_revoked:
                    content += "其管理员身份已撤销。"
            elif duration_seconds > self.config.admin_ban_revoke_threshold_seconds:
                if admin_revoked:
                    content = (
                        f"检测到管理员 QQ {operator_qq_id} 禁言 QQ {target_qq_id} "
                        f"{minutes:g} 分钟，已立即解除；因原时长超过 "
                        f"{threshold_minutes:g} 分钟，"
                        "该管理员身份也已撤销。"
                    )
                else:
                    content = (
                        f"已解除管理员 QQ {operator_qq_id} 对 QQ {target_qq_id} 的禁言，"
                        "但自动撤销管理员身份失败，已记录告警，请群主立即检查。"
                    )
            else:
                content = (
                    f"检测到管理员 QQ {operator_qq_id} 禁言 QQ {target_qq_id} "
                    f"{minutes:g} 分钟，已立即解除。管理员不得禁言群成员；"
                    f"原时长超过 {threshold_minutes:g} 分钟将同时撤销管理员身份。"
                )
            try:
                await self._reply("group", group_id, content)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: failed to announce admin ban policy group={}: {}",
                    group_id,
                    exc,
                )

    async def _fetch_group_member_ids(self, group_id: str) -> set[str]:
        data = await self._fetch_group_members(group_id)
        member_ids = {
            str(item.get("user_id"))
            for item in data
            if item.get("user_id") is not None
        }
        if not member_ids:
            raise RuntimeError("NapCat returned an empty group member list")
        return member_ids

    async def _fetch_group_members(self, group_id: str) -> list[dict[str, Any]]:
        response = await self._call_action(
            "get_group_member_list",
            {"group_id": int(group_id), "no_cache": True},
        )
        data = response.get("data")
        if not isinstance(data, list):
            raise RuntimeError("NapCat returned an invalid group member list")
        members = [item for item in data if isinstance(item, dict)]
        if not members:
            raise RuntimeError("NapCat returned an empty group member list")
        if self.config.group_card_lock_enabled:
            bot_id = str(self._self_id or "")
            cards: dict[str, str] = {}
            for item in members:
                qq_id = str(item.get("user_id") or "")
                role = str(item.get("role") or "").strip().casefold()
                if (
                    not qq_id.isdecimal()
                    or qq_id == bot_id
                    or role not in {"member", "admin"}
                    or _parse_onebot_boolean(item.get("is_robot", False))
                ):
                    continue
                cards[qq_id] = str(item.get("card") or item.get("nickname") or qq_id)
            self.repository.ensure_group_card_locks(group_id, cards)
        self.member_memory.remember_member_list(group_id, members)
        return members

    async def _handle_group_card_rename(
        self,
        group_id: str,
        sender_qq_id: str,
        sender_role: str,
        argument: str,
    ) -> None:
        # Message-event roles can lag immediately after the daily-admin
        # rotation. Treat the event value as diagnostics only; authorization
        # below always uses fresh NapCat member data.
        event_sender_role = sender_role.strip().casefold()
        target_token, separator, raw_card = argument.strip().partition(" ")
        target_qq_id = target_token.removeprefix("@").strip()
        card = " ".join(raw_card.split())
        if (
            not separator
            or not target_token.startswith("@")
            or not target_qq_id.isdecimal()
            or not card
        ):
            await self._reply("group", group_id, "格式：#改名 @群友 新名字")
            return
        if target_qq_id == sender_qq_id:
            await self._reply("group", group_id, "管理员只能修改别人，不能用该命令修改自己")
            return
        if len(card) > 20 or len(card.encode("utf-8")) > 60:
            await self._reply("group", group_id, "新名字过长，请控制在 20 个字符以内")
            return
        if any(ord(character) < 32 for character in card):
            await self._reply("group", group_id, "新名字不能包含控制字符")
            return

        try:
            members = await self._fetch_group_members(group_id)
            by_id = {
                str(item.get("user_id")): item
                for item in members
                if item.get("user_id") is not None
            }
            sender = by_id.get(sender_qq_id)
            target = by_id.get(target_qq_id)
            live_sender_role = str((sender or {}).get("role") or "").casefold()
            if live_sender_role not in {"owner", "admin"}:
                logger.info(
                    "pwcs2: rejected group-card rename group={} sender={} "
                    "event_role={} live_role={}",
                    group_id,
                    sender_qq_id,
                    event_sender_role,
                    live_sender_role,
                )
                raise PermissionError("发起者已不再是群管理员")
            if target is None:
                raise ValueError("目标成员已不在群内")
            target_role = str(target.get("role") or "").strip().casefold()
            if target_role != "member":
                raise PermissionError("不能修改群主或其他管理员的群名片")
            if _parse_onebot_boolean(target.get("is_robot", False)):
                raise PermissionError("不能修改机器人的群名片")

            async with self._group_card_operation_lock:
                sender_response = await self._call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(sender_qq_id),
                        "no_cache": True,
                    },
                )
                target_response = await self._call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(target_qq_id),
                        "no_cache": True,
                    },
                )
                fresh_sender = sender_response.get("data")
                fresh_target = target_response.get("data")
                if (
                    not isinstance(fresh_sender, dict)
                    or str(fresh_sender.get("user_id") or "") != sender_qq_id
                    or str(fresh_sender.get("role") or "").strip().casefold()
                    not in {"owner", "admin"}
                ):
                    raise PermissionError("发起者已不再是群管理员")
                if (
                    not isinstance(fresh_target, dict)
                    or str(fresh_target.get("user_id") or "") != target_qq_id
                ):
                    raise ValueError("目标成员已不在群内")
                fresh_target_role = (
                    str(fresh_target.get("role") or "").strip().casefold()
                )
                if fresh_target_role != "member":
                    raise PermissionError("不能修改群主或其他管理员的群名片")
                if _parse_onebot_boolean(fresh_target.get("is_robot", False)):
                    raise PermissionError("不能修改机器人的群名片")

                previous_lock = self.repository.get_group_card_lock(
                    group_id, target_qq_id
                )
                self.repository.set_group_card_lock(
                    group_id,
                    target_qq_id,
                    card,
                    updated_by_qq=sender_qq_id,
                )
                try:
                    await self._call_action(
                        "set_group_card",
                        {
                            "group_id": int(group_id),
                            "user_id": int(target_qq_id),
                            "card": card,
                        },
                    )
                except Exception:
                    if previous_lock is None:
                        self.repository.clear_group_card_lock(group_id, target_qq_id)
                    else:
                        self.repository.set_group_card_lock(
                            group_id,
                            target_qq_id,
                            previous_lock,
                            updated_by_qq="rollback",
                        )
                    raise
            self.member_memory.remember_identity(group_id, target_qq_id, card)
        except (PermissionError, ValueError) as exc:
            await self._reply("group", group_id, str(exc))
            return
        except Exception as exc:
            logger.warning(
                "pwcs2: failed to rename group card group={} target={}: {}",
                group_id,
                target_qq_id,
                exc,
            )
            await self._reply("group", group_id, "群名片修改失败，请稍后重试")
            return
        await self._reply(
            "group",
            group_id,
            f"改名成功：QQ {target_qq_id} → {card}（已锁定，成员自行修改会自动恢复）",
        )

    async def _restore_locked_group_card(
        self,
        group_id: str,
        target_qq_id: str,
        *,
        baseline_card: str | None = None,
    ) -> bool:
        """Safely restore one lock after rechecking persistent and live state."""
        if (
            not group_id.isdecimal()
            or not target_qq_id.isdecimal()
            or target_qq_id == str(self._self_id or "")
            or (self.config.allowed_groups and group_id not in self.config.allowed_groups)
        ):
            return False

        async with self._group_card_operation_lock:
            desired = self.repository.get_group_card_lock(group_id, target_qq_id)
            if desired is None:
                if baseline_card is None:
                    logger.warning(
                        "pwcs2: cannot restore untracked group card without baseline "
                        "group={} qq={}",
                        group_id,
                        target_qq_id,
                    )
                    return False
                desired = baseline_card

            response = await self._call_action(
                "get_group_member_info",
                {
                    "group_id": int(group_id),
                    "user_id": int(target_qq_id),
                    "no_cache": True,
                },
            )
            member = response.get("data")
            if not isinstance(member, dict):
                raise RuntimeError("NapCat returned invalid member data")
            if str(member.get("user_id") or "") != target_qq_id:
                raise RuntimeError("NapCat returned mismatched member data")
            role = str(member.get("role") or "").strip().casefold()
            if role == "owner" or _parse_onebot_boolean(member.get("is_robot", False)):
                return False
            if role not in {"member", "admin"}:
                raise RuntimeError(f"NapCat returned unknown member role: {role!r}")

            # An empty OneBot card means QQ is displaying the nickname. Store
            # that visible name instead of an empty lock, otherwise every poll
            # would try to clear the card again forever.
            if desired == "":
                desired = str(member.get("nickname") or target_qq_id)

            # Never persist a first-seen event baseline until the authoritative
            # lookup proves the target is a lockable human member.
            latest_desired = self.repository.get_group_card_lock(group_id, target_qq_id)
            if latest_desired is None:
                self.repository.set_group_card_lock(
                    group_id,
                    target_qq_id,
                    desired,
                    updated_by_qq="baseline",
                )
            elif latest_desired == "":
                self.repository.set_group_card_lock(
                    group_id,
                    target_qq_id,
                    desired,
                    updated_by_qq="baseline",
                )
            else:
                desired = latest_desired

            current = str(member.get("card") or member.get("nickname") or target_qq_id)
            if current == desired:
                return False
            await self._call_action(
                "set_group_card",
                {
                    "group_id": int(group_id),
                    "user_id": int(target_qq_id),
                    "card": desired,
                },
            )
            self.member_memory.remember_identity(group_id, target_qq_id, desired)
            logger.info(
                "pwcs2: restored locked group card group={} qq={}",
                group_id,
                target_qq_id,
            )
            return True

    async def _enforce_group_card_lock(self, event: dict[str, Any]) -> None:
        group_id = str(event.get("group_id") or "")
        target_qq_id = str(event.get("user_id") or "")
        if (
            not group_id.isdecimal()
            or not target_qq_id.isdecimal()
            or target_qq_id == str(self._self_id or "")
            or (self.config.allowed_groups and group_id not in self.config.allowed_groups)
            or "card_new" not in event
        ):
            return
        desired = self.repository.get_group_card_lock(group_id, target_qq_id)
        baseline_card = (
            str(event.get("card_old") or "") if "card_old" in event else None
        )
        expected = desired if desired is not None else baseline_card
        if expected is not None and str(event.get("card_new") or "") == expected:
            return
        try:
            await self._restore_locked_group_card(
                group_id,
                target_qq_id,
                baseline_card=baseline_card,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "pwcs2: failed to restore group card group={} qq={}: {}",
                group_id,
                target_qq_id,
                exc,
            )

    def _schedule_group_card_restore(self, group_id: str, target_qq_id: str) -> None:
        """Coalesce repeated messages from the same drifted member."""
        key = (group_id, target_qq_id)
        if key in self._pending_group_card_restores:
            return
        self._pending_group_card_restores.add(key)
        self._create_background_task(
            self._restore_group_card_from_message(group_id, target_qq_id),
            "group-card-message-check",
        )

    async def _restore_group_card_from_message(
        self, group_id: str, target_qq_id: str
    ) -> None:
        key = (group_id, target_qq_id)
        try:
            await self._restore_locked_group_card(group_id, target_qq_id)
        finally:
            self._pending_group_card_restores.discard(key)

    def _automatic_quiet_active(self) -> bool:
        until = self.config.automatic_quiet_until
        return until is not None and datetime.now(UTC) < until

    async def send(self, msg: OutboundMessage) -> None:
        if self._automatic_quiet_active() and not _EXPLICIT_REPLY.get():
            logger.info("pwcs2: automatic output suppressed during requested quiet period")
            return
        await super().send(msg)

    async def _on_message(self, event: dict[str, Any]) -> None:
        cutoff = self.config.message_replay_ignore_before
        stamp = event.get("time")
        if (
            cutoff is not None
            and isinstance(stamp, (int, float))
            and not isinstance(stamp, bool)
            and stamp <= cutoff.timestamp()
        ):
            return
        segments = self._normalize_segments(event.get("message"))
        text, _, mentioned_self, reply_to = self._parse_segments(segments)
        explicit = (
            event.get("message_type") == "private"
            or self.commands.parse(text) is not None
            or mentioned_self
            or (isinstance(reply_to, int) and reply_to in self._bot_outbound_ids)
            or text.strip().casefold().startswith(("#ai", "/ai"))
            or self.companion.wake_prompt(str(event.get("group_id") or ""), text) is not None
        )
        token = _EXPLICIT_REPLY.set(explicit)
        try:
            await self._on_message_impl(event)
        finally:
            _EXPLICIT_REPLY.reset(token)

    async def _on_message_impl(self, event: dict[str, Any]) -> None:
        message_type = event.get("message_type")
        user_id = event.get("user_id")
        if user_id is None or message_type not in {"group", "private"}:
            return
        if str(user_id) == str(self._self_id or ""):
            return

        if message_type == "private" and not self.config.private_replies_enabled:
            return

        if message_type == "group":
            source_group_id = str(event.get("group_id") or "")
            if self.content_relay.is_source_group(source_group_id):
                if str(user_id) != str(self._self_id or ""):
                    await self.content_relay.handle_message(
                        source_group_id,
                        self._normalize_segments(event.get("message")),
                    )
                return

        segments = self._normalize_segments(event.get("message"))
        text, _images, mentioned_self, reply_to = self._parse_segments(segments)
        command = self.commands.parse(text)

        message_id = event.get("message_id")
        if isinstance(message_id, int | str):
            if message_id in self._processed_ids:
                return
            self._processed_ids.append(message_id)

        if not self.is_allowed(str(user_id)):
            return

        group_id = ""
        nickname = ""
        sender_role = "member"
        if message_type == "group":
            group_id = str(event.get("group_id") or "")
            if not group_id:
                return
            if self.config.allowed_groups and group_id not in self.config.allowed_groups:
                return
            sender = event.get("sender") or {}
            nickname = str(sender.get("card") or sender.get("nickname") or "")
            sender_role = str(sender.get("role") or "member")
            if (
                self.config.group_card_lock_enabled
                and str(user_id) != str(self._self_id or "")
            ):
                desired_card = self.repository.get_group_card_lock(
                    group_id, str(user_id)
                )
                if desired_card is not None and nickname != desired_card:
                    # NapCat synthesizes some group_card notices only when a
                    # member next speaks. Use that same message as an immediate
                    # fallback signal; the shared lock and fresh lookup make
                    # duplicate notice/poll attempts harmless.
                    self._schedule_group_card_restore(group_id, str(user_id))
            self.member_memory.remember_identity(group_id, str(user_id), nickname)
            if command is None:
                self.companion.observe(group_id, str(user_id), nickname, text)
            if str(user_id) != str(self._self_id or ""):
                self.daily_admin.record_message(group_id, str(user_id))
            normalized_text = text.strip().casefold()
            if (
                str(user_id) != str(self._self_id or "")
                and command is None
                and not normalized_text.startswith(("#ai", "/ai"))
            ):
                self.daily_word_cloud.record_message(group_id, text)
            if self.config.unbound_guard_enabled and await self._guard_unbound_message(
                event,
                command=command,
                group_id=group_id,
                user_id=str(user_id),
            ):
                return
            if (
                self.config.xh_reaction_enabled
                and str(user_id) != str(self._self_id or "")
                and command is None
                and is_xh_reaction_trigger(text)
            ):
                image_path = xh_reaction_image_path()
                if image_path.is_file():
                    await self._reply(
                        "group",
                        group_id,
                        "",
                        media=[str(image_path)],
                    )
                    return
                logger.error("pwcs2: xh reaction image is missing: {}", image_path)
            if (
                self.config.training_game_enabled
                and str(user_id) != str(self._self_id or "")
                and command is None
            ):
                replying_to_bot = (
                    isinstance(reply_to, int) and reply_to in self._bot_outbound_ids
                )
                addressed_to_bot = (mentioned_self or replying_to_bot
                                    or self.companion.wake_prompt(group_id,text) is not None
                                    or text.strip().casefold().startswith(("#ai", "/ai")))
                if is_training_game_file_request(
                    text,
                    addressed_to_bot=addressed_to_bot,
                ):
                    if self.training_game_limiter.allow(
                        group_id,
                        str(user_id),
                        direct=True,
                    ):
                        await self._send_training_game_file(group_id)
                    return
                if is_training_game_trigger(text):
                    direct = is_training_game_direct_request(text)
                    if direct or not addressed_to_bot:
                        if self.training_game_limiter.allow(
                            group_id,
                            str(user_id),
                            direct=direct,
                        ):
                            await self._reply(
                                "group",
                                group_id,
                                format_training_game_intro(
                                    self.config.training_game_url
                                ),
                            )
                        return

        if command is None and not self.config.ai_enabled and self.config.forward_unmatched:
            if isinstance(message_id, int | str):
                with suppress(ValueError):
                    self._processed_ids.remove(message_id)
            await super()._on_message(event)
            return

        if command is None:
            if self.config.ai_enabled:
                await self._handle_ai_message(
                    event,
                    segments=segments,
                    text=text,
                    mentioned_self=mentioned_self,
                    reply_to=reply_to,
                )
            return

        if message_type != "group":
            await self._reply("private", str(user_id), "请在 QQ 群内使用绑定、数据和战绩命令")
            return

        if command.action == "card_rename":
            await self._handle_group_card_rename(
                group_id,
                str(user_id),
                sender_role,
                command.argument,
            )
            return

        history_source: HistorySource | None = None
        if command.action == "history_add":
            history_source = await self._resolve_history_source(group_id, reply_to)
        result = await self.commands.handle_result(
            command,
            group_id=group_id,
            qq_id=str(user_id),
            sender_role=sender_role,
            nickname=nickname,
            history_source=history_source,
            local_date=datetime.now(self.grenade_quiz.timezone).date().isoformat(),
            request_id=str(message_id) if isinstance(message_id, int | str) else "",
        )
        if result.stats is not None:
            self.member_memory.remember_stats_snapshot(group_id, result.stats)
        if result.match_history is not None:
            self.member_memory.remember_match_history(group_id, result.match_history)
        if result.ranking is not None:
            self.member_memory.remember_ranking(group_id, result.ranking)
        if result.valorant is not None and self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_valorant(result.valorant)
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: VALORANT image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: VALORANT image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary VALORANT image: {}",
                            exc,
                        )
        if result.skin_report is not None and self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_skin_prices(
                    result.skin_report
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: skin price image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: skin price image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary skin price image: {}",
                            exc,
                        )
        if result.game_status is not None and self.stats_renderer.enabled:
            image_path = None
            try:
                image_path = await self.stats_renderer.render_game_status(
                    result.game_status
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: game-status image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: game-status image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary game-status image: {}",
                            exc,
                        )
        if (
            result.watch_stage is not None
            and result.watch_stage.status == "running"
            and self.stats_renderer.enabled
        ):
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_watch_stage(
                    result.watch_stage
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: watch-stage image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: watch-stage image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary watch-stage image: {}",
                            exc,
                        )
        if result.match_history is not None and self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_match_history(
                    result.match_history,
                    background_key=result.stats_background_key,
                    member_badge=result.member_badge,
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning("pwcs2: history image rendering failed, using text: {}", exc)
            except Exception as exc:
                logger.warning("pwcs2: history image sending failed, using text: {}", exc)
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary history image: {}",
                            exc,
                        )
        if result.ranking is not None and self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_ranking(result.ranking)
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning("pwcs2: ranking image rendering failed, using text: {}", exc)
            except Exception as exc:
                logger.warning("pwcs2: ranking image sending failed, using text: {}", exc)
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary ranking image: {}",
                            exc,
                        )
        if result.stats is not None and self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render(
                    result.stats,
                    background_key=result.stats_background_key,
                    member_badge=result.member_badge,
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning("pwcs2: stats image rendering failed, using text: {}", exc)
            except Exception as exc:
                logger.warning("pwcs2: stats image sending failed, using text: {}", exc)
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning("pwcs2: failed to remove temporary stats image: {}", exc)
        await self._reply("group", group_id, result.content)

    async def _resolve_history_source(
        self,
        group_id: str,
        reply_to: int | str | None,
    ) -> HistorySource | None:
        if reply_to is None:
            return None
        try:
            message_id: int | str = (
                int(reply_to) if str(reply_to).strip().isdigit() else reply_to
            )
            response = await self._call_action("get_msg", {"message_id": message_id})
            data = response.get("data")
            if not isinstance(data, dict):
                return None
            source_group = str(data.get("group_id") or "")
            if source_group != group_id:
                return None
            source_qq = str(data.get("user_id") or "")
            sender = data.get("sender")
            if not source_qq and isinstance(sender, dict):
                source_qq = str(sender.get("user_id") or "")
            if not source_qq or source_qq == str(self._self_id or ""):
                return None
            source_segments = self._normalize_segments(data.get("message"))
            source_text, _images, _mentioned, _reply = self._parse_segments(
                source_segments
            )
            if isinstance(sender, dict):
                source_name = str(
                    sender.get("card") or sender.get("nickname") or f"QQ {source_qq}"
                )
            else:
                source_name = f"QQ {source_qq}"
            return HistorySource(
                message_id=str(reply_to),
                qq_id=source_qq,
                nickname=" ".join(source_name.split())[:80],
                text=source_text,
            )
        except Exception as exc:
            logger.warning("pwcs2: failed to resolve museum reply message: {}", exc)
            return None

    async def _guard_unbound_message(
        self,
        event: dict[str, Any],
        *,
        command: ParsedCommand | None,
        group_id: str,
        user_id: str,
    ) -> bool:
        if user_id == str(self._self_id or ""):
            return False
        sender = event.get("sender") or {}
        role = str(sender.get("role") or "member")
        decision = self.unbound_guard.record_if_restricted(
            group_id=group_id,
            qq_id=user_id,
            role=role,
            command=command,
        )
        if decision is None:
            return False

        muted = False
        mute_error: Exception | None = None
        if decision.should_mute:
            try:
                await self._call_action(
                    "set_group_ban",
                    {
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "duration": decision.mute_duration_seconds,
                    },
                )
                muted = True
            except Exception as exc:
                mute_error = exc
                logger.warning(
                    "pwcs2: failed to mute unbound member group={} qq={}: {}",
                    group_id,
                    user_id,
                    exc,
                )

        if decision.should_remind:
            display_name = " ".join(
                str(sender.get("card") or sender.get("nickname") or f"QQ {user_id}").split()
            )
            await self._reply(
                "group",
                group_id,
                self._format_unbound_guard_message(
                    display_name,
                    decision,
                    muted=muted,
                    mute_failed=mute_error is not None,
                ),
            )
        return True

    def _format_unbound_guard_message(
        self,
        display_name: str,
        decision: UnboundMessageDecision,
        *,
        muted: bool,
        mute_failed: bool,
    ) -> str:
        minutes = max(1, decision.mute_duration_seconds // 60)
        bind_command = f"{self.config.command_prefix}绑定 <SteamID64>"
        if muted:
            return (
                f"{display_name}，你还没有绑定 Steam，已达到今日 {decision.limit} 条"
                f"消息限额，现禁言 {minutes} 分钟。解除后请发送 {bind_command}。"
            )
        if mute_failed:
            return (
                f"{display_name}，你还没有绑定 Steam，已达到今日 {decision.limit} 条"
                f"消息限额，但机器人禁言失败，请管理员检查群管理权限。请发送 {bind_command}。"
            )
        return (
            f"{display_name}，你还没有绑定 Steam。今日未绑定消息额度剩余 "
            f"{decision.remaining} 条；达到 {decision.limit} 条后将禁言 {minutes} 分钟。"
            f"请发送 {bind_command}。"
        )

    async def _resolve_replied_ai_image_segments(
        self,
        group_id: str,
        reply_to: int | str | None,
    ) -> list[dict[str, Any]]:
        """Return image segments from a replied message in the same QQ group."""
        return await self._resolve_replied_ai_media_segments(
            group_id, reply_to, kinds={"image"},
        )

    async def _resolve_replied_ai_media_segments(
        self,
        group_id: str,
        reply_to: int | str | None,
        *,
        kinds: set[str],
    ) -> list[dict[str, Any]]:
        """Only resolve explicitly quoted media after validating its group."""
        if reply_to is None:
            return []
        try:
            message_id: int | str = (
                int(reply_to) if str(reply_to).strip().isdigit() else reply_to
            )
            response = await self._call_action("get_msg", {"message_id": message_id})
            data = response.get("data")
            if not isinstance(data, dict):
                return []
            if str(data.get("group_id") or "") != group_id:
                return []
            return [
                segment
                for segment in self._normalize_segments(data.get("message"))
                if segment.get("type") in kinds
            ]
        except Exception:
            logger.warning("pwcs2: failed to resolve replied AI media")
            return []

    async def _collect_ai_image_data_urls(
        self, segments: list[dict[str, Any]]
    ) -> tuple[str, ...]:
        if not self.config.ai_multimodal_enabled or self.config.ai_max_images <= 0:
            return ()
        image_data_urls: list[str] = []
        for segment in segments:
            if len(image_data_urls) >= self.config.ai_max_images:
                break
            if segment.get("type") != "image":
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            source = await self._resolve_ai_image_source(data)
            if not source:
                continue
            try:
                image_data_url = await self._read_ai_image_data_url(source)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("pwcs2: failed to read AI image: {}", exc)
                continue
            image_data_urls.append(image_data_url)
        return tuple(image_data_urls)

    async def _resolve_ai_image_source(self, data: dict[str, Any]) -> str:
        for key in ("url", "file"):
            value = str(data.get(key) or "").strip()
            if self._is_supported_image_source(value):
                return value
        file_id = str(data.get("file") or "").strip()
        if not file_id:
            return ""
        try:
            response = await self._call_action("get_image", {"file": file_id})
        except Exception as exc:
            logger.warning("pwcs2: get_image failed for AI image: {}", exc)
            return ""
        resolved = response.get("data")
        if not isinstance(resolved, dict):
            return ""
        for key in ("url", "file"):
            value = str(resolved.get(key) or "").strip()
            if self._is_supported_image_source(value):
                return value
        return ""

    @staticmethod
    def _is_supported_image_source(value: str) -> bool:
        if value.startswith(_DATA_IMAGE_PREFIX):
            return ";base64," in value[:128]
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and not parsed.username and not parsed.password

    async def _read_ai_image_data_url(self, source: str) -> str:
        if source.startswith(_DATA_IMAGE_PREFIX):
            content_type = source[5 : source.find(";")].lower()
            if content_type not in _AI_IMAGE_CONTENT_TYPES:
                raise ValueError("unsupported image content type")
            if len(source) > self.config.ai_image_max_bytes * 2:
                raise ValueError("image data URL is too large")
            return source
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(self.config.ai_image_timeout_seconds),
            headers={"User-Agent": "pwcs2-qq-bot/0.1"},
        ) as client:
            async with client.stream("GET", source) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > self.config.ai_image_max_bytes:
                        raise ValueError("image is too large")
        raw = bytes(chunks)
        detected = self._detect_ai_image_content_type(content_type, raw)
        if detected is None:
            raise ValueError("unsupported image content type")
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{detected};base64,{encoded}"

    @staticmethod
    def _detect_ai_image_content_type(content_type: str, raw: bytes) -> str | None:
        if content_type in _AI_IMAGE_CONTENT_TYPES:
            return content_type
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "image/webp"
        return None

    async def _handle_ai_message(
        self,
        event: dict[str, Any],
        *,
        segments: list[dict[str, Any]],
        text: str,
        mentioned_self: bool,
        reply_to: int | str | None,
    ) -> None:
        message_type = str(event.get("message_type") or "")
        user_id = str(event.get("user_id") or "")
        prompt = text.strip()
        explicit_ai = False
        voice_reply = False
        decision = None
        group_id = ""
        for prefix in ("#ai语音", "/ai语音", "#ai", "/ai"):
            if prompt.casefold().startswith(prefix):
                remainder = prompt[len(prefix) :]
                if not remainder or remainder[0].isspace():
                    explicit_ai = True
                    voice_reply = prefix.endswith("语音")
                    prompt = remainder.strip()
                    break

        if message_type == "group":
            group_id = str(event.get("group_id") or "")
            if not group_id:
                return
            if self.config.allowed_groups and group_id not in self.config.allowed_groups:
                return
            replying_to_bot = (
                isinstance(reply_to, int) and reply_to in self._bot_outbound_ids
            )
            if not (mentioned_self or replying_to_bot or explicit_ai):
                decision = self.companion.decide(
                    group_id, prompt,
                    eligible=(not self._automatic_quiet_active()
                              and reply_to is None and not self._mentioned_qq_ids(segments)
                              and all(s.get("type") in {"text", "face"} for s in segments)
                              and not prompt.startswith(("#", "/"))),
                )
                if decision is None:
                    return
                prompt = decision.prompt
            reply_kind = "group"
            reply_target = group_id
            chat_id = (
                f"group:{group_id}"
                if self.config.ai_group_shared_context_enabled
                else f"group:{group_id}:user:{user_id}"
            )
            sender = event.get("sender") or {}
            speaker = str(sender.get("card") or sender.get("nickname") or user_id)
        elif message_type == "private":
            reply_kind = "private"
            reply_target = user_id
            chat_id = f"private:{user_id}"
            speaker = ""
        else:
            return

        proactive = decision is not None and decision.mode == "proactive"
        for prefix in ("用语音回答", "语音回答"):
            if prompt.startswith(prefix):
                voice_reply = True
                prompt = prompt[len(prefix):].lstrip(" ：:,，")
                break
        replied_media: list[dict[str, Any]] | None = None
        if message_type == "group" and reply_to is not None and self.config.ai_voice_enabled:
            replied_media = await self._resolve_replied_ai_media_segments(
                reply_target, reply_to, kinds={"image", "record"},
            )
        voice_message_id = None
        if any(s.get("type") == "record" for s in segments):
            voice_message_id = event.get("message_id")
        elif any(s.get("type") == "record" for s in (replied_media or [])):
            voice_message_id = reply_to
        if voice_message_id is not None:
            try:
                transcript = await self.native_voice.transcribe(voice_message_id)
            except NativeVoiceError as exc:
                await self._reply(reply_kind, reply_target, str(exc))
                return
            prompt = ((prompt+"\n") if prompt else "")+"语音转写（可能有识别误差）："+transcript

        image_segments = [
            segment for segment in segments if segment.get("type") == "image"
        ][: self.config.ai_max_images]
        if (
            message_type == "group"
            and reply_to is not None
            and len(image_segments) < self.config.ai_max_images
        ):
            replied_image_segments = (
                [s for s in replied_media if s.get("type") == "image"]
                if replied_media is not None
                else await self._resolve_replied_ai_image_segments(reply_target, reply_to)
            )
            remaining = self.config.ai_max_images - len(image_segments)
            image_segments.extend(replied_image_segments[:remaining])

        image_data_urls: tuple[str, ...] = ()
        if image_segments:
            image_data_urls = await self._collect_ai_image_data_urls(image_segments)
            if not image_data_urls:
                await self._reply(
                    reply_kind,
                    reply_target,
                    "我没能读取这张图片；请换一张普通 JPG/PNG/WebP 图片再试。",
                )
                return

        if prompt.casefold() in {"清空", "清除上下文", "clear", "reset"}:
            self.ai.clear_history(chat_id)
            self.companion.clear(group_id)
            await self._reply(reply_kind, reply_target, "AI 对话上下文已清空")
            return
        ai_failed = False
        try:
            mentioned_qq_ids = self._mentioned_qq_ids(segments)
            budget_prompt = prompt or ("请描述这张图片。" if image_data_urls else "")
            model_user_content = (
                f"{speaker}：{budget_prompt}" if speaker else budget_prompt
            )
            memory_budget = self.ai.memory_context_budget(
                chat_id,
                model_user_content,
                reserve_chars=(
                    self.config.ai_vision_max_tokens * 4 if image_data_urls else 0
                ),
            )
            companion_context = self.companion.context(
                group_id, max_chars=min(1400, memory_budget // 3),
            ) if message_type == "group" else ""
            memory_budget = max(0, memory_budget-len(companion_context))
            member_memory_context = ""
            if message_type == "group":
                memory_args = {
                    "requester_qq_id": user_id, "mentioned_qq_ids": mentioned_qq_ids,
                    "query": prompt, "max_context_chars": memory_budget,
                }
                if self.config.ai_data_tools_enabled:
                    # Identity/cached context is cheap. Live requests are chosen
                    # by the model and bounded by the read-only tool dispatcher.
                    member_memory_context = self.member_memory.build_ai_context(
                        group_id, **memory_args,
                    )
                else:
                    member_memory_context = await self.member_memory.build_smart_ai_context(
                        group_id,
                        **memory_args,
                    )
            group_memory_budget = max(
                0,
                memory_budget
                - len(member_memory_context)
                - (1 if member_memory_context else 0),
            )
            group_memory_context = (
                self.group_ai_memory.build_ai_context(
                    group_id,
                    max_chars=group_memory_budget,
                    query=(
                        (text if proactive else prompt)
                        if self.companion.allowed(group_id) else ""
                    ),
                    requester_qq_id=user_id,
                )
                if message_type == "group"
                else ""
            )
            memory_context = "\n".join(
                part
                for part in (companion_context, member_memory_context, group_memory_context)
                if part
            )
            chat_kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "requester_id": user_id,
                "content": prompt,
                "speaker": speaker,
                "memory_context": memory_context,
            }
            if image_data_urls:
                chat_kwargs["image_data_urls"] = image_data_urls
            if proactive:
                # A superseded/silent suggestion was never said in the group;
                # do not insert it into shared history as an actual reply.
                chat_kwargs["retain_history"] = False
                chat_kwargs["use_request_cooldown"] = False
            if message_type == "group" and self.config.ai_data_tools_enabled:
                chat_kwargs["toolbox"] = self.ai_group_data.for_request(
                    group_id, user_id, mentioned_qq_ids,
                )
            answer = await self.ai.chat(**chat_kwargs)
        except AIChatError as exc:
            if proactive:
                return
            ai_failed = True
            answer = f"AI 调用失败：{exc}"
        finally:
            if proactive:
                self.companion.release(group_id)
        answer = self.companion.prepare_answer(group_id, answer, decision)
        if not answer:
            return
        if voice_reply and not ai_failed and message_type == "group":
            try:
                self._assert_group_output_allowed(group_id)
                await self.native_voice.speak(group_id, answer)
                return
            except NativeVoiceError as exc:
                answer = str(exc)+"\n"+answer
        if message_type == "group" and not self.companion.allowed(group_id):
            answer = f"{speaker}：\n{answer}"
        for chunk in split_outbound_text(answer):
            await self._reply(reply_kind, reply_target, chunk)

        if proactive:
            self.ai.remember_exchange(chat_id, f"{speaker}：{text.strip()}", answer)

    def _mentioned_qq_ids(
        self,
        segments: list[dict[str, Any]],
    ) -> tuple[str, ...]:
        bot_id = str(self._self_id or "")
        mentioned: list[str] = []
        for segment in segments:
            if segment.get("type") != "at":
                continue
            data = segment.get("data")
            if not isinstance(data, dict):
                continue
            qq_id = str(data.get("qq") or "").strip()
            if (
                not qq_id
                or qq_id in {"all", "0"}
                or qq_id == bot_id
                or qq_id in mentioned
            ):
                continue
            mentioned.append(qq_id)
        return tuple(mentioned)

    async def _send_match_notification(
        self,
        group_id: str,
        match: MatchSnapshot,
    ) -> None:
        quiet = self._automatic_quiet_active()
        if self.highlight_store is not None and not quiet:
            await self._enqueue_match_highlight_with_retry(group_id, match)
        self.member_memory.remember_match(group_id, match)
        if self.commands.challenges is not None:
            try:
                self.commands.challenges.record_match(group_id, match)
            except Exception:
                logger.exception("pwcs2: challenge progress update failed group={}", group_id)
        if quiet:
            await self._apply_match_rewards(group_id, match, announce=False)
            return
        content = format_match_snapshot(match)
        commentary = None
        if (
            self.stats_renderer.enabled
            and self.match_commentary.enabled
            and self.ai.configured
        ):
            commentary = await self._match_commentary_for_image(group_id, match)
        sent_image = False
        if self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_match(
                    match,
                    commentary=commentary,
                )
                await self._reply("group", group_id, "", media=[str(image_path)])
                sent_image = True
            except StatsCardRenderError as exc:
                logger.warning("pwcs2: match image rendering failed, using text: {}", exc)
            except Exception as exc:
                logger.warning("pwcs2: match image sending failed, using text: {}", exc)
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning("pwcs2: failed to remove temporary match image: {}", exc)
        if not sent_image:
            await self._reply("group", group_id, content)
        await self._apply_match_rewards(group_id, match, announce=True)

    async def _apply_match_rewards(
        self, group_id: str, match: MatchSnapshot, *, announce: bool,
    ) -> None:
        try:
            # Credit completion/first-win rewards without another group message.
            self.group_play.reward_match(group_id, match)
        except Exception:
            logger.exception(
                "pwcs2: match coin reward failed group={} match={}",
                group_id,
                match.match_id,
            )
        try:
            revenge = self.group_play.process_match(group_id, match)
            if revenge and announce:
                await self._reply("group", group_id, revenge)
        except Exception:
            logger.exception(
                "pwcs2: revenge tracking failed group={} match={}",
                group_id,
                match.match_id,
            )

    async def _enqueue_match_highlight_with_retry(
        self,
        group_id: str,
        match: MatchSnapshot,
    ) -> None:
        """Persist the Demo intent before the match notification is acknowledged.

        A transient SQLite failure must not be swallowed after the visible match
        message has already been sent: the notifier would mark that match as
        handled and the Demo could never be acquired.  Two short retries cover
        ordinary WAL contention; a persistent failure aborts this notification
        before any QQ message is sent so the notifier can retry the whole match
        on its next poll.  The intent itself is idempotent.
        """

        delays = (0.0, 0.25, 1.0)
        for attempt, delay in enumerate(delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._enqueue_match_highlight(group_id, match)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if attempt == len(delays):
                    logger.exception(
                        "pwcs2: failed to persist Demo highlight intent after "
                        "{} attempts group={} match={}",
                        attempt,
                        group_id,
                        match.match_id,
                    )
                    raise
                logger.warning(
                    "pwcs2: Demo highlight intent write failed; retrying "
                    "attempt={}/{} group={} match={}",
                    attempt,
                    len(delays),
                    group_id,
                    match.match_id,
                )

    async def _enqueue_match_highlight(
        self,
        group_id: str,
        match: MatchSnapshot,
    ) -> None:
        """Persist a lightweight Demo-acquisition intent after a match is sent.

        The PWA access token is shared while the download signature uses the
        target player whose match list discovered this match.  The expensive
        Demo download and parsing happen in the isolated controller service.
        """
        if (
            not self.config.highlight_enabled
            or match.source != "pvp"
        ):
            return
        tracked_steam_ids = tuple(
            sorted(
                {
                    player.steam_id
                    for player in match.players
                    if player.is_group_member
                    and player.steam_id.isdecimal()
                    and len(player.steam_id) == 17
                }
            )
        )
        if (
            not match.steam_id.isdecimal()
            or len(match.steam_id) != 17
            or not tracked_steam_ids
        ):
            return
        identity = f"{group_id}\n{match.source}\n{match.match_id}".encode()
        intent_id = hashlib.sha256(identity).hexdigest()[:24]
        payload = {
            "schema_version": 1,
            "kind": "demo_match",
            "highlight_id": intent_id,
            "source": "perfectworld",
            "tracked_steam_ids": list(tracked_steam_ids),
            "match": {
                "map_name": match.map_name,
                "ended_at": match.ended_at,
                "result_label": match.result_label,
            },
        }
        intent_repository = self.highlight_intent_repository
        if intent_repository is None:
            return
        _job, created = await asyncio.to_thread(
            intent_repository.enqueue_highlight_render_job,
            group_id,
            match.match_id,
            match.steam_id,
            payload,
            dedupe_key=f"demo-match:{group_id}:{match.source}:{match.match_id}",
            priority=10,
        )
        if created:
            logger.info(
                "pwcs2: queued Demo highlight group={} match={} tracked_players={}",
                group_id,
                match.match_id,
                len(tracked_steam_ids),
            )

    async def _highlight_message_details(self, item: Any) -> tuple[str, str]:
        caption = _highlight_caption(item)
        binding = await asyncio.to_thread(
            self.repository.get_by_steam, item.group_id, item.steam_id
        )
        if binding is None:
            return caption, ""
        qq_id = binding.qq_id
        if not self.match_commentary.enabled or not self.ai.configured:
            return caption, qq_id
        if not await asyncio.to_thread(
            self.repository.is_match_commentary_enabled, item.group_id, qq_id
        ):
            return caption, qq_id
        try:
            comment = await asyncio.wait_for(
                generate_highlight_commentary(self.ai, item), timeout=12.0
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "pwcs2: highlight commentary unavailable; sending video without "
                "commentary job={}: {}", item.job_id, exc
            )
            comment = None
        if comment:
            caption += f"\n🐮 {comment}"
        return caption, qq_id

    async def _run_highlight_delivery(self) -> None:
        store = self.highlight_store
        delivery = self.highlight_delivery
        if store is None or delivery is None:
            return
        if self.config.highlight_delivery_initial_delay_seconds:
            await asyncio.sleep(self.config.highlight_delivery_initial_delay_seconds)
        next_unknown_cleanup_at = 0.0
        while True:
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_unknown_cleanup_at:
                try:
                    quarantined = await asyncio.to_thread(
                        store.quarantine_stale_delivery_claims,
                    )
                    if quarantined:
                        logger.error(
                            "pwcs2: quarantined {} stale in-flight highlight "
                            "deliveries after restart/timeout: {}",
                            len(quarantined),
                            ",".join(quarantined),
                        )
                    archived = await asyncio.to_thread(
                        store.archive_unknown_delivery_files,
                        older_than_seconds=(
                            self.config.highlight_unknown_retention_days * 24 * 60 * 60
                        ),
                    )
                    if archived:
                        logger.warning(
                            "pwcs2: archived {} ambiguous highlight deliveries after "
                            "the retention window: {}",
                            len(archived),
                            ",".join(archived),
                        )
                    pruned = await asyncio.to_thread(store.prune)
                    if (
                        pruned.failed_jobs
                        or pruned.delivered_jobs
                        or pruned.orphan_files
                    ):
                        logger.info(
                            "pwcs2: pruned highlight artifacts failed={} delivered={} "
                            "orphans={}",
                            len(pruned.failed_jobs),
                            len(pruned.delivered_jobs),
                            len(pruned.orphan_files),
                        )
                    if pruned.skipped_jobs:
                        logger.error(
                            "pwcs2: refused unsafe highlight artifact paths for jobs: {}",
                            ",".join(pruned.skipped_jobs),
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("pwcs2: ambiguous highlight cleanup failed: {}", exc)
                finally:
                    next_unknown_cleanup_at = loop_time + 60 * 60
            if self._automatic_quiet_active():
                await asyncio.sleep(self.config.highlight_delivery_poll_seconds)
                continue
            try:
                completed = await asyncio.to_thread(store.completed, limit=3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "pwcs2: failed to inspect completed Demo highlights; "
                    "delivery worker will retry: {}",
                    exc,
                )
                await asyncio.sleep(self.config.highlight_delivery_poll_seconds)
                continue
            for item in completed:
                try:
                    delivery.preflight()
                    caption, mention_qq_id = await self._highlight_message_details(item)
                    prepared = await delivery.prepare(
                        item.group_id,
                        item.output_path,
                        caption=caption,
                        mention_qq_id=mention_qq_id,
                    )
                except asyncio.CancelledError:
                    raise
                except HighlightPreDispatchError as exc:
                    logger.warning(
                        "pwcs2: Demo highlight transport unavailable before "
                        "preparation; will retry job={} group={}: {}",
                        item.job_id,
                        item.group_id,
                        exc,
                    )
                    continue
                except HighlightProtocolError as exc:
                    try:
                        terminal = await asyncio.to_thread(
                            store.fail_pre_dispatch_delivery,
                            item.job_id,
                            str(exc),
                        )
                    except Exception as state_exc:
                        logger.error(
                            "pwcs2: failed to persist deterministic highlight "
                            "preparation failure job={}: {}",
                            item.job_id,
                            state_exc,
                        )
                        continue
                    if terminal:
                        logger.error(
                            "pwcs2: Demo highlight cannot be delivered and was "
                            "terminalized before sending job={} group={}: {}",
                            item.job_id,
                            item.group_id,
                            exc,
                        )
                    continue
                except Exception as exc:
                    logger.warning(
                        "pwcs2: Demo highlight preparation failed; will retry "
                        "job={} group={}: {}",
                        item.job_id,
                        item.group_id,
                        exc,
                    )
                    continue
                try:
                    claimed = await asyncio.to_thread(store.claim_delivery, item.job_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "pwcs2: failed to claim completed Demo highlight; "
                        "delivery worker will retry job={}: {}",
                        item.job_id,
                        exc,
                    )
                    continue
                if not claimed:
                    continue
                try:
                    await delivery.dispatch(prepared)
                except HighlightPreDispatchError as exc:
                    released = True
                    try:
                        await asyncio.to_thread(
                            store.release_delivery_claim,
                            item.job_id,
                            str(exc),
                        )
                    except Exception as state_exc:
                        logger.error(
                            "pwcs2: failed to release pre-dispatch highlight claim "
                            "job={}: {}",
                            item.job_id,
                            state_exc,
                        )
                        released = False
                    if released:
                        logger.warning(
                            "pwcs2: Demo highlight transport unavailable before send; "
                            "will retry job={} group={}: {}",
                            item.job_id,
                            item.group_id,
                            exc,
                        )
                    continue
                except asyncio.CancelledError:
                    try:
                        store.mark_delivery_unknown(
                            item.job_id,
                            "delivery task cancelled after OneBot dispatch claim",
                        )
                    except Exception as state_exc:
                        logger.error(
                            "pwcs2: failed to persist cancelled highlight delivery "
                            "job={}: {}",
                            item.job_id,
                            state_exc,
                        )
                    raise
                except Exception as exc:
                    try:
                        await asyncio.to_thread(
                            store.mark_delivery_unknown,
                            item.job_id,
                            str(exc),
                        )
                    except Exception as state_exc:
                        logger.error(
                            "pwcs2: failed to persist ambiguous highlight delivery "
                            "job={}: {}",
                            item.job_id,
                            state_exc,
                        )
                    logger.error(
                        "pwcs2: Demo highlight delivery result is ambiguous; "
                        "automatic retry suppressed job={} group={}: {}",
                        item.job_id,
                        item.group_id,
                        exc,
                    )
                    continue
                try:
                    await asyncio.to_thread(store.mark_delivered, item.job_id)
                    await asyncio.to_thread(store.cleanup_delivered_files, item.job_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "pwcs2: Demo highlight was accepted but post-send persistence "
                        "failed; automatic resend remains suppressed job={} group={}: {}",
                        item.job_id,
                        item.group_id,
                        exc,
                    )
            await asyncio.sleep(self.config.highlight_delivery_poll_seconds)

    async def _match_commentary_for_image(
        self,
        group_id: str,
        match: MatchSnapshot,
    ) -> str | None:
        try:
            return await self.match_commentary.generate(group_id, match)
        except AIChatError as exc:
            logger.warning(
                "pwcs2: post-match AI commentary failed group={} match={}: {}",
                group_id,
                match.match_id,
                exc,
            )
            return None
        except Exception:
            logger.exception(
                "pwcs2: unexpected post-match commentary failure group={} match={}",
                group_id,
                match.match_id,
            )
            return None

    async def _send_daily_summary(self, group_id: str, content: str) -> None:
        for chunk in split_outbound_text(content):
            await self._reply("group", group_id, chunk)

    async def _send_stream_live_notification(
        self,
        group_id: str,
        content: str,
    ) -> None:
        await self._reply("group", group_id, content)

    async def _send_daily_admin(self, group_id: str, run: DailyAdminRun) -> None:
        content = format_daily_admin_announcement(
            run,
            ban_revoke_threshold_seconds=(
                self.config.admin_ban_revoke_threshold_seconds
            ),
        )
        for chunk in split_outbound_text(content):
            await self._reply("group", group_id, chunk)

    async def _apply_daily_admin(
        self,
        group_id: str,
        previous_qq_id: str | None,
        winner_qq_id: str | None,
    ) -> None:
        """Apply one idempotent rotation while touching only bot-tracked privileges."""
        self._assert_group_output_allowed(group_id)
        bot_id = str(self._self_id or "").strip()
        if not bot_id.isdecimal():
            raise RuntimeError("机器人 QQ 尚未就绪，拒绝修改管理员")
        members = await self._fetch_group_members(group_id)
        by_id = {
            str(item.get("user_id")): item
            for item in members
            if item.get("user_id") is not None
        }
        bot = by_id.get(bot_id)
        if bot is None or str(bot.get("role") or "").casefold() != "owner":
            raise RuntimeError("机器人不是群主，无法安全轮换管理员")

        winner = by_id.get(winner_qq_id or "")
        previous = by_id.get(previous_qq_id or "")
        winner_role = str((winner or {}).get("role") or "").strip().casefold()
        previous_role = str((previous or {}).get("role") or "").strip().casefold()
        granted = False
        if winner_qq_id is not None:
            if not winner_qq_id.isdecimal() or winner_qq_id == bot_id or winner is None:
                raise RuntimeError("候选成员已不在群内，稍后重新尝试")
            if _parse_onebot_boolean(winner.get("is_robot", False)):
                raise RuntimeError("拒绝把机器人设为值日管理员")
            if winner_role == "admin" and winner_qq_id != previous_qq_id:
                raise RuntimeError("候选人已由群主手动设为管理员，拒绝接管该权限")
            if winner_role not in {"member", "admin"}:
                raise RuntimeError(f"候选成员角色异常：{winner_role!r}")
            if winner_role != "admin":
                await self._call_action(
                    "set_group_admin",
                    {
                        "group_id": int(group_id),
                        "user_id": int(winner_qq_id),
                        "enable": True,
                    },
                )
                granted = True

        try:
            if (
                previous_qq_id
                and previous_qq_id != winner_qq_id
                and previous_role == "admin"
            ):
                await self._call_action(
                    "set_group_admin",
                    {
                        "group_id": int(group_id),
                        "user_id": int(previous_qq_id),
                        "enable": False,
                    },
                )
        except Exception:
            if granted and winner_qq_id is not None:
                with suppress(Exception):
                    await self._call_action(
                        "set_group_admin",
                        {
                            "group_id": int(group_id),
                            "user_id": int(winner_qq_id),
                            "enable": False,
                        },
                    )
            raise

    async def _send_auto_prediction(self, group_id: str, content: str) -> None:
        await self._reply("group", group_id, content)

    async def _send_grenade_question(
        self,
        group_id: str,
        question: QuizQuestion,
    ) -> None:
        await self._reply("group", group_id, format_quiz_question(question))

    async def _send_grenade_reveal(
        self,
        group_id: str,
        reveal: QuizReveal,
    ) -> None:
        await self._reply("group", group_id, format_quiz_reveal(reveal))

    async def _send_weekly_report(
        self,
        group_id: str,
        snapshot: WeeklyReportSnapshot,
    ) -> None:
        if self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_weekly_report(snapshot)
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: weekly report image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: weekly report image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary weekly report image: {}",
                            exc,
                        )
        await self._reply("group", group_id, format_weekly_report(snapshot))

    async def _send_weekly_chemistry(
        self,
        group_id: str,
        snapshot: ChemistryNetworkSnapshot,
    ) -> None:
        if self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_chemistry_network(snapshot)
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: chemistry network image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: chemistry network image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary chemistry image: {}",
                            exc,
                        )
        await self._reply("group", group_id, format_chemistry_network(snapshot))

    async def _send_skin_price_report(
        self,
        group_id: str,
        report: SkinPriceReport,
    ) -> None:
        if self.stats_renderer.enabled:
            image_path: Path | None = None
            try:
                image_path = await self.stats_renderer.render_skin_prices(report)
                await self._reply("group", group_id, "", media=[str(image_path)])
                return
            except StatsCardRenderError as exc:
                logger.warning(
                    "pwcs2: daily skin price image rendering failed, using text: {}",
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "pwcs2: daily skin price image sending failed, using text: {}",
                    exc,
                )
            finally:
                if image_path is not None:
                    try:
                        await asyncio.to_thread(image_path.unlink, missing_ok=True)
                    except OSError as exc:
                        logger.warning(
                            "pwcs2: failed to remove temporary skin price image: {}",
                            exc,
                        )
        await self._reply("group", group_id, report.text)

    async def _send_daily_word_cloud(
        self,
        group_id: str,
        cloud: WordCloudSnapshot,
    ) -> None:
        image_path: Path | None = None
        try:
            image_path = await self.stats_renderer.render_word_cloud(cloud)
            await self._reply("group", group_id, "", media=[str(image_path)])
        finally:
            if image_path is not None:
                try:
                    await asyncio.to_thread(image_path.unlink, missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "pwcs2: failed to remove temporary word cloud image: {}",
                        exc,
                    )

    async def _remove_noncompliant_members(
        self,
        group_id: str,
        user_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Recheck every destructive decision against live OneBot data before kicking."""
        self._assert_group_output_allowed(group_id)
        bot_id = str(self._self_id or "").strip()
        if not bot_id.isdecimal():
            logger.error(
                "pwcs2: refusing compliance removals because the bot QQ is unknown"
            )
            return ()
        try:
            bot_response = await self._call_action(
                "get_group_member_info",
                {
                    "group_id": int(group_id),
                    "user_id": int(bot_id),
                    "no_cache": True,
                },
            )
            bot_member = bot_response.get("data")
            if not isinstance(bot_member, dict):
                raise RuntimeError("NapCat returned invalid bot member data")
            if str(bot_member.get("user_id") or "") != bot_id:
                raise RuntimeError("NapCat returned mismatched bot member data")
            bot_role = str(bot_member.get("role") or "").strip().casefold()
        except Exception as exc:
            logger.warning(
                "pwcs2: failed to verify bot role before removals group={}: {}",
                group_id,
                exc,
            )
            return ()
        if bot_role not in {"owner", "admin"}:
            logger.warning(
                "pwcs2: refusing compliance removals because bot role is {} group={}",
                bot_role,
                group_id,
            )
            return ()

        resolved: list[str] = []
        for user_id in user_ids:
            if not user_id.isdecimal() or user_id == bot_id:
                resolved.append(user_id)
                continue
            try:
                response = await self._call_action(
                    "get_group_member_info",
                    {
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "no_cache": True,
                    },
                )
                member = response.get("data")
                if not isinstance(member, dict):
                    raise RuntimeError("NapCat returned invalid group member data")
                if str(member.get("user_id") or "") != user_id:
                    raise RuntimeError("NapCat returned mismatched group member data")
                role = str(member.get("role") or "").strip().casefold()
                if role in {"owner", "admin"}:
                    resolved.append(user_id)
                    continue
                if role != "member":
                    raise RuntimeError(f"NapCat returned unknown member role: {role!r}")
                if "is_robot" not in member:
                    raise RuntimeError("NapCat group member data omitted is_robot")
                if _parse_onebot_boolean(member["is_robot"]):
                    resolved.append(user_id)
                    continue
                if "level" not in member:
                    raise RuntimeError("NapCat group member data omitted level")
                try:
                    level = int(str(member.get("level", "")).strip())
                except ValueError:
                    raise RuntimeError("NapCat returned invalid group member level") from None
                still_low_level = level == self.config.group_level_reminder_target_level
                still_unbound = (
                    self.config.group_level_reminder_include_unbound
                    and self.repository.get_by_qq(group_id, user_id) is None
                )
                if not still_low_level and not still_unbound:
                    resolved.append(user_id)
                    continue
                await self._call_action(
                    "set_group_kick",
                    {
                        "group_id": int(group_id),
                        "user_id": int(user_id),
                        "reject_add_request": False,
                    },
                )
                resolved.append(user_id)
                logger.info(
                    "pwcs2: removed noncompliant member after grace period "
                    "group={} qq={} low_level={} unbound={}",
                    group_id,
                    user_id,
                    still_low_level,
                    still_unbound,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: failed to remove noncompliant member group={} qq={}: {}",
                    group_id,
                    user_id,
                    exc,
                )
        return tuple(resolved)

    async def _send_group_level_reminder(
        self,
        group_id: str,
        reminders: tuple[ComplianceReminder, ...],
    ) -> None:
        if self._automatic_quiet_active():
            return
        self._assert_group_output_allowed(group_id)
        bot_id = str(self._self_id or "")
        targets = tuple(item for item in reminders if item.user_id != bot_id)
        if not targets:
            return
        message: list[dict[str, Any]] = []
        for item in targets:
            reasons: list[str] = []
            if UNBOUND_REASON in item.reasons:
                reasons.append("未绑定 Steam")
            if LEVEL_REASON in item.reasons:
                reasons.append(
                    f"群等级为 {self.config.group_level_reminder_target_level}"
                )
            countdown = (
                f"剩 {item.remaining_days} 天"
                if item.remaining_days > 0
                else "已到期，等待机器人重试移出"
            )
            message.append({"type": "at", "data": {"qq": item.user_id}})
            message.append(
                {
                    "type": "text",
                    "data": {
                        "text": f"（{'、'.join(reasons)}；{countdown}）\n"
                    },
                }
            )
        message.append(
            {
                "type": "text",
                "data": {
                    "text": (
                        "请在倒计时结束前发送 #绑定 <SteamID64> 完成绑定，"
                        f"并将群等级升至 {self.config.group_level_reminder_target_level + 1} "
                        "以上。任一条件连续未满足满 "
                        f"{self.config.group_level_removal_grace_days} 天将自动移出本群。"
                    )
                },
            }
        )
        await self._call_action(
            "send_group_msg",
            {"group_id": int(group_id), "message": message},
        )

    async def _send_content_relay(
        self,
        target_group: str,
        message: list[dict[str, Any]],
    ) -> None:
        if self._automatic_quiet_active():
            return
        self._assert_group_output_allowed(target_group)
        await self._call_action(
            "send_group_msg",
            {"group_id": int(target_group), "message": message},
        )

    async def _send_training_game_file(self, group_id: str) -> None:
        self._assert_group_output_allowed(group_id)
        if not self.config.training_game_file_upload_enabled:
            await self._reply(
                "group",
                group_id,
                "群文件发送当前关闭，可直接打开或另存为："
                f"{self.config.training_game_url}",
            )
            return
        path = training_game_html_path()
        try:
            data = await asyncio.to_thread(path.read_bytes)
            if not data or len(data) > TRAINING_GAME_MAX_FILE_BYTES:
                raise ValueError("training game file size is invalid")
            await self._call_action(
                "upload_group_file",
                {
                    "group_id": int(group_id),
                    "file": "base64://" + base64.b64encode(data).decode("ascii"),
                    "name": TRAINING_GAME_FILE_NAME,
                },
            )
            await self._reply(
                "group",
                group_id,
                "已上传 CS2 战术训练营原文件；下载后双击即可运行。",
            )
        except Exception as exc:
            logger.warning("pwcs2: training game file upload failed: {}", exc)
            await self._reply(
                "group",
                group_id,
                "群文件上传失败，可直接打开或另存为："
                f"{self.config.training_game_url}",
            )

    async def _reply(
        self,
        kind: str,
        target: str,
        content: str,
        *,
        media: list[str] | None = None,
    ) -> None:
        if kind == "private" and not self.config.private_replies_enabled:
            return
        if kind == "group":
            self._assert_group_output_allowed(target)
        await self.send(
            OutboundMessage(
                channel=self.name,
                chat_id=f"{kind}:{target}",
                content=content,
                media=media or [],
            )
        )

    def _assert_group_output_allowed(self, group_id: str) -> None:
        if self.config.allowed_groups and group_id not in self.config.allowed_groups:
            raise RuntimeError(f"outbound QQ group is not allowed: {group_id}")


def __getattr__(name: str):
    if name == "PWCS2Channel":
        from pwcs2_bot.nanobot_adapter import PWCS2Channel

        return PWCS2Channel
    raise AttributeError(name)
