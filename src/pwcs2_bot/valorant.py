"""Experimental public Chinese VALORANT ranked-statistics integration."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

_VALORANT_ID_RE = re.compile(r"^([^#\r\n]{1,32})#(\d{3,8})$")
_WIN_LOSS_RE = re.compile(
    r"^\d+\s*胜\s+\d+\s*负(?:\s+\d+\s*平)?$"
)
_SCORE_RE = re.compile(r"^\d{1,2}:\d{1,2}$")
_KDA_RE = re.compile(r"^\d{1,3}\s*/\s*\d{1,3}\s*/\s*\d{1,3}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_PERCENT_RE = re.compile(r"^(?:100(?:\.0+)?|\d{1,2}(?:\.\d+)?)%$")
_DECIMAL_RE = re.compile(r"^\d+(?:\.\d+)?$")
_RR_RE = re.compile(r"^(?:净增\s*)?[+\-−]?\d+(?:\s*RR)?$", re.IGNORECASE)
_DEFAULT_QUERY_URL = "https://wgval.cloud/public-query"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ValorantQueryError(RuntimeError):
    """Raised when the community data source cannot complete a query."""


class ValorantPlayerNotFound(ValorantQueryError):
    """Raised when a syntactically valid Riot ID has no public ranked result."""


class ValorantParseError(ValorantQueryError):
    """Raised when the community result page no longer matches its public contract."""


@dataclass(frozen=True, slots=True)
class ValorantMatch:
    agent: str
    result: str
    score: str
    kda: str
    acs: str
    map_name: str
    rank: str
    rr_change: str
    ended_at: str
    duration: str
    highlight: str = ""


CacheStatus = Literal["live", "fresh", "stale"]


@dataclass(frozen=True, slots=True)
class ValorantSnapshot:
    riot_id: str
    rank: str
    record: str
    win_rate: str
    kda: str
    acs: str
    rr_change: str
    source_updated_at: str
    fetched_at: str
    matches: tuple[ValorantMatch, ...]
    cache_status: CacheStatus = "live"
    cache_note: str = ""


def parse_valorant_id(value: str) -> str | None:
    """Normalize a Chinese VALORANT ``nickname#numeric-tag`` identifier."""
    candidate = unicodedata.normalize("NFKC", value).strip()
    matched = _VALORANT_ID_RE.fullmatch(candidate)
    if matched is None:
        return None
    nickname = " ".join(matched.group(1).split()).strip()
    if not nickname or nickname.startswith(".") or nickname.endswith("."):
        return None
    if any(unicodedata.category(character).startswith("C") for character in nickname):
        return None
    return f"{nickname}#{matched.group(2)}"


def valorant_id_key(value: str) -> str:
    normalized = parse_valorant_id(value)
    if normalized is None:
        raise ValueError("invalid VALORANT Riot ID")
    return normalized.casefold()


@dataclass(slots=True)
class _HtmlNode:
    tag: str
    attrs: dict[str, str]
    children: list[_HtmlNode | str]


_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
_SKIPPED_TEXT_TAGS = {"script", "style", "svg", "noscript", "template"}


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("#document", {}, [])
        self._stack = [self.root]
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        node = _HtmlNode(
            normalized_tag,
            {
                key.casefold(): str(value or "")
                for key, value in attrs
                if key
            },
            [],
        )
        self._stack[-1].children.append(node)
        if normalized_tag in _SKIPPED_TEXT_TAGS:
            self._skip_depth += 1
        if normalized_tag not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        self._stack[-1].children.append(
            _HtmlNode(
                normalized_tag,
                {
                    key.casefold(): str(value or "")
                    for key, value in attrs
                    if key
                },
                [],
            )
        )

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _SKIPPED_TEXT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == normalized_tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if data:
            self._stack[-1].children.append(data)


def _node_classes(node: _HtmlNode) -> frozenset[str]:
    return frozenset(node.attrs.get("class", "").split())


def _iter_nodes(node: _HtmlNode):
    for child in node.children:
        if not isinstance(child, _HtmlNode):
            continue
        yield child
        yield from _iter_nodes(child)


def _find_all(
    node: _HtmlNode,
    *,
    tag: str | None = None,
    class_name: str | None = None,
    id_value: str | None = None,
) -> list[_HtmlNode]:
    normalized_tag = tag.casefold() if tag else None
    return [
        candidate
        for candidate in _iter_nodes(node)
        if (normalized_tag is None or candidate.tag == normalized_tag)
        and (class_name is None or class_name in _node_classes(candidate))
        and (id_value is None or candidate.attrs.get("id") == id_value)
    ]


