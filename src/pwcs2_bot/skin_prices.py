"""Group-managed CS2 skin watchlists and daily reference-price reports."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import httpx
from loguru import logger

from pwcs2_bot.database import BindingRepository

DEFAULT_PRICE_URL = "https://prices.csgotrader.app/latest/buff163.json"
DEFAULT_EXCHANGE_URL = "https://open.er-api.com/v6/latest/USD"
DEFAULT_SKINS_EN_URL = (
    "https://gcore.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/en/skins.json"
)
DEFAULT_SKINS_ZH_URL = (
    "https://gcore.jsdelivr.net/gh/ByMykel/CSGO-API@main/public/api/zh-CN/skins.json"
)
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_SEARCH_CHARS_RE = re.compile(r"[^0-9a-z\u3400-\u9fff]+")
_WEAR_ALIASES: dict[str, tuple[str, ...]] = {
    "SFUI_InvTooltip_Wear_Amount_0": ("崭新出厂", "崭新", "fn"),
    "SFUI_InvTooltip_Wear_Amount_1": ("略有磨损", "略磨", "mw"),
    "SFUI_InvTooltip_Wear_Amount_2": ("久经沙场", "久经", "ft"),
    "SFUI_InvTooltip_Wear_Amount_3": ("破损不堪", "破损", "ww"),
    "SFUI_InvTooltip_Wear_Amount_4": ("战痕累累", "战痕", "bs"),
}
_COMMON_CN_ALIASES: dict[str, tuple[str, ...]] = {
    "AK-47 | Fire Serpent": ("火蛇",),
    "AWP | Dragon Lore": ("龙狙", "巨龙传说"),
    "AWP | Gungnir": ("永恒之枪", "冈格尼尔"),
    "AWP | Medusa": ("美杜莎",),
    "M4A4 | Howl": ("咆哮",),
}


class SkinPriceError(RuntimeError):
    """A safe skin-price error that can be shown in QQ."""


@dataclass(frozen=True, slots=True)
class SkinQuote:
    market_hash_name: str
    sell_usd: float | None
    buy_usd: float | None
    usd_cny_rate: float
    image_url: str = ""

    @property
    def sell_cny(self) -> float | None:
        return None if self.sell_usd is None else self.sell_usd * self.usd_cny_rate

    @property
    def buy_cny(self) -> float | None:
        return None if self.buy_usd is None else self.buy_usd * self.usd_cny_rate


@dataclass(frozen=True, slots=True)
class SkinReportEntry:
    quote: SkinQuote
    previous_sell_cny: float | None = None

    @property
    def change_percent(self) -> float | None:
        current = self.quote.sell_cny
        previous = self.previous_sell_cny
        if current is None or previous is None or previous <= 0:
            return None
        return (current - previous) / previous * 100


@dataclass(frozen=True, slots=True)
class SkinPriceReport:
    entries: tuple[SkinReportEntry, ...]
    report_date: str | None = None

    @property
    def text(self) -> str:
        return format_skin_report(self.entries, report_date=self.report_date)


SendSkinReport = Callable[[str, SkinPriceReport], Awaitable[None]]


def _price(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if parsed >= 0 else None


def parse_market_name(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise SkinPriceError("请提供中文关键词、完整英文市场名或 Steam 市场链接")
    if len(raw) > 500:
        raise SkinPriceError("饰品名称或链接过长")
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        if parsed.hostname not in {"steamcommunity.com", "www.steamcommunity.com"}:
            raise SkinPriceError("只支持 Steam 社区市场链接")
        prefix = "/market/listings/730/"
        if not parsed.path.startswith(prefix):
            raise SkinPriceError("链接不是 CS2 Steam 市场商品页")
        raw = unquote(parsed.path[len(prefix) :]).strip()
    if not raw or len(raw) > 180:
        raise SkinPriceError("饰品市场名无效")
    return raw


def _search_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _SEARCH_CHARS_RE.sub("", normalized)


def _dict_text(value: Any, key: str) -> str:
    return str(value.get(key) or "").strip() if isinstance(value, dict) else ""


def _weapon_aliases(english_name: str, chinese_name: str) -> set[str]:
    aliases = {_search_key(english_name), _search_key(chinese_name)}
    english = _search_key(english_name)
    if english.startswith("ak47"):
        aliases.add("ak")
    if english.startswith("m4a1s"):
        aliases.update(("m4", "m4a1"))
    elif english.startswith("m4a4"):
        aliases.add("m4")
    if english.startswith("deserteagle"):
        aliases.update(("deagle", "沙鹰"))
    return {alias for alias in aliases if alias}


class SkinPriceClient:
    def __init__(
        self,
        *,
        price_url: str = DEFAULT_PRICE_URL,
        exchange_url: str = DEFAULT_EXCHANGE_URL,
        skins_en_url: str = DEFAULT_SKINS_EN_URL,
        skins_zh_url: str = DEFAULT_SKINS_ZH_URL,
        timeout_seconds: float = 20,
        cache_ttl_seconds: float = 3600,
        localization_cache_ttl_seconds: float = 86400,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.price_url = price_url
        self.exchange_url = exchange_url
        self.skins_en_url = skins_en_url
        self.skins_zh_url = skins_zh_url
        self.cache_ttl_seconds = cache_ttl_seconds
        self.localization_cache_ttl_seconds = localization_cache_ttl_seconds
        self.directory_timeout_seconds = min(30.0, max(10.0, timeout_seconds * 1.5))
        self._clock = clock
        self._catalog: dict[str, Any] | None = None
        self._catalog_at = 0.0
        self._rate: float | None = None
        self._rate_at = 0.0
        self._localized_aliases: dict[str, tuple[str, ...]] | None = None
        self._localized_labels: dict[str, str] = {}
        self._localized_images: dict[str, str] = {}
        self._localized_at = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": "pwcs2-qq-bot/0.1",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def resolve_name(self, value: str) -> str:
        query = parse_market_name(value)
        catalog = await self._get_catalog()
        exact = {name.casefold(): name for name in catalog}
        if matched := exact.get(query.casefold()):
            return matched
        folded = query.casefold()
        matches = [name for name in catalog if folded in name.casefold()]
        matches.sort(key=lambda name: (not name.casefold().startswith(folded), len(name), name))
        if matches:
            return self._one_match_or_error(matches)
        if _CJK_RE.search(query):
            localized = await self._localized_matches(query, catalog)
            if localized:
                return self._one_match_or_error(localized, localized=True)
        if not matches:
            raise SkinPriceError("没有找到该饰品，请补充武器、皮肤或磨损信息")
        return matches[0]

    def _one_match_or_error(
        self,
        matches: Sequence[str],
        *,
        localized: bool = False,
    ) -> str:
        unique = tuple(dict.fromkeys(matches))
        if len(unique) == 1:
            return unique[0]
        suggestions = "\n".join(
            f"• {self._localized_labels.get(name, name) if localized else name}"
            for name in unique[:5]
        )
        raise SkinPriceError(f"匹配到多个饰品，请补充武器、皮肤或磨损：\n{suggestions}")

    async def _localized_matches(
        self,
        query: str,
        catalog: dict[str, Any],
    ) -> list[str]:
        aliases = await self._get_localized_aliases(catalog)
        key = _search_key(query)
        if not key:
            return []
        scored: list[tuple[int, int, str]] = []
        for market_name, terms in aliases.items():
            if market_name.startswith("StatTrak™ ") and not (
                "暗金" in key or "stattrak" in key
            ):
                continue
            if market_name.startswith("Souvenir ") and "纪念品" not in key:
                continue
            best: tuple[int, int] | None = None
            for term in terms:
                if key == term:
                    score = (0, len(term))
                elif term.startswith(key):
                    score = (1, len(term))
                elif key in term:
                    score = (2, len(term))
                else:
                    continue
                best = score if best is None or score < best else best
            if best is not None:
                scored.append((best[0], best[1], market_name))
        scored.sort(key=lambda row: (row[0], row[1], row[2]))
        if scored and scored[0][0] == 0:
            scored = [row for row in scored if row[0] == 0]
        return [market_name for _rank, _length, market_name in scored]

    async def _get_localized_aliases(
        self,
        catalog: dict[str, Any],
    ) -> dict[str, tuple[str, ...]]:
        now = self._clock()
        if (
            self._localized_aliases is not None
            and now - self._localized_at < self.localization_cache_ttl_seconds
        ):
            return self._localized_aliases
        try:
            async with asyncio.timeout(self.directory_timeout_seconds):
                en_response, zh_response = await asyncio.gather(
                    self._client.get(self.skins_en_url),
                    self._client.get(self.skins_zh_url),
                )
            en_response.raise_for_status()
            zh_response.raise_for_status()
            english: Any = en_response.json()
            chinese: Any = zh_response.json()
        except (TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise SkinPriceError("中文饰品目录暂时不可用，请稍后重试") from exc
        if not isinstance(english, list) or not isinstance(chinese, list):
            raise SkinPriceError("中文饰品目录返回了无效数据")
        chinese_by_id = {
            str(item.get("id")): item
            for item in chinese
            if isinstance(item, dict) and item.get("id")
        }
        aliases: dict[str, tuple[str, ...]] = {}
        labels: dict[str, str] = {}
        images: dict[str, str] = {}
        for en_item in english:
            if not isinstance(en_item, dict):
                continue
            zh_item = chinese_by_id.get(str(en_item.get("id")))
            if not isinstance(zh_item, dict):
                continue
            self._add_skin_aliases(
                catalog,
                aliases,
                labels,
                images,
                en_item,
                zh_item,
            )
        self._localized_aliases = aliases
        self._localized_labels = labels
        self._localized_images = images
        self._localized_at = now
        return aliases

    @staticmethod
    def _add_skin_aliases(
        catalog: dict[str, Any],
        aliases: dict[str, tuple[str, ...]],
        labels: dict[str, str],
        images: dict[str, str],
        en_item: dict[str, Any],
        zh_item: dict[str, Any],
    ) -> None:
        en_name = str(en_item.get("name") or "").strip()
        zh_name = str(zh_item.get("name") or "").strip()
        if not en_name or not zh_name:
            return
        en_weapon = _dict_text(en_item.get("weapon"), "name")
        zh_weapon = _dict_text(zh_item.get("weapon"), "name")
        zh_pattern = _dict_text(zh_item.get("pattern"), "name")
        weapon_terms = _weapon_aliases(en_weapon, zh_weapon)
        common_terms = _COMMON_CN_ALIASES.get(en_name, ())
        zh_wears = {
            str(wear.get("id")): str(wear.get("name") or "")
            for wear in zh_item.get("wears") or []
            if isinstance(wear, dict) and wear.get("id")
        }
        variants = [("", "")]
        if en_item.get("stattrak"):
            variants.append(("StatTrak™ ", "暗金"))
        if en_item.get("souvenir"):
            variants.append(("Souvenir ", "纪念品"))
        for wear in en_item.get("wears") or []:
            if not isinstance(wear, dict):
                continue
            wear_id = str(wear.get("id") or "")
            en_wear = str(wear.get("name") or "").strip()
            zh_wear = zh_wears.get(wear_id, "").strip()
            if not en_wear or not zh_wear:
                continue
            wear_terms = set(_WEAR_ALIASES.get(wear_id, (zh_wear,)))
            wear_terms.add(zh_wear)
            for en_prefix, zh_prefix in variants:
                market_name = f"{en_prefix}{en_name} ({en_wear})"
                if market_name not in catalog:
                    continue
                terms = {
                    _search_key(f"{zh_prefix}{zh_name}{wear_term}")
                    for wear_term in wear_terms
                }
                for weapon in weapon_terms:
                    for wear_term in wear_terms:
                        terms.add(
                            _search_key(
                                f"{zh_prefix}{weapon}{zh_pattern}{wear_term}"
                            )
                        )
                for common in common_terms:
                    for wear_term in wear_terms:
                        terms.add(_search_key(f"{zh_prefix}{common}{wear_term}"))
                aliases[market_name] = tuple(sorted(term for term in terms if term))
                labels[market_name] = f"{zh_prefix}{zh_name}（{zh_wear}） → {market_name}"
                image_url = str(en_item.get("image") or "").strip()
                if image_url:
                    images[market_name] = image_url

    async def quotes(
        self,
        names: Sequence[str],
        *,
        include_images: bool = False,
    ) -> tuple[SkinQuote, ...]:
        catalog, rate = await asyncio.gather(self._get_catalog(), self._get_rate())
        if include_images:
            try:
                await self._get_localized_aliases(catalog)
            except SkinPriceError as exc:
                logger.warning("pwcs2: skin preview catalog unavailable: {}", exc)
        results: list[SkinQuote] = []
        for name in names:
            item = catalog.get(name)
            starting_at = item.get("starting_at") if isinstance(item, dict) else None
            highest_order = item.get("highest_order") if isinstance(item, dict) else None
            results.append(
                SkinQuote(
                    market_hash_name=name,
                    sell_usd=_price(
                        starting_at.get("price")
                        if isinstance(starting_at, dict)
                        else None
                    ),
                    buy_usd=_price(
                        highest_order.get("price")
                        if isinstance(highest_order, dict)
                        else None
                    ),
                    usd_cny_rate=rate,
                    image_url=self._localized_images.get(name, ""),
                )
            )
        return tuple(results)

    async def _get_catalog(self) -> dict[str, Any]:
        now = self._clock()
        if self._catalog is not None and now - self._catalog_at < self.cache_ttl_seconds:
            return self._catalog
        try:
            response = await self._client.get(self.price_url)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SkinPriceError("饰品行情源暂时不可用，请稍后重试") from exc
        if not isinstance(payload, dict) or not payload:
            raise SkinPriceError("饰品行情源返回了无效数据")
        self._catalog = payload
        self._catalog_at = now
        return payload

    async def _get_rate(self) -> float:
        now = self._clock()
        if self._rate is not None and now - self._rate_at < self.cache_ttl_seconds:
            return self._rate
        try:
            response = await self._client.get(self.exchange_url)
            response.raise_for_status()
            payload: Any = response.json()
            rate = float(payload["rates"]["CNY"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise SkinPriceError("人民币汇率暂时不可用，请稍后重试") from exc
        if not 4 <= rate <= 10:
            raise SkinPriceError("人民币汇率数据异常，请稍后重试")
        self._rate = rate
        self._rate_at = now
        return rate


def _money(value: float | None) -> str:
    return "暂无" if value is None else f"¥{value:,.2f}"


def _change(entry: SkinReportEntry) -> str:
    change = entry.change_percent
    if change is None:
        return "首次记录"
    if abs(change) < 0.005:
        return "→ 0.00%"
    return f"{'↑' if change > 0 else '↓'} {change:+.2f}%"


def format_skin_report(
    entries: Sequence[SkinReportEntry],
    *,
    report_date: str | None = None,
) -> str:
    title = f"🎨 CS2 饰品日报｜{report_date}" if report_date else "🎨 CS2 饰品价格"
    lines = [title, "BUFF163 聚合参考价 · 人民币汇率折算"]
    for index, entry in enumerate(entries, start=1):
        quote = entry.quote
        lines.append(
            f"{index}. {quote.market_hash_name}\n"
            f"   售价 {_money(quote.sell_cny)}　求购 {_money(quote.buy_cny)}　{_change(entry)}"
        )
    lines.append("仅供行情参考；实际成交价、磨损和印花会造成差异。")
    return "\n".join(lines)


class DailySkinPriceService:
    def __init__(
        self,
        repository: BindingRepository,
        client: SkinPriceClient,
        send_report: SendSkinReport,
        *,
        enabled: bool = True,
        send_time: str = "12:00",
        timezone_name: str = "Asia/Shanghai",
        poll_interval_seconds: float = 300,
        initial_delay_seconds: float = 45,
        max_items_per_group: int = 20,
        retention_days: int = 90,
        allowed_groups: list[str] | None = None,
    ) -> None:
        hour, minute = (int(part) for part in send_time.split(":"))
        self.repository = repository
        self.client = client
        self.send_report = send_report
        self.enabled = enabled
        self.send_time = datetime.min.replace(hour=hour, minute=minute).time()
        self.timezone = ZoneInfo(timezone_name)
        self.poll_interval_seconds = poll_interval_seconds
        self.initial_delay_seconds = initial_delay_seconds
        self.max_items_per_group = max_items_per_group
        self.retention_days = retention_days
        self.allowed_groups = set(allowed_groups or [])
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._stop_event.set()

    async def close(self) -> None:
        await self.client.close()

    async def add_watch(self, group_id: str, qq_id: str, value: str) -> str:
        name = await self.client.resolve_name(value)
        item, created = self.repository.add_skin_watch(
            group_id,
            name,
            qq_id,
            max_items=self.max_items_per_group,
        )
        if not created:
            return f"该饰品已在关注列表：#{item.item_id} {item.market_hash_name}"
        try:
            quote = (await self.client.quotes((name,)))[0]
        except SkinPriceError:
            return f"已添加饰品：#{item.item_id} {name}\n当前行情暂不可用，将在日报中重试"
        return f"已添加饰品：#{item.item_id} {name}\n当前参考售价：{_money(quote.sell_cny)}"

    def remove_watch(self, group_id: str, qq_id: str, value: str) -> str:
        target = value.strip()
        if not target:
            raise SkinPriceError("请提供列表编号或完整饰品名，例如 #饰品删除 3")
        items = self.repository.list_skin_watches(group_id)
        item = next(
            (
                row
                for row in items
                if (target.isdigit() and row.item_id == int(target))
                or row.market_hash_name.casefold() == target.casefold()
            ),
            None,
        )
        if item is None:
            raise SkinPriceError("关注列表中没有该饰品")
        if item.added_by_qq != qq_id:
            raise SkinPriceError("只能删除自己添加的饰品")
        self.repository.delete_skin_watch(group_id, item.item_id)
        return f"已删除饰品：{item.market_hash_name}"

    def watchlist(self, group_id: str) -> str:
        items = self.repository.list_skin_watches(group_id)
        if not items:
            return "本群饰品关注列表为空，发送 #饰品添加 <中文关键词/英文名/Steam链接> 添加"
        lines = [f"本群饰品关注列表（{len(items)}/{self.max_items_per_group}）"]
        lines.extend(f"#{item.item_id} {item.market_hash_name}" for item in items)
        return "\n".join(lines)

    async def current_report(
        self,
        group_id: str,
        value: str = "",
    ) -> SkinPriceReport:
        if value.strip():
            names = (await self.client.resolve_name(value),)
        else:
            names = tuple(
                item.market_hash_name
                for item in self.repository.list_skin_watches(group_id)
            )
        if not names:
            raise SkinPriceError("本群饰品关注列表为空")
        quotes = await self.client.quotes(names, include_images=True)
        entries = tuple(
            SkinReportEntry(
                quote,
                self.repository.latest_skin_sell_price(group_id, quote.market_hash_name),
            )
            for quote in quotes
        )
        return SkinPriceReport(entries)

    async def current_prices(self, group_id: str, value: str = "") -> str:
        return (await self.current_report(group_id, value)).text

    async def run(self) -> None:
        if not self.enabled or await self._wait(self.initial_delay_seconds):
            return
        logger.info(
            "pwcs2: daily skin prices started (time={} timezone={})",
            self.send_time.strftime("%H:%M"),
            self.timezone.key,
        )
        while not self._stop_event.is_set():
            try:
                await self.run_due_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("pwcs2: unexpected daily skin price failure")
            if await self._wait(self.poll_interval_seconds):
                return

    async def _wait(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return False
        return True

    async def run_due_once(self, now: datetime | None = None) -> int:
        local_now = now or datetime.now(self.timezone)
        if local_now.tzinfo is None:
            local_now = local_now.replace(tzinfo=self.timezone)
        else:
            local_now = local_now.astimezone(self.timezone)
        if local_now.time().replace(tzinfo=None) < self.send_time:
            return 0
        report_date = local_now.date().isoformat()
        groups = self.allowed_groups or set(self.repository.list_skin_watch_groups())
        sent = 0
        for group_id in sorted(groups):
            if self.repository.is_skin_price_report_processed(group_id, report_date):
                continue
            watches = self.repository.list_skin_watches(group_id)
            if not watches:
                self.repository.mark_skin_price_report_processed(
                    group_id, report_date, was_sent=False, retention_days=self.retention_days
                )
                continue
            try:
                quotes = await self.client.quotes(
                    tuple(item.market_hash_name for item in watches),
                    include_images=True,
                )
                entries: list[SkinReportEntry] = []
                for quote in quotes:
                    previous = self.repository.skin_sell_price_before(
                        group_id, quote.market_hash_name, report_date
                    )
                    entries.append(SkinReportEntry(quote, previous))
                    if quote.sell_cny is not None:
                        self.repository.save_skin_price_snapshot(
                            group_id=group_id,
                            market_hash_name=quote.market_hash_name,
                            snapshot_date=report_date,
                            sell_price_cny=quote.sell_cny,
                            buy_price_cny=quote.buy_cny,
                            source_price_usd=quote.sell_usd,
                            usd_cny_rate=quote.usd_cny_rate,
                        )
                await self.send_report(
                    group_id,
                    SkinPriceReport(tuple(entries), report_date=report_date),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "pwcs2: daily skin prices failed group={} date={}: {}",
                    group_id,
                    report_date,
                    exc,
                )
                continue
            self.repository.mark_skin_price_report_processed(
                group_id, report_date, was_sent=True, retention_days=self.retention_days
            )
            sent += 1
        return sent
