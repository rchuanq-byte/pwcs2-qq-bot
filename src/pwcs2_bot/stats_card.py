"""Render self-contained game-stat HTML/CSS cards to PNG with Playwright."""

from __future__ import annotations

import asyncio
import base64
import html
import math
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from loguru import logger

from pwcs2_bot.appearance import STATS_BACKGROUNDS, stats_background_asset_path
from pwcs2_bot.chemistry import ChemistryNetworkSnapshot
from pwcs2_bot.formatting import StatsSnapshot
from pwcs2_bot.match_formatting import MatchHistorySnapshot, MatchSnapshot
from pwcs2_bot.ranking import RankingEntry, RankingMetric, RankingSnapshot
from pwcs2_bot.watch_stage import display_map_name

if TYPE_CHECKING:
    from pwcs2_bot.daily_word_cloud import WordCloudSnapshot
    from pwcs2_bot.skin_prices import SkinPriceReport
    from pwcs2_bot.steam_presence import GameStatusSnapshot
    from pwcs2_bot.valorant import ValorantSnapshot
    from pwcs2_bot.watch_stage import WatchStageSnapshot
    from pwcs2_bot.weekly_report import WeeklyReportSnapshot


class StatsCardRenderError(RuntimeError):
    """Raised when the optional image renderer cannot produce a PNG."""


_AVATAR_HOSTS = {"img.wmpvp.com"}
_AVATAR_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_SKIN_IMAGE_HOSTS = {"community.akamai.steamstatic.com"}
_SKIN_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_SKIN_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_STATS_BACKGROUND_BYTES = 5 * 1024 * 1024
_BROWSER_CLOSE_TIMEOUT_SECONDS = 5.0
_RENDER_CIRCUIT_BREAKER_SECONDS = 300.0
_RENDER_QUEUE_TIMEOUT_SECONDS = 10.0
_MAX_PENDING_RENDERS = 3
_RENDER_ATTEMPTS = 2


def _escape(value: str) -> str:
    return html.escape(value, quote=True)


def _bar_width(value: float | None) -> str:
    if value is None:
        return "0"
    return f"{min(max(value, 0), 100):.1f}"


@lru_cache(maxsize=len(STATS_BACKGROUNDS))
def stats_background_data_uri(key: str) -> str | None:
    path = stats_background_asset_path(key)
    if path is None:
        return None
    content = path.read_bytes()
    if not content or len(content) > _MAX_STATS_BACKGROUND_BYTES:
        raise StatsCardRenderError(f"战绩背景资源无效：{key}")
    encoded = base64.b64encode(content).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def _read_avatar_data_uri(url: str, client: httpx.AsyncClient) -> str | None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _AVATAR_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in _AVATAR_CONTENT_TYPES:
                return None
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > _MAX_AVATAR_BYTES:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_AVATAR_BYTES:
                    return None
                chunks.append(chunk)
    except (httpx.HTTPError, OSError, ValueError):
        return None
    encoded = base64.b64encode(b"".join(chunks)).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


async def fetch_avatar_data_uri(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 5.0,
) -> str | None:
    if not url:
        return None
    if client is not None:
        return await _read_avatar_data_uri(url, client)
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
    ) as owned_client:
        return await _read_avatar_data_uri(url, owned_client)


async def _read_skin_image_data_uri(
    url: str,
    client: httpx.AsyncClient,
) -> str | None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _SKIN_IMAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    try:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
            if content_type not in _SKIN_IMAGE_CONTENT_TYPES:
                return None
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > _MAX_SKIN_IMAGE_BYTES:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_SKIN_IMAGE_BYTES:
                    return None
                chunks.append(chunk)
    except (httpx.HTTPError, OSError, ValueError):
        return None
    encoded = base64.b64encode(b"".join(chunks)).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


async def fetch_skin_image_data_uri(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 5.0,
) -> str | None:
    if not url:
        return None
    if client is not None:
        return await _read_skin_image_data_uri(url, client)
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=False,
    ) as owned_client:
        return await _read_skin_image_data_uri(url, owned_client)


def _skin_money(value: float | None) -> str:
    return "暂无" if value is None else f"¥{value:,.2f}"


def _skin_change(value: float | None) -> tuple[str, str]:
    if value is None:
        return "首次记录", "neutral"
    if abs(value) < 0.005:
        return "→ 0.00%", "neutral"
    return (
        f"{'↑' if value > 0 else '↓'} {value:+.2f}%",
        "up" if value > 0 else "down",
    )