def _find_first(
    node: _HtmlNode,
    *,
    tag: str | None = None,
    class_name: str | None = None,
    id_value: str | None = None,
) -> _HtmlNode | None:
    matches = _find_all(
        node,
        tag=tag,
        class_name=class_name,
        id_value=id_value,
    )
    return matches[0] if matches else None


def _direct_children(
    node: _HtmlNode,
    *,
    tag: str | None = None,
) -> list[_HtmlNode]:
    normalized_tag = tag.casefold() if tag else None
    return [
        child
        for child in node.children
        if isinstance(child, _HtmlNode)
        and (normalized_tag is None or child.tag == normalized_tag)
    ]


def _node_text(node: _HtmlNode) -> str:
    pieces: list[str] = []

    def visit(current: _HtmlNode) -> None:
        if current.tag in _SKIPPED_TEXT_TAGS:
            return
        for child in current.children:
            if isinstance(child, str):
                pieces.append(child)
            else:
                visit(child)

    visit(node)
    return " ".join(" ".join(pieces).split()).strip()


def _required_descendant(
    node: _HtmlNode,
    class_name: str,
    *,
    field: str,
) -> _HtmlNode:
    match = _find_first(node, class_name=class_name)
    if match is None:
        raise ValorantParseError(f"战绩页面缺少{field}")
    return match


def _required_direct_text(node: _HtmlNode, tag: str, *, field: str) -> str:
    children = _direct_children(node, tag=tag)
    value = _node_text(children[0]) if children else ""
    if not value:
        raise ValorantParseError(f"战绩页面缺少{field}")
    return value


