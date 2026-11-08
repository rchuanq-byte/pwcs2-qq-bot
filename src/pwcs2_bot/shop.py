"""Shop catalogue and deterministic daily offers; no database or network access."""

from __future__ import annotations

import hashlib
from datetime import date

SHOP_ITEMS: dict[str, tuple[str, int, str]] = {
    "1": ("称号盲盒", 80, "随机解锁未拥有的称号，永久收藏并自动佩戴"),
    "2": ("双倍答题卡", 120, "下一次答对每日一题时奖励翻倍"),
    "3": ("黑历史保护伞", 100, "自动拦截下一次针对你的黑历史收录"),
    "4": ("复仇喇叭", 100, "下一次产生复仇关系时获得醒目播报"),
    "5": ("自选称号券", 300, "#使用称号 <称号>；创建后永久收藏"),
    "6": ("答题提示卡", 60, "#提示 排除每日一题的两个错误选项"),
    "7": ("竞猜保险", 80, "下一次竞猜猜错时自动赔付一半下注，最高 50 牛币"),
    "8": ("战绩背景·赛博霓虹", 240, "永久解锁并自动切换，可随时更换"),
    "9": ("战绩背景·沙城余晖", 240, "永久解锁并自动切换，可随时更换"),
    "10": ("补签卡", 35, "#补签 修复昨日断签；先补签再签到，不补发牛币"),
    "11": ("战绩背景·极光终夜", 180, "青绿极光主题，永久解锁"),
    "12": ("战绩背景·冰川蓝图", 200, "冰蓝战术网格主题，永久解锁"),
    "13": ("战绩背景·炽焰熔岩", 220, "赤红熔岩主题，永久解锁"),
}

SHOP_INVENTORY_KEYS = {
    "2": "double_quiz", "3": "history_shield", "4": "revenge_horn",
    "5": "custom_badge", "6": "quiz_hint", "7": "prediction_insurance",
    "10": "checkin_repair",
}
INVENTORY_LABELS = {value: SHOP_ITEMS[key][0] for key, value in SHOP_INVENTORY_KEYS.items()}
SHOP_CATEGORIES = {
    "道具": ("2", "3", "4", "6", "10", "7"),
    "称号": ("1", "5"),
    "背景": ("8", "9", "11", "12", "13"),
}
MAX_PURCHASE_QUANTITY = 20
DAILY_OFFER_POOL = ("2", "3", "4", "5", "6", "10")


def daily_offers(group_id: str, shop_date: str) -> dict[str, int]:
    """Three offers at 80% of list price, stable across processes and restarts."""
    day = date.fromisoformat(shop_date).isoformat()
    ordered = sorted(DAILY_OFFER_POOL, key=lambda key: hashlib.sha256(
        f"niuniu-shop-v2:{group_id}:{day}:{key}".encode()
    ).digest())
    return {key: (SHOP_ITEMS[key][1] * 8 + 9) // 10 for key in ordered[:3]}


def resolve_shop_item(value: str) -> str:
    normalized = "".join(value.casefold().split()).replace("·", "")
    if normalized in SHOP_ITEMS:
        return normalized
    if not normalized or len(normalized) > 40 or normalized.isdecimal():
        raise ValueError("商品不存在，请发送 #商店 查看编号或名称")
    exact = [key for key, item in SHOP_ITEMS.items() if item[0].replace("·", "") == normalized]
    if exact:
        return exact[0]
    matches = [key for key, item in SHOP_ITEMS.items() if normalized in item[0].replace("·", "")]
    if len(matches) == 1:
        return matches[0]
    if matches:
        options = "、".join(f"{key}.{SHOP_ITEMS[key][0]}" for key in matches)
        raise ValueError(f"匹配到多个商品，请写完整名称或编号：{options}")
    raise ValueError("商品不存在，请发送 #商店 查看编号或名称")
