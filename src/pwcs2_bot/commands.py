"""Deterministic QQ command parsing and handling."""

from __future__ import annotations

import asyncio
import math
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from loguru import logger

from pwcs2_bot.challenges import ChallengeService
from pwcs2_bot.chemistry import (
    ChemistrySnapshot,
    build_chemistry_snapshot,
    format_chemistry_snapshot,
)
from pwcs2_bot.community import (
    CommunityError,
    GroupPlayService,
    HistorySource,
)
from pwcs2_bot.database import (
    Binding,
    BindingConflictError,
    BindingRepository,
    ValorantBindingConflictError,
)
from pwcs2_bot.formatting import (
    StatsSnapshot,
    build_official_stats_snapshot,
    build_stats_snapshot,
    format_stats_snapshot,
)
from pwcs2_bot.game_identity import resolve_game_profile
from pwcs2_bot.group_ai_memory import GroupAIMemoryService
from pwcs2_bot.match_formatting import (
    MatchHistorySnapshot,
    build_match_history,
    format_match_history,
)
from pwcs2_bot.party import PartyService
from pwcs2_bot.perfectworld import PerfectWorldApiError, PerfectWorldClient
from pwcs2_bot.polls import PollService
from pwcs2_bot.query_source import parse_source_argument
from pwcs2_bot.ranking import (
    RankingEntry,
    RankingSnapshot,
    build_official_ranking_entry,
    build_ranking_entry,
    build_ranking_snapshot,
    format_ranking_snapshot,
    parse_ranking_metric,
)
from pwcs2_bot.skin_prices import (
    DailySkinPriceService,
    SkinPriceError,
    SkinPriceReport,
)
from pwcs2_bot.steam_presence import (
    GameStatusEntry,
    GameStatusSnapshot,
    SteamPresenceClient,
    SteamPresenceError,
    build_group_game_status_snapshot,
    build_player_game_status_snapshot,
    format_group_game_status,
    format_player_game_status,
)
from pwcs2_bot.teamspeak import (
    TeamSpeakQueryError,
    TeamSpeakStatusProvider,
    format_teamspeak_status,
)
from pwcs2_bot.valorant import (
    ValorantClient,
    ValorantPlayerNotFound,
    ValorantQueryError,
    ValorantSnapshot,
    format_valorant_snapshot,
    parse_valorant_id,
    valorant_id_key,
    valorant_snapshot_from_payload,
    valorant_snapshot_to_payload,
)
from pwcs2_bot.watch_stage import (
    WatchStageClient,
    WatchStageError,
    WatchStageSnapshot,
    display_map_name,
    format_watch_stage,
)

Action = Literal[
    "bind",
    "valorant_bind",
    "valorant_stats",
    "valorant_unbind",
    "valorant_binding",
    "stats",
    "matches",
    "spectate",
    "playing",
    "game_status",
    "ranking",
    "teamspeak",
    "skin_add",
    "skin_remove",
    "skin_list",
    "skin_prices",
    "party_create",
    "party_join",
    "party_leave",
    "party_status",
    "party_disband",
    "party_balance",
    "challenge",
    "poll",
    "interaction_help",
    "chemistry",
    "commentary_on",
    "commentary_off",
    "commentary_status",
    "coin_balance",
    "checkin",
    "coin_earn",
    "coin_ranking",
    "coin_ledger",
    "coin_tip",
    "shop",
    "shop_specials",
    "shop_gift",
    "badge_collection",
    "badge_equip",
    "checkin_repair",
    "buy",
    "inventory",
    "custom_badge",
    "quiz_hint",
    "stats_background",
    "prediction_status",
    "prediction_create",
    "prediction_bet",
    "prediction_settle",
    "prediction_cancel",
    "grenade_quiz",
    "history",
    "history_add",
    "history_delete",
    "history_on",
    "history_off",
    "revenge",
    "card_rename",
    "memory_add",
    "memory_list",
    "memory_delete",
    "unbind",
    "binding",
    "help",
]
_STEAM_ID_RE = re.compile(r"^7656119\d{10}$")
_QQ_ID_RE = re.compile(r"^[1-9]\d{4,11}$")
_MENTION_RE = re.compile(r"^@([1-9]\d{4,11})$")


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    action: Action
    argument: str = ""


@dataclass(frozen=True, slots=True)
class CommandResult:
    content: str
    stats: StatsSnapshot | None = None
    stats_background_key: str = "classic"
    match_history: MatchHistorySnapshot | None = None
    ranking: RankingSnapshot | None = None
    skin_report: SkinPriceReport | None = None
    chemistry: ChemistrySnapshot | None = None
    watch_stage: WatchStageSnapshot | None = None
    game_status: GameStatusSnapshot | None = None
    valorant: ValorantSnapshot | None = None
    member_badge: str = ""