def render_skin_price_html(
    report: SkinPriceReport,
    *,
    image_data_uris: tuple[str | None, ...] = (),
) -> str:
    cards: list[str] = []
    for index, entry in enumerate(report.entries, start=1):
        quote = entry.quote
        image = image_data_uris[index - 1] if index <= len(image_data_uris) else None
        preview = (
            f'<img src="{_escape(image)}" alt="" />'
            if image
            else '<div class="skin-placeholder">CS2</div>'
        )
        change_text, change_class = _skin_change(entry.change_percent)
        cards.append(
            f"""
            <article class="skin-item">
              <div class="skin-preview">{preview}</div>
              <div class="skin-copy">
                <div class="skin-topline">
                  <span class="skin-index">#{index:02d}</span>
                  <span class="skin-change {change_class}">{_escape(change_text)}</span>
                </div>
                <div class="skin-name">{_escape(quote.market_hash_name)}</div>
                <div class="skin-prices">
                  <div><span>参考售价</span><strong>{_escape(_skin_money(quote.sell_cny))}</strong></div>
                  <div><span>参考求购</span><strong>{_escape(_skin_money(quote.buy_cny))}</strong></div>
                </div>
              </div>
            </article>
            """
        )
    title = "CS2 饰品日报" if report.report_date else "CS2 饰品价格"
    date_text = report.report_date or "实时查询"
    rate = report.entries[0].quote.usd_cny_rate if report.entries else 0
    grid_class = "skin-grid single" if len(report.entries) == 1 else "skin-grid"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: transparent; }}
  body {{
    width: 1160px;
    padding: 28px;
    color: #edf4ff;
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
  }}
  #skin-price-card {{
    width: 1104px;
    padding: 34px;
    overflow: hidden;
    border: 1px solid rgba(125, 180, 255, .26);
    border-radius: 28px;
    background:
      radial-gradient(circle at 88% 0%, rgba(255, 172, 56, .24), transparent 34%),
      radial-gradient(circle at 0% 85%, rgba(47, 123, 255, .18), transparent 40%),
      linear-gradient(145deg, #111a2a 0%, #08101e 100%);
    box-shadow: 0 24px 70px rgba(0, 0, 0, .42);
  }}
  .header {{ display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; }}
  .eyebrow {{ color: #ffbd55; font-size: 16px; font-weight: 800; letter-spacing: 3px; }}
  h1 {{ margin: 8px 0 0; font-size: 38px; line-height: 1.1; letter-spacing: 1px; }}
  .meta {{ color: #98aac4; font-size: 16px; text-align: right; line-height: 1.7; }}
  .meta strong {{ color: #dce8fa; }}
  .rule {{
    height: 1px;
    margin: 26px 0;
    background: linear-gradient(90deg, #ffb84e, rgba(94, 154, 255, .5), transparent);
  }}
  .skin-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .skin-grid.single {{ grid-template-columns: 1fr; }}
  .skin-item {{
    min-height: 178px;
    display: grid;
    grid-template-columns: 170px 1fr;
    gap: 16px;
    padding: 16px;
    border: 1px solid rgba(127, 164, 217, .18);
    border-radius: 20px;
    background: linear-gradient(135deg, rgba(27, 40, 61, .94), rgba(13, 23, 39, .94));
  }}
  .skin-preview {{
    min-height: 142px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 15px;
    background: radial-gradient(circle, rgba(82, 127, 192, .2), rgba(5, 11, 21, .5));
  }}
  .skin-preview img {{
    width: 158px;
    height: 120px;
    object-fit: contain;
    filter: drop-shadow(0 13px 12px rgba(0,0,0,.48));
  }}
  .skin-placeholder {{ color: #58708e; font-size: 24px; font-weight: 900; letter-spacing: 4px; }}
  .skin-copy {{ min-width: 0; display: flex; flex-direction: column; }}
  .skin-topline {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; }}
  .skin-index {{ color: #7891b3; font-size: 14px; font-weight: 800; letter-spacing: 1px; }}
  .skin-change {{ padding: 4px 9px; border-radius: 99px; font-size: 13px; font-weight: 800; }}
  .skin-change.up {{ color: #57e29a; background: rgba(45, 201, 125, .12); }}
  .skin-change.down {{ color: #ff7c82; background: rgba(255, 83, 91, .12); }}
  .skin-change.neutral {{ color: #a6b6cc; background: rgba(142, 160, 188, .12); }}
  .skin-name {{
    margin-top: 10px;
    min-height: 47px;
    color: #f4f7fc;
    font-size: 17px;
    font-weight: 750;
    line-height: 1.38;
    overflow-wrap: anywhere;
  }}
  .skin-prices {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: auto; }}
  .skin-prices div {{ padding: 9px 10px; border-radius: 11px; background: rgba(3, 9, 18, .42); }}
  .skin-prices span {{ display: block; color: #7f93ad; font-size: 12px; }}
  .skin-prices strong {{ display: block; margin-top: 2px; color: #f6c368; font-size: 17px; }}
  .footer {{
    display: flex;
    justify-content: space-between;
    gap: 20px;
    margin-top: 24px;
    color: #71849e;
    font-size: 13px;
  }}
</style>
</head>
<body>
  <main id="skin-price-card">
    <header class="header">
      <div><div class="eyebrow">BUFF163 REFERENCE</div><h1>{_escape(title)}</h1></div>
      <div class="meta">
        <strong>{_escape(date_text)}</strong><br />
        {len(report.entries)} 件关注饰品 · USD/CNY {rate:.4f}
      </div>
    </header>
    <div class="rule"></div>
    <section class="{grid_class}">{''.join(cards)}</section>
    <footer class="footer">
      <span>价格仅供行情参考</span>
      <span>磨损、印花与实际成交情况会造成差异</span>
    </footer>
  </main>
</body>
</html>"""


def render_chemistry_network_html(snapshot: ChemistryNetworkSnapshot) -> str:
    pairs = snapshot.pairs[:12]
    member_names: dict[str, str] = {}
    for pair in pairs:
        member_names.setdefault(pair.first_qq, pair.first_name)
        member_names.setdefault(pair.second_qq, pair.second_name)
        if len(member_names) >= 10:
            break
    member_ids = tuple(member_names)[:10]
    positions: dict[str, tuple[float, float]] = {}
    count = max(1, len(member_ids))
    for index, qq_id in enumerate(member_ids):
        angle = math.pi * 2 * index / count
        positions[qq_id] = (550 + math.cos(angle) * 410, 260 + math.sin(angle) * 190)

    edge_parts: list[str] = []
    for pair in reversed(pairs):
        if pair.first_qq not in positions or pair.second_qq not in positions:
            continue
        x1, y1 = positions[pair.first_qq]
        x2, y2 = positions[pair.second_qq]
        width = min(12, 2 + pair.same_team_matches * 1.4)
        opacity = min(0.92, 0.28 + pair.shared_matches * 0.08)
        midpoint_x = (x1 + x2) / 2
        midpoint_y = (y1 + y2) / 2
        edge_parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            f'stroke-width="{width:.1f}" opacity="{opacity:.2f}" />'
            f'<text class="edge-label" x="{midpoint_x:.0f}" y="{midpoint_y - 7:.0f}">'
            f'{pair.same_team_matches} 场</text>'
        )

    node_parts: list[str] = []
    for qq_id in member_ids:
        x, y = positions[qq_id]
        raw_name = member_names[qq_id]
        name = raw_name if len(raw_name) <= 9 else raw_name[:8] + "…"
        node_parts.append(
            f'<g class="person" transform="translate({x:.0f} {y:.0f})">'
            '<circle r="49" />'
            f'<text y="5">{_escape(name)}</text>'
            "</g>"
        )

    pair_cards = "".join(
        f"""
        <div class="pair-item">
          <div class="pair-rank">{index:02d}</div>
          <div class="pair-main">
            <strong>{_escape(pair.first_name)} × {_escape(pair.second_name)}</strong>
            <span>{_escape(pair.title)}</span>
          </div>
          <div class="pair-stat">
            <strong>{pair.same_team_matches}</strong><span>同队场次</span>
          </div>
          <div class="pair-stat">
            <strong>{_escape(pair.same_team_win_rate)}</strong><span>同队胜率</span>
          </div>
        </div>
        """
        for index, pair in enumerate(pairs[:5], start=1)
    )
    best = pairs[0] if pairs else None
    best_line = (
        f"最佳搭档 · {_escape(best.first_name)} × {_escape(best.second_name)}"
        if best is not None
        else "本周暂未形成羁绊"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: transparent; color: #edf3f8;
    font-family: "Noto Sans CJK SC", "Noto Sans SC", sans-serif; }}
  .network-card {{ width: 1160px; min-height: 900px; padding: 38px 44px 34px;
    border-radius: 30px; overflow: hidden; position: relative;
    background: radial-gradient(circle at 50% 35%, #173647 0, #0a1b27 43%, #050c13 100%);
    border: 1px solid rgba(108,220,255,.23); box-shadow: 0 28px 70px rgba(0,0,0,.48); }}
  .network-card::before {{ content: ""; position: absolute; inset: 0; pointer-events:none;
    background: linear-gradient(115deg, rgba(86,221,255,.08), transparent 35%,
      rgba(255,190,68,.07)); }}
  header {{ position: relative; display:flex; justify-content:space-between;
    align-items:flex-start; }}
  .eyebrow {{ color:#5bd8ff; font-size:14px; font-weight:800; letter-spacing:4px; }}
  h1 {{ margin:7px 0 4px; font-size:42px; letter-spacing:2px; }}
  .week {{ color:#90a6b5; font-size:17px; }}
  .summary {{ text-align:right; padding:12px 18px; border:1px solid rgba(255,255,255,.12);
    border-radius:15px; background:rgba(0,0,0,.2); }}
  .summary strong {{ display:block; color:#ffd36a; font-size:25px; }}
  .summary span {{ color:#9db0bd; font-size:14px; }}
  .graph {{ position:relative; margin-top:18px; height:520px; border-radius:24px;
    border:1px solid rgba(255,255,255,.08); background:rgba(0,0,0,.17); overflow:hidden; }}
  svg {{ width:100%; height:100%; }}
  line {{ stroke:#51d7ff; }}
  .edge-label {{ fill:#9edff2; text-anchor:middle; paint-order:stroke;
    stroke:#0a1b27; stroke-width:6px; font-size:13px; font-weight:700; }}
  .person circle {{ fill:#102d3d; stroke:#67ddff; stroke-width:3px;
    filter:drop-shadow(0 8px 10px rgba(0,0,0,.45)); }}
  .person text {{ fill:#f5fbff; text-anchor:middle; font-size:14px; font-weight:800; }}
  .best {{ position:absolute; left:50%; top:16px; transform:translateX(-50%);
    padding:9px 20px; color:#ffd36a; background:rgba(6,16,23,.82);
    border:1px solid rgba(255,211,106,.35); border-radius:999px; font-weight:800; }}
  .pairs {{ position:relative; margin-top:18px; display:grid; gap:8px; }}
  .pair-item {{ min-height:62px; display:grid; grid-template-columns:48px 1fr 112px 112px;
    align-items:center; gap:14px; padding:8px 14px; border-radius:15px;
    background:rgba(255,255,255,.055); border:1px solid rgba(255,255,255,.07); }}
  .pair-rank {{ color:#5bd8ff; font-size:17px; font-weight:900; }}
  .pair-main strong {{ display:block; font-size:18px; }}
  .pair-main span {{ color:#ffd36a; font-size:13px; }}
  .pair-stat {{ text-align:right; }} .pair-stat strong {{ display:block; font-size:18px; }}
  .pair-stat span {{ color:#8499a7; font-size:12px; }}
  footer {{ position:relative; margin-top:18px; display:flex; justify-content:space-between;
    color:#728895; font-size:13px; letter-spacing:.5px; }}
</style>
</head>
<body>
<main id="chemistry-network-card" class="network-card">
  <header>
    <div><div class="eyebrow">NIUNIU SOCIAL GRAPH</div><h1>牛牛关系网</h1>
      <div class="week">{_escape(snapshot.week_start)} 当周 · 最近 50 场</div></div>
    <div class="summary">
      <strong>{snapshot.member_count} 人 / {snapshot.relationship_count} 组</strong>
      <span>已绑定成员 / 活跃羁绊</span></div>
  </header>
  <section class="graph"><div class="best">{best_line}</div>
    <svg viewBox="0 0 1100 520" role="img">{''.join(edge_parts)}{''.join(node_parts)}</svg>
  </section>
  <section class="pairs">{pair_cards}</section>
  <footer><span>同队场次越多，连线越粗</span><span>PERFECT WORLD · GROUP WEEKLY</span></footer>
</main>
</body>
</html>"""


def render_weekly_report_html(snapshot: WeeklyReportSnapshot) -> str:
    members = snapshot.members[:8]
    battle = max(
        members,
        key=lambda member: member.avg_rating if member.avg_rating is not None else -1,
    )
    grinder = max(members, key=lambda member: member.matches)
    climber = max(
        members,
        key=lambda member: member.elo_change if member.elo_change is not None else -9999,
    )
    medals = (
        ("本周战神", battle.name, _weekly_value(battle.avg_rating, " Rating")),
        ("劳模担当", grinder.name, f"{grinder.matches} 场"),
        ("上分之王", climber.name, _weekly_signed(climber.elo_change, " ELO")),
    )
    medal_cards = "".join(
        f"""
        <div class="medal">
          <span>{_escape(label)}</span><strong>{_escape(name)}</strong><em>{_escape(value)}</em>
        </div>
        """
        for label, name, value in medals
    )
    rows = "".join(
        f"""
        <div class="weekly-row">
          <div class="weekly-rank">{index:02d}</div>
          <div class="weekly-name"><strong>{_escape(member.name)}</strong>
            <span>{_escape(member.favorite_map)}</span></div>
          <div><strong>{member.matches}</strong><span>场次</span></div>
          <div><strong>{member.wins}</strong><span>胜场</span></div>
          <div><strong>{_weekly_value(member.avg_rating)}</strong><span>Rating</span></div>
          <div><strong>{_weekly_signed(member.elo_change)}</strong><span>ELO</span></div>
        </div>
        """
        for index, member in enumerate(members, 1)
    )
    maps = "".join(
        f'<span class="map-pill">{_escape(name)} · {count} 场</span>'
        for name, count in snapshot.popular_maps
    )
    reward_line = ""
    if snapshot.activity_rewards:
        rewards = "　".join(
            f"{_escape(name)} +{reward}"
            for _qq_id, name, reward in snapshot.activity_rewards
        )
        reward_line = (
            '<section class="weekly-reward"><strong>🎁 五场活跃奖励</strong>'
            f"<span>{rewards} 牛币</span></section>"
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; background:transparent; color:#f4f7f9;
  font-family:"Noto Sans CJK SC","Noto Sans SC",sans-serif; }}
.weekly {{ width:1160px; min-height:900px; padding:44px; overflow:hidden; position:relative;
  border-radius:30px; background:linear-gradient(145deg,#07121a 0%,#102d34 52%,#071015 100%);
  border:1px solid rgba(112,239,211,.22); box-shadow:0 30px 80px rgba(0,0,0,.5); }}
.weekly::before {{ content:""; position:absolute; width:620px; height:620px; border-radius:50%;
  right:-250px; top:-340px; background:rgba(63,230,188,.12); filter:blur(10px); }}
header {{ position:relative; display:flex; justify-content:space-between; align-items:flex-start; }}
.kicker {{ color:#5ce7c1; font-weight:900; letter-spacing:4px; font-size:14px; }}
h1 {{ margin:8px 0 5px; font-size:45px; letter-spacing:3px; }}
.period {{ color:#8da5aa; font-size:17px; }}
.overview {{ display:grid; grid-template-columns:repeat(2,110px); gap:8px; }}
.overview div {{ padding:12px 10px; text-align:center; border-radius:14px;
  background:rgba(255,255,255,.055); border:1px solid rgba(255,255,255,.08); }}
.overview strong {{ display:block; color:#ffd76b; font-size:24px; }}
.overview span {{ color:#82969d; font-size:12px; }}
.medals {{ position:relative; display:grid; grid-template-columns:repeat(3,1fr); gap:12px;
  margin:30px 0 22px; }}
.medal {{ min-height:116px; padding:18px; border-radius:18px;
  background:linear-gradient(135deg,rgba(93,232,195,.13),rgba(255,215,107,.055));
  border:1px solid rgba(93,232,195,.16); }}
.medal span {{ display:block; color:#5ce7c1; font-size:13px; font-weight:800; letter-spacing:2px; }}
.medal strong {{ display:block; margin:9px 0 4px; font-size:22px; }}
.medal em {{ color:#ffd76b; font-size:14px; font-style:normal; }}
.map-line {{ position:relative; display:flex; gap:9px; margin-bottom:14px; }}
.map-pill {{ padding:7px 12px; border-radius:999px; color:#a9bdc2; font-size:13px;
  border:1px solid rgba(255,255,255,.09); background:rgba(0,0,0,.18); }}
.members {{ position:relative; display:grid; gap:7px; }}
.weekly-row {{ min-height:66px; display:grid;
  grid-template-columns:52px minmax(280px,1fr) repeat(4,100px); gap:12px;
  align-items:center; padding:8px 14px; border-radius:15px;
  background:rgba(255,255,255,.045); border:1px solid rgba(255,255,255,.065); }}
.weekly-rank {{ color:#5ce7c1; font-size:17px; font-weight:900; }}
.weekly-name strong {{ display:block; font-size:18px; }}
.weekly-name span,.weekly-row div>span {{ display:block; color:#7f949b; font-size:12px; }}
.weekly-row div:not(.weekly-name):not(.weekly-rank) {{ text-align:right; }}
.weekly-row div>strong {{ font-size:18px; }}
.weekly-reward {{ position:relative; display:flex; justify-content:space-between;
  align-items:center; margin-top:14px; padding:13px 17px; border-radius:15px;
  color:#ffd76b; background:rgba(255,215,107,.08);
  border:1px solid rgba(255,215,107,.17); }}
.weekly-reward strong {{ font-size:15px; }}
.weekly-reward span {{ color:#e7dca7; font-size:14px; }}
footer {{ position:relative; display:flex; justify-content:space-between; margin-top:22px;
  color:#6f858b; font-size:13px; }}
</style>
</head>
<body>
<main id="weekly-report-card" class="weekly">
  <header>
    <div><div class="kicker">NIUNIU WEEKLY INTEL</div><h1>每周战报</h1>
      <div class="period">{_escape(snapshot.week_start)} — {_escape(snapshot.week_end)}</div></div>
    <div class="overview">
      <div><strong>{snapshot.active_members}/{snapshot.bound_members}</strong>
        <span>活跃 / 绑定</span></div>
      <div><strong>{snapshot.unique_matches}</strong><span>独立对局</span></div>
      <div><strong>{snapshot.total_kills}</strong><span>总击杀</span></div>
      <div><strong>{_weekly_signed(snapshot.total_elo_change)}</strong><span>ELO 净变化</span></div>
    </div>
  </header>
  <section class="medals">{medal_cards}</section>
  <div class="map-line">{maps}</div>
  <section class="members">{rows}</section>
  {reward_line}
  <footer><span>按平均 Rating 排列 · 同场群友对局已去重</span>
    <span>PERFECT WORLD · GROUP WEEKLY</span></footer>
</main>
</body>
</html>"""


def _weekly_value(value: float | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:.2f}{suffix}"


def _weekly_signed(value: float | None, suffix: str = "") -> str:
    return "-" if value is None else f"{value:+.0f}{suffix}"


_GAME_STATUS_KINDS = {
    "playing": ("正在游戏", "PLAYING"),
    "online": ("在线", "ONLINE"),
    "away": ("离开", "AWAY"),
    "busy": ("忙碌", "BUSY"),
    "snooze": ("打盹", "SNOOZE"),
    "trade": ("想交易", "TRADE"),
    "looking": ("想一起玩", "LOOKING"),
    "offline": ("离线", "OFFLINE"),
    "unknown": ("状态不可见", "UNKNOWN"),
}


def render_game_status_html(snapshot: GameStatusSnapshot) -> str:
    """Render a Steam/group game-presence snapshot as a self-contained card."""

    rows: list[str] = []
    for entry in snapshot.entries:
        kind = entry.status_kind if entry.status_kind in _GAME_STATUS_KINDS else "unknown"
        default_label, english_label = _GAME_STATUS_KINDS[kind]
        status_label = entry.status_label.strip() or default_label
        game_name = entry.game_name.strip()
        detail = entry.detail.strip()
        steam_id = entry.steam_id.strip()
        app_id = entry.app_id.strip()
        game_line = (
            f'<div class="game-name">{_escape(game_name)}</div>'
            if game_name
            else '<div class="game-name muted">未公开显示正在游玩的游戏</div>'
        )
        detail_line = (
            f'<div class="entry-detail">{_escape(detail)}</div>' if detail else ""
        )
        identifiers = " · ".join(
            value
            for value in (
                f"SteamID {steam_id}" if steam_id else "",
                f"AppID {app_id}" if app_id else "",
            )
            if value
        )
        identifier_line = (
            f'<div class="identifiers">{_escape(identifiers)}</div>'
            if identifiers
            else ""
        )
        rows.append(
            f"""
            <article class="presence-entry {kind}">
              <div class="status-rail"><i></i><span>{english_label}</span></div>
              <div class="entry-copy">
                <div class="entry-topline">
                  <strong>{_escape(entry.name)}</strong>
                  <span class="status-pill">{_escape(status_label)}</span>
                </div>
                {game_line}
                {detail_line}
                {identifier_line}
              </div>
            </article>
            """
        )

    entries_html = "".join(rows) or """
      <div class="empty-presence">
        <strong>当前没有可展示的公开状态</strong>
        <span>成员可能处于离线、隐身，或 Steam 暂未返回状态。</span>
      </div>
    """
    visible_count = sum(
        entry.status_kind not in {"offline", "unknown"}
        and entry.status_kind in _GAME_STATUS_KINDS
        for entry in snapshot.entries
    )
    non_playing_visible = sum(
        entry.status_kind not in {"playing", "offline", "unknown"}
        and entry.status_kind in _GAME_STATUS_KINDS
        for entry in snapshot.entries
    )
    health_badges = "".join(
        (
            '<span class="health stale">缓存状态</span>' if snapshot.stale else "",
            '<span class="health degraded">降级数据</span>' if snapshot.degraded else "",
        )
    ) or '<span class="health live">实时数据</span>'
    note = (
        f'<div class="notice"><i>i</i><span>{_escape(snapshot.note)}</span></div>'
        if snapshot.note
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; background: transparent; }}
  body {{
    width: 1160px;
    padding: 28px;
    color: #eef5ff;
    font-family: "Noto Sans CJK SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
  }}
  #game-status-card {{
    width: 1104px;
    min-height: 620px;
    position: relative;
    overflow: hidden;
    padding: 38px;
    border: 1px solid rgba(111, 190, 255, .22);
    border-radius: 30px;
    background:
      radial-gradient(circle at 92% 0%, rgba(79, 255, 179, .16), transparent 34%),
      radial-gradient(circle at 0% 100%, rgba(67, 128, 255, .19), transparent 40%),
      linear-gradient(145deg, #101a2a 0%, #08111f 56%, #0a1622 100%);
    box-shadow: 0 28px 80px rgba(0, 0, 0, .46);
  }}
  #game-status-card::before {{
    content: "";
    position: absolute;
    width: 460px;
    height: 460px;
    right: -250px;
    top: -250px;
    border: 1px solid rgba(87, 229, 171, .12);
    border-radius: 50%;
    box-shadow: 0 0 0 70px rgba(87, 229, 171, .025),
      0 0 0 140px rgba(87, 229, 171, .018);
  }}
  header {{ position: relative; display: flex; justify-content: space-between;
    align-items: flex-end; gap: 24px; }}
  .kicker {{ color: #62e8bd; font-size: 13px; font-weight: 900;
    letter-spacing: 4px; }}
  h1 {{ margin: 8px 0 0; font-size: 38px; line-height: 1.12; letter-spacing: 1px; }}
  .health-row {{ display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; }}
  .health {{ padding: 7px 11px; border-radius: 999px; font-size: 11px;
    font-weight: 800; letter-spacing: 1px; }}
  .health.live {{ color: #75f0c2; border: 1px solid rgba(117, 240, 194, .25);
    background: rgba(117, 240, 194, .08); }}
  .health.stale {{ color: #ffd074; border: 1px solid rgba(255, 208, 116, .26);
    background: rgba(255, 208, 116, .08); }}
  .health.degraded {{ color: #ff9a8b; border: 1px solid rgba(255, 154, 139, .25);
    background: rgba(255, 154, 139, .08); }}
  .summary {{ position: relative; display: grid; grid-template-columns: 1.25fr 1fr 1fr;
    gap: 13px; margin: 27px 0 22px; }}
  .summary-item {{ min-height: 104px; padding: 18px 20px; border-radius: 18px;
    border: 1px solid rgba(255, 255, 255, .07);
    background: rgba(255, 255, 255, .045); }}
  .summary-item.primary {{ background: linear-gradient(135deg,
    rgba(80, 231, 176, .14), rgba(255, 255, 255, .035));
    border-color: rgba(80, 231, 176, .16); }}
  .summary-item strong {{ display: block; font-size: 32px; line-height: 1; }}
  .summary-item.primary strong {{ color: #67ebbc; }}
  .summary-item span {{ display: block; margin-top: 10px; color: #8297ad;
    font-size: 12px; font-weight: 700; letter-spacing: 1px; }}
  .entries {{ position: relative; display: grid; grid-template-columns: 1fr 1fr;
    gap: 13px; }}
  .presence-entry {{ min-height: 132px; display: grid; grid-template-columns: 74px 1fr;
    overflow: hidden; border-radius: 18px; border: 1px solid rgba(255, 255, 255, .065);
    background: rgba(255, 255, 255, .038); }}
  .presence-entry.playing {{ border-color: rgba(97, 235, 184, .19);
    background: linear-gradient(135deg, rgba(75, 218, 166, .11),
      rgba(255, 255, 255, .035)); }}
  .status-rail {{ display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 9px; border-right: 1px solid rgba(255, 255, 255, .055);
    background: rgba(0, 0, 0, .13); }}
  .status-rail i {{ width: 13px; height: 13px; border-radius: 50%;
    background: #748397; box-shadow: 0 0 0 5px rgba(116, 131, 151, .10); }}
  .status-rail span {{ color: #708399; font-size: 8px; font-weight: 900;
    letter-spacing: 1px; }}
  .playing .status-rail i, .online .status-rail i, .trade .status-rail i,
  .looking .status-rail i {{ background: #5ce7b4;
    box-shadow: 0 0 16px rgba(92, 231, 180, .65); }}
  .away .status-rail i, .snooze .status-rail i {{ background: #f4c96b;
    box-shadow: 0 0 14px rgba(244, 201, 107, .4); }}
  .busy .status-rail i {{ background: #ff7d79;
    box-shadow: 0 0 14px rgba(255, 125, 121, .42); }}
  .entry-copy {{ min-width: 0; padding: 16px 18px; }}
  .entry-topline {{ display: flex; align-items: center; justify-content: space-between;
    gap: 12px; }}
  .entry-topline strong {{ min-width: 0; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; font-size: 18px; }}
  .status-pill {{ flex: 0 0 auto; padding: 4px 8px; color: #8fa5bb;
    background: rgba(255, 255, 255, .05); border-radius: 7px; font-size: 10px; }}
  .playing .status-pill {{ color: #65e7b8; background: rgba(101, 231, 184, .09); }}
  .game-name {{ margin-top: 9px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; color: #f0f5fc; font-size: 14px; font-weight: 800; }}
  .game-name.muted {{ color: #71859a; font-weight: 500; }}
  .entry-detail {{ margin-top: 5px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; color: #b5c2d1; font-size: 11px; }}
  .identifiers {{ margin-top: 6px; color: #60758b; font-size: 9px; letter-spacing: .3px; }}
  .empty-presence {{ grid-column: 1 / -1; min-height: 190px; display: flex;
    flex-direction: column; align-items: center; justify-content: center; gap: 9px;
    border: 1px dashed rgba(130, 158, 190, .18); border-radius: 20px;
    color: #8195aa; background: rgba(255, 255, 255, .025); }}
  .empty-presence strong {{ color: #c6d3df; font-size: 19px; }}
  .empty-presence span {{ font-size: 13px; }}
  .notice {{ position: relative; display: flex; align-items: center; gap: 10px;
    margin-top: 17px; padding: 12px 15px; border-radius: 13px; color: #9eafc1;
    background: rgba(76, 119, 168, .09); border: 1px solid rgba(103, 155, 210, .11);
    font-size: 12px; }}
  .notice i {{ display: grid; place-items: center; flex: 0 0 auto; width: 19px;
    height: 19px; border-radius: 50%; color: #75b8ff; font-size: 11px;
    font-style: normal; font-weight: 900; border: 1px solid rgba(117, 184, 255, .35); }}
  footer {{ position: relative; display: flex; justify-content: space-between;
    margin-top: 22px; color: #60758a; font-size: 11px; letter-spacing: .5px; }}
</style>
</head>
<body>
<main id="game-status-card">
  <header>
    <div>
      <div class="kicker">{_escape(snapshot.source_label)}</div>
      <h1>{_escape(snapshot.title)}</h1>
    </div>
    <div class="health-row">{health_badges}</div>
  </header>
  <section class="summary">
    <div class="summary-item primary"><strong>{snapshot.playing_count}</strong>
      <span>正在游戏 / PLAYING</span></div>
    <div class="summary-item"><strong>{non_playing_visible}</strong>
      <span>其他公开状态 / VISIBLE</span></div>
    <div class="summary-item"><strong>{snapshot.hidden_count}</strong>
      <span>离线或不可见 / HIDDEN</span></div>
  </section>
  <section class="entries">{entries_html}</section>
  {note}
  <footer>
    <span>已绑定成员 {snapshot.total_count} 人 · 当前公开 {visible_count} 人</span>
    <span>NIUNIU GAME PRESENCE</span>
  </footer>
</main>
</body>
</html>"""


def render_watch_stage_html(snapshot: WatchStageSnapshot) -> str:
    source_label = (
        "CS2 OFFICIAL · WATCH STAGE"
        if snapshot.platform == "1"
        else "PERFECT WORLD · WATCH STAGE"
    )

    def side_rows(side: str) -> str:
        rows = []
        for player in (item for item in snapshot.players if item.side == side):
            alive_class = "alive" if player.alive else "dead"
            alive_label = "ALIVE" if player.alive else "DEAD"
            member = '<span class="member">群友</span>' if player.is_group_member else ""
            pvp_score = "-" if player.pvp_score is None else str(player.pvp_score)
            rating = "-" if player.avg_rating is None else f"{player.avg_rating:.2f}"
            profile_metrics = (
                f"Rating {rating}"
                if snapshot.platform == "1"
                else f"ELO {pvp_score} · Rating {rating}"
            )
            rows.append(
                f"""
                <div class="live-row {alive_class}">
                  <div class="life"><i></i><span>{alive_label}</span></div>
                  <div class="player">
                    <strong>{_escape(player.nickname)}{member}</strong>
                    <span>{_escape(player.steam_id)} · {_escape(profile_metrics)}</span>
                  </div>
                  <div><strong>{player.kills}</strong><span>K</span></div>
                  <div><strong>{player.assists}</strong><span>A</span></div>
                  <div><strong>{player.deaths}</strong><span>D</span></div>
                  <div><strong>{player.adr:.0f}</strong><span>ADR</span></div>
                  <div><strong>{_escape(player.headshot)}</strong><span>HS</span></div>
                </div>
                """
            )
        return "".join(rows) or '<div class="empty-side">暂无阵容数据</div>'

    target = snapshot.requested_name or snapshot.requested_steam_id
    state = "热身中" if snapshot.warmup else snapshot.duration or "进行中"
    updated = snapshot.updated_at.replace("T", " ")[:19]
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
body {{ margin:0; background:transparent; color:#f4f7fb;
  font-family:"Noto Sans CJK SC","Noto Sans SC",sans-serif; }}
.watch {{ width:1160px; min-height:840px; padding:38px; overflow:hidden; position:relative;
  border-radius:30px; background:
    radial-gradient(circle at 50% -20%,rgba(70,225,196,.16),transparent 42%),
    linear-gradient(145deg,#071018,#11232d 52%,#070e14);
  border:1px solid rgba(120,230,214,.2); box-shadow:0 30px 80px rgba(0,0,0,.5); }}
.watch::after {{ content:"LIVE"; position:absolute; right:34px; top:30px; color:#ff6b72;
  font-size:13px; font-weight:900; letter-spacing:4px; padding:7px 12px;
  border-radius:999px; border:1px solid rgba(255,107,114,.35);
  background:rgba(255,107,114,.08); }}
.kicker {{ color:#62e2c5; font-size:13px; font-weight:900; letter-spacing:4px; }}
h1 {{ margin:7px 0 2px; font-size:38px; letter-spacing:2px; }}
.sub {{ color:#81969f; font-size:13px; }}
.scoreboard {{ display:grid; grid-template-columns:1fr 250px 1fr; align-items:center;
  margin:26px 0 24px; padding:20px 24px; border-radius:20px;
  background:rgba(255,255,255,.045); border:1px solid rgba(255,255,255,.07); }}
.team {{ font-size:17px; font-weight:900; letter-spacing:2px; }}
.team.ct {{ color:#70a9ff; }}
.team.t {{ color:#ffb45f; text-align:right; }}
.score {{ text-align:center; }}
.score strong {{ display:block; font-size:43px; line-height:1; letter-spacing:8px; }}
.score span {{ display:block; margin-top:7px; color:#8da0a8; font-size:13px; }}
.columns {{ display:grid; grid-template-columns:1fr 1fr; gap:15px; }}
.side {{ padding:14px; border-radius:20px; background:rgba(0,0,0,.18);
  border:1px solid rgba(255,255,255,.065); }}
.side-title {{ display:flex; justify-content:space-between; align-items:center;
  margin:0 4px 11px; font-size:14px; font-weight:900; letter-spacing:2px; }}
.side-title span {{ color:#71858d; font-size:11px; font-weight:500; letter-spacing:0; }}
.ct-side .side-title strong {{ color:#70a9ff; }}
.t-side .side-title strong {{ color:#ffb45f; }}
.live-row {{ min-height:76px; display:grid;
  grid-template-columns:44px minmax(190px,1fr) repeat(5,48px); gap:4px;
  align-items:center; padding:8px 9px; margin-top:7px; border-radius:14px;
  background:rgba(255,255,255,.045); border:1px solid rgba(255,255,255,.055); }}
.live-row.dead {{ opacity:.55; }}
.life {{ text-align:center; }}
.life i {{ display:block; width:9px; height:9px; margin:0 auto 5px; border-radius:50%;
  background:#65e1bd; box-shadow:0 0 12px rgba(101,225,189,.65); }}
.dead .life i {{ background:#68777d; box-shadow:none; }}
.life span {{ color:#71858d; font-size:8px; font-style:normal; }}
.player {{ min-width:0; padding-left:3px; }}
.player strong {{ display:block; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
  font-size:15px; }}
.player>span {{ display:block; overflow:hidden; white-space:nowrap; text-overflow:ellipsis;
  color:#71858d; font-size:9px; margin-top:3px; }}
.member {{ display:inline-block; margin-left:5px; padding:2px 5px; vertical-align:2px;
  border-radius:5px; color:#07201a; background:#62e2c5; font-size:8px; }}
.live-row>div:not(.player):not(.life) {{ text-align:right; }}
.live-row>div>strong {{ display:block; font-size:14px; }}
.live-row>div>span {{ color:#71858d; font-size:9px; }}
.empty-side {{ padding:40px; text-align:center; color:#71858d; }}
footer {{ display:flex; justify-content:space-between; margin-top:20px;
  color:#657a83; font-size:11px; }}
</style>
</head>
<body>
<main id="watch-stage-card" class="watch">
  <header>
    <div class="kicker">{source_label}</div>
    <h1>{_escape(display_map_name(snapshot.map_name))}</h1>
    <div class="sub">查询目标：{_escape(target)} · Match {_escape(snapshot.match_id or "-")}</div>
  </header>
  <section class="scoreboard">
    <div class="team ct">COUNTER-TERRORISTS</div>
    <div class="score"><strong>{snapshot.ct_score or 0} : {snapshot.terrorist_score or 0}</strong>
      <span>{_escape(state)}</span></div>
    <div class="team t">TERRORISTS</div>
  </section>
  <section class="columns">
    <div class="side ct-side"><div class="side-title"><strong>CT 阵营</strong>
      <span>实时 K / A / D</span></div>{side_rows("CT")}</div>
    <div class="side t-side"><div class="side-title"><strong>T 阵营</strong>
      <span>实时 K / A / D</span></div>{side_rows("TERRORIST")}</div>
  </section>
  <footer><span>● 存活　○ 阵亡　群友以绿色标记</span>
    <span>快照 {updated} UTC · {_escape(source_label)}</span></footer>
</main>
</body>
</html>"""


def render_stats_html(
    stats: StatsSnapshot,
    *,
    avatar_data_uri: str | None = None,
    background_key: str = "classic",
    background_data_uri: str | None = None,
    member_badge: str = "",
) -> str:
    official = stats.source == "official"
    eyebrow = "CS2 OFFICIAL · RECENT MATCHES" if official else "PERFECT WORLD · CS2"
    score_label = "胜-负-平" if official else "完美分数"
    summary_label = "官匹说明" if official else "赛季评价"
    footer_left = (
        "官匹不提供 ELO · WE · RWS"
        if official
        else f"RWS {stats.rws} · MVP {stats.mvp}"
    )
    footer_right = (
        "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD"
        if official
        else "PWCS2 BOT · DATA FROM PERFECT WORLD"
    )
    theme = STATS_BACKGROUNDS.get(
        background_key,
        STATS_BACKGROUNDS["classic"],
    )
    if background_data_uri:
        card_background = (
            "linear-gradient(145deg, rgba(5,8,16,.88) 0%, "
            "rgba(5,8,16,.70) 54%, rgba(5,8,16,.82) 100%), "
            f'url("{_escape(background_data_uri)}") center / cover no-repeat'
        )
    else:
        card_background = theme.css_background or (
            "radial-gradient(circle at 86% 8%, rgba(245,158,11,.28), "
            "transparent 29%), "
            "radial-gradient(circle at 7% 95%, rgba(42,105,180,.22), "
            "transparent 34%), "
            "linear-gradient(145deg, #121825 0%, #0b101a 56%, #111722 100%)"
        )
    map_rows = "".join(
        f"""
        <div class="map-row">
          <div class="map-line">
            <span class="map-name">{_escape(item.name)}</span>
            <span class="map-meta">{_escape(item.matches)} 场 · {_escape(item.win_rate)}</span>
          </div>
          <div class="track"><span style="width:{_bar_width(item.win_percent)}%"></span></div>
        </div>
        """
        for item in stats.maps
    ) or '<div class="empty">暂无地图数据</div>'
    weapon_rows = "".join(
        f"""
        <div class="weapon">
          <div class="weapon-name">{_escape(item.name)}</div>
          <div class="weapon-kills"><strong>{_escape(item.kills)}</strong><span> KILLS</span></div>
        </div>
        """
        for item in stats.weapons
    ) or '<div class="empty">暂无武器数据</div>'
    if official:
        metrics_class = "metrics official-grid"
        metrics_html = f"""
        <div class="metric"><label>{_escape(score_label)}</label>
          <strong>{_escape(stats.score)}</strong></div>
        <div class="metric"><label>胜率</label><strong>{_escape(stats.win_rate)}</strong></div>
        <div class="metric"><label>K / D</label><strong>{_escape(stats.kd)}</strong></div>
        <div class="metric"><label>场次</label><strong>{_escape(stats.matches)}</strong></div>
        """
        secondary_panel_title = "DATA SCOPE · 官匹口径"
        secondary_panel_body = (
            '<div class="empty">按最近最多 50 场正式官匹汇总；'
            "不混入完美平台 ELO、WE、急停、RWS、武器或赛季字段。</div>"
        )
    else:
        metrics_class = "metrics"
        metrics_html = f"""
        <div class="metric"><label>{_escape(score_label)}</label>
          <strong>{_escape(stats.score)}</strong></div>
        <div class="metric"><label>胜率</label><strong>{_escape(stats.win_rate)}</strong></div>
        <div class="metric"><label>K / D</label><strong>{_escape(stats.kd)}</strong></div>
        <div class="metric"><label>ADR</label><strong>{_escape(stats.adr)}</strong></div>
        <div class="metric"><label>爆头率</label>
          <strong>{_escape(stats.headshot_rate)}</strong></div>
        <div class="metric"><label>场次</label><strong>{_escape(stats.matches)}</strong></div>
        """
        secondary_panel_title = "ARSENAL · 常用武器"
        secondary_panel_body = f'<div class="weapons">{weapon_rows}</div>'
    summary = stats.summary or "保持手感，下一局继续突破"
    if avatar_data_uri:
        avatar = (
            '<div class="avatar"><img src="'
            f'{_escape(avatar_data_uri)}" alt="{_escape(stats.name)} 头像"></div>'
        )
    else:
        avatar_fallback = "MM" if official else "PW"
        avatar = (
            '<div class="avatar avatar-fallback" aria-hidden="true">'
            f"{avatar_fallback}</div>"
        )
    member_badge_html = (
        f'<span class="member-title" title="{_escape(member_badge)}">'
        f"{_escape(member_badge)}</span>"
        if member_badge
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans Math", "Noto Sans Symbols2",
      "Noto Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    color: #f6f7fb;
  }}
  .card {{
    --accent: {theme.accent};
    --accent-secondary: {theme.accent_secondary};
    --accent-rgb: {theme.accent_rgb};
    width: 960px;
    min-height: 720px;
    padding: 42px 44px 34px;
    position: relative;
    overflow: hidden;
    border: 1px solid rgba(255,255,255,.12);
    border-radius: 30px;
    background: {card_background};
    box-shadow: 0 28px 90px rgba(0,0,0,.48);
  }}
  .card::before {{
    content: "";
    position: absolute;
    inset: 0;
    opacity: .28;
    pointer-events: none;
    background-image:
      linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 34px 34px;
  }}
  .accent {{
    position: absolute;
    width: 8px;
    height: 124px;
    top: 0;
    left: 24px;
    border-radius: 0 0 8px 8px;
    background: linear-gradient(var(--accent), var(--accent-secondary));
    box-shadow: 0 0 30px rgba(var(--accent-rgb),.42);
  }}
  header {{ position: relative; display: flex; justify-content: space-between; gap: 30px; }}
  .player-info {{ flex: 1 1 auto; min-width: 0; }}
  .eyebrow {{
    color: var(--accent);
    font-size: 15px;
    font-weight: 800;
    letter-spacing: 3px;
    margin: 0 0 12px;
  }}
  .player-row {{
    display: flex;
    align-items: center;
    gap: 17px;
    min-width: 0;
  }}
  h1 {{
    flex: 1 1 auto;
    min-width: 0;
    margin: 0;
    font-size: 42px;
    line-height: 1.18;
    letter-spacing: -.8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .avatar {{
    width: 78px;
    height: 78px;
    flex: 0 0 78px;
    padding: 3px;
    overflow: hidden;
    border-radius: 22px;
    background: linear-gradient(145deg, var(--accent), var(--accent-secondary));
    border: 1px solid rgba(var(--accent-rgb),.42);
    box-shadow: 0 10px 28px rgba(0,0,0,.34), 0 0 24px rgba(var(--accent-rgb),.13);
  }}
  .avatar img {{
    display: block;
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: 18px;
    background: #171e2b;
  }}
  .avatar-fallback {{
    display: grid;
    place-items: center;
    padding: 0;
    color: var(--accent);
    background: rgba(var(--accent-rgb),.09);
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 1px;
  }}
  .identity {{ margin-top: 13px; color: #8f9bb0; font-size: 16px; letter-spacing: .4px; }}
  .member-title {{
    display: inline-block;
    max-width: 220px;
    margin-right: 10px;
    padding: 5px 11px;
    overflow: hidden;
    vertical-align: middle;
    border-radius: 999px;
    color: var(--accent);
    background: rgba(var(--accent-rgb),.12);
    border: 1px solid rgba(var(--accent-rgb),.28);
    font-size: 13px;
    font-weight: 800;
    letter-spacing: .3px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .season {{
    display: inline-flex;
    padding: 5px 11px;
    margin-right: 10px;
    border-radius: 999px;
    color: var(--accent);
    background: rgba(var(--accent-rgb),.12);
    border: 1px solid rgba(var(--accent-rgb),.28);
    font-weight: 700;
  }}
  .rating {{
    width: 156px;
    height: 132px;
    flex: 0 0 auto;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    border-radius: 24px;
    background: linear-gradient(145deg, rgba(255,255,255,.10), rgba(255,255,255,.035));
    border: 1px solid rgba(255,255,255,.13);
    box-shadow: inset 0 1px rgba(255,255,255,.08);
  }}
  .rating strong {{ font-size: 45px; line-height: 1; color: var(--accent); }}
  .rating span {{
    margin-top: 9px;
    color: #939fb2;
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 2px;
  }}
  .metrics {{
    position: relative;
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 10px;
    margin-top: 30px;
  }}
  .metrics.official-grid {{ grid-template-columns: repeat(4, 1fr); }}
  .metric {{
    min-width: 0;
    padding: 18px 13px 16px;
    border-radius: 17px;
    background: rgba(255,255,255,.055);
    border: 1px solid rgba(255,255,255,.075);
  }}
  .metric label {{ display: block; color: #818da1; font-size: 12px; letter-spacing: 1px; }}
  .metric strong {{
    display: block;
    margin-top: 5px;
    font-size: 25px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .metric:nth-child(2) strong, .metric:nth-child(3) strong {{ color: var(--accent); }}
  .content {{
    position: relative;
    display: grid;
    grid-template-columns: 1.12fr .88fr;
    gap: 16px;
    margin-top: 16px;
  }}
  .panel {{
    padding: 22px 23px;
    border-radius: 21px;
    background: rgba(5,9,15,.48);
    border: 1px solid rgba(255,255,255,.075);
  }}
  .panel-title {{
    color: #aab3c3;
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 2px;
    margin-bottom: 16px;
  }}
  .map-row + .map-row {{ margin-top: 15px; }}
  .map-line {{ display: flex; justify-content: space-between; gap: 14px; margin-bottom: 8px; }}
  .map-name {{
    max-width: 230px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .map-meta {{ color: #8c98aa; font-size: 14px; white-space: nowrap; }}
  .track {{
    height: 7px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(255,255,255,.075);
  }}
  .track span {{
    display: block;
    height: 100%;
    border-radius: inherit;
    background: linear-gradient(90deg,var(--accent-secondary),var(--accent));
  }}
  .weapons {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
  .weapon {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    min-height: 47px;
    padding: 10px 13px;
    border-radius: 13px;
    background: rgba(255,255,255,.045);
  }}
  .weapon-name {{
    max-width: 150px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .weapon-kills strong {{ color: var(--accent); font-size: 20px; }}
  .weapon-kills span {{ color: #737f92; font-size: 10px; letter-spacing: 1px; }}
  .kad {{
    position: relative;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 1px;
    margin-top: 16px;
    overflow: hidden;
    border-radius: 17px;
    background: rgba(255,255,255,.08);
    border: 1px solid rgba(255,255,255,.075);
  }}
  .kad div {{
    padding: 14px 18px;
    background: #111722;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .kad span {{ color: #7f8a9d; font-size: 12px; letter-spacing: 1px; }}
  .kad strong {{ font-size: 22px; }}
  .summary {{
    position: relative;
    margin-top: 15px;
    padding: 15px 19px;
    border-left: 4px solid var(--accent);
    border-radius: 6px 14px 14px 6px;
    color: #c9d0dc;
    background: rgba(var(--accent-rgb),.075);
    font-size: 15px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  footer {{
    position: relative;
    display: flex;
    justify-content: space-between;
    margin-top: 22px;
    color: #606c7e;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.5px;
  }}
  .empty {{ color: #667286; padding: 12px 0; }}
</style>
</head>
<body>
<main id="stats-card" class="card" data-testid="stats-card">
  <div class="accent"></div>
  <header>
    <div class="player-info">
      <p class="eyebrow">{_escape(eyebrow)}</p>
      <div class="player-row">
        <h1>{_escape(stats.name)}</h1>
        {avatar}
      </div>
      <div class="identity">
        <span class="season">{_escape(stats.season)}</span>
        {member_badge_html}
        STEAM · {_escape(stats.steam_id)}
      </div>
    </div>
    <div class="rating"><strong>{_escape(stats.rating)}</strong><span>RATING</span></div>
  </header>
  <section class="{metrics_class}">
    {metrics_html}
  </section>
  <section class="content">
    <div class="panel">
      <div class="panel-title">HOT MAPS · 常用地图</div>
      {map_rows}
    </div>
    <div class="panel">
      <div class="panel-title">{_escape(secondary_panel_title)}</div>
      {secondary_panel_body}
    </div>
  </section>
  <section class="kad">
    <div><span>KILLS</span><strong>{_escape(stats.kills)}</strong></div>
    <div><span>ASSISTS</span><strong>{_escape(stats.assists)}</strong></div>
    <div><span>DEATHS</span><strong>{_escape(stats.deaths)}</strong></div>
  </section>
  <div class="summary">{_escape(summary_label)} · {_escape(summary)}</div>
  <footer>
    <span>{_escape(footer_left)}</span>
    <span>{_escape(footer_right)}</span>
  </footer>
</main>
</body>
</html>"""


def _ranking_number(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    if value.is_integer():
        return str(int(value))
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _ranking_percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}%"


def _ranking_cell_class(metric: RankingMetric, column: RankingMetric) -> str:
    return "metric-active" if metric == column else ""


def _render_ranking_row(
    entry: RankingEntry,
    *,
    position: int,
    metric: RankingMetric,
    source: str,
) -> str:
    medal = f"podium podium-{position}" if position <= 3 else "position"
    school_rank = f"校内 #{entry.school_rank}" if entry.school_rank is not None else ""
    if source == "official":
        matches_class = _ranking_cell_class(metric, "matches")
        rating_class = _ranking_cell_class(metric, "rating")
        win_class = _ranking_cell_class(metric, "win_rate")
        kd_class = _ranking_cell_class(metric, "kd")
        return f"""
        <div class="ranking-row official-grid">
          <div class="{medal}">{position}</div>
          <div class="rank-player">
            <strong>{_escape(entry.name)}</strong>
            <span>Steam {_escape(entry.steam_id)} · {_escape(entry.season)}</span>
          </div>
          <div class="{matches_class}">{_ranking_number(entry.matches, 0)}</div>
          <div class="{rating_class}">{_ranking_number(entry.rating)}</div>
          <div class="{win_class}">{_ranking_percent(entry.win_rate)}</div>
          <div class="{kd_class}">{_ranking_number(entry.kd)}</div>
        </div>
        """
    elo_class = _ranking_cell_class(metric, "elo")
    rating_class = _ranking_cell_class(metric, "rating")
    win_class = _ranking_cell_class(metric, "win_rate")
    adr_class = _ranking_cell_class(metric, "adr")
    stop_class = _ranking_cell_class(metric, "rapid_stop")
    headshot_class = _ranking_cell_class(metric, "headshot")
    return f"""
    <div class="ranking-row">
      <div class="{medal}">{position}</div>
      <div class="rank-player">
        <strong>{_escape(entry.name)}</strong>
        <span>Steam {_escape(entry.steam_id)}{f' · {school_rank}' if school_rank else ''}</span>
      </div>
      <div class="{elo_class}">{_ranking_number(entry.elo, 0)}</div>
      <div class="{rating_class}">{_ranking_number(entry.rating)}</div>
      <div class="{win_class}">{_ranking_percent(entry.win_rate)}</div>
      <div class="{adr_class}">{_ranking_number(entry.adr, 1)}</div>
      <div class="{stop_class}">{_ranking_percent(entry.rapid_stop)}</div>
      <div class="{headshot_class}">{_ranking_percent(entry.headshot)}</div>
    </div>
    """


def render_ranking_html(ranking: RankingSnapshot) -> str:
    official = ranking.source == "official"
    visible_entries = ranking.entries[:30]
    rows = "".join(
        _render_ranking_row(
            entry,
            position=index,
            metric=ranking.metric,
            source=ranking.source,
        )
        for index, entry in enumerate(visible_entries, start=1)
    )
    if official:
        season = "每人最近最多 50 场"
    else:
        season_values = {entry.season for entry in ranking.entries if entry.season != "-"}
        season = " / ".join(sorted(season_values)) if season_values else "当前赛季"
    omitted = len(ranking.entries) - len(visible_entries)
    notes = [f"有效数据 {len(ranking.entries)}/{ranking.total_count} 人"]
    if ranking.failed_count:
        notes.append(f"{ranking.failed_count} 人查询失败")
    if omitted:
        notes.append(f"另有 {omitted} 人未展示")
    eyebrow = (
        "GROUP LEADERBOARD · CS2 OFFICIAL"
        if official
        else "GROUP LEADERBOARD · PERFECT WORLD CS2"
    )
    subtitle = (
        f"{season} · 最近官匹汇总"
        if official
        else f"{season} · 完美平台扩展战绩"
    )
    table_head = (
        """
        <div class="table-head official-grid">
          <span>排名</span><span>玩家</span><span>场次</span>
          <span>Rating</span><span>胜率</span><span>K/D</span>
        </div>
        """
        if official
        else """
        <div class="table-head">
          <span>排名</span><span>玩家</span><span>ELO</span><span>Rating</span>
          <span>胜率</span><span>ADR</span><span>急停率</span><span>爆头率</span>
        </div>
        """
    )
    footer_left = (
        "官匹排行按每人最近 50 场有效记录统计"
        if official
        else "数据按当前完美平台赛季统计"
    )
    footer_right = (
        "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD"
        if official
        else "PWCS2 BOT · DATA FROM PERFECT WORLD"
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans Math", "Noto Sans Symbols2",
      "Noto Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    color: #f7f8fb;
  }}
  .ranking-card {{
    width: 1080px; min-height: 620px; position: relative; overflow: hidden;
    padding: 38px 42px 30px; border: 1px solid rgba(255,255,255,.13);
    border-radius: 30px;
    background:
      radial-gradient(circle at 88% 3%, rgba(245,158,11,.24), transparent 27%),
      radial-gradient(circle at 0 100%, rgba(35,103,185,.2), transparent 34%),
      linear-gradient(145deg, #121925 0%, #090e16 58%, #111722 100%);
    box-shadow: 0 28px 90px rgba(0,0,0,.48);
  }}
  .ranking-card::before {{
    content: ""; position: absolute; inset: 0; opacity: .23; pointer-events: none;
    background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 34px 34px;
  }}
  .top-line {{
    position: absolute; left: 42px; right: 42px; top: 0; height: 5px;
    background: linear-gradient(90deg, #f07b18, #ffc84d 42%, transparent 78%);
  }}
  header {{ position: relative; display: flex; justify-content: space-between; gap: 24px; }}
  .eyebrow {{
    margin: 0 0 8px; color: #f4a329; font-size: 13px; font-weight: 900;
    letter-spacing: 3px;
  }}
  h1 {{ margin: 0; font-size: 38px; line-height: 1.2; }}
  .subtitle {{ margin-top: 8px; color: #7f8b9f; font-size: 13px; }}
  .sort-badge {{
    align-self: center; padding: 11px 17px; border: 1px solid rgba(245,158,11,.28);
    border-radius: 999px; color: #ffc34d; background: rgba(245,158,11,.08);
    font-size: 13px; font-weight: 850;
  }}
  .ranking-table {{
    position: relative; overflow: hidden; margin-top: 25px; border-radius: 20px;
    border: 1px solid rgba(255,255,255,.085); background: rgba(3,7,13,.58);
  }}
  .table-head, .ranking-row {{
    display: grid; grid-template-columns: 50px minmax(245px, 1fr) 78px 84px 80px 70px 102px 88px;
    align-items: center; column-gap: 10px; padding: 0 18px; text-align: center;
  }}
  .table-head.official-grid, .ranking-row.official-grid {{
    grid-template-columns: 50px minmax(330px, 1fr) 105px 120px 110px 110px;
  }}
  .table-head {{ min-height: 43px; color: #667286; font-size: 10px; font-weight: 850; }}
  .table-head span:nth-child(2) {{ text-align: left; }}
  .ranking-row {{
    min-height: 62px; border-top: 1px solid rgba(255,255,255,.055);
    color: #cbd2dd; font-size: 13px;
  }}
  .ranking-row:nth-child(even) {{ background: rgba(255,255,255,.018); }}
  .position {{ color: #657185; font-size: 14px; font-weight: 800; }}
  .podium {{
    width: 30px; height: 30px; display: grid; place-items: center; margin: auto;
    border-radius: 10px; color: #111722; font-size: 13px; font-weight: 950;
  }}
  .podium-1 {{ background: linear-gradient(145deg, #ffe28a, #f1a21d); }}
  .podium-2 {{ background: linear-gradient(145deg, #e5ecf5, #9ba9bd); }}
  .podium-3 {{ background: linear-gradient(145deg, #e9ac75, #a85c2e); }}
  .rank-player {{ min-width: 0; text-align: left; }}
  .rank-player strong {{
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: #f5f7fa; font-size: 15px;
  }}
  .rank-player span {{ display: block; margin-top: 3px; color: #657185; font-size: 9px; }}
  .metric-active {{ color: #ffc34d; font-size: 15px; font-weight: 900; }}
  .bottom {{
    position: relative; display: flex; justify-content: space-between; margin-top: 17px;
    color: #657185; font-size: 10px;
  }}
  footer {{
    position: relative; display: flex; justify-content: space-between; margin-top: 14px;
    color: #536074; font-size: 9px; font-weight: 700; letter-spacing: 1px;
  }}
</style>
</head>
<body>
<main id="ranking-card" class="ranking-card" data-testid="ranking-card">
  <div class="top-line"></div>
  <header>
    <div>
      <p class="eyebrow">{_escape(eyebrow)}</p>
      <h1>群内 {_escape(ranking.metric_label)} 排行榜</h1>
      <div class="subtitle">{_escape(subtitle)}</div>
    </div>
    <div class="sort-badge">按 {_escape(ranking.metric_label)} 排序</div>
  </header>
  <div class="ranking-table">
    {table_head}
    {rows}
  </div>
  <div class="bottom"><span>{_escape(' · '.join(notes))}</span><span>最多展示前 30 名</span></div>
  <footer>
    <span>{_escape(footer_left)}</span>
    <span>{_escape(footer_right)}</span>
  </footer>
</main>
</body>
</html>"""


def render_match_history_html(
    history: MatchHistorySnapshot,
    *,
    avatar_data_uri: str | None = None,
    background_key: str = "classic",
    background_data_uri: str | None = None,
    member_badge: str = "",
) -> str:
    official = history.source == "official"
    source_name = {
        "pvp": "完美",
        "official": "官匹",
        "mixed": "综合",
    }[history.source]
    eyebrow = {
        "pvp": "RECENT MATCHES · PERFECT WORLD",
        "official": "RECENT MATCHES · CS2 OFFICIAL",
        "mixed": "RECENT MATCHES · PERFECT WORLD + OFFICIAL",
    }[history.source]
    footer_scope = {
        "pvp": f"最近 {history.total} 场完美平台比赛",
        "official": f"最近 {history.total} 场官匹比赛",
        "mixed": f"最近 {history.total} 场完美平台与官匹比赛",
    }[history.source]
    footer_source = (
        "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD"
        if official
        else "PWCS2 BOT · MATCH DATA VIA PERFECT WORLD"
    )
    theme = STATS_BACKGROUNDS.get(
        background_key,
        STATS_BACKGROUNDS["classic"],
    )
    if background_data_uri:
        card_background = (
            "linear-gradient(145deg, rgba(5,8,16,.90) 0%, "
            "rgba(5,8,16,.76) 56%, rgba(5,8,16,.84) 100%), "
            f'url("{_escape(background_data_uri)}") center / cover no-repeat'
        )
    else:
        card_background = theme.css_background or (
            "radial-gradient(circle at 90% 0, rgba(245,158,11,.24), "
            "transparent 27%), "
            "radial-gradient(circle at 0 100%, rgba(30,91,166,.22), "
            "transparent 34%), "
            "linear-gradient(145deg, #121925, #090e16 58%, #111722)"
        )
    if avatar_data_uri:
        avatar = f'<img src="{_escape(avatar_data_uri)}" alt="玩家头像">'
    else:
        avatar = f'<span aria-hidden="true">{"MM" if official else "PW"}</span>'
    member_badge_html = (
        f'<span class="history-member-title" title="{_escape(member_badge)}">'
        f"{_escape(member_badge)}</span>"
        if member_badge
        else ""
    )
    rows: list[str] = []
    for index, match in enumerate(history.matches, start=1):
        result_class = {
            "win": "history-win",
            "loss": "history-loss",
            "draw": "history-draw",
        }.get(match.result, "history-finished")
        change_class = ""
        if match.score_change.startswith("+"):
            change_class = "history-positive"
        elif match.score_change not in {"-", "0"} and match.score_change.startswith("-"):
            change_class = "history-negative"
        rows.append(
            f"""
            <div class="history-row">
              <div class="history-index">{index}</div>
              <div class="history-result {result_class}">{_escape(match.result_label)}</div>
              <div class="history-map">
                <strong>{_escape(match.map_name)}</strong>
                <span>{_escape(match.ended_at)} · {_escape(match.mode)}</span>
              </div>
              <div class="history-score">
                {_escape(match.own_score)}:{_escape(match.opponent_score)}
              </div>
              <div>{_escape(match.kills)}/{_escape(match.assists)}/{_escape(match.deaths)}</div>
              <div>{_escape(match.kd)}</div>
              <div class="history-rating">{_escape(match.rating)}</div>
              <div>{_escape(match.we)}</div>
              <div class="{change_class}">{_escape(match.score_change)}</div>
            </div>
            """
        )
    empty_label = "官匹" if official else "完美平台"
    match_rows = (
        "".join(rows)
        or f'<div class="history-empty">暂无可用的{empty_label}比赛记录</div>'
    )
    history_head = """
        <div class="history-head">
          <span>#</span><span>结果</span><span>地图 / 时间</span><span>比分</span>
          <span>K / A / D</span><span>K/D</span><span>Rating</span><span>WE</span><span>ELO</span>
        </div>
        """
    fourth_summary = (
        f"<div><strong>{_escape(history.elo_change)}</strong><span>ELO 变化</span></div>"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans", "Microsoft YaHei", sans-serif;
    color: #f5f7fb;
  }}
  .history-card {{
    --accent: {theme.accent};
    --accent-secondary: {theme.accent_secondary};
    --accent-rgb: {theme.accent_rgb};
    width: 1080px; min-height: 680px; position: relative; overflow: hidden;
    padding: 38px 40px 28px; border-radius: 30px;
    border: 1px solid rgba(255,255,255,.12);
    background: {card_background};
  }}
  .history-card::before {{
    content: ""; position: absolute; inset: 0; opacity: .2; pointer-events: none;
    background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 34px 34px;
  }}
  .history-accent {{
    position: absolute; left: 40px; right: 40px; top: 0; height: 5px;
    background: linear-gradient(
      90deg, var(--accent-secondary), var(--accent) 42%, transparent 80%
    );
  }}
  header {{ position: relative; display: flex; align-items: center; gap: 18px; }}
  .history-avatar {{
    width: 70px; height: 70px; display: grid; place-items: center; overflow: hidden;
    flex: 0 0 auto; border-radius: 20px; color: #151a23; background: var(--accent);
    font-weight: 950;
  }}
  .history-avatar img {{ width: 100%; height: 100%; object-fit: cover; }}
  .history-title {{ min-width: 0; flex: 1; }}
  .history-eyebrow {{
    color: var(--accent); font-size: 12px; font-weight: 900; letter-spacing: 3px;
  }}
  h1 {{
    margin: 5px 0 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-size: 31px;
  }}
  .history-steam {{
    display: flex; align-items: center; gap: 8px; min-width: 0;
    margin-top: 4px; color: #657185; font-size: 10px;
  }}
  .history-member-title {{
    display: inline-block; max-width: 190px; padding: 3px 8px;
    overflow: hidden; flex: 0 1 auto; border-radius: 999px;
    color: var(--accent); background: rgba(var(--accent-rgb),.12);
    border: 1px solid rgba(var(--accent-rgb),.28); font-size: 10px;
    font-weight: 800; text-overflow: ellipsis; white-space: nowrap;
  }}
  .history-summary {{ display: flex; gap: 10px; }}
  .history-summary div {{
    min-width: 82px; padding: 10px 12px; text-align: center; border-radius: 14px;
    background: rgba(255,255,255,.045); border: 1px solid rgba(255,255,255,.07);
  }}
  .history-summary strong {{ display: block; color: var(--accent); font-size: 18px; }}
  .history-summary span {{ color: #6e7a8e; font-size: 9px; }}
  .history-table {{
    position: relative; overflow: hidden; margin-top: 24px; border-radius: 19px;
    border: 1px solid rgba(255,255,255,.08); background: rgba(3,7,13,.56);
  }}
  .history-head, .history-row {{
    display: grid; grid-template-columns: 30px 54px minmax(260px,1fr) 60px 90px 48px 62px 48px 58px;
    align-items: center; column-gap: 9px; padding: 0 15px; text-align: center;
  }}
  .history-head {{ min-height: 42px; color: #667286; font-size: 9px; font-weight: 850; }}
  .history-head span:nth-child(3) {{ text-align: left; }}
  .history-row {{
    min-height: 64px; border-top: 1px solid rgba(255,255,255,.055);
    color: #cbd2dd; font-size: 12px;
  }}
  .history-row:nth-child(odd) {{ background: rgba(255,255,255,.018); }}
  .history-index {{ color: #667286; font-weight: 800; }}
  .history-result {{ padding: 5px 3px; border-radius: 8px; font-weight: 900; }}
  .history-win {{ color: #69e0a1; background: rgba(31,177,104,.12); }}
  .history-loss {{ color: #ff7f85; background: rgba(225,66,75,.12); }}
  .history-draw, .history-finished {{ color: #b7c0ce; background: rgba(255,255,255,.06); }}
  .history-map {{ min-width: 0; text-align: left; }}
  .history-map strong {{
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: #f5f7fa; font-size: 14px;
  }}
  .history-map span {{
    display: block; margin-top: 3px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; color: #657185; font-size: 9px;
  }}
  .history-score, .history-rating {{ color: #f5f7fa; font-size: 14px; font-weight: 900; }}
  .history-positive {{ color: #69e0a1; font-weight: 900; }}
  .history-negative {{ color: #ff7f85; font-weight: 900; }}
  .history-empty {{ padding: 100px 20px; text-align: center; color: #657185; }}
  footer {{
    position: relative; display: flex; justify-content: space-between;
    margin-top: 16px; color: #536074; font-size: 9px; letter-spacing: 1px;
  }}
</style>
</head>
<body>
<main id="history-card" class="history-card" data-testid="history-card">
  <div class="history-accent"></div>
  <header>
    <div class="history-avatar">{avatar}</div>
    <div class="history-title">
      <div class="history-eyebrow">{_escape(eyebrow)}</div>
      <h1>{_escape(history.name)} · {_escape(source_name)}近十场战绩</h1>
      <div class="history-steam">
        {member_badge_html}<span>SteamID · {_escape(history.steam_id)}</span>
      </div>
    </div>
    <div class="history-summary">
      <div><strong>{history.wins}-{history.losses}</strong><span>胜负</span></div>
      <div><strong>{_escape(history.win_rate)}</strong><span>胜率</span></div>
      <div><strong>{_escape(history.avg_rating)}</strong><span>平均 Rating</span></div>
      {fourth_summary}
    </div>
  </header>
  <section class="history-table">
    {history_head}
    {match_rows}
  </section>
  <footer>
    <span>{_escape(footer_scope)}</span>
    <span>{_escape(footer_source)}</span>
  </footer>
</main>
</body>
</html>"""


def _is_official_match(match: MatchSnapshot) -> bool:
    if match.source == "official":
        return True
    mode = match.mode.casefold()
    return (
        match.match_id.upper().startswith(("LOCAL@", "MM@"))
        or "官匹" in mode
        or "国服竞技" in mode
    )


def _match_card_source(match: MatchSnapshot) -> tuple[str, str, str]:
    if _is_official_match(match):
        return (
            "NEW MATCH · CS2 OFFICIAL",
            "PWCS2 BOT · OFFICIAL MATCH VIA PERFECT WORLD",
            "MM",
        )
    return (
        "NEW MATCH · PERFECT WORLD CS2",
        "PWCS2 BOT · DATA FROM PERFECT WORLD",
        "PW",
    )


def _render_single_match_html(
    match: MatchSnapshot,
    *,
    avatar_data_uri: str | None = None,
) -> str:
    eyebrow, source_footer, fallback_avatar = _match_card_source(match)
    if avatar_data_uri:
        avatar = (
            '<div class="match-avatar"><img src="'
            f'{_escape(avatar_data_uri)}" alt="{_escape(match.name)} 头像"></div>'
        )
    else:
        avatar = (
            '<div class="match-avatar match-avatar-fallback" aria-hidden="true">'
            f"{fallback_avatar}</div>"
        )
    mvp_badge = '<span class="badge mvp">MVP</span>' if match.mvp else ""
    multi_kill = ""
    if match.k5 not in {"-", "0"}:
        multi_kill = f'<span class="badge">五杀 × {_escape(match.k5)}</span>'
    elif match.k4 not in {"-", "0"}:
        multi_kill = f'<span class="badge">四杀 × {_escape(match.k4)}</span>'
    if match.score_change.startswith("+"):
        change_class = "positive"
    elif match.score_change not in {"-", "0"} and match.score_change.startswith("-"):
        change_class = "negative"
    else:
        change_class = ""
    metrics_html = f"""
    <div class="metric">
      <label>K / A / D</label>
      <strong>{_escape(match.kills)} / {_escape(match.assists)} / {_escape(match.deaths)}</strong>
    </div>
    <div class="metric"><label>K / D</label><strong>{_escape(match.kd)}</strong></div>
    <div class="metric"><label>RATING</label><strong>{_escape(match.rating)}</strong></div>
    <div class="metric"><label>WE</label><strong>{_escape(match.we)}</strong></div>
    <div class="metric"><label>完美分</label><strong>{_escape(match.pvp_score)}</strong></div>
    <div class="metric">
      <label>分数变化</label>
      <strong class="{change_class}">{_escape(match.score_change)}</strong>
    </div>
    """

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans Math", "Noto Sans Symbols2",
      "Noto Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    color: #f7f8fb;
  }}
  .match-card {{
    width: 960px;
    min-height: 580px;
    position: relative;
    overflow: hidden;
    padding: 38px 42px 30px;
    border: 1px solid rgba(255,255,255,.13);
    border-radius: 30px;
    background:
      radial-gradient(circle at 87% 6%, rgba(245,158,11,.25), transparent 30%),
      radial-gradient(circle at 2% 96%, rgba(35,103,185,.22), transparent 34%),
      linear-gradient(145deg, #121925 0%, #0a0f18 58%, #111722 100%);
    box-shadow: 0 28px 90px rgba(0,0,0,.48);
  }}
  .match-card::before {{
    content: ""; position: absolute; inset: 0; opacity: .27; pointer-events: none;
    background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 34px 34px;
  }}
  .top-line {{
    position: absolute; left: 42px; right: 42px; top: 0; height: 5px;
    background: linear-gradient(90deg, #f07b18, #ffc84d 42%, transparent 78%);
  }}
  header {{ position: relative; display: flex; align-items: center; gap: 18px; }}
  .player {{ flex: 1 1 auto; min-width: 0; }}
  .eyebrow {{
    margin: 0 0 8px; color: #f4a329; font-size: 14px; font-weight: 900;
    letter-spacing: 3px;
  }}
  h1 {{
    margin: 0; max-width: 610px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; font-size: 37px; line-height: 1.2; letter-spacing: -.6px;
  }}
  .steam {{ margin-top: 8px; color: #7f8b9f; font-size: 14px; letter-spacing: .5px; }}
  .match-avatar {{
    width: 76px; height: 76px; flex: 0 0 76px; overflow: hidden; padding: 3px;
    border-radius: 22px; border: 1px solid rgba(255,198,88,.42);
    background: linear-gradient(145deg, rgba(255,195,77,.8), rgba(240,120,24,.22));
    box-shadow: 0 10px 28px rgba(0,0,0,.34);
  }}
  .match-avatar img {{
    width: 100%; height: 100%; display: block; object-fit: cover; border-radius: 18px;
  }}
  .match-avatar-fallback {{
    display: grid; place-items: center; padding: 0; color: #ffc34d;
    background: rgba(245,158,11,.09); font-size: 21px; font-weight: 900;
  }}
  .hero {{
    position: relative; display: grid; grid-template-columns: 1fr 236px; gap: 18px;
    margin-top: 27px;
  }}
  .score-panel, .result-panel {{
    min-height: 177px; border-radius: 22px; border: 1px solid rgba(255,255,255,.085);
    background: rgba(4,8,14,.5);
  }}
  .score-panel {{ padding: 22px 26px; }}
  .map {{ color: #aab3c2; font-size: 14px; font-weight: 800; letter-spacing: 2px; }}
  .score {{ display: flex; align-items: baseline; gap: 15px; margin-top: 9px; }}
  .score strong {{ font-size: 64px; line-height: 1; letter-spacing: -2px; }}
  .score span {{ color: #566174; font-size: 35px; }}
  .mode {{ margin-top: 12px; color: #7d899c; font-size: 15px; }}
  .result-panel {{
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    background: linear-gradient(145deg, rgba(255,255,255,.085), rgba(255,255,255,.025));
  }}
  .result-label {{ font-size: 43px; font-weight: 950; letter-spacing: 3px; }}
  .result-win .result-label {{ color: #ffbd42; }}
  .result-loss .result-label {{ color: #f06b63; }}
  .result-draw .result-label, .result-finished .result-label {{ color: #b7c2d2; }}
  .result-panel small {{ margin-top: 8px; color: #778397; font-size: 13px; letter-spacing: 2px; }}
  .metrics {{
    position: relative; display: grid; grid-template-columns: repeat(6, 1fr); gap: 9px;
    margin-top: 15px;
  }}
  .metric {{
    min-width: 0; padding: 15px 12px 13px; border-radius: 15px;
    border: 1px solid rgba(255,255,255,.07); background: rgba(255,255,255,.05);
  }}
  .metric label {{ display: block; color: #778397; font-size: 11px; letter-spacing: 1px; }}
  .metric strong {{
    display: block; margin-top: 5px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; font-size: 23px;
  }}
  .metric:nth-child(1) strong, .metric:nth-child(4) strong {{ color: #ffc34d; }}
  .bottom {{
    position: relative; display: flex; align-items: center; justify-content: space-between;
    gap: 18px; margin-top: 16px; padding: 13px 16px; border-radius: 14px;
    border-left: 4px solid #f4a326; background: rgba(245,158,11,.07);
  }}
  .time {{ color: #aeb7c5; font-size: 14px; }}
  .badges {{ display: flex; gap: 8px; }}
  .badge {{
    padding: 5px 10px; border-radius: 999px; color: #d9e0ea; font-size: 12px;
    font-weight: 800; border: 1px solid rgba(255,255,255,.12); background: rgba(255,255,255,.06);
  }}
  .badge.mvp {{ color: #ffc34d; border-color: rgba(245,158,11,.28); }}
  .positive {{ color: #69d692 !important; }}
  .negative {{ color: #ef7770 !important; }}
  footer {{
    position: relative; display: flex; justify-content: space-between; margin-top: 20px;
    color: #5f6b7d; font-size: 10px; font-weight: 700; letter-spacing: 1.2px;
  }}
</style>
</head>
<body>
<main id="match-card" class="match-card" data-testid="match-card">
  <div class="top-line"></div>
  <header>
    <div class="player">
      <p class="eyebrow">{eyebrow}</p>
      <h1>{_escape(match.name)}</h1>
      <div class="steam">STEAM · {_escape(match.steam_id)}</div>
    </div>
    {avatar}
  </header>
  <section class="hero">
    <div class="score-panel">
      <div class="map">{_escape(match.map_name)}</div>
      <div class="score">
        <strong>{_escape(match.own_score)}</strong><span>:</span>
        <strong>{_escape(match.opponent_score)}</strong>
      </div>
      <div class="mode">{_escape(match.mode)}</div>
    </div>
    <div class="result-panel result-{match.result}">
      <div class="result-label">{_escape(match.result_label)}</div>
      <small>比赛已结束</small>
    </div>
  </section>
  <section class="metrics">
    {metrics_html}
  </section>
  <div class="bottom">
    <div class="time">{_escape(match.ended_at)} · {_escape(match.duration)}</div>
    <div class="badges">{mvp_badge}{multi_kill}</div>
  </div>
  <footer>
    <span>MATCH · {_escape(match.match_id)}</span>
    <span>{source_footer}</span>
  </footer>
</main>
</body>
</html>"""


def _render_match_team_rows(match: MatchSnapshot, team: int) -> str:
    rows: list[str] = []
    for index, player in enumerate(
        (item for item in match.players if item.team == team),
        start=1,
    ):
        member_badge = '<span class="member-badge">群成员</span>' if player.is_group_member else ""
        mvp_badge = '<span class="mini-badge">MVP</span>' if player.mvp else ""
        change_class = ""
        if player.score_change.startswith("+"):
            change_class = "positive"
        elif player.score_change.startswith("-"):
            change_class = "negative"
        score_change = ""
        if player.score_change not in {"-", "0"}:
            score_change = (
                f'<small class="{change_class}">{_escape(player.score_change)}</small>'
            )
        kad = f"{player.kills} / {player.assists} / {player.deaths}"
        row_class = "player-row group-member" if player.is_group_member else "player-row"
        source_cells = (
            f"<div>{_escape(player.we)}</div>"
            f'<div class="pvp-score">{_escape(player.pvp_score)}'
            f"{score_change}</div>"
        )
        rows.append(
            f"""
            <div class="{row_class}">
              <div class="rank">{index}</div>
              <div class="player-name">
                <strong>{_escape(player.name)}</strong>
                <span>{member_badge}{mvp_badge}</span>
              </div>
              <div class="kad">{_escape(kad)}</div>
              <div>{_escape(player.kd)}</div>
              <div class="rating">{_escape(player.rating)}</div>
              <div>{_escape(player.adr)}</div>
              {source_cells}
            </div>
            """
        )
    return "".join(rows) or '<div class="empty-team">暂无该队玩家数据</div>'


def _render_match_commentary_html(commentary: str | None) -> str:
    if not commentary:
        return ""
    lines = [line.strip() for line in commentary.splitlines() if line.strip()]
    if lines and "赛后 AI 锐评" in lines[0]:
        lines.pop(0)
    lines = [line for line in lines if not line.startswith("赛果核对：")]
    if not lines:
        return ""
    rendered: list[str] = []
    for line in lines:
        if line.startswith("不想被点评"):
            rendered.append(f'<div class="commentary-note">{_escape(line)}</div>')
        elif "｜" in line:
            rendered.append(f'<div class="commentary-player">{_escape(line)}</div>')
        else:
            rendered.append(f'<div class="commentary-text">{_escape(line)}</div>')
    return f"""
    <section class="ai-commentary" data-testid="match-commentary">
      <div class="commentary-heading">
        <span>AI</span><strong>牛牛赛后锐评</strong>
      </div>
      <div class="commentary-body">{"".join(rendered)}</div>
    </section>
    """


def render_match_html(
    match: MatchSnapshot,
    *,
    avatar_data_uri: str | None = None,
    commentary: str | None = None,
) -> str:
    if not match.players:
        return _render_single_match_html(match, avatar_data_uri=avatar_data_uri)
    eyebrow, source_footer, _fallback_avatar = _match_card_source(match)

    try:
        score1 = float(match.team1_score)
        score2 = float(match.team2_score)
    except ValueError:
        winner = 0
    else:
        winner = 1 if score1 > score2 else 2 if score2 > score1 else 0

    def team_panel(team: int, name: str, score: str) -> str:
        result_badge = '<span class="winner">胜方</span>' if winner == team else ""
        return f"""
        <section class="team-panel team-{team}">
          <header class="team-header">
            <div><span class="team-dot"></span><strong>{_escape(name)}</strong>{result_badge}</div>
            <b>{_escape(score)}</b>
          </header>
          <div class="table-head">
            <span>#</span><span>玩家</span><span>K / A / D</span><span>K/D</span>
            <span>Rating</span><span>ADR</span><span>WE</span><span>完美分</span>
          </div>
          <div class="team-rows">{_render_match_team_rows(match, team)}</div>
        </section>
        """

    member_names = "、".join(player.name for player in match.players if player.is_group_member)
    member_line = (
        f'<span class="member-summary">群成员 · {_escape(member_names)}</span>'
        if member_names
        else ""
    )
    meta = f"{match.mode} · {match.ended_at} · {match.duration}"
    commentary_html = _render_match_commentary_html(commentary)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans Math", "Noto Sans Symbols2",
      "Noto Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    color: #f7f8fb;
  }}
  .match-card {{
    width: 1080px; min-height: 710px; position: relative; overflow: hidden;
    padding: 36px 38px 28px; border: 1px solid rgba(255,255,255,.13);
    border-radius: 30px;
    background:
      radial-gradient(circle at 92% 2%, rgba(245,158,11,.22), transparent 28%),
      radial-gradient(circle at 0 100%, rgba(35,103,185,.22), transparent 34%),
      linear-gradient(145deg, #121925 0%, #090e16 58%, #111722 100%);
    box-shadow: 0 28px 90px rgba(0,0,0,.48);
  }}
  .match-card::before {{
    content: ""; position: absolute; inset: 0; opacity: .24; pointer-events: none;
    background-image: linear-gradient(rgba(255,255,255,.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.035) 1px, transparent 1px);
    background-size: 34px 34px;
  }}
  .top-line {{
    position: absolute; left: 38px; right: 38px; top: 0; height: 5px;
    background: linear-gradient(90deg, #f07b18, #ffc84d 42%, transparent 78%);
  }}
  .page-head {{
    position: relative; display: flex; align-items: flex-end; justify-content: space-between;
    gap: 24px;
  }}
  .eyebrow {{
    margin: 0 0 7px; color: #f4a329; font-size: 13px; font-weight: 900;
    letter-spacing: 3px;
  }}
  h1 {{ margin: 0; font-size: 36px; line-height: 1.2; letter-spacing: -.5px; }}
  .meta {{ margin-top: 8px; color: #7f8b9f; font-size: 13px; }}
  .final-score {{ display: flex; align-items: baseline; gap: 12px; white-space: nowrap; }}
  .final-score strong {{ font-size: 54px; line-height: 1; color: #fff; }}
  .final-score span {{ color: #596579; font-size: 28px; }}
  .teams {{
    position: relative; display: grid; grid-template-columns: 1fr 1fr; gap: 14px;
    margin-top: 26px;
  }}
  .team-panel {{
    overflow: hidden; border: 1px solid rgba(255,255,255,.09); border-radius: 20px;
    background: rgba(3,7,13,.58);
  }}
  .team-header {{
    display: flex; align-items: center; justify-content: space-between; min-height: 62px;
    padding: 13px 17px; border-bottom: 1px solid rgba(255,255,255,.08);
  }}
  .team-header > div {{ display: flex; align-items: center; min-width: 0; gap: 9px; }}
  .team-header strong {{
    max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    font-size: 19px;
  }}
  .team-header b {{ font-size: 31px; }}
  .team-dot {{ width: 10px; height: 10px; flex: 0 0 10px; border-radius: 50%; }}
  .team-1 .team-dot {{ background: #ef9b2d; box-shadow: 0 0 12px rgba(239,155,45,.55); }}
  .team-2 .team-dot {{ background: #4aa3df; box-shadow: 0 0 12px rgba(74,163,223,.55); }}
  .winner {{
    padding: 3px 7px; border-radius: 999px; color: #ffc34d; font-size: 10px;
    border: 1px solid rgba(245,158,11,.3); background: rgba(245,158,11,.09);
  }}
  .table-head, .player-row {{
    display: grid; grid-template-columns: 22px minmax(120px, 1fr) 78px 39px 48px 39px 37px 56px;
    align-items: center; column-gap: 5px;
  }}
  .table-head {{
    min-height: 35px; padding: 0 12px; color: #667286; font-size: 9px;
    font-weight: 800; letter-spacing: .2px; text-align: center;
  }}
  .table-head span:nth-child(2) {{ text-align: left; }}
  .player-row {{
    position: relative; min-height: 57px; padding: 6px 12px; color: #cbd2dd;
    border-top: 1px solid rgba(255,255,255,.055); font-size: 12px; text-align: center;
  }}
  .player-row.group-member {{
    background: linear-gradient(90deg, rgba(245,158,11,.16), rgba(245,158,11,.035));
    box-shadow: inset 3px 0 #f4a329;
  }}
  .rank {{ color: #596579; font-size: 11px; }}
  .player-name {{ min-width: 0; text-align: left; }}
  .player-name strong {{
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: #f5f7fa; font-size: 13px;
  }}
  .player-name > span {{ display: flex; gap: 4px; min-height: 14px; margin-top: 2px; }}
  .member-badge, .mini-badge {{
    padding: 1px 5px; border-radius: 999px; color: #ffc34d; font-size: 8px;
    border: 1px solid rgba(245,158,11,.28);
  }}
  .mini-badge {{ color: #8ec9f1; border-color: rgba(74,163,223,.3); }}
  .kad {{ color: #e7ebf1; font-weight: 750; }}
  .rating {{ color: #ffc34d; font-weight: 850; }}
  .pvp-score small {{ display: block; margin-top: 1px; font-size: 8px; }}
  .positive {{ color: #69d692 !important; }}
  .negative {{ color: #ef7770 !important; }}
  .empty-team {{ padding: 28px; color: #657185; text-align: center; }}
  .ai-commentary {{
    position: relative; margin-top: 17px; padding: 17px 19px 15px;
    border: 1px solid rgba(245,158,11,.18); border-radius: 18px;
    background: linear-gradient(135deg, rgba(245,158,11,.115), rgba(13,20,31,.72));
    box-shadow: inset 3px 0 #f4a329;
  }}
  .commentary-heading {{ display: flex; align-items: center; gap: 9px; }}
  .commentary-heading span {{
    display: grid; place-items: center; width: 29px; height: 22px; border-radius: 7px;
    color: #10151e; background: #ffc34d; font-size: 10px; font-weight: 950;
  }}
  .commentary-heading strong {{ font-size: 15px; letter-spacing: .5px; }}
  .commentary-body {{
    display: grid; grid-template-columns: minmax(170px, .7fr) minmax(0, 2.3fr);
    column-gap: 15px; row-gap: 7px; margin-top: 12px;
  }}
  .commentary-player {{ color: #e4b45e; font-size: 11px; font-weight: 850; }}
  .commentary-text {{ color: #d6dce6; font-size: 12px; line-height: 1.58; }}
  .commentary-note {{
    grid-column: 1 / -1; margin-top: 2px; color: #68758a; font-size: 9px;
  }}
  .bottom {{
    position: relative; display: flex; justify-content: space-between; align-items: center;
    gap: 16px; margin-top: 17px; color: #748095; font-size: 11px;
  }}
  .member-summary {{
    max-width: 650px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: #d8b16b;
  }}
  footer {{
    position: relative; display: flex; justify-content: space-between; margin-top: 14px;
    color: #536074; font-size: 9px; font-weight: 700; letter-spacing: 1px;
  }}
</style>
</head>
<body>
<main id="match-card" class="match-card" data-testid="match-card">
  <div class="top-line"></div>
  <header class="page-head">
    <div>
      <p class="eyebrow">{eyebrow}</p>
      <h1>{_escape(match.map_name)}</h1>
      <div class="meta">{_escape(meta)}</div>
    </div>
    <div class="final-score">
      <strong>{_escape(match.team1_score)}</strong><span>:</span>
      <strong>{_escape(match.team2_score)}</strong>
    </div>
  </header>
  <div class="teams">
    {team_panel(1, match.team1_name, match.team1_score)}
    {team_panel(2, match.team2_name, match.team2_score)}
  </div>
  {commentary_html}
  <div class="bottom">
    {member_line}<span>共 {len(match.players)} 名玩家</span>
  </div>
  <footer>
    <span>MATCH · {_escape(match.match_id)}</span>
    <span>{source_footer}</span>
  </footer>
</main>
</body>
</html>"""


def render_valorant_html(snapshot: ValorantSnapshot) -> str:
    """Render a self-contained Chinese VALORANT ranked snapshot."""

    cache_label = {
        "live": "实时查询",
        "fresh": "近期缓存",
        "stale": "历史缓存",
    }.get(snapshot.cache_status, "缓存数据")
    cache_class = (
        "live"
        if snapshot.cache_status == "live"
        else "stale"
        if snapshot.cache_status == "stale"
        else "cached"
    )
    matches = snapshot.matches[:10]
    match_rows: list[str] = []
    for index, match in enumerate(matches, 1):
        result_class = (
            "win"
            if match.result == "胜利"
            else "loss"
            if match.result == "失败"
            else "draw"
        )
        highlight = (
            f'<span class="highlight">{_escape(match.highlight)}</span>'
            if match.highlight
            else ""
        )
        match_rows.append(
            f"""
            <article class="match-row {result_class}">
              <div class="match-index">{index:02d}</div>
              <div class="agent">
                <span class="agent-mark" aria-hidden="true"></span>
                <div><strong>{_escape(match.agent)}</strong>{highlight}</div>
              </div>
              <div class="outcome">
                <span>{_escape(match.result)}</span>
                <strong>{_escape(match.score)}</strong>
              </div>
              <div class="map">
                <span>地图</span>
                <strong>{_escape(match.map_name)}</strong>
                <small>{_escape(match.ended_at)} · {_escape(match.duration)}</small>
              </div>
              <div class="combat">
                <span>K / D / A</span>
                <strong>{_escape(match.kda)}</strong>
                <small>ACS {_escape(match.acs)}</small>
              </div>
              <div class="match-rank">
                <span>段位 / RR</span>
                <strong>{_escape(match.rank)}</strong>
                <small>{_escape(match.rr_change)}</small>
              </div>
            </article>
            """
        )
    matches_html = "".join(match_rows) or """
      <div class="empty-state">
        <strong>暂无近期排位记录</strong>
        <span>公开数据可能仍在同步，稍后再查询一次。</span>
      </div>
    """
    cache_note = (
        f'<p class="cache-note">{_escape(snapshot.cache_note)}</p>'
        if snapshot.cache_note
        else ""
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:,">
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: transparent; }}
  body {{
    font-family: "Noto Sans CJK SC", "Noto Sans Math", "Noto Sans Symbols2",
      "Noto Sans", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    color: #f5f7f8;
  }}
  .valorant-card {{
    width: 1080px; min-height: 700px; position: relative; overflow: hidden;
    padding: 38px 40px 28px; border: 1px solid rgba(255,255,255,.12);
    border-radius: 26px;
    background:
      linear-gradient(118deg, transparent 0 68%, rgba(255,70,85,.075) 68% 69%, transparent 69%),
      radial-gradient(circle at 92% 3%, rgba(255,70,85,.24), transparent 31%),
      radial-gradient(circle at 3% 100%, rgba(39,231,197,.11), transparent 32%),
      linear-gradient(145deg, #11171d 0%, #0b1015 58%, #151a20 100%);
    box-shadow: 0 30px 95px rgba(0,0,0,.52);
  }}
  .valorant-card::before {{
    content: ""; position: absolute; inset: 0; opacity: .15; pointer-events: none;
    background-image:
      linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px);
    background-size: 44px 44px;
    mask-image: linear-gradient(110deg, #000, transparent 78%);
  }}
  .top-rule {{
    position: absolute; top: 0; left: 40px; right: 40px; height: 5px;
    background: linear-gradient(90deg, #ff4655 0 52%, #f5f7f8 52% 60%, transparent 60%);
  }}
  .page-head {{
    position: relative; display: flex; justify-content: space-between; align-items: flex-start;
    gap: 28px;
  }}
  .identity {{ min-width: 0; }}
  .eyebrow {{
    margin: 0 0 8px; color: #ff5966; font-size: 12px; font-weight: 900;
    letter-spacing: 3.2px;
  }}
  h1 {{
    max-width: 720px; margin: 0; overflow: hidden; color: #fff;
    font-size: 42px; line-height: 1.12; letter-spacing: -1px;
    text-overflow: ellipsis; white-space: nowrap;
  }}
  .record-line {{
    display: flex; align-items: center; gap: 12px; margin-top: 10px;
    color: #a9b2ba; font-size: 14px;
  }}
  .record-line strong {{ color: #f1f3f4; font-size: 20px; }}
  .brand-mark {{
    width: 84px; height: 84px; flex: 0 0 84px; position: relative;
    border: 1px solid rgba(255,70,85,.42); background: rgba(255,70,85,.08);
    clip-path: polygon(12% 0, 100% 0, 100% 88%, 88% 100%, 0 100%, 0 12%);
  }}
  .brand-mark::before {{
    content: "V"; position: absolute; inset: 0; display: grid; place-items: center;
    color: #ff4655; font: 900 47px/1 "Arial Black", sans-serif;
  }}
  .summary {{
    position: relative; display: grid; grid-template-columns: 1.2fr repeat(4, 1fr);
    gap: 10px; margin-top: 28px;
  }}
  .metric {{
    min-height: 94px; padding: 16px 17px; border: 1px solid rgba(255,255,255,.085);
    background: rgba(5,10,14,.63);
    clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 0 100%);
  }}
  .metric.primary {{ border-color: rgba(255,70,85,.34); background: rgba(255,70,85,.105); }}
  .metric span {{
    display: block; color: #717e88; font-size: 10px; font-weight: 850;
    letter-spacing: 1.1px;
  }}
  .metric strong {{
    display: block; margin-top: 9px; overflow: hidden; color: #f7f8f9;
    font-size: 25px; line-height: 1.05; text-overflow: ellipsis; white-space: nowrap;
  }}
  .metric.primary strong {{ color: #ff6471; font-size: 28px; }}
  .section-head {{
    position: relative; display: flex; justify-content: space-between; align-items: end;
    margin: 30px 0 11px;
  }}
  .section-head h2 {{ margin: 0; color: #f1f3f4; font-size: 20px; }}
  .section-head span {{ color: #68747e; font-size: 10px; letter-spacing: 1px; }}
  .match-list {{ position: relative; display: grid; gap: 8px; }}
  .match-row {{
    --result-color: #8d98a1;
    display: grid; grid-template-columns: 36px 150px 94px minmax(190px, 1fr) 130px 126px;
    align-items: center; gap: 12px; min-height: 76px; padding: 10px 15px 10px 10px;
    border: 1px solid rgba(255,255,255,.075); border-left: 3px solid var(--result-color);
    background: linear-gradient(90deg, rgba(255,255,255,.038), rgba(255,255,255,.017));
  }}
  .match-row.win {{ --result-color: #27e7c5; }}
  .match-row.loss {{ --result-color: #ff5966; }}
  .match-index {{ color: #53606a; font: 900 11px/1 "Arial", sans-serif; text-align: center; }}
  .agent {{
    min-width: 0; display: flex; align-items: center; gap: 10px;
  }}
  .agent-mark {{
    width: 8px; height: 36px; flex: 0 0 8px; background: var(--result-color);
    clip-path: polygon(0 0, 100% 7px, 100% 100%, 0 calc(100% - 7px));
  }}
  .agent > div {{ min-width: 0; }}
  .agent strong, .map strong, .combat strong, .match-rank strong {{
    display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .agent strong {{ color: #f4f5f6; font-size: 15px; }}
  .highlight {{
    display: block; margin-top: 3px; overflow: hidden; color: #a6b0b7;
    font-size: 9px; text-overflow: ellipsis; white-space: nowrap;
  }}
  .outcome span {{
    display: block; color: var(--result-color); font-size: 11px; font-weight: 900;
  }}
  .outcome strong {{ display: block; margin-top: 3px; color: #fff; font-size: 22px; }}
  .map span, .combat span, .match-rank span {{
    display: block; color: #68747e; font-size: 8px; font-weight: 800; letter-spacing: .6px;
  }}
  .map strong, .combat strong, .match-rank strong {{
    margin-top: 4px; color: #e7eaec; font-size: 13px;
  }}
  .map small, .combat small, .match-rank small {{
    display: block; margin-top: 4px; overflow: hidden; color: #68747e;
    font-size: 9px; text-overflow: ellipsis; white-space: nowrap;
  }}
  .combat strong {{ font-size: 15px; }}
  .match-rank small {{ color: var(--result-color); font-weight: 850; }}
  .empty-state {{
    min-height: 180px; display: grid; place-content: center; gap: 8px; text-align: center;
    border: 1px dashed rgba(255,255,255,.13); color: #76828c;
    background: rgba(5,10,14,.42);
  }}
  .empty-state strong {{ color: #dbe0e3; font-size: 18px; }}
  .cache-note {{
    position: relative; margin: 14px 0 0; padding: 10px 13px;
    border-left: 2px solid #d5a94d; background: rgba(213,169,77,.08);
    color: #b8a578; font-size: 11px; line-height: 1.55;
  }}
  footer {{
    position: relative; display: flex; justify-content: space-between; align-items: center;
    gap: 18px; margin-top: 18px; color: #56626c; font-size: 9px;
    font-weight: 750; letter-spacing: .7px;
  }}
  .source-time {{
    max-width: 720px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .status {{
    padding: 5px 9px; border: 1px solid rgba(255,255,255,.12); color: #9ca6ae;
    background: rgba(255,255,255,.04);
  }}
  .status.live {{ border-color: rgba(39,231,197,.3); color: #56e8cf; }}
  .status.stale {{ border-color: rgba(213,169,77,.35); color: #d5a94d; }}
</style>
</head>
<body>
<main id="valorant-card" class="valorant-card" data-testid="valorant-card">
  <div class="top-rule"></div>
  <header class="page-head">
    <div class="identity">
      <p class="eyebrow">VALORANT · CN RANKED</p>
      <h1>{_escape(snapshot.riot_id)}</h1>
      <div class="record-line">
        <strong>{_escape(snapshot.rank)}</strong>
        <span>{_escape(snapshot.record)}</span>
      </div>
    </div>
    <div class="brand-mark" aria-hidden="true"></div>
  </header>
  <section class="summary" aria-label="近十场汇总">
    <div class="metric primary">
      <span>近 10 场胜率</span><strong>{_escape(snapshot.win_rate)}</strong>
    </div>
    <div class="metric"><span>KDA</span><strong>{_escape(snapshot.kda)}</strong></div>
    <div class="metric"><span>平均 ACS</span><strong>{_escape(snapshot.acs)}</strong></div>
    <div class="metric"><span>RR 变化</span><strong>{_escape(snapshot.rr_change)}</strong></div>
    <div class="metric"><span>已载入对局</span><strong>{len(matches)}</strong></div>
  </section>
  <div class="section-head">
    <h2>最近排位</h2>
    <span>MATCH HISTORY · 最多 10 场</span>
  </div>
  <section class="match-list">{matches_html}</section>
  {cache_note}
  <footer>
    <span class="source-time">
      来源状态 · {_escape(snapshot.source_updated_at)} · 获取于 {_escape(snapshot.fetched_at)}
    </span>
    <span class="status {cache_class}">{cache_label}</span>
  </footer>
</main>
</body>
</html>"""


class StatsCardRenderer:
    def __init__(
        self,
        *,
        enabled: bool = True,
        timeout_seconds: float = 45.0,
        idle_timeout_seconds: float = 120.0,
        max_browser_renders: int = 40,
    ) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.idle_timeout_seconds = max(0.0, idle_timeout_seconds)
        self.max_browser_renders = max(1, max_browser_renders)
        self._lock = asyncio.Lock()
        self._pending_renders = 0
        self._circuit_open_until = 0.0
        self._playwright: Any = None
        self._browser: Any = None
        self._browser_render_count = 0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._last_activity = 0.0
        self._closed = False

    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _schedule_idle_close(self) -> None:
        self._cancel_idle_timer()
        if self._closed or self._pending_renders or self.idle_timeout_seconds <= 0:
            return
        if self._browser is None and self._playwright is None:
            return
        loop = asyncio.get_running_loop()
        self._last_activity = loop.time()
        self._idle_handle = loop.call_later(self.idle_timeout_seconds, self._start_idle_close)

    def _start_idle_close(self) -> None:
        self._idle_handle = None
        if self._closed or self._idle_task is not None:
            return
        self._idle_task = asyncio.create_task(
            self._close_when_idle(), name="pwcs2-renderer-idle-close"
        )

    async def _close_when_idle(self) -> None:
        try:
            async with self._lock:
                # The timer can fire just as a new render arrives. Never close
                # its browser, or cancel a browser teardown halfway through.
                idle_for = asyncio.get_running_loop().time() - self._last_activity
                if self._closed or self._pending_renders:
                    return
                if idle_for < self.idle_timeout_seconds:
                    # Timer callbacks can fire slightly early (notably on
                    # Windows); keep the original deadline instead of leaking
                    # the browser until the next user request.
                    self._cancel_idle_timer()
                    self._idle_handle = asyncio.get_running_loop().call_later(
                        self.idle_timeout_seconds - idle_for, self._start_idle_close
                    )
                    return
                cleanup_error = await self._reset()
                if cleanup_error is not None:
                    logger.warning("pwcs2: idle renderer cleanup failed; circuit opened")
        except Exception:
            self._trip_circuit_breaker()
            logger.warning("pwcs2: idle renderer cleanup failed; circuit opened")
        finally:
            self._idle_task = None

    @staticmethod
    def _is_timeout_error(error: BaseException) -> bool:
        # Playwright exposes its own TimeoutError type, while asyncio.timeout()
        # raises the built-in type. Treat both as resource-pressure timeouts.
        return isinstance(error, TimeoutError) or type(error).__name__ == "TimeoutError"

    def _trip_circuit_breaker(self) -> None:
        now = asyncio.get_running_loop().time()
        self._circuit_open_until = max(
            self._circuit_open_until,
            now + _RENDER_CIRCUIT_BREAKER_SECONDS,
        )

    def _circuit_is_open(self) -> bool:
        return self._circuit_open_until > asyncio.get_running_loop().time()

    def _raise_if_circuit_open(self) -> None:
        remaining = self._circuit_open_until - asyncio.get_running_loop().time()
        if remaining > 0:
            raise StatsCardRenderError(
                f"图片渲染暂时不可用（熔断剩余约 {math.ceil(remaining)} 秒）"
            )

    @asynccontextmanager
    async def _render_slot(self) -> AsyncIterator[None]:
        if self._closed:
            raise StatsCardRenderError("图片渲染器已关闭")
        self._raise_if_circuit_open()
        if self._pending_renders >= _MAX_PENDING_RENDERS:
            raise StatsCardRenderError("图片渲染繁忙，请稍后重试")

        self._pending_renders += 1
        self._cancel_idle_timer()
        acquired = False
        try:
            try:
                async with asyncio.timeout(_RENDER_QUEUE_TIMEOUT_SECONDS):
                    await self._lock.acquire()
                    acquired = True
            except TimeoutError as exc:
                raise StatsCardRenderError("图片渲染排队超时，请稍后重试") from exc

            # A render that was already queued must also observe a breaker that
            # another render may have opened while this task waited.
            self._raise_if_circuit_open()
            if self._closed:
                raise StatsCardRenderError("图片渲染器已关闭")
            yield
        finally:
            if acquired:
                self._lock.release()
            self._pending_renders -= 1
            self._schedule_idle_close()

    async def close(self) -> None:
        self._closed = True
        self._cancel_idle_timer()
        async with self._lock:
            await self._reset()
        task = self._idle_task
        if task is not None:
            await task

    async def _reset(self) -> Exception | None:
        self._cancel_idle_timer()
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        self._browser_render_count = 0
        cleanup_error: Exception | None = None
        cancelled_error: asyncio.CancelledError | None = None
        if browser is not None:
            try:
                async with asyncio.timeout(_BROWSER_CLOSE_TIMEOUT_SECONDS):
                    await browser.close()
            except asyncio.CancelledError as exc:
                cancelled_error = exc
                self._trip_circuit_breaker()
            except Exception as exc:
                cleanup_error = exc
        if playwright is not None:
            try:
                async with asyncio.timeout(_BROWSER_CLOSE_TIMEOUT_SECONDS):
                    await playwright.stop()
            except asyncio.CancelledError as exc:
                if cancelled_error is None:
                    cancelled_error = exc
                self._trip_circuit_breaker()
            except Exception as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if cleanup_error is not None:
            self._trip_circuit_breaker()
        if cancelled_error is not None:
            raise cancelled_error
        return cleanup_error

    async def _ensure_browser(self) -> Any:
        if (
            self._browser is not None
            and self._browser.is_connected()
            and self._browser_render_count < self.max_browser_renders
        ):
            return self._browser
        if self._playwright is not None or self._browser is not None:
            cleanup_error = await self._reset()
            if cleanup_error is not None:
                detail = " ".join(str(cleanup_error).split())[:500]
                raise StatsCardRenderError(
                    "浏览器清理失败"
                    f"（{type(cleanup_error).__name__}: {detail or 'no detail'}）"
                ) from cleanup_error
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        return self._browser

    async def render(
        self,
        stats: StatsSnapshot,
        *,
        background_key: str = "classic",
        member_badge: str = "",
    ) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            avatar_data_uri = await fetch_avatar_data_uri(
                stats.avatar_url,
                timeout_seconds=min(5.0, self.timeout_seconds),
            )
            background_data_uri = stats_background_data_uri(background_key)
            return await self._render_document(
                render_stats_html(
                    stats,
                    avatar_data_uri=avatar_data_uri,
                    background_key=background_key,
                    background_data_uri=background_data_uri,
                    member_badge=member_badge,
                ),
                selector="#stats-card",
                prefix="pwcs2-stats-",
                viewport={"width": 1040, "height": 1000},
            )

    async def render_ranking(self, ranking: RankingSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            viewport_height = min(2200, max(760, 300 + len(ranking.entries[:30]) * 64))
            return await self._render_document(
                render_ranking_html(ranking),
                selector="#ranking-card",
                prefix="pwcs2-ranking-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_chemistry_network(
        self,
        snapshot: ChemistryNetworkSnapshot,
    ) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            return await self._render_document(
                render_chemistry_network_html(snapshot),
                selector="#chemistry-network-card",
                prefix="pwcs2-chemistry-",
                viewport={"width": 1160, "height": 1300},
            )

    async def render_weekly_report(self, snapshot: WeeklyReportSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            viewport_height = min(1800, max(940, 420 + len(snapshot.members[:8]) * 74))
            return await self._render_document(
                render_weekly_report_html(snapshot),
                selector="#weekly-report-card",
                prefix="pwcs2-weekly-report-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_watch_stage(self, snapshot: WatchStageSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            return await self._render_document(
                render_watch_stage_html(snapshot),
                selector="#watch-stage-card",
                prefix="pwcs2-watch-stage-",
                viewport={"width": 1160, "height": 980},
            )

    async def render_game_status(self, snapshot: GameStatusSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            rows = max(1, math.ceil(len(snapshot.entries) / 2))
            viewport_height = min(2600, max(760, 370 + rows * 145))
            return await self._render_document(
                render_game_status_html(snapshot),
                selector="#game-status-card",
                prefix="pwcs2-game-status-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_valorant(self, snapshot: ValorantSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            viewport_height = min(1800, max(760, 430 + len(snapshot.matches[:10]) * 84))
            return await self._render_document(
                render_valorant_html(snapshot),
                selector="#valorant-card",
                prefix="pwcs2-valorant-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_match_history(
        self,
        history: MatchHistorySnapshot,
        *,
        background_key: str = "classic",
        member_badge: str = "",
    ) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            avatar_data_uri = await fetch_avatar_data_uri(
                history.avatar_url,
                timeout_seconds=min(5.0, self.timeout_seconds),
            )
            background_data_uri = stats_background_data_uri(background_key)
            viewport_height = max(760, 270 + len(history.matches) * 66)
            return await self._render_document(
                render_match_history_html(
                    history,
                    avatar_data_uri=avatar_data_uri,
                    background_key=background_key,
                    background_data_uri=background_data_uri,
                    member_badge=member_badge,
                ),
                selector="#history-card",
                prefix="pwcs2-history-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_match(
        self,
        match: MatchSnapshot,
        *,
        commentary: str | None = None,
    ) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            avatar_data_uri = None
            if not match.players:
                avatar_data_uri = await fetch_avatar_data_uri(
                    match.avatar_url,
                    timeout_seconds=min(5.0, self.timeout_seconds),
                )
            return await self._render_document(
                render_match_html(
                    match,
                    avatar_data_uri=avatar_data_uri,
                    commentary=commentary,
                ),
                selector="#match-card",
                prefix="pwcs2-match-",
                viewport={
                    "width": 1160,
                    "height": 1180 if commentary else 920,
                },
            )

    async def render_skin_prices(self, report: SkinPriceReport) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        async with self._render_slot():
            semaphore = asyncio.Semaphore(6)

            async def load(url: str) -> str | None:
                async with semaphore:
                    return await fetch_skin_image_data_uri(
                        url,
                        client=client,
                        timeout_seconds=min(5.0, self.timeout_seconds),
                    )

            try:
                async with asyncio.timeout(min(12.0, self.timeout_seconds)):
                    async with httpx.AsyncClient(
                        timeout=min(5.0, self.timeout_seconds),
                        follow_redirects=False,
                    ) as client:
                        images = await asyncio.gather(
                            *(load(entry.quote.image_url) for entry in report.entries)
                        )
            except TimeoutError:
                images = [None] * len(report.entries)
            rows = max(1, (len(report.entries) + 1) // 2)
            viewport_height = min(2600, 330 + rows * 194)
            return await self._render_document(
                render_skin_price_html(report, image_data_uris=tuple(images)),
                selector="#skin-price-card",
                prefix="pwcs2-skin-prices-",
                viewport={"width": 1160, "height": viewport_height},
            )

    async def render_word_cloud(self, cloud: WordCloudSnapshot) -> Path:
        if not self.enabled:
            raise StatsCardRenderError("图片渲染未启用")
        from pwcs2_bot.daily_word_cloud import render_word_cloud_html

        async with self._render_slot():
            return await self._render_document(
                render_word_cloud_html(cloud),
                selector="#word-cloud",
                prefix="pwcs2-word-cloud-",
                viewport={"width": 1160, "height": 840},
            )

    async def _render_document(
        self,
        document: str,
        *,
        selector: str,
        prefix: str,
        viewport: dict[str, int],
    ) -> Path:
        descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".png", dir="/tmp")
        os.close(descriptor)
        output_path = Path(raw_path)
        output_complete = False
        try:
            last_error: Exception | None = None
            for _attempt in range(_RENDER_ATTEMPTS):
                context = None
                page = None
                attempt_error: Exception | None = None
                cancelled_error: asyncio.CancelledError | None = None
                cleanup_error: Exception | None = None
                cleanup_cancelled = False
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        browser = await self._ensure_browser()
                        context = await browser.new_context(
                            viewport=viewport,
                            # Integer scaling avoids tiled-raster gaps in headless Chromium.
                            device_scale_factor=2,
                            color_scheme="dark",
                        )
                        page = await context.new_page()
                        await page.set_content(document, wait_until="load")
                        await page.evaluate("() => document.fonts.ready")
                        await page.evaluate(
                            "() => new Promise(resolve => requestAnimationFrame("
                            "() => requestAnimationFrame(resolve)))"
                        )
                        card = page.locator(selector)
                        await card.wait_for(state="visible")
                        bounds = await card.bounding_box()
                        if bounds is None or bounds["width"] <= 0 or bounds["height"] <= 0:
                            raise RuntimeError("render target has no visible bounds")
                        # A page clip avoids Locator.screenshot's actionability wait, which can
                        # stall on low-resource servers while waiting for an already-static card.
                        await page.screenshot(
                            path=output_path,
                            type="png",
                            animations="disabled",
                            caret="hide",
                            clip=bounds,
                        )
                except asyncio.CancelledError as exc:
                    cancelled_error = exc
                except Exception as exc:
                    attempt_error = exc
                    last_error = exc
                finally:
                    resource = context if context is not None else page
                    if resource is not None:
                        try:
                            async with asyncio.timeout(_BROWSER_CLOSE_TIMEOUT_SECONDS):
                                await resource.close()
                        except asyncio.CancelledError as exc:
                            if cancelled_error is None:
                                cancelled_error = exc
                            cleanup_cancelled = True
                        except Exception as exc:
                            cleanup_error = exc

                if cleanup_error is not None or cleanup_cancelled:
                    # A context that cannot be closed may retain a renderer and
                    # its backing memory. Poison the shared browser and stop
                    # accepting work until the host has had time to recover.
                    if cleanup_error is not None:
                        last_error = cleanup_error
                    self._trip_circuit_breaker()
                    reset_error = await self._reset()
                    if reset_error is not None:
                        last_error = reset_error

                if cancelled_error is not None:
                    raise cancelled_error

                if attempt_error is None:
                    output_complete = True
                    self._browser_render_count += 1
                    return output_path

                # _ensure_browser() and failed cleanup open the circuit. Do not
                # reset again or advance to an attempt that could start Chrome.
                if self._circuit_is_open():
                    break

                if self._is_timeout_error(attempt_error):
                    # Retrying a timeout by launching another Chromium is exactly
                    # what turns an I/O stall into sustained memory pressure.
                    self._trip_circuit_breaker()
                    reset_error = await self._reset()
                    if reset_error is not None:
                        last_error = reset_error
                    break

                reset_error = await self._reset()
                if reset_error is not None:
                    last_error = reset_error
                    break

            assert last_error is not None
            detail = " ".join(str(last_error).split())[:500]
            raise StatsCardRenderError(
                f"生成图片失败（{type(last_error).__name__}: {detail or 'no detail'}）"
            ) from last_error
        finally:
            if not output_complete:
                await asyncio.to_thread(output_path.unlink, missing_ok=True)
