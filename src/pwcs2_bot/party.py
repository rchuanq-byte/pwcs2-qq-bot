"""Persistent multi-lobby group game parties with fuzzy natural-language parsing."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime, timedelta, timezone
from itertools import combinations
from statistics import median

from pwcs2_bot.database import BindingRepository, GameParty

_QUALIFIER = (
    r"明天晚上|明天早上|明天下午|明天上午|明天中午|"
    r"后天晚上|后天早上|后天下午|后天上午|后天中午|"
    r"明晚|今晚|明天|后天|今天|晚上|傍晚|下午|中午|上午|早上|凌晨"
)
_HOUR_TOKEN = r"(?:[01]?\d|2[0-3]|[零〇一二两三四五六七八九十]{1,3})"
_MINUTE_TOKEN = r"(?:[0-5]?\d|[零〇一二两三四五六七八九十]{1,3})"
_COLON_TIME_RE = re.compile(
    rf"(?<!\d)(?P<qual>{_QUALIFIER})?\s*"
    r"(?P<hour>[01]?\d|2[0-3])\s*[:：]\s*(?P<minute>[0-5]\d)"
    r"(?!\d)"
)
_POINT_TIME_RE = re.compile(
    rf"(?<!\d)(?P<qual>{_QUALIFIER})?\s*"
    rf"(?P<hour>{_HOUR_TOKEN})\s*(?:点|时)"
    rf"(?P<minute>半|{_MINUTE_TOKEN}\s*分?)?"
)
_TIME_LIKE_RE = re.compile(
    rf"(?<!\d)\d{{1,2}}\s*[:：]\s*\d{{1,2}}(?!\d)|"
    rf"(?<!\d){_HOUR_TOKEN}\s*(?:点|时)"
)
_VS_CAPACITY_RE = re.compile(
    r"(?<!\d)(?P<count>[1-9])\s*[vV](?:[sS])?\s*(?P<opponents>[1-9])(?!\d)"
)
_LABELED_CAPACITY_RE = re.compile(
    r"(?<!\d)(?P<count>\d{1,2}|[一二两三四五六七八九十])\s*"
    r"(?:个人|人|个|黑|排)(?:车|队)?"
)
_BARE_CAPACITY_RE = re.compile(r"(?<!\d)(?P<count>\d{1,2})(?!\d)")
_NAME_LABEL_RE = re.compile(r"(?:车名|名字)\s*(?:叫|是|为|[:：])?\s*")
_SELECTOR_NUMBER_RE = re.compile(r"^#?(\d+)$")
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINA_TIME = timezone(timedelta(hours=8))
_BALANCE_SCORE_MAX_AGE = timedelta(days=7)


class PartyService:
    def __init__(
        self,
        repository: BindingRepository,
        *,
        teamspeak_address: str,
        expiration_hours: int = 6,
    ) -> None:
        self.repository = repository
        self.teamspeak_address = teamspeak_address.strip()
        self.expiration_hours = expiration_hours

    def create(self, group_id: str, qq_id: str, argument: str) -> str:
        self._cleanup()
        capacity, scheduled_for, requested_name = self._parse_create_argument(argument)
        name = requested_name or self._next_default_name(group_id, qq_id)
        party = self.repository.create_party(
            group_id,
            qq_id,
            self._nickname(group_id, qq_id),
            name=name,
            capacity=capacity,
            scheduled_for=scheduled_for,
        )
        return "开黑车队创建成功！\n" + self.format_party(party)

    def join(self, group_id: str, qq_id: str, selector: str = "") -> str:
        self._cleanup()
        party = self._resolve_party(group_id, selector, action="上车")
        party, joined = self.repository.join_party(
            group_id,
            qq_id,
            self._nickname(group_id, qq_id),
            party_id=party.party_id,
        )
        prefix = "上车成功！" if joined else "你已经在这辆车上了。"
        if len(party.members) >= party.capacity:
            prefix += "\n🚀 人齐了，可以开车！"
        return prefix + "\n" + self.format_party(party)

    def leave(self, group_id: str, qq_id: str, selector: str = "") -> str:
        self._cleanup()
        party = self._resolve_member_party(
            group_id,
            qq_id,
            selector,
            owner_only=False,
            action="下车",
        )
        remaining = self.repository.leave_party(
            group_id,
            qq_id,
            party_id=party.party_id,
        )
        if remaining is None:
            return f"你已从 #{party.party_id}「{party.name}」下车，空车已自动解散。"
        return "你已下车。\n" + self.format_party(remaining)

    def status(self, group_id: str, selector: str = "") -> str:
        self._cleanup()
        parties = self.repository.list_parties(group_id)
        if not parties:
            return (
                "本群当前没有车队，发送 #发车 [车名] [人数] [时间] 创建一辆吧。\n"
                "例如：#发车 荒漠迷城五人车 晚上八点"
            )
        if selector.strip():
            return self.format_party(
                self._resolve_party(group_id, selector, action="查看")
            )
        if len(parties) == 1:
            return self.format_party(parties[0])
        lines = [f"🚗 本群当前有 {len(parties)} 辆车："]
        for party in parties:
            count = len(party.members)
            status = "满员" if count >= party.capacity else f"差 {party.capacity - count} 人"
            lines.append(
                f"#{party.party_id}｜{party.name}｜{count}/{party.capacity}（{status}）"
                f"｜{party.scheduled_for or '人齐即开'}"
            )
        lines.append("发送 #车队 <编号/车名> 查看详情，#上车 <编号/车名> 加入。")
        return "\n".join(lines)

    def disband(self, group_id: str, qq_id: str, selector: str = "") -> str:
        self._cleanup()
        party = self._resolve_member_party(
            group_id,
            qq_id,
            selector,
            owner_only=True,
            action="散车",
        )
        self.repository.disband_party(
            group_id,
            qq_id,
            party_id=party.party_id,
        )
        return f"#{party.party_id}「{party.name}」已解散。"

    def balance(self, group_id: str, qq_id: str, selector: str = "") -> str:
        """Suggest two teams from this party without changing seats or fetching data."""
        self._cleanup()
        party = self._resolve_member_party(
            group_id, qq_id, selector, owner_only=False, action="分队",
        )
        member_ids = [member.qq_id for member in party.members]
        if len(set(member_ids)) != len(member_ids):
            raise ValueError("车队成员数据有重复，请重新整理车队后再分队")
        if not 2 <= len(member_ids) <= 10 or len(member_ids) % 2:
            raise ValueError("分队需要 2～10 名已上车成员，且人数必须为偶数")

        # Stable hash order supplies the no-data shuffle and deterministic tie-breaking.
        seed = f"{group_id}:{party.party_id}:" + ",".join(sorted(member_ids))
        members = sorted(
            party.members,
            key=lambda member: hashlib.sha256(f"{seed}:{member.qq_id}".encode()).digest(),
        )
        memories = {
            member.qq_id: member
            for member in self.repository.list_group_member_memories(group_id)
        }
        now = datetime.now(UTC)
        known: dict[str, tuple[float, datetime]] = {}
        for member in members:
            memory = memories.get(member.qq_id)
            if memory is None or not memory.steam_id:
                continue
            # The shared stats_updated_at can be refreshed by official-match data.
            # Only a Perfect match's own end time can establish this score's age.
            cached = memory.stats.get("last_match")
            if not isinstance(cached, dict) or cached.get("source") != "perfect":
                continue
            value, ended_at = cached.get("elo"), cached.get("ended_at")
            if isinstance(value, bool) or not isinstance(ended_at, str):
                continue
            try:
                score = float(value)
                timestamp = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=_CHINA_TIME)
            age = now - timestamp
            if (
                not math.isfinite(score)
                or not 0 < score <= 10_000
                or age < timedelta(0)
                or age > _BALANCE_SCORE_MAX_AGE
            ):
                continue
            known[member.qq_id] = (score, timestamp)

        half = len(members) // 2
        estimates: dict[str, float] = {}
        if known:
            fallback = float(median(score for score, _timestamp in known.values()))
            estimates = {
                member.qq_id: known.get(member.qq_id, (fallback, now))[0]
                for member in members
            }
            total = sum(estimates.values())
            # Fix one member in A to avoid enumerating each partition twice.
            first_team = min(
                ((0, *rest) for rest in combinations(range(1, len(members)), half - 1)),
                key=lambda indices: abs(
                    2 * sum(estimates[members[index].qq_id] for index in indices) - total
                ),
            )
        else:
            first_team = tuple(range(half))
        first_indices = set(first_team)
        teams = (
            [member for index, member in enumerate(members) if index in first_indices],
            [member for index, member in enumerate(members) if index not in first_indices],
        )
        lines = [f"⚔️ #{party.party_id}「{party.name}」分队建议｜{half} v {half}"]
        if known:
            lines.append("依据：本地 7 天内完美对局分数缓存；按两队总分差最小均分。")
            if len(known) < len(members):
                lines.append(f"缺失或过期分数按已知中位数 {fallback:g} 估算（标 *）。")
            for label, team in zip(("A 队", "B 队"), teams, strict=True):
                team_total = sum(estimates[member.qq_id] for member in team)
                lines.append(f"{label}｜参考总分 {team_total:g}")
                for member in team:
                    if member.qq_id in known:
                        score, timestamp = known[member.qq_id]
                        time_label = timestamp.astimezone(_CHINA_TIME).strftime("%m-%d %H:%M")
                        suffix = f"{score:g}｜对局缓存 {time_label}"
                    else:
                        suffix = f"{fallback:g}*（估算）"
                    lines.append(f"• {member.nickname}（QQ {member.qq_id}）｜{suffix}")
            difference = abs(
                sum(estimates[member.qq_id] for member in teams[0])
                - sum(estimates[member.qq_id] for member in teams[1])
            )
            lines.append(f"参考总分差：{difference:g}；时间为北京时间，缓存分数不代表实时实力。")
        else:
            lines.append("无可信完美分数缓存：使用稳定随机均分，不代表实力平衡。")
            for label, team in zip(("A 队", "B 队"), teams, strict=True):
                lines.append(f"{label}：" + "、".join(
                    f"{member.nickname}（QQ {member.qq_id}）" for member in team
                ))
        lines.append("仅提供分队建议；未改动车队席位。")
        return "\n".join(lines)

    def format_party(self, party: GameParty) -> str:
        count = len(party.members)
        remaining = max(0, party.capacity - count)
        status = "已满员" if remaining == 0 else f"还差 {remaining} 人"
        members = []
        for index, member in enumerate(party.members, start=1):
            owner = "（车主）" if member.qq_id == party.owner_qq else ""
            members.append(f"{index}. {member.nickname}{owner}")
        for index in range(count + 1, party.capacity + 1):
            members.append(f"{index}. 等待上车…")
        lines = [
            f"🎮 #{party.party_id}｜{party.name}｜{count}/{party.capacity}｜{status}",
            f"开打时间：{party.scheduled_for or '人齐即开'}",
            *members,
        ]
        if self.teamspeak_address:
            lines.append(f"TS：{self.teamspeak_address}")
        if remaining:
            lines.append(
                f"发送 #上车 {party.party_id} 加入，"
                f"#下车 {party.party_id} 退出，#车队 {party.party_id} 查看。"
            )
        else:
            lines.append("全员集合，开冲！")
        return "\n".join(lines)

    def _resolve_party(
        self,
        group_id: str,
        selector: str,
        *,
        action: str,
    ) -> GameParty:
        parties = self.repository.list_parties(group_id)
        if not parties:
            raise ValueError("本群当前没有车队，请先发送 #发车")
        query = selector.strip()
        if not query:
            if len(parties) == 1:
                return parties[0]
            raise ValueError(
                f"本群有多辆车，请发送 #{action} <编号/车名>；可先用 #车队 查看"
            )
        return self._match_selector(parties, query)

    def _resolve_member_party(
        self,
        group_id: str,
        qq_id: str,
        selector: str,
        *,
        owner_only: bool,
        action: str,
    ) -> GameParty:
        parties = self.repository.list_parties(group_id)
        if not parties:
            raise ValueError("本群当前没有车队")
        query = selector.strip()
        if query:
            party = self._match_selector(parties, query)
            if owner_only and party.owner_qq != qq_id:
                raise ValueError("只有车主可以解散车队")
            if not owner_only and qq_id not in {member.qq_id for member in party.members}:
                raise ValueError("你不在这辆车上")
            return party
        candidates = [
            party
            for party in parties
            if (
                party.owner_qq == qq_id
                if owner_only
                else qq_id in {member.qq_id for member in party.members}
            )
        ]
        if not candidates:
            raise ValueError("只有车主可以解散车队" if owner_only else "你还没有上车")
        if len(candidates) > 1:
            raise ValueError(
                f"你关联了多辆车，请发送 #{action} <编号/车名>；可先用 #车队 查看"
            )
        return candidates[0]

    @staticmethod
    def _match_selector(parties: tuple[GameParty, ...], selector: str) -> GameParty:
        number = _SELECTOR_NUMBER_RE.fullmatch(selector)
        if number:
            party_id = int(number.group(1))
            for party in parties:
                if party.party_id == party_id:
                    return party
            raise ValueError(f"没有找到编号为 #{party_id} 的车")

        normalized = PartyService._normalize_name(selector)
        exact = [
            party
            for party in parties
            if PartyService._normalize_name(party.name) == normalized
        ]
        if len(exact) == 1:
            return exact[0]
        fuzzy = [
            party
            for party in parties
            if normalized in PartyService._normalize_name(party.name)
            or PartyService._normalize_name(party.name) in normalized
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if not fuzzy:
            raise ValueError(f"没有找到名称包含“{selector}”的车")
        matches = "、".join(f"#{party.party_id} {party.name}" for party in fuzzy)
        raise ValueError(f"车名“{selector}”匹配到多辆车：{matches}，请改用编号")

    def _next_default_name(self, group_id: str, qq_id: str) -> str:
        nickname = self._nickname(group_id, qq_id)
        base = f"{nickname[:20]}的车"
        existing = {
            self._normalize_name(party.name)
            for party in self.repository.list_parties(group_id)
        }
        if self._normalize_name(base) not in existing:
            return base
        index = 2
        while self._normalize_name(f"{base}{index}") in existing:
            index += 1
        return f"{base}{index}"

    def _nickname(self, group_id: str, qq_id: str) -> str:
        for member in self.repository.list_group_member_memories(group_id):
            if member.qq_id == qq_id:
                return member.nickname or f"QQ {qq_id}"
        return f"QQ {qq_id}"

    def _cleanup(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=self.expiration_hours)
        self.repository.delete_parties_updated_before(cutoff.isoformat(timespec="seconds"))

    @staticmethod
    def _parse_create_argument(argument: str) -> tuple[int, str, str]:
        raw = " ".join(argument.replace("|", " ").split()).strip()
        if not raw:
            return 5, "", ""

        time_match = _COLON_TIME_RE.search(raw) or _POINT_TIME_RE.search(raw)
        scheduled_for = ""
        spans: list[tuple[int, int]] = []
        if time_match is not None:
            scheduled_for = PartyService._normalize_time(time_match)
            spans.append(time_match.span())
            remaining_time_text = PartyService._remove_spans(raw, spans)
            if _TIME_LIKE_RE.search(remaining_time_text):
                raise ValueError("检测到多个开打时间，请只写一次")
        elif _TIME_LIKE_RE.search(raw):
            raise ValueError("开打时间不正确")

        capacity_matches: list[tuple[int, tuple[int, int]]] = []
        occupied = spans.copy()
        for pattern in (_VS_CAPACITY_RE, _LABELED_CAPACITY_RE):
            for match in pattern.finditer(raw):
                if PartyService._overlaps(match.span(), occupied):
                    continue
                count = PartyService._parse_number(match.group("count"))
                if pattern is _VS_CAPACITY_RE:
                    count += int(match.group("opponents"))
                capacity_matches.append((count, match.span()))
                occupied.append(match.span())
        if not capacity_matches:
            for match in _BARE_CAPACITY_RE.finditer(raw):
                if PartyService._overlaps(match.span(), occupied):
                    continue
                capacity_matches.append((int(match.group("count")), match.span()))

        capacities = {count for count, _span in capacity_matches}
        if len(capacities) > 1:
            raise ValueError("检测到多个人数，请只写一次，例如“五人”或“5人”")
        capacity = next(iter(capacities), 5)
        if not 2 <= capacity <= 10:
            raise ValueError("车队人数应为 2 到 10 人")
        spans.extend(span for _count, span in capacity_matches)

        name = PartyService._remove_spans(raw, spans)
        name = _NAME_LABEL_RE.sub("", name)
        name = re.sub(r"^(?:来|组|建|开|搞|整|约)\s*(?:一辆|一个)?\s*", "", name)
        name = re.sub(r"^(?:一辆|一个)\s*", "", name)
        name = re.sub(r"\s*(?:开黑|发车)$", "", name)
        if capacity_matches and name in {"车", "车队", "的车", "的车队"}:
            name = ""
        name = name.strip(" ，,。.!！?？:：-_")
        name = " ".join(name.split())
        if len(name) > 24:
            raise ValueError("车名最多 24 个字符")
        return capacity, scheduled_for, name

    @staticmethod
    def _normalize_time(match: re.Match[str]) -> str:
        qualifier = str(match.groupdict().get("qual") or "")
        hour = PartyService._parse_number(match.group("hour"))
        minute_value = str(match.group("minute") or "")
        if minute_value == "半":
            minute = 30
        elif minute_value:
            minute = PartyService._parse_number(minute_value.replace("分", "").strip())
        else:
            minute = 0
        if hour > 23 or minute > 59:
            raise ValueError("开打时间不正确")

        if "凌晨" in qualifier and hour == 12:
            hour = 0
        elif any(
            token in qualifier
            for token in ("晚上", "傍晚", "下午", "今晚", "明晚")
        ):
            if 1 <= hour < 12:
                hour += 12
        elif "中午" in qualifier and 1 <= hour < 11:
            hour += 12
        elif not qualifier and 1 <= hour <= 11 and ":" not in match.group(0):
            hour += 12

        day = ""
        if "后天" in qualifier:
            day = "后天 "
        elif "明天" in qualifier or "明晚" in qualifier:
            day = "明天 "
        return f"{day}{hour:02d}:{minute:02d}"

    @staticmethod
    def _parse_number(value: str) -> int:
        normalized = value.strip()
        if normalized.isdecimal():
            return int(normalized)
        normalized = normalized.replace("两", "二")
        if normalized == "十":
            return 10
        if "十" in normalized:
            left, right = normalized.split("十", 1)
            tens = _CHINESE_DIGITS.get(left, 1) if left else 1
            ones = _CHINESE_DIGITS.get(right, 0) if right else 0
            return tens * 10 + ones
        if len(normalized) == 1 and normalized in _CHINESE_DIGITS:
            return _CHINESE_DIGITS[normalized]
        raise ValueError(f"无法识别数字：{value}")

    @staticmethod
    def _remove_spans(value: str, spans: list[tuple[int, int]]) -> str:
        characters = list(value)
        for start, end in spans:
            characters[start:end] = " " * (end - start)
        return " ".join("".join(characters).split())

    @staticmethod
    def _overlaps(
        span: tuple[int, int],
        occupied: list[tuple[int, int]],
    ) -> bool:
        return any(span[0] < end and start < span[1] for start, end in occupied)

    @staticmethod
    def _normalize_name(value: str) -> str:
        return "".join(value.casefold().split()).strip("#")