class CommandService:
    _ALIASES: dict[str, Action] = {
        "绑定": "bind",
        "bind": "bind",
        "瓦绑定": "valorant_bind",
        "无畏契约绑定": "valorant_bind",
        "valbind": "valorant_bind",
        "瓦战绩": "valorant_stats",
        "无畏契约战绩": "valorant_stats",
        "val": "valorant_stats",
        "valorant": "valorant_stats",
        "瓦解绑": "valorant_unbind",
        "无畏契约解绑": "valorant_unbind",
        "我的瓦绑定": "valorant_binding",
        "瓦绑定信息": "valorant_binding",
        "数据": "stats",
        "查询": "stats",
        "stats": "stats",
        "data": "stats",
        "战绩": "matches",
        "比赛": "matches",
        "recent": "matches",
        "matches": "matches",
        "观战": "spectate",
        "观将台": "spectate",
        "实时": "spectate",
        "谁在打": "playing",
        "谁在玩": "playing",
        "在玩": "playing",
        "游戏状态": "game_status",
        "steam游戏": "game_status",
        "steam状态": "game_status",
        "在玩什么": "game_status",
        "排行": "ranking",
        "排行榜": "ranking",
        "rank": "ranking",
        "ranking": "ranking",
        "ts": "teamspeak",
        "ts状态": "teamspeak",
        "语音": "teamspeak",
        "开黑": "party_create",
        "发车": "party_create",
        "上车": "party_join",
        "下车": "party_leave",
        "车队": "party_status",
        "解散车队": "party_disband",
        "散车": "party_disband",
        "分队": "party_balance",
        "平衡分队": "party_balance",
        "挑战": "challenge",
        "每日挑战": "challenge",
        "投票": "poll",
        "互动": "interaction_help",
        "互动帮助": "interaction_help",
        "羁绊": "chemistry",
        "默契": "chemistry",
        "开启锐评": "commentary_on",
        "打开锐评": "commentary_on",
        "关闭锐评": "commentary_off",
        "锐评状态": "commentary_status",
        "牛币": "coin_balance",
        "签到": "checkin",
        "打卡": "checkin",
        "牛币获取": "coin_earn",
        "牛币规则": "coin_earn",
        "牛币排行": "coin_ranking",
        "富豪榜": "coin_ranking",
        "牛币流水": "coin_ledger",
        "账单": "coin_ledger",
        "打赏": "coin_tip",
        "商店": "shop",
        "牛币商店": "shop",
        "牛牛商店": "shop",
        "特惠": "shop_specials",
        "每日特惠": "shop_specials",
        "赠送": "shop_gift",
        "赠送道具": "shop_gift",
        "称号": "badge_collection",
        "称号收藏": "badge_collection",
        "佩戴称号": "badge_equip",
        "补签": "checkin_repair",
        "购买": "buy",
        "背包": "inventory",
        "使用称号": "custom_badge",
        "提示": "quiz_hint",
        "战绩背景": "stats_background",
        "数据背景": "stats_background",
        "竞猜": "prediction_status",
        "发起竞猜": "prediction_create",
        "下注": "prediction_bet",
        "开奖": "prediction_settle",
        "取消竞猜": "prediction_cancel",
        "选手": "grenade_quiz",
        "职业选手": "grenade_quiz",
        "选手竞猜": "grenade_quiz",
        "投掷物": "grenade_quiz",
        "每日一题": "grenade_quiz",
        "黑历史": "history",
        "收录黑历史": "history_add",
        "删除黑历史": "history_delete",
        "开启黑历史": "history_on",
        "关闭黑历史": "history_off",
        "复仇": "revenge",
        "复仇榜": "revenge",
        "改名": "card_rename",
        "修改群名片": "card_rename",
        "记住": "memory_add",
        "添加记忆": "memory_add",
        "记忆": "memory_list",
        "长期记忆": "memory_list",
        "忘记": "memory_delete",
        "删除记忆": "memory_delete",
        "饰品添加": "skin_add",
        "添加饰品": "skin_add",
        "饰品删除": "skin_remove",
        "删除饰品": "skin_remove",
        "饰品列表": "skin_list",
        "饰品价格": "skin_prices",
        "饰品": "skin_prices",
        "解绑": "unbind",
        "unbind": "unbind",
        "我的绑定": "binding",
        "binding": "binding",
        "帮助": "help",
        "help": "help",
    }

    def __init__(
        self,
        repository: BindingRepository,
        api: PerfectWorldClient,
        *,
        prefix: str = "#",
        query_cooldown_seconds: float = 5.0,
        teamspeak: TeamSpeakStatusProvider | None = None,
        teamspeak_address: str = "ts.example.com",
        skin_prices: DailySkinPriceService | None = None,
        party: PartyService | None = None,
        group_play: GroupPlayService | None = None,
        watch_stage: WatchStageClient | None = None,
        steam_presence: SteamPresenceClient | None = None,
        valorant: ValorantClient | None = None,
        group_ai_memory: GroupAIMemoryService | None = None,
        valorant_cache_ttl_seconds: float = 600.0,
        valorant_stale_ttl_seconds: float = 86400.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not prefix or any(character.isspace() for character in prefix):
            raise ValueError("command prefix must be non-empty and contain no whitespace")
        if query_cooldown_seconds < 0:
            raise ValueError("query cooldown must not be negative")
        if valorant_cache_ttl_seconds < 0:
            raise ValueError("VALORANT cache TTL must not be negative")
        if valorant_stale_ttl_seconds < valorant_cache_ttl_seconds:
            raise ValueError("VALORANT stale TTL must cover the fresh cache TTL")
        self.repository = repository
        self.api = api
        self.prefix = prefix
        self.query_cooldown_seconds = query_cooldown_seconds
        self.teamspeak = teamspeak
        self.teamspeak_address = teamspeak_address
        self.skin_prices = skin_prices
        self.party = party
        self.group_play = group_play
        self.challenges = (
            ChallengeService(group_play.repository, repository) if group_play is not None else None
        )
        self.polls = PollService(repository.path)
        self.watch_stage = watch_stage
        self.steam_presence = steam_presence
        self.valorant = valorant
        self.group_ai_memory = group_ai_memory
        self.valorant_cache_ttl_seconds = valorant_cache_ttl_seconds
        self.valorant_stale_ttl_seconds = valorant_stale_ttl_seconds
        self._clock = clock
        self._last_queries: dict[tuple[str, str], float] = {}

    def parse(self, text: str) -> ParsedCommand | None:
        raw = text.strip()
        if raw.startswith(self.prefix):
            raw = raw[len(self.prefix) :].strip()
        elif raw.startswith("/"):
            raw = raw[1:].strip()
        else:
            return None
        if not raw:
            return ParsedCommand("help")
        parts = raw.split(maxsplit=1)
        name = parts[0]
        argument = parts[1] if len(parts) == 2 else ""
        action = self._ALIASES.get(name.casefold())
        if action is None:
            folded = raw.casefold()
            for alias in (
                "解散车队",
                "发车",
                "开黑",
                "上车",
                "下车",
                "车队",
                "散车",
            ):
                if folded.startswith(alias) and len(raw) > len(alias):
                    action = self._ALIASES[alias]
                    argument = raw[len(alias) :].strip()
                    break
        if action is None:
            return None
        return ParsedCommand(action, argument.strip())

    async def handle(self, command: ParsedCommand, *, group_id: str, qq_id: str) -> str:
        return (await self.handle_result(command, group_id=group_id, qq_id=qq_id)).content

    async def handle_result(
        self,
        command: ParsedCommand,
        *,
        group_id: str,
        qq_id: str,
        sender_role: str = "member",
        nickname: str = "",
        history_source: HistorySource | None = None,
        local_date: str = "",
        request_id: str = "",
    ) -> CommandResult:
        try:
            if command.action == "help":
                if command.argument in {"互动", "玩法"}:
                    return CommandResult(self.interaction_help())
                return CommandResult(self.help_text())
            if command.action == "interaction_help":
                return CommandResult(self.interaction_help())
            if command.action == "poll":
                return CommandResult(self.polls.handle(
                    group_id, qq_id, sender_role, command.argument, request_id=request_id,
                ))
            if command.action == "challenge":
                if self.challenges is None:
                    raise ValueError("每日挑战尚未配置")
                return CommandResult(self.challenges.handle(group_id, qq_id, command.argument))
            if command.action == "bind":
                return CommandResult(self._bind(command.argument, group_id=group_id, qq_id=qq_id))
            if command.action == "valorant_bind":
                return CommandResult(
                    self._bind_valorant(
                        command.argument,
                        group_id=group_id,
                        qq_id=qq_id,
                    )
                )
            if command.action == "valorant_unbind":
                return CommandResult(self._unbind_valorant(group_id=group_id, qq_id=qq_id))
            if command.action == "valorant_binding":
                return CommandResult(
                    self._show_valorant_binding(group_id=group_id, qq_id=qq_id)
                )
            if command.action == "valorant_stats":
                return await self._valorant_stats_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action == "unbind":
                return CommandResult(self._unbind(group_id=group_id, qq_id=qq_id))
            if command.action == "binding":
                return CommandResult(self._show_binding(group_id=group_id, qq_id=qq_id))
            if command.action == "card_rename":
                return CommandResult("请在群内使用：#改名 @群友 新名字")
            if command.action.startswith("memory_"):
                if self.group_ai_memory is None:
                    raise ValueError("AI 长期记忆功能尚未配置")
                if command.action == "memory_add":
                    content = self.group_ai_memory.add(
                        group_id,
                        qq_id,
                        nickname,
                        command.argument,
                    )
                elif command.action == "memory_list":
                    content = self.group_ai_memory.list_text(group_id, command.argument)
                else:
                    content = self.group_ai_memory.delete(
                        group_id,
                        qq_id,
                        sender_role,
                        command.argument,
                    )
                return CommandResult(content)
            if command.action == "ranking":
                return await self._ranking_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action.startswith("party_"):
                if self.party is None:
                    raise ValueError("开黑车队功能尚未配置")
                if command.action == "party_create":
                    return CommandResult(self.party.create(group_id, qq_id, command.argument))
                if command.action == "party_join":
                    return CommandResult(self.party.join(group_id, qq_id, command.argument))
                if command.action == "party_leave":
                    return CommandResult(self.party.leave(group_id, qq_id, command.argument))
                if command.action == "party_status":
                    return CommandResult(self.party.status(group_id, command.argument))
                if command.action == "party_balance":
                    return CommandResult(self.party.balance(group_id, qq_id, command.argument))
                return CommandResult(self.party.disband(group_id, qq_id, command.argument))
            if command.action == "chemistry":
                return await self._chemistry_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action == "commentary_on":
                self.repository.set_match_commentary_enabled(group_id, qq_id, enabled=True)
                return CommandResult("赛后 AI 锐评已开启；以后会显示在比赛图片内。")
            if command.action == "commentary_off":
                self.repository.set_match_commentary_enabled(group_id, qq_id, enabled=False)
                return CommandResult("赛后 AI 锐评已关闭；比赛图片仍会正常发送且不显示锐评。")
            if command.action == "commentary_status":
                enabled = self.repository.is_match_commentary_enabled(group_id, qq_id)
                return CommandResult(f"你的赛后 AI 锐评当前为：{'开启' if enabled else '关闭'}")
            if command.action in {
                "coin_balance",
                "checkin",
                "coin_earn",
                "coin_ranking",
                "coin_ledger",
                "coin_tip",
                "shop",
                "shop_specials",
                "shop_gift",
                "badge_collection",
                "badge_equip",
                "checkin_repair",
                "buy",
                "inventory",
                "custom_badge",
                "quiz_hint",
                "stats_background",
                "prediction_status",
                "prediction_create",
                "prediction_bet",
                "prediction_settle",
                "prediction_cancel",
                "grenade_quiz",
                "history",
                "history_add",
                "history_delete",
                "history_on",
                "history_off",
                "revenge",
            }:
                if self.group_play is None:
                    raise ValueError("群玩法功能尚未配置")
                if command.action == "coin_balance":
                    content = self.group_play.wallet(group_id, qq_id)
                elif command.action == "checkin":
                    content = self.group_play.checkin(group_id, qq_id, checkin_date=local_date)
                elif command.action == "coin_earn":
                    content = self.group_play.coin_earning_guide()
                elif command.action == "coin_ranking":
                    content = self.group_play.coin_ranking(group_id)
                elif command.action == "coin_ledger":
                    content = self.group_play.coin_ledger(group_id, qq_id)
                elif command.action == "coin_tip":
                    content = self.group_play.tip(
                        group_id, qq_id, command.argument, request_id=request_id,
                    )
                elif command.action == "shop":
                    content = self.group_play.shop(
                        group_id, qq_id, command.argument, shop_date=local_date,
                    )
                elif command.action == "shop_specials":
                    content = self.group_play.specials(group_id, qq_id, shop_date=local_date)
                elif command.action == "shop_gift":
                    content = self.group_play.gift(
                        group_id, qq_id, command.argument, request_id=request_id,
                    )
                elif command.action == "badge_collection":
                    content = self.group_play.badge_collection(group_id, qq_id)
                elif command.action == "badge_equip":
                    content = self.group_play.equip_badge(group_id, qq_id, command.argument)
                elif command.action == "checkin_repair":
                    content = self.group_play.repair_checkin(
                        group_id, qq_id, checkin_date=local_date, request_id=request_id,
                    )
                elif command.action == "buy":
                    content = self.group_play.buy(
                        group_id, qq_id, command.argument, shop_date=local_date,
                        request_id=request_id,
                    )
                elif command.action == "inventory":
                    content = self.group_play.inventory(group_id, qq_id)
                elif command.action == "custom_badge":
                    content = self.group_play.use_custom_badge(group_id, qq_id, command.argument)
                elif command.action == "quiz_hint":
                    content = self.group_play.quiz_hint(group_id, qq_id, quiz_date=local_date)
                elif command.action == "stats_background":
                    content = self.group_play.stats_background(group_id, qq_id, command.argument)
                elif command.action == "prediction_status":
                    content = self.group_play.prediction_status(group_id)
                elif command.action == "prediction_create":
                    content = self.group_play.create_prediction(
                        group_id, qq_id, sender_role, command.argument
                    )
                elif command.action == "prediction_bet":
                    content = self.group_play.bet(group_id, qq_id, command.argument)
                elif command.action == "prediction_settle":
                    content = self.group_play.settle_prediction(
                        group_id, qq_id, sender_role, command.argument
                    )
                elif command.action == "prediction_cancel":
                    content = self.group_play.cancel_prediction(group_id, qq_id, sender_role)
                elif command.action == "grenade_quiz":
                    content = self.group_play.quiz_answer(
                        group_id,
                        qq_id,
                        nickname,
                        command.argument,
                        quiz_date=local_date,
                    )
                elif command.action == "history":
                    content = self.group_play.history(group_id, command.argument)
                elif command.action == "history_add":
                    content = self.group_play.add_history(
                        group_id, qq_id, command.argument, history_source
                    )
                elif command.action == "history_delete":
                    content = self.group_play.delete_history(
                        group_id, qq_id, sender_role, command.argument
                    )
                elif command.action == "history_on":
                    content = self.group_play.set_history(group_id, qq_id, enabled=True)
                elif command.action == "history_off":
                    content = self.group_play.set_history(group_id, qq_id, enabled=False)
                else:
                    content = self.group_play.revenge_status(group_id)
                return CommandResult(content)
            if command.action.startswith("skin_"):
                if self.skin_prices is None:
                    raise ValueError("饰品价格功能尚未配置")
                if command.action == "skin_add":
                    return CommandResult(
                        await self.skin_prices.add_watch(group_id, qq_id, command.argument)
                    )
                if command.action == "skin_remove":
                    return CommandResult(
                        self.skin_prices.remove_watch(group_id, qq_id, command.argument)
                    )
                if command.action == "skin_list":
                    return CommandResult(self.skin_prices.watchlist(group_id))
                report = await self.skin_prices.current_report(
                    group_id,
                    command.argument,
                )
                return CommandResult(report.text, skin_report=report)
            if command.action == "teamspeak":
                if self.teamspeak is None:
                    raise ValueError("TS3 状态查询尚未配置")
                status = await self.teamspeak.status()
                return CommandResult(
                    format_teamspeak_status(status, address=self.teamspeak_address)
                )
            if command.action == "matches":
                return await self._match_history_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action == "spectate":
                return await self._watch_stage_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action == "playing":
                return await self._who_is_playing_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            if command.action == "game_status":
                return await self._game_status_result(
                    command.argument,
                    group_id=group_id,
                    qq_id=qq_id,
                )
            return await self._stats_result(
                command.argument,
                group_id=group_id,
                qq_id=qq_id,
            )
        except (
            BindingConflictError,
            ValorantBindingConflictError,
            PerfectWorldApiError,
            TeamSpeakQueryError,
            SkinPriceError,
            CommunityError,
            WatchStageError,
            SteamPresenceError,
            ValorantQueryError,
            ValueError,
        ) as exc:
            return CommandResult(f"操作失败：{exc}")
        except sqlite3.Error:
            logger.exception("pwcs2: command database failure action={}", command.action)
            return CommandResult("数据暂时无法读写，请稍后重试；牛币操作会检查重复请求。")

    def _bind(self, argument: str, *, group_id: str, qq_id: str) -> str:
        steam_id = parse_steam_id(argument)
        if steam_id is None:
            raise ValueError("请提供有效的 SteamID64，例如 #绑定 7656119xxxxxxxxxx")
        previous = self.repository.get_by_qq(group_id, qq_id)
        self.repository.bind(group_id, qq_id, steam_id)
        self.repository.clear_unbound_message_usage(group_id, qq_id)
        if previous is not None and previous.steam_id == steam_id:
            return f"你已绑定 SteamID：{steam_id}"
        if previous is not None:
            return f"绑定已更新：{previous.steam_id} → {steam_id}"
        return f"绑定成功：QQ {qq_id} → SteamID {steam_id}"

    def _bind_valorant(self, argument: str, *, group_id: str, qq_id: str) -> str:
        riot_id = parse_valorant_id(argument)
        if riot_id is None:
            raise ValueError(
                f"请提供完整国服 ID，例如 {self.prefix}瓦绑定 ExamplePlayer#12345"
            )
        previous = self.repository.get_valorant_by_qq(group_id, qq_id)
        self.repository.bind_valorant(group_id, qq_id, riot_id)
        if previous is not None and previous.riot_id_key == valorant_id_key(riot_id):
            return f"你已绑定瓦 ID：{riot_id}"
        if previous is not None:
            return f"瓦绑定已更新：{previous.riot_id} → {riot_id}"
        return f"瓦绑定成功：QQ {qq_id} → {riot_id}"

    def _unbind_valorant(self, *, group_id: str, qq_id: str) -> str:
        if self.repository.unbind_valorant(group_id, qq_id):
            return "瓦绑定已解除；Steam 绑定未受影响"
        return "你还没有在本群绑定瓦 ID"

    def _show_valorant_binding(self, *, group_id: str, qq_id: str) -> str:
        binding = self.repository.get_valorant_by_qq(group_id, qq_id)
        if binding is None:
            return "你还没有在本群绑定瓦 ID"
        return f"你的本群瓦绑定：QQ {qq_id} → {binding.riot_id}"

    def _resolve_valorant_id(self, argument: str, *, group_id: str, qq_id: str) -> str:
        target = argument.strip()
        if not target:
            binding = self.repository.get_valorant_by_qq(group_id, qq_id)
            if binding is None:
                raise ValueError(
                    f"你还没有绑定瓦 ID，请先发送 "
                    f"{self.prefix}瓦绑定 <昵称#数字ID>"
                )
            return binding.riot_id
        if riot_id := parse_valorant_id(target):
            return riot_id
        mention = _MENTION_RE.fullmatch(target)
        target_qq = mention.group(1) if mention else target
        if not _QQ_ID_RE.fullmatch(target_qq):
            raise ValueError("查询目标应为群成员 @、QQ 号或完整的昵称#数字ID")
        binding = self.repository.get_valorant_by_qq(group_id, target_qq)
        if binding is None:
            raise ValueError("该成员还没有在本群绑定瓦 ID")
        return binding.riot_id

    @staticmethod
    def _valorant_cache_age_seconds(fetched_at: str) -> float:
        try:
            timestamp = datetime.fromisoformat(fetched_at)
        except ValueError:
            return math.inf
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return max(0.0, (datetime.now(UTC) - timestamp.astimezone(UTC)).total_seconds())

    def _cached_valorant_snapshot(
        self,
        riot_id: str,
        *,
        maximum_age_seconds: float,
        cache_status: Literal["fresh", "stale"],
        cache_note: str = "",
    ) -> ValorantSnapshot | None:
        cached = self.repository.get_valorant_cache(riot_id)
        if cached is None:
            return None
        if self._valorant_cache_age_seconds(cached.fetched_at) > maximum_age_seconds:
            return None
        try:
            return valorant_snapshot_from_payload(
                cached.payload,
                cache_status=cache_status,
                cache_note=cache_note,
            )
        except (TypeError, ValueError):
            return None

    async def _valorant_stats_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        if self.valorant is None:
            raise ValueError("瓦战绩查询尚未配置")
        riot_id = self._resolve_valorant_id(
            argument,
            group_id=group_id,
            qq_id=qq_id,
        )
        self._enforce_query_cooldown(group_id, qq_id)
        fresh = self._cached_valorant_snapshot(
            riot_id,
            maximum_age_seconds=self.valorant_cache_ttl_seconds,
            cache_status="fresh",
        )
        if fresh is not None:
            return CommandResult(format_valorant_snapshot(fresh), valorant=fresh)
        try:
            snapshot = await self.valorant.query(riot_id)
        except ValorantPlayerNotFound:
            raise
        except ValorantQueryError as exc:
            stale = self._cached_valorant_snapshot(
                riot_id,
                maximum_age_seconds=self.valorant_stale_ttl_seconds,
                cache_status="stale",
                cache_note=str(exc),
            )
            if stale is None:
                raise
            return CommandResult(format_valorant_snapshot(stale), valorant=stale)
        self.repository.save_valorant_cache(
            snapshot.riot_id,
            valorant_snapshot_to_payload(snapshot),
            fetched_at=snapshot.fetched_at,
        )
        return CommandResult(format_valorant_snapshot(snapshot), valorant=snapshot)

    def _unbind(self, *, group_id: str, qq_id: str) -> str:
        if self.repository.unbind(group_id, qq_id):
            return "解绑成功"
        return "你还没有在本群绑定 Steam 账号"

    def _show_binding(self, *, group_id: str, qq_id: str) -> str:
        binding = self.repository.get_by_qq(group_id, qq_id)
        if binding is None:
            return "你还没有在本群绑定 Steam 账号"
        return f"你的本群绑定：QQ {qq_id} → SteamID {binding.steam_id}"

    async def _stats_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        selected = parse_source_argument(argument)
        steam_id = self._resolve_steam_id(
            selected.argument,
            group_id=group_id,
            qq_id=qq_id,
        )
        self._enforce_query_cooldown(group_id, qq_id)
        if selected.source == "official":
            matches = await self.api.official_match_list(steam_id, page_size=50)
            stats = build_official_stats_snapshot(
                matches,
                steam_id=steam_id,
                profile=await resolve_game_profile(
                    self.api, steam_id, matches, source="official",
                    steam_presence=self.steam_presence,
                ),
            )
        else:
            data = await self.api.detail_stats(steam_id)
            stats = build_stats_snapshot({**data, "steamId": steam_id})
        background_key = "classic"
        member_badge = ""
        binding = self.repository.get_by_steam(group_id, steam_id)
        if binding is not None and self.group_play is not None:
            background_key = self.group_play.stats_background_key(
                group_id,
                binding.qq_id,
            )
            member_badge = self.group_play.equipped_badge(group_id, binding.qq_id)
        return CommandResult(
            format_stats_snapshot(stats),
            stats=stats,
            stats_background_key=background_key,
            member_badge=member_badge,
        )

    def _resolve_steam_id(self, argument: str, *, group_id: str, qq_id: str) -> str:
        target = argument.strip()
        if not target:
            binding = self.repository.get_by_qq(group_id, qq_id)
            if binding is None:
                raise ValueError(f"你还没有绑定，请先发送 {self.prefix}绑定 <SteamID64>")
            steam_id = binding.steam_id
        elif steam_id := parse_steam_id(target):
            pass
        else:
            mention = _MENTION_RE.fullmatch(target)
            target_qq = mention.group(1) if mention else target
            if not _QQ_ID_RE.fullmatch(target_qq):
                raise ValueError("查询目标应为群成员 @、QQ 号或 SteamID64")
            binding = self.repository.get_by_qq(group_id, target_qq)
            if binding is None:
                raise ValueError("该成员还没有在本群绑定 Steam 账号")
            steam_id = binding.steam_id

        return steam_id

    async def _match_history_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        selected = parse_source_argument(argument)
        steam_id = self._resolve_steam_id(
            selected.argument,
            group_id=group_id,
            qq_id=qq_id,
        )
        self._enforce_query_cooldown(group_id, qq_id)
        if selected.source == "official":
            matches = await self.api.official_match_list(steam_id, page_size=10)
            profile = await resolve_game_profile(
                self.api, steam_id, matches, source="official", steam_presence=self.steam_presence,
            )
        else:
            matches = await self.api.match_list(steam_id, page_size=10)
            profile: dict[str, object] = {}
            try:
                profile = await self.api.detail_stats(steam_id)
            except PerfectWorldApiError:
                pass
        history = build_match_history(
            matches,
            steam_id=steam_id,
            profile=profile,
            source=selected.source,
        )
        if self.challenges is not None:
            try:
                for match in history.matches:
                    self.challenges.record_match(group_id, match)
            except Exception:
                # The challenge feature must not prevent the original stats reply.
                logger.exception("pwcs2: challenge history update failed group={}", group_id)
        background_key = "classic"
        member_badge = ""
        binding = self.repository.get_by_steam(group_id, steam_id)
        if binding is not None and self.group_play is not None:
            background_key = self.group_play.stats_background_key(group_id, binding.qq_id)
            member_badge = self.group_play.equipped_badge(group_id, binding.qq_id)
        return CommandResult(
            format_match_history(history),
            match_history=history,
            stats_background_key=background_key,
            member_badge=member_badge,
        )

    async def _watch_stage_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        if self.watch_stage is None:
            raise WatchStageError("观战功能尚未配置")
        selected = parse_source_argument(argument)
        steam_id = self._resolve_steam_id(
            selected.argument,
            group_id=group_id,
            qq_id=qq_id,
        )
        self._enforce_query_cooldown(group_id, qq_id)
        snapshot = await self.watch_stage.snapshot(steam_id)
        requested_name = next(
            (
                player.nickname for player in snapshot.players
                if player.steam_id == steam_id and player.nickname
            ),
            snapshot.requested_name or steam_id,
        )
        players = []
        for player in snapshot.players:
            binding = self.repository.get_by_steam(group_id, player.steam_id)
            if binding is None:
                players.append(player)
                continue
            players.append(
                replace(
                    player,
                    is_group_member=True,
                )
            )
        snapshot = replace(
            snapshot,
            requested_name=requested_name,
            players=tuple(players),
        )
        return CommandResult(
            format_watch_stage(snapshot),
            watch_stage=snapshot,
        )

    async def _who_is_playing_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        if self.watch_stage is None:
            raise WatchStageError("观战功能尚未配置")
        bindings = self.repository.list_group_bindings(group_id)
        if not bindings:
            raise ValueError("本群还没有已绑定的 Steam 账号")
        parse_source_argument(argument)
        self._enforce_query_cooldown(group_id, qq_id)
        semaphore = asyncio.Semaphore(4)

        async def fetch(binding: Binding) -> tuple[Binding, WatchStageSnapshot | None]:
            try:
                async with semaphore:
                    snapshot = await self.watch_stage.snapshot(binding.steam_id)
            except (WatchStageError, ValueError):
                snapshot = None
            return binding, snapshot

        results = await asyncio.gather(*(fetch(binding) for binding in bindings))
        failed = sum(snapshot is None for _binding, snapshot in results)
        if failed == len(results):
            raise WatchStageError("观将台查询失败，请稍后重试")

        memories = {
            memory.steam_id: memory.nickname
            for memory in self.repository.list_group_member_memories(group_id)
        }
        matches: dict[tuple[str, str], tuple[WatchStageSnapshot, list[str]]] = {}
        for binding, snapshot in results:
            if snapshot is None or snapshot.status != "running":
                continue
            if not any(
                player.steam_id == binding.steam_id
                for player in snapshot.players
            ):
                continue
            key = (
                snapshot.platform,
                snapshot.match_id or f"steam:{binding.steam_id}",
            )
            if key not in matches:
                matches[key] = (snapshot, [])
            matches[key][1].append(memories.get(binding.steam_id) or f"QQ {binding.qq_id}")

        if not matches:
            suffix = f"（另有 {failed} 人查询失败）" if failed else ""
            return CommandResult(f"🎮 本群当前没有检测到可观战对局{suffix}")

        lines = [f"🎮 本群当前有 {len(matches)} 场可观战对局"]
        for index, (snapshot, names) in enumerate(matches.values(), 1):
            state = "热身中" if snapshot.warmup else snapshot.duration or "进行中"
            source_name = "官匹" if snapshot.platform == "1" else "完美"
            lines.extend(
                (
                    f"{index}. [{source_name}] {display_map_name(snapshot.map_name)}｜"
                    f"CT {snapshot.ct_score or 0}:{snapshot.terrorist_score or 0} T｜{state}",
                    f"群友：{'、'.join(dict.fromkeys(names))}",
                    f"观战：{self.prefix}观战 {snapshot.requested_steam_id}",
                )
            )
        if failed:
            lines.append(f"另有 {failed} 名成员查询失败，已跳过")
        return CommandResult("\n".join(lines))

    async def _game_status_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        target = argument.strip()
        self._enforce_query_cooldown(group_id, qq_id)
        memories = {
            memory.steam_id: memory.nickname
            for memory in self.repository.list_group_member_memories(group_id)
        }
        if target:
            steam_id = self._resolve_steam_id(
                target,
                group_id=group_id,
                qq_id=qq_id,
            )
            if self.steam_presence is None or not self.steam_presence.configured:
                return await self._game_status_watch_stage_fallback(
                    (self.repository.get_by_steam(group_id, steam_id),),
                    memories=memories,
                    target_steam_id=steam_id,
                )
            try:
                summaries = await self.steam_presence.summaries((steam_id,))
            except SteamPresenceError:
                return await self._game_status_watch_stage_fallback(
                    (self.repository.get_by_steam(group_id, steam_id),),
                    memories=memories,
                    target_steam_id=steam_id,
                )
            presence = summaries.get(steam_id)
            binding = self.repository.get_by_steam(group_id, steam_id)
            name = (
                memories.get(steam_id)
                or (presence.persona_name if presence is not None else "")
                or (f"QQ {binding.qq_id}" if binding is not None else steam_id)
            )
            stale = bool(getattr(summaries, "stale", False))
            return CommandResult(
                format_player_game_status(
                    name,
                    presence,
                    stale=stale,
                ),
                game_status=build_player_game_status_snapshot(
                    name,
                    presence,
                    stale=stale,
                ),
            )

        bindings = self.repository.list_group_bindings(group_id)
        if not bindings:
            raise ValueError("本群还没有已绑定的 Steam 账号")
        if self.steam_presence is None or not self.steam_presence.configured:
            return await self._game_status_watch_stage_fallback(
                bindings,
                memories=memories,
            )
        try:
            summaries = await self.steam_presence.summaries(
                tuple(binding.steam_id for binding in bindings)
            )
        except SteamPresenceError:
            return await self._game_status_watch_stage_fallback(
                bindings,
                memories=memories,
            )
        entries = [
            (
                memories.get(binding.steam_id)
                or (
                    summaries[binding.steam_id].persona_name
                    if binding.steam_id in summaries
                    else ""
                )
                or f"QQ {binding.qq_id}",
                summaries[binding.steam_id],
            )
            for binding in bindings
            if binding.steam_id in summaries
        ]
        stale = bool(getattr(summaries, "stale", False))
        return CommandResult(
            format_group_game_status(entries, total_count=len(bindings), stale=stale),
            game_status=build_group_game_status_snapshot(
                entries,
                total_count=len(bindings),
                stale=stale,
            ),
        )

    async def _game_status_watch_stage_fallback(
        self,
        bindings: tuple[Binding | None, ...],
        *,
        memories: dict[str, str],
        target_steam_id: str = "",
    ) -> CommandResult:
        if self.watch_stage is None:
            raise SteamPresenceError("Steam 实时状态查询失败，且观将台降级查询尚未配置")

        valid_bindings = tuple(binding for binding in bindings if binding is not None)
        steam_ids = (
            (target_steam_id,)
            if target_steam_id
            else tuple(binding.steam_id for binding in valid_bindings)
        )
        semaphore = asyncio.Semaphore(4)

        async def fetch(steam_id: str) -> tuple[str, WatchStageSnapshot | None]:
            try:
                async with semaphore:
                    snapshot = await self.watch_stage.snapshot(steam_id)
            except (WatchStageError, ValueError):
                snapshot = None
            return steam_id, snapshot

        snapshots = await asyncio.gather(*(fetch(steam_id) for steam_id in steam_ids))
        binding_by_steam = {binding.steam_id: binding for binding in valid_bindings}

        def is_confirmed_running(
            steam_id: str,
            snapshot: WatchStageSnapshot | None,
        ) -> bool:
            return (
                snapshot is not None
                and snapshot.status == "running"
                and any(player.steam_id == steam_id for player in snapshot.players)
            )

        def display_name(steam_id: str) -> str:
            binding = binding_by_steam.get(steam_id)
            return memories.get(steam_id) or (
                f"QQ {binding.qq_id}" if binding is not None else steam_id
            )

        if target_steam_id:
            snapshot = snapshots[0][1] if snapshots else None
            name = display_name(target_steam_id)
            lines = [
                f"🎮 游戏状态（观将台降级）｜{name}",
                "⚠️ Steam 官方接口当前不可达",
            ]
            if is_confirmed_running(target_steam_id, snapshot):
                assert snapshot is not None
                state = "热身中" if snapshot.warmup else snapshot.duration or "进行中"
                lines.extend(
                    (
                        "🎯 观将台已检测到正在进行的 Counter-Strike 2 对局",
                        f"地图：{display_map_name(snapshot.map_name)}",
                        f"比分：CT {snapshot.ct_score or 0}:"
                        f"{snapshot.terrorist_score or 0} T｜{state}",
                    )
                )
                card_entry = GameStatusEntry(
                    name=name,
                    steam_id=target_steam_id,
                    status_label="正在游戏",
                    status_kind="playing",
                    game_name="Counter-Strike 2",
                    detail=(
                        f"{display_map_name(snapshot.map_name)} · "
                        f"CT {snapshot.ct_score or 0}:"
                        f"{snapshot.terrorist_score or 0} T · {state}"
                    ),
                    app_id="730",
                )
                note = "Steam 官方接口当前不可达，状态来自完美或官匹观将台"
            else:
                lines.append(
                    "观将台未检测到正在进行的 CS2 对局；其他游戏、离线或隐身状态暂无法确认"
                )
                card_entry = GameStatusEntry(
                    name=name,
                    steam_id=target_steam_id,
                    status_label="状态暂无法确认",
                    status_kind="unknown",
                )
                note = (
                    "Steam 官方接口当前不可达；观将台未检测到 CS2 对局，"
                    "无法判断其他游戏或隐身状态"
                )
            return CommandResult(
                "\n".join(lines),
                game_status=GameStatusSnapshot(
                    entries=(card_entry,),
                    total_count=1,
                    playing_count=int(
                        is_confirmed_running(target_steam_id, snapshot)
                    ),
                    hidden_count=int(
                        not is_confirmed_running(target_steam_id, snapshot)
                    ),
                    degraded=True,
                    title=f"{name} 的游戏状态",
                    source_label="CS2 · WATCH STAGE",
                    note=note,
                ),
            )

        running: list[tuple[str, WatchStageSnapshot]] = []
        for steam_id, snapshot in snapshots:
            if is_confirmed_running(steam_id, snapshot):
                assert snapshot is not None
                running.append((steam_id, snapshot))
        lines = [
            "🎮 游戏状态（观将台降级）",
            "⚠️ Steam 官方接口当前不可达，仅显示观将台确认的 CS2 对局",
        ]
        for steam_id, snapshot in running:
            state = "热身中" if snapshot.warmup else snapshot.duration or "进行中"
            lines.append(
                f"🎯 {display_name(steam_id)}｜Counter-Strike 2｜"
                f"{display_map_name(snapshot.map_name)}｜"
                f"CT {snapshot.ct_score or 0}:{snapshot.terrorist_score or 0} T｜"
                f"{state}"
            )
        if not running:
            lines.append("当前没有成员被观将台确认为正在进行 CS2 对局")
        unknown_count = len(steam_ids) - len(running)
        if unknown_count:
            lines.append(f"其余 {unknown_count} 人状态暂无法确认")
        card_entries = tuple(
            GameStatusEntry(
                name=display_name(steam_id),
                steam_id=steam_id,
                status_label="正在游戏",
                status_kind="playing",
                game_name="Counter-Strike 2",
                detail=(
                    f"{display_map_name(snapshot.map_name)} · "
                    f"CT {snapshot.ct_score or 0}:"
                    f"{snapshot.terrorist_score or 0} T · "
                    f"{'热身中' if snapshot.warmup else snapshot.duration or '进行中'}"
                ),
                app_id="730",
            )
            for steam_id, snapshot in running
        )
        return CommandResult(
            "\n".join(lines),
            game_status=GameStatusSnapshot(
                entries=card_entries,
                total_count=len(steam_ids),
                playing_count=len(running),
                hidden_count=unknown_count,
                degraded=True,
                source_label="CS2 · WATCH STAGE",
                note=(
                    "Steam 官方接口当前不可达，仅显示观将台确认的 CS2 对局"
                ),
            ),
        )

    async def _ranking_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        selected = parse_source_argument(argument)
        metric = parse_ranking_metric(
            selected.argument,
            source=selected.source,
        )
        bindings = self.repository.list_group_bindings(group_id)
        if not bindings:
            raise ValueError("本群还没有已绑定的 Steam 账号")
        self._enforce_query_cooldown(group_id, qq_id)

        semaphore = asyncio.Semaphore(4)

        async def fetch(binding: Binding) -> RankingEntry | None:
            try:
                async with semaphore:
                    if selected.source == "official":
                        matches = await self.api.official_match_list(
                            binding.steam_id,
                            page_size=50,
                        )
                        profile = (
                            await resolve_game_profile(
                                self.api, binding.steam_id, matches, source="official",
                                steam_presence=self.steam_presence,
                            ) if matches else {}
                        )
                    else:
                        data = await self.api.detail_stats_v2(binding.steam_id)
                if selected.source == "official":
                    return build_official_ranking_entry(
                        matches,
                        qq_id=binding.qq_id,
                        steam_id=binding.steam_id,
                        name=profile.get("name", ""),
                    )
                return build_ranking_entry(
                    data,
                    qq_id=binding.qq_id,
                    steam_id=binding.steam_id,
                )
            except (PerfectWorldApiError, ValueError):
                return None

        results = await asyncio.gather(*(fetch(binding) for binding in bindings))
        entries = [entry for entry in results if entry is not None]
        if not entries:
            source_name = "官匹" if selected.source == "official" else "完美扩展"
            raise PerfectWorldApiError(
                f"本群成员的{source_name}战绩暂时都没有有效数据，请稍后重试"
            )
        ranking = build_ranking_snapshot(
            entries,
            metric=metric,
            total_count=len(bindings),
            failed_count=len(bindings) - len(entries),
            source=selected.source,
        )
        return CommandResult(format_ranking_snapshot(ranking), ranking=ranking)

    async def _chemistry_result(
        self,
        argument: str,
        *,
        group_id: str,
        qq_id: str,
    ) -> CommandResult:
        selected = parse_source_argument(argument)
        first = self.repository.get_by_qq(group_id, qq_id)
        if first is None:
            raise ValueError(f"你还没有绑定，请先发送 {self.prefix}绑定 <SteamID64>")
        target = selected.argument
        if not target:
            raise ValueError(f"请指定一位群友，例如 {self.prefix}羁绊 @群友")
        if steam_id := parse_steam_id(target):
            second = self.repository.get_by_steam(group_id, steam_id)
        else:
            mention = _MENTION_RE.fullmatch(target)
            target_qq = mention.group(1) if mention else target
            if not _QQ_ID_RE.fullmatch(target_qq):
                raise ValueError("羁绊对象应为本群成员 @、QQ 号或已绑定的 SteamID64")
            second = self.repository.get_by_qq(group_id, target_qq)
        if second is None:
            raise ValueError("该群友还没有在本群绑定 Steam 账号")
        if first.qq_id == second.qq_id:
            raise ValueError("不能查询自己和自己的羁绊")
        self._enforce_query_cooldown(group_id, qq_id)
        fetch_matches = (
            self.api.official_match_list
            if selected.source == "official"
            else self.api.match_list
        )
        first_matches, second_matches = await asyncio.gather(
            fetch_matches(first.steam_id, page_size=50),
            fetch_matches(second.steam_id, page_size=50),
        )
        names = {
            item.qq_id: item.nickname
            for item in self.repository.list_group_member_memories(group_id)
        }
        snapshot = build_chemistry_snapshot(
            first_matches,
            second_matches,
            first_qq=first.qq_id,
            second_qq=second.qq_id,
            first_name=names.get(first.qq_id) or f"QQ {first.qq_id}",
            second_name=names.get(second.qq_id) or f"QQ {second.qq_id}",
            source=selected.source,
        )
        return CommandResult(
            format_chemistry_snapshot(snapshot),
            chemistry=snapshot,
        )

    def _enforce_query_cooldown(self, group_id: str, qq_id: str) -> None:
        if self.query_cooldown_seconds == 0:
            return
        now = self._clock()
        key = (group_id, qq_id)
        if previous := self._last_queries.get(key):
            remaining = self.query_cooldown_seconds - (now - previous)
            if remaining > 0:
                raise ValueError(f"查询过于频繁，请 {math.ceil(remaining)} 秒后重试")
        self._last_queries[key] = now

        if len(self._last_queries) > 4096:
            cutoff = now - self.query_cooldown_seconds
            self._last_queries = {
                item_key: timestamp
                for item_key, timestamp in self._last_queries.items()
                if timestamp >= cutoff
            }

    def interaction_help(self) -> str:
        return (
            "🎮 牛牛互动菜单\n"
            f"{self.prefix}挑战 [gp] [昨天]　查看每日战绩挑战\n"
            f"{self.prefix}挑战 [gp] [昨天] 领取　领取达标牛币，完美/官匹分开\n"
            f"{self.prefix}发车 内战 10人 → {self.prefix}上车 <编号> → "
            f"{self.prefix}分队 <编号>　车内成员均可分队\n"
            f"{self.prefix}投票 发起 今晚玩什么 | CS2 | 瓦 | 休息　默认 30 分钟\n"
            f"{self.prefix}投票 <编号> A　参与或改票；结果/结束 <编号> 查看或收票\n"
            f"{self.prefix}签到 / {self.prefix}商店 / {self.prefix}特惠 / "
            f"{self.prefix}赠送 @群友 道具　牛币与收藏\n"
            "新玩法按需回复，投票不押币，不自动刷结果；分队仅提供建议。"
        )

    def help_text(self) -> str:
        prediction_help = ""
        if self.group_play is not None and self.group_play.prediction_enabled:
            prediction_help = (
                f"{self.prefix}竞猜 / {self.prefix}下注 <选项> <牛币>　参与群内竞猜\n"
                f"{self.prefix}发起竞猜 <题目|选项...> / "
                f"{self.prefix}开奖 <选项>　授权管理员操作\n"
                "机器人检测到群友进入 CS2 服务器后自动发起，并在完赛后开奖\n"
            )
        return (
            "CS2 战绩机器人命令\n"
            f"{self.prefix}互动　每日挑战、群内投票、车队分队速查\n"
            f"{self.prefix}绑定 <SteamID64>　绑定/更换账号\n"
            f"{self.prefix}瓦绑定 <昵称#数字ID>　绑定/更换无畏契约国服账号\n"
            f"{self.prefix}瓦战绩 [@群友/QQ号/昵称#数字ID]　查询国服近十场排位\n"
            f"{self.prefix}我的瓦绑定 / {self.prefix}瓦解绑　查看或解除瓦绑定\n"
            "瓦战绩来自第三方非官方公开查询，仅在发送命令时按需读取\n"
            "数据源：默认完美；命令参数加 gp 查询官匹\n"
            f"{self.prefix}数据 [gp] [@群友/QQ号/SteamID64]　查询数据总览\n"
            f"{self.prefix}战绩 [gp] [@群友/QQ号/SteamID64]　查询近十场比赛\n"
            f"{self.prefix}观战 [@群友/QQ号/SteamID64]　查看实时记分板\n"
            f"{self.prefix}谁在打　扫描本群当前可观战对局\n"
            "观战和谁在打会自动识别完美或官匹\n"
            f"{self.prefix}游戏状态 [@群友/QQ号/SteamID64]　查看群友正在玩的 Steam 游戏\n"
            f"{self.prefix}排行 [gp] [指标]　完美或官匹群内排行榜\n"
            f"{self.prefix}发车 [车名] [2-10人] [时间]　创建一辆开黑车\n"
            f"{self.prefix}车队 [编号/车名]　列出所有车或查看详情\n"
            f"{self.prefix}上车 / {self.prefix}下车 / {self.prefix}散车 [编号/车名]　管理席位\n"
            f"{self.prefix}羁绊 [gp] @群友　统计双方最近 50 场共同比赛\n"
            f"{self.prefix}关闭锐评 / {self.prefix}开启锐评　设置比赛图是否显示 AI 点评\n"
            f"{prediction_help}"
            f"{self.prefix}牛币 / {self.prefix}商店 / {self.prefix}背包　查看牛币玩法\n"
            f"{self.prefix}特惠 / {self.prefix}赠送 @群友 <道具> [数量]　特惠与赠礼\n"
            f"{self.prefix}称号 / {self.prefix}佩戴称号 <编号>　永久收藏、免费换装\n"
            f"{self.prefix}补签　使用补签卡修复昨日断签（先补签再签到）\n"
            f"{self.prefix}签到 / {self.prefix}牛币获取　领取每日奖励或查看获取方式\n"
            f"{self.prefix}牛币流水 / {self.prefix}打赏 @群友 <牛币>　查看或流通牛币\n"
            f"{self.prefix}提示 / {self.prefix}使用称号 <称号>　使用商城主动道具\n"
            f"{self.prefix}战绩背景 [编号/名称]　查看或切换已解锁的数据卡背景\n"
            f"{self.prefix}选手 <A/B/C/D>　回答每日 CS 职业选手冷知识\n"
            f"{self.prefix}黑历史 [@群友/编号]　随机参观黑历史博物馆\n"
            f"{self.prefix}收录黑历史 [备注]　回复群友消息后收录纯文本\n"
            f"{self.prefix}复仇　查看群友对位复仇榜\n"
            f"{self.prefix}改名 @群友 <新名字>　群主或管理员修改并锁定群名片\n"
            f"{self.prefix}记住 <内容>　添加本群 AI 长期记忆（每人最多 5 条）\n"
            f"{self.prefix}记忆 [页码] / {self.prefix}忘记 <编号/全部>　查看或删除自己的记忆\n"
            f"{self.prefix}训练营　打开 CS2 网页练枪小游戏\n"
            f"{self.prefix}训练文件　发送可离线运行的 HTML 原文件\n"
            f"{self.prefix}ts　查看 TS3 在线成员和所在频道\n"
            f"{self.prefix}饰品添加 <中文关键词/英文名/Steam链接>　加入每日价格关注\n"
            f"{self.prefix}饰品删除 <编号/完整名>　删除自己添加的饰品\n"
            f"{self.prefix}饰品列表　查看本群关注列表\n"
            f"{self.prefix}饰品价格 [中文关键词/英文名/Steam链接]　查询当前价格\n"
            f"{self.prefix}我的绑定　查看当前绑定\n"
            f"{self.prefix}解绑　删除绑定\n"
            "@机器人 <问题>　与 AI 对话\n"
            f"{self.prefix}ai <问题>　与 AI 对话（发送“#ai 清空”重置上下文）"
        )


def parse_steam_id(value: str) -> str | None:
    """Accept a SteamID64 or a steamcommunity.com/profiles/<SteamID64> URL."""
    candidate = value.strip()
    if _STEAM_ID_RE.fullmatch(candidate):
        return candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"}:
        return None
    hostname = (parsed.hostname or "").lower()
    if hostname not in {"steamcommunity.com", "www.steamcommunity.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 2 and parts[0].lower() == "profiles" and _STEAM_ID_RE.fullmatch(parts[1]):
        return parts[1]
    return None