def _metric_key(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _safe_source_note(body: _HtmlNode) -> str:
    location = _find_first(body, class_name="result-location")
    if location is None:
        return ""
    value = _node_text(location)
    marker = re.search(r"IP\s*属地", value, flags=re.IGNORECASE)
    if marker is not None:
        value = value[: marker.start()].rstrip(" ·|｜-—")
    return value[:100] if "IP属地" not in value else ""


def _raise_for_query_alert(root: _HtmlNode, normalized_id: str) -> None:
    for message in _find_all(root, class_name="query-message"):
        if message.attrs.get("role", "").casefold() != "alert":
            continue
        text = _node_text(message)
        if any(
            marker in text
            for marker in (
                "没有找到该玩家",
                "未找到玩家",
                "没有找到玩家",
                "玩家不存在",
                "暂无该玩家",
                "查询不到该玩家",
            )
        ):
            raise ValorantPlayerNotFound(
                f"没有查到 {normalized_id} 的国服排位战绩"
            )
        if "请输入" in text and ("昵称#ID" in text or "昵称 # ID" in text):
            raise ValorantParseError("国服战绩源拒绝了查询参数")
        # Never reflect the upstream body: it can contain diagnostic codes or
        # location information that does not belong in a QQ group response.
        raise ValorantQueryError("国服战绩源暂不可用，请稍后重试")


def _parse_match(card: _HtmlNode) -> ValorantMatch:
    agent_box = _required_descendant(card, "match-agent", field="英雄")
    outcome_box = _required_descendant(card, "match-outcome", field="比分")
    kda_box = _required_descendant(card, "match-kda", field="K/D/A")
    acs_box = _required_descendant(card, "match-acs", field="ACS")
    map_box = _required_descendant(card, "match-map", field="地图")
    rank_box = _required_descendant(card, "match-rank", field="段位和 RR")
    time_box = _required_descendant(card, "match-time", field="比赛时间")
    result_box = _required_descendant(card, "match-result", field="胜负结果")

    agent = _required_direct_text(agent_box, "span", field="英雄")
    result = _node_text(result_box)
    score = _required_direct_text(outcome_box, "strong", field="比分")
    kda = _required_direct_text(kda_box, "strong", field="K/D/A")
    acs = _required_direct_text(acs_box, "strong", field="ACS")
    map_name = _required_direct_text(map_box, "strong", field="地图")
    rank = _required_direct_text(rank_box, "strong", field="段位")
    rr_change = _required_direct_text(rank_box, "b", field="RR")
    date_value = _required_direct_text(time_box, "strong", field="比赛日期")
    time_parts = [
        _node_text(item)
        for item in _direct_children(time_box, tag="span")
        if _node_text(item)
    ]
    if len(time_parts) < 2:
        raise ValorantParseError("战绩页面缺少比赛时刻或时长")
    time_value, duration = time_parts[:2]

    normalized_score = unicodedata.normalize("NFKC", score).replace("：", ":")
    normalized_kda = unicodedata.normalize("NFKC", kda)
    normalized_rr = unicodedata.normalize("NFKC", rr_change).replace("−", "-")
    if result not in {"胜利", "失败", "平局", "-"}:
        raise ValorantParseError("战绩页面包含未知胜负结果")
    if _SCORE_RE.fullmatch(normalized_score) is None:
        raise ValorantParseError("战绩页面包含无效比分")
    if _KDA_RE.fullmatch(normalized_kda) is None:
        raise ValorantParseError("战绩页面包含无效 K/D/A")
    if _DECIMAL_RE.fullmatch(acs) is None:
        raise ValorantParseError("战绩页面包含无效 ACS")
    if _RR_RE.fullmatch(normalized_rr) is None:
        raise ValorantParseError("战绩页面包含无效 RR")
    if _DATE_RE.fullmatch(date_value) is None or _TIME_RE.fullmatch(time_value) is None:
        raise ValorantParseError("战绩页面包含无效比赛时间")

    card_classes = _node_classes(card)
    if ("win" in card_classes and result != "胜利") or (
        "loss" in card_classes and result != "失败"
    ):
        raise ValorantParseError("战绩页面胜负标记互相矛盾")
    highlight_box = _find_first(card, class_name="match-highlight")
    highlight = _node_text(highlight_box) if highlight_box is not None else ""
    return ValorantMatch(
        agent=agent[:32],
        result=result,
        score=normalized_score,
        kda=re.sub(r"\s+", "", normalized_kda)[:16],
        acs=acs[:12],
        map_name=map_name[:40],
        rank=rank[:24],
        rr_change=normalized_rr[:12],
        ended_at=f"{date_value} {time_value}",
        duration=duration[:24],
        highlight=highlight[:80],
    )


def parse_valorant_result_html(
    document: str,
    *,
    requested_riot_id: str,
    fetched_at: str | None = None,
) -> ValorantSnapshot:
    """Parse the public server-rendered result without executing page scripts."""
    normalized_id = parse_valorant_id(requested_riot_id)
    if normalized_id is None:
        raise ValueError("invalid VALORANT Riot ID")

    parser = _DocumentParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:
        raise ValorantParseError("战绩页面 HTML 无法解析") from exc
    root = parser.root
    _raise_for_query_alert(root, normalized_id)

    body = next(
        (
            item
            for item in _find_all(root, tag="body")
            if "public-results-page" in _node_classes(item)
        ),
        None,
    )
    if body is None:
        raise ValorantParseError("返回页面不是公开战绩结果页")
    player = _find_first(body, id_value="result-player-name")
    if player is None:
        raise ValorantParseError("公开战绩结果页缺少玩家 ID")
    returned_id = parse_valorant_id(_node_text(player))
    if returned_id is None or returned_id.casefold() != normalized_id.casefold():
        raise ValorantParseError("战绩页面没有返回所查询的玩家")

    empty = _find_first(body, class_name="result-empty")
    match_list = _find_first(body, class_name="public-match-list")
    if match_list is None and empty is None:
        raise ValorantParseError("公开战绩结果页缺少近期比赛区域")

    rank_line = _find_first(body, class_name="result-rank-line")
    if rank_line is None:
        if empty is None:
            raise ValorantParseError("战绩页面缺少段位和胜负记录")
        rank = "暂无段位"
        record = "0胜0负"
    else:
        rank = _required_direct_text(rank_line, "strong", field="段位")
        record = _required_direct_text(rank_line, "span", field="胜负记录")
        if _WIN_LOSS_RE.fullmatch(record) is None:
            raise ValorantParseError("战绩页面包含无效胜负记录")

    metric_values: dict[str, str] = {}
    for metric in _find_all(body, class_name="result-metric"):
        labels = _direct_children(metric, tag="span")
        values = _direct_children(metric, tag="strong")
        if not labels or not values:
            raise ValorantParseError("战绩页面包含不完整的近十场指标")
        label = _metric_key(_node_text(labels[0]))
        value = _node_text(values[0])
        if label and value:
            metric_values[label] = value
    expected_metrics = {
        "近10场胜率": "win_rate",
        "近10场kda": "kda",
        "近10场acs": "acs",
        "近10场rr": "rr_change",
    }
    if empty is None:
        missing = [
            label
            for label in expected_metrics
            if label not in metric_values
        ]
        if missing:
            raise ValorantParseError("战绩页面缺少近十场汇总指标")
    win_rate = metric_values.get("近10场胜率", "-")
    kda = metric_values.get("近10场kda", "-")
    acs = metric_values.get("近10场acs", "-")
    rr_change = unicodedata.normalize(
        "NFKC",
        metric_values.get("近10场rr", "-"),
    ).replace("−", "-")
    if empty is None:
        if _PERCENT_RE.fullmatch(win_rate) is None:
            raise ValorantParseError("战绩页面包含无效胜率")
        if _DECIMAL_RE.fullmatch(kda) is None:
            raise ValorantParseError("战绩页面包含无效 KDA")
        if _DECIMAL_RE.fullmatch(acs) is None:
            raise ValorantParseError("战绩页面包含无效 ACS")
        if _RR_RE.fullmatch(rr_change) is None:
            raise ValorantParseError("战绩页面包含无效 RR")

    matches: tuple[ValorantMatch, ...]
    if match_list is None:
        matches = ()
    else:
        cards = _find_all(
            match_list,
            tag="article",
            class_name="public-match-card",
        )
        if not cards and empty is None:
            raise ValorantParseError("近期比赛区域没有有效比赛卡片")
        matches = tuple(_parse_match(card) for card in cards[:10])
    return ValorantSnapshot(
        riot_id=normalized_id,
        rank=rank[:24],
        record=re.sub(r"\s+", "", record)[:24],
        win_rate=win_rate[:12],
        kda=kda[:12],
        acs=acs[:12],
        rr_change=rr_change[:12],
        source_updated_at=_safe_source_note(body),
        fetched_at=fetched_at or datetime.now(UTC).isoformat(timespec="seconds"),
        matches=matches,
    )


def valorant_snapshot_to_payload(snapshot: ValorantSnapshot) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["version"] = 1
    payload.pop("cache_status", None)
    payload.pop("cache_note", None)
    return payload


def valorant_snapshot_from_payload(
    payload: dict[str, Any],
    *,
    cache_status: CacheStatus,
    cache_note: str = "",
) -> ValorantSnapshot:
    if int(payload.get("version") or 0) != 1:
        raise ValueError("unsupported VALORANT cache version")
    riot_id = parse_valorant_id(str(payload.get("riot_id") or ""))
    if riot_id is None:
        raise ValueError("invalid cached VALORANT Riot ID")
    raw_matches = payload.get("matches")
    if not isinstance(raw_matches, list):
        raise ValueError("invalid cached VALORANT match list")
    matches = tuple(
        ValorantMatch(
            agent=str(item.get("agent") or "")[:32],
            result=str(item.get("result") or "")[:8],
            score=str(item.get("score") or "")[:12],
            kda=str(item.get("kda") or "")[:16],
            acs=str(item.get("acs") or "")[:12],
            map_name=str(item.get("map_name") or "")[:40],
            rank=str(item.get("rank") or "")[:24],
            rr_change=str(item.get("rr_change") or "")[:12],
            ended_at=str(item.get("ended_at") or "")[:32],
            duration=str(item.get("duration") or "")[:24],
            highlight=str(item.get("highlight") or "")[:80],
        )
        for item in raw_matches[:10]
        if isinstance(item, dict)
    )
    return ValorantSnapshot(
        riot_id=riot_id,
        rank=str(payload.get("rank") or "暂无段位")[:24],
        record=str(payload.get("record") or "")[:24],
        win_rate=str(payload.get("win_rate") or "")[:12],
        kda=str(payload.get("kda") or "")[:12],
        acs=str(payload.get("acs") or "")[:12],
        rr_change=str(payload.get("rr_change") or "")[:12],
        source_updated_at=str(payload.get("source_updated_at") or "")[:100],
        fetched_at=str(payload.get("fetched_at") or "")[:40],
        matches=matches,
        cache_status=cache_status,
        cache_note=cache_note[:160],
    )


def format_valorant_snapshot(snapshot: ValorantSnapshot) -> str:
    status = ""
    if snapshot.cache_status == "fresh":
        status = "\n📦 使用近期缓存，避免频繁请求非官方数据源"
    elif snapshot.cache_status == "stale":
        status = f"\n⚠️ 数据源暂不可用，显示缓存数据：{snapshot.cache_note or '请稍后重试'}"
    lines = [
        f"无畏契约国服战绩｜{snapshot.riot_id}",
        f"段位：{snapshot.rank}　近十场：{snapshot.record}",
        (
            f"胜率：{snapshot.win_rate}　KDA：{snapshot.kda}　"
            f"ACS：{snapshot.acs}　RR：{snapshot.rr_change}"
        ),
    ]
    for match in snapshot.matches[:10]:
        result_label = {
            "胜利": "胜",
            "失败": "负",
            "平局": "平",
        }.get(match.result, "未知")
        lines.append(
            f"{result_label} "
            f"{match.score}｜{match.agent}｜{match.kda}｜ACS {match.acs}｜"
            f"{match.map_name}｜{match.rr_change}"
        )
    if snapshot.source_updated_at:
        lines.append(snapshot.source_updated_at)
    lines.append("数据源：WGame VAL 非官方公开查询")
    return "\n".join(lines) + status


class ValorantClient:
    """Low-rate client with a single in-flight request and failure circuit breaker."""

    def __init__(
        self,
        *,
        query_url: str = _DEFAULT_QUERY_URL,
        timeout_seconds: float = 20.0,
        queue_timeout_seconds: float = 3.0,
        failure_threshold: int = 3,
        failure_cooldown_seconds: float = 1800.0,
        clock: Any = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlparse(query_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "wgval.cloud"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("VALORANT query URL must be the trusted HTTPS community host")
        if timeout_seconds <= 0 or queue_timeout_seconds <= 0:
            raise ValueError("VALORANT timeouts must be positive")
        if failure_threshold < 1 or failure_cooldown_seconds < 0:
            raise ValueError("invalid VALORANT circuit-breaker settings")
        self.query_url = query_url
        self.timeout_seconds = timeout_seconds
        self.queue_timeout_seconds = queue_timeout_seconds
        self.failure_threshold = failure_threshold
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._failure_count = 0
        self._blocked_until = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                timeout_seconds,
                connect=min(10.0, timeout_seconds),
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": "pwcs2-qq-bot/0.1 (+manual-group-query)",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _record_failure(self) -> None:
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._blocked_until = self._clock() + self.failure_cooldown_seconds

    def _record_success(self) -> None:
        self._failure_count = 0
        self._blocked_until = 0.0

    async def query(self, riot_id: str) -> ValorantSnapshot:
        normalized_id = parse_valorant_id(riot_id)
        if normalized_id is None:
            raise ValueError("请使用完整国服 ID，例如 ExamplePlayer#12345")
        if self._clock() < self._blocked_until:
            raise ValorantQueryError("国服战绩源连续失败，已暂时熔断，请稍后重试")

        try:
            async with asyncio.timeout(self.queue_timeout_seconds):
                await self._lock.acquire()
        except TimeoutError as exc:
            raise ValorantQueryError("当前有其他瓦战绩查询正在进行，请稍后重试") from exc
        try:
            if self._clock() < self._blocked_until:
                raise ValorantQueryError("国服战绩源连续失败，已暂时熔断，请稍后重试")
            try:
                response = await self._client.get(
                    self.query_url,
                    params={"q": normalized_id},
                )
            except httpx.TransportError as exc:
                self._record_failure()
                raise ValorantQueryError("国服战绩源连接超时或网络异常") from exc
            if response.status_code in {404, 422}:
                raise ValorantPlayerNotFound(f"没有查到 {normalized_id} 的国服排位战绩")
            if response.status_code == 429:
                self._record_failure()
                raise ValorantQueryError("国服战绩源请求过多，请稍后重试")
            if response.status_code != 200:
                self._record_failure()
                raise ValorantQueryError(
                    f"国服战绩源暂不可用（HTTP {response.status_code}）"
                )
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                self._record_failure()
                raise ValorantQueryError("国服战绩源返回了非网页数据")
            if len(response.content) > _MAX_RESPONSE_BYTES:
                self._record_failure()
                raise ValorantQueryError("国服战绩源返回内容过大，已拒绝处理")
            try:
                document = response.content.decode(response.encoding or "utf-8", errors="replace")
                snapshot = parse_valorant_result_html(
                    document,
                    requested_riot_id=normalized_id,
                )
            except ValorantPlayerNotFound:
                raise
            except ValorantQueryError:
                self._record_failure()
                raise
            self._record_success()
            return replace(snapshot, cache_status="live", cache_note="")
        finally:
            self._lock.release()
