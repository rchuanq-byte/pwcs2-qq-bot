"""Permanent cosmetic themes used by the CS2 statistics card."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StatsBackground:
    key: str
    number: str
    name: str
    shop_item: str | None
    price: int
    asset_filename: str | None
    accent: str
    accent_secondary: str
    accent_rgb: str
    css_background: str = ""


STATS_BACKGROUNDS: dict[str, StatsBackground] = {
    "classic": StatsBackground(
        key="classic",
        number="0",
        name="经典黑金",
        shop_item=None,
        price=0,
        asset_filename=None,
        accent="#ffc34d",
        accent_secondary="#f07818",
        accent_rgb="245,158,11",
    ),
    "cyber_neon": StatsBackground(
        key="cyber_neon",
        number="1",
        name="赛博霓虹",
        shop_item="8",
        price=240,
        asset_filename="cyber-neon.png",
        accent="#63e6ff",
        accent_secondary="#b66cff",
        accent_rgb="99,230,255",
    ),
    "desert_sunset": StatsBackground(
        key="desert_sunset",
        number="2",
        name="沙城余晖",
        shop_item="9",
        price=240,
        asset_filename="desert-sunset.png",
        accent="#ffd166",
        accent_secondary="#f07818",
        accent_rgb="255,177,66",
    ),
    "aurora": StatsBackground(
        key="aurora", number="3", name="极光终夜", shop_item="11", price=180,
        asset_filename=None, accent="#68efc2", accent_secondary="#6986ff",
        accent_rgb="104,239,194",
        css_background=(
            "radial-gradient(ellipse at 90% 0%,rgba(104,239,194,.35),transparent 50%),"
            "radial-gradient(ellipse at 0% 90%,rgba(105,134,255,.30),transparent 50%),"
            "linear-gradient(140deg,#091d24,#0a1022 55%,#091a24)"
        ),
    ),
    "ice_blueprint": StatsBackground(
        key="ice_blueprint", number="4", name="冰川蓝图", shop_item="12", price=200,
        asset_filename=None, accent="#8bddff", accent_secondary="#4285ff",
        accent_rgb="139,221,255",
        css_background=(
            "linear-gradient(rgba(139,221,255,.045) 1px,transparent 1px) 0 0 / 36px 36px,"
            "linear-gradient(90deg,rgba(139,221,255,.045) 1px,transparent 1px) 0 0 / 36px 36px,"
            "radial-gradient(ellipse at 80% 0%,#19426a,transparent 55%),"
            "linear-gradient(145deg,#0b192c,#0c1220)"
        ),
    ),
    "molten": StatsBackground(
        key="molten", number="5", name="炽焰熔岩", shop_item="13", price=220,
        asset_filename=None, accent="#ff8966", accent_secondary="#e13d71",
        accent_rgb="255,137,102",
        css_background=(
            "radial-gradient(ellipse at 95% 5%,rgba(255,100,45,.32),transparent 45%),"
            "radial-gradient(ellipse at 0% 100%,rgba(225,61,113,.24),transparent 50%),"
            "linear-gradient(150deg,#23121c,#140f17 55%,#24121a)"
        ),
    ),
}

STATS_BACKGROUND_SHOP_ITEMS: dict[str, str] = {
    theme.shop_item: theme.key
    for theme in STATS_BACKGROUNDS.values()
    if theme.shop_item is not None
}

_STATS_BACKGROUND_ALIASES = {
    "0": "classic",
    "默认": "classic",
    "经典": "classic",
    "经典黑金": "classic",
    "classic": "classic",
    "1": "cyber_neon",
    "8": "cyber_neon",
    "霓虹": "cyber_neon",
    "赛博": "cyber_neon",
    "赛博霓虹": "cyber_neon",
    "cyber": "cyber_neon",
    "neon": "cyber_neon",
    "2": "desert_sunset",
    "9": "desert_sunset",
    "沙城": "desert_sunset",
    "余晖": "desert_sunset",
    "沙城余晖": "desert_sunset",
    "desert": "desert_sunset",
    "3": "aurora", "11": "aurora", "极光": "aurora", "极光终夜": "aurora",
    "4": "ice_blueprint", "12": "ice_blueprint", "冰川": "ice_blueprint",
    "冰川蓝图": "ice_blueprint", "蓝图": "ice_blueprint",
    "5": "molten", "13": "molten", "熔岩": "molten", "炽焰熔岩": "molten",
}


def resolve_stats_background(value: str) -> str | None:
    normalized = "".join(value.casefold().split())
    return _STATS_BACKGROUND_ALIASES.get(normalized)


def stats_background_asset_path(key: str) -> Path | None:
    theme = STATS_BACKGROUNDS.get(key)
    if theme is None or theme.asset_filename is None:
        return None
    return (
        Path(__file__).with_name("assets")
        / "stats_backgrounds"
        / theme.asset_filename
    )
