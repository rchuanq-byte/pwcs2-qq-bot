import asyncio
from pathlib import Path

import httpx
import pytest

import pwcs2_bot.stats_card as stats_card_module
from pwcs2_bot.chemistry import build_chemistry_network
from pwcs2_bot.formatting import (
    build_official_stats_snapshot,
    build_stats_snapshot,
)
from pwcs2_bot.match_formatting import build_match_history, build_match_snapshot
from pwcs2_bot.ranking import (
    build_official_ranking_entry,
    build_ranking_entry,
    build_ranking_snapshot,
)
from pwcs2_bot.skin_prices import SkinPriceReport, SkinQuote, SkinReportEntry
from pwcs2_bot.stats_card import (
    StatsCardRenderer,
    StatsCardRenderError,
    fetch_avatar_data_uri,
    fetch_skin_image_data_uri,
    render_chemistry_network_html,
    render_game_status_html,
    render_match_history_html,
    render_match_html,
    render_ranking_html,
    render_skin_price_html,
    render_stats_html,
    render_valorant_html,
    render_watch_stage_html,
    render_weekly_report_html,
    stats_background_data_uri,
)
from pwcs2_bot.steam_presence import GameStatusEntry, GameStatusSnapshot
from pwcs2_bot.valorant import ValorantMatch, ValorantSnapshot
from pwcs2_bot.watch_stage import WatchStagePlayer, WatchStageSnapshot
from pwcs2_bot.weekly_report import WeeklyReportMember, WeeklyReportSnapshot


async def test_renderer_retries_with_page_clip_after_target_closes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeLocator:
        def __init__(self) -> None:
            self.waited = False

        async def wait_for(self, **kwargs) -> None:
            assert kwargs == {"state": "visible"}
            self.waited = True

        async def bounding_box(self) -> dict[str, float]:
            return {"x": 4, "y": 6, "width": 320, "height": 180}

    class FakePage:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.locator_value = FakeLocator()
            self.screenshot_kwargs: dict | None = None
            self.closed = False

        async def set_content(self, *_args, **_kwargs) -> None:
            return None

        async def evaluate(self, *_args, **_kwargs) -> None:
            return None

        def locator(self, selector: str) -> FakeLocator:
            assert selector == "#card"
            return self.locator_value

        async def screenshot(self, **kwargs) -> None:
            self.screenshot_kwargs = kwargs
            if self.fail:
                raise RuntimeError("target page closed")
            await asyncio.to_thread(Path(kwargs["path"]).write_bytes, b"png")

        async def close(self) -> None:
            self.closed = True

    class FakeContext:
        def __init__(self, page: FakePage) -> None:
            self.page = page

        async def new_page(self) -> FakePage:
            return self.page

        async def close(self) -> None:
            await self.page.close()

    class FakeBrowser:
        def __init__(self, pages: list[FakePage]) -> None:
            self.pages = pages
            self.context_kwargs: list[dict] = []

        async def new_context(self, **kwargs) -> FakeContext:
            self.context_kwargs.append(kwargs)
            return FakeContext(self.pages.pop(0))

    pages = [FakePage(fail=True), FakePage(fail=False)]
    browser = FakeBrowser(pages.copy())
    renderer = StatsCardRenderer(timeout_seconds=1)
    reset_count = 0

    async def ensure_browser() -> FakeBrowser:
        return browser

    async def reset() -> None:
        nonlocal reset_count
        reset_count += 1

    original_mkstemp = stats_card_module.tempfile.mkstemp

    def local_mkstemp(*, prefix: str, suffix: str, dir: str):
        assert dir == "/tmp"
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr(renderer, "_ensure_browser", ensure_browser)
    monkeypatch.setattr(renderer, "_reset", reset)
    monkeypatch.setattr(stats_card_module.tempfile, "mkstemp", local_mkstemp)

    output = await renderer._render_document(
        "<main id='card'>ok</main>",
        selector="#card",
        prefix="test-card-",
        viewport={"width": 400, "height": 300},
    )

    assert output.read_bytes() == b"png"
    assert reset_count == 1
    assert all(page.closed for page in pages)
    assert all(page.locator_value.waited for page in pages)
    assert browser.context_kwargs == [
        {
            "viewport": {"width": 400, "height": 300},
            "device_scale_factor": 2,
            "color_scheme": "dark",
        },
        {
            "viewport": {"width": 400, "height": 300},
            "device_scale_factor": 2,
            "color_scheme": "dark",
        },
    ]
    assert pages[1].screenshot_kwargs is not None
    assert pages[1].screenshot_kwargs["clip"] == {
        "x": 4,
        "y": 6,
        "width": 320,
        "height": 180,
    }
    output.unlink()


async def test_renderer_timeout_does_not_retry_and_opens_circuit_breaker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class TimeoutPage:
        async def set_content(self, *_args, **_kwargs) -> None:
            raise TimeoutError("renderer stalled")

    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        async def new_page(self) -> TimeoutPage:
            return TimeoutPage()

        async def close(self) -> None:
            self.closed = True

    class FakeBrowser:
        def __init__(self) -> None:
            self.new_context_calls = 0
            self.contexts: list[FakeContext] = []

        async def new_context(self, **_kwargs) -> FakeContext:
            self.new_context_calls += 1
            context = FakeContext()
            self.contexts.append(context)
            return context

    browser = FakeBrowser()
    renderer = StatsCardRenderer(timeout_seconds=1)
    reset_count = 0

    async def ensure_browser() -> FakeBrowser:
        return browser

    async def reset() -> None:
        nonlocal reset_count
        reset_count += 1

    original_mkstemp = stats_card_module.tempfile.mkstemp

    def local_mkstemp(*, prefix: str, suffix: str, dir: str):
        assert dir == "/tmp"
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr(renderer, "_ensure_browser", ensure_browser)
    monkeypatch.setattr(renderer, "_reset", reset)
    monkeypatch.setattr(stats_card_module.tempfile, "mkstemp", local_mkstemp)

    with pytest.raises(StatsCardRenderError, match="TimeoutError"):
        await renderer._render_document(
            "<main id='card'>ok</main>",
            selector="#card",
            prefix="timeout-card-",
            viewport={"width": 400, "height": 300},
        )

    assert browser.new_context_calls == 1
    assert reset_count == 1
    assert browser.contexts[0].closed is True
    remaining = renderer._circuit_open_until - asyncio.get_running_loop().time()
    assert stats_card_module._RENDER_CIRCUIT_BREAKER_SECONDS == 300
    assert 295 <= remaining <= 300
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    with pytest.raises(StatsCardRenderError, match="熔断"):
        async with renderer._render_slot():
            pass
    renderer._circuit_open_until = asyncio.get_running_loop().time()
    async with renderer._render_slot():
        pass


async def test_ensure_browser_stops_when_disconnected_browser_cannot_close() -> None:
    class DisconnectedBrowser:
        def __init__(self) -> None:
            self.close_calls = 0

        def is_connected(self) -> bool:
            return False

        async def close(self) -> None:
            self.close_calls += 1
            raise TimeoutError("browser close stalled")

    browser = DisconnectedBrowser()
    renderer = StatsCardRenderer()
    renderer._browser = browser

    with pytest.raises(StatsCardRenderError, match="浏览器清理失败.*TimeoutError"):
        await renderer._ensure_browser()

    assert browser.close_calls == 1
    assert renderer._browser is None
    assert renderer._circuit_is_open()


async def test_renderer_reset_cancellation_trips_circuit_and_finishes_driver_cleanup() -> None:
    close_started = asyncio.Event()

    class BlockingBrowser:
        async def close(self) -> None:
            close_started.set()
            await asyncio.Event().wait()

    class FakePlaywright:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def stop(self) -> None:
            self.stop_calls += 1

    playwright = FakePlaywright()
    renderer = StatsCardRenderer()
    renderer._browser = BlockingBrowser()
    renderer._playwright = playwright

    task = asyncio.create_task(renderer._reset())
    await close_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert renderer._browser is None
    assert renderer._playwright is None
    assert playwright.stop_calls == 1
    assert renderer._circuit_is_open()
    with pytest.raises(StatsCardRenderError, match="熔断"):
        async with renderer._render_slot():
            pass


async def test_renderer_reset_timeout_stops_non_timeout_retry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeLocator:
        async def wait_for(self, **_kwargs) -> None:
            return None

        async def bounding_box(self) -> dict[str, float]:
            return {"x": 0, "y": 0, "width": 20, "height": 10}

    class FailingPage:
        async def set_content(self, *_args, **_kwargs) -> None:
            return None

        async def evaluate(self, *_args, **_kwargs) -> None:
            return None

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

        async def screenshot(self, **_kwargs) -> None:
            raise RuntimeError("target page closed")

    class FakeContext:
        async def new_page(self) -> FailingPage:
            return FailingPage()

        async def close(self) -> None:
            return None

    class FakeBrowser:
        def __init__(self) -> None:
            self.new_context_calls = 0
            self.close_calls = 0

        async def new_context(self, **_kwargs) -> FakeContext:
            self.new_context_calls += 1
            return FakeContext()

        async def close(self) -> None:
            self.close_calls += 1
            raise TimeoutError("browser close stalled")

    browser = FakeBrowser()
    renderer = StatsCardRenderer(timeout_seconds=1)
    renderer._browser = browser
    ensure_calls = 0

    async def ensure_browser() -> FakeBrowser:
        nonlocal ensure_calls
        ensure_calls += 1
        return browser

    original_mkstemp = stats_card_module.tempfile.mkstemp

    def local_mkstemp(*, prefix: str, suffix: str, dir: str):
        assert dir == "/tmp"
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr(renderer, "_ensure_browser", ensure_browser)
    monkeypatch.setattr(stats_card_module.tempfile, "mkstemp", local_mkstemp)

    with pytest.raises(StatsCardRenderError, match="TimeoutError"):
        await renderer._render_document(
            "<main id='card'>ok</main>",
            selector="#card",
            prefix="reset-timeout-card-",
            viewport={"width": 400, "height": 300},
        )

    assert ensure_calls == 1
    assert browser.new_context_calls == 1
    assert browser.close_calls == 1
    assert renderer._browser is None
    assert renderer._circuit_is_open()
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_renderer_cancellation_closes_context_and_removes_partial_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    render_started = asyncio.Event()

    class BlockingPage:
        async def set_content(self, *_args, **_kwargs) -> None:
            render_started.set()
            await asyncio.Event().wait()

    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        async def new_page(self) -> BlockingPage:
            return BlockingPage()

        async def close(self) -> None:
            self.closed = True

    class FakeBrowser:
        def __init__(self) -> None:
            self.context = FakeContext()

        async def new_context(self, **_kwargs) -> FakeContext:
            return self.context

    browser = FakeBrowser()
    renderer = StatsCardRenderer(timeout_seconds=10)

    async def ensure_browser() -> FakeBrowser:
        return browser

    original_mkstemp = stats_card_module.tempfile.mkstemp

    def local_mkstemp(*, prefix: str, suffix: str, dir: str):
        assert dir == "/tmp"
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr(renderer, "_ensure_browser", ensure_browser)
    monkeypatch.setattr(stats_card_module.tempfile, "mkstemp", local_mkstemp)

    task = asyncio.create_task(
        renderer._render_document(
            "<main id='card'>ok</main>",
            selector="#card",
            prefix="cancelled-card-",
            viewport={"width": 400, "height": 300},
        )
    )
    await render_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert browser.context.closed is True
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    assert not renderer._circuit_is_open()


async def test_renderer_context_close_failure_resets_and_opens_circuit_breaker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeLocator:
        async def wait_for(self, **_kwargs) -> None:
            return None

        async def bounding_box(self) -> dict[str, float]:
            return {"x": 0, "y": 0, "width": 20, "height": 10}

    class SuccessfulPage:
        async def set_content(self, *_args, **_kwargs) -> None:
            return None

        async def evaluate(self, *_args, **_kwargs) -> None:
            return None

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

        async def screenshot(self, **kwargs) -> None:
            await asyncio.to_thread(Path(kwargs["path"]).write_bytes, b"png")

    class BrokenContext:
        async def new_page(self) -> SuccessfulPage:
            return SuccessfulPage()

        async def close(self) -> None:
            raise RuntimeError("context close failed")

    class FakeBrowser:
        def __init__(self) -> None:
            self.new_context_calls = 0

        async def new_context(self, **_kwargs) -> BrokenContext:
            self.new_context_calls += 1
            return BrokenContext()

    browser = FakeBrowser()
    renderer = StatsCardRenderer(timeout_seconds=1)
    reset_count = 0

    async def ensure_browser() -> FakeBrowser:
        return browser

    async def reset() -> None:
        nonlocal reset_count
        reset_count += 1

    original_mkstemp = stats_card_module.tempfile.mkstemp

    def local_mkstemp(*, prefix: str, suffix: str, dir: str):
        assert dir == "/tmp"
        return original_mkstemp(prefix=prefix, suffix=suffix, dir=tmp_path)

    monkeypatch.setattr(renderer, "_ensure_browser", ensure_browser)
    monkeypatch.setattr(renderer, "_reset", reset)
    monkeypatch.setattr(stats_card_module.tempfile, "mkstemp", local_mkstemp)

    output = await renderer._render_document(
        "<main id='card'>ok</main>",
        selector="#card",
        prefix="close-failure-card-",
        viewport={"width": 400, "height": 300},
    )

    assert output.read_bytes() == b"png"
    assert browser.new_context_calls == 1
    assert reset_count == 1
    with pytest.raises(StatsCardRenderError, match="熔断"):
        async with renderer._render_slot():
            pass
    output.unlink()


async def test_renderer_limits_active_and_queued_work_and_times_out_queue(
    monkeypatch,
) -> None:
    monkeypatch.setattr(stats_card_module, "_RENDER_QUEUE_TIMEOUT_SECONDS", 0.05)
    renderer = StatsCardRenderer()
    release_active = asyncio.Event()
    active_started = asyncio.Event()

    async def hold_active_slot() -> None:
        async with renderer._render_slot():
            active_started.set()
            await release_active.wait()

    async def wait_for_slot() -> None:
        with pytest.raises(StatsCardRenderError, match="排队超时"):
            async with renderer._render_slot():
                pass

    active = asyncio.create_task(hold_active_slot())
    await active_started.wait()
    queued = [asyncio.create_task(wait_for_slot()) for _ in range(2)]
    for _ in range(100):
        if renderer._pending_renders == 3:
            break
        await asyncio.sleep(0)

    assert renderer._pending_renders == 3
    with pytest.raises(StatsCardRenderError, match="繁忙"):
        async with renderer._render_slot():
            pass

    await asyncio.gather(*queued)
    release_active.set()
    await active
    assert renderer._pending_renders == 0


def test_stats_card_html_contains_metrics_and_escapes_api_text() -> None:
    stats = build_stats_snapshot(
        {
            "name": '<script>alert("x")</script>',
            "avatar": "https://img.wmpvp.com/pvp/avatar.webp",
            "steamId": "76561198929215155",
            "seasonId": "S24",
            "pvpScore": 1950,
            "pwRating": 1.07,
            "cnt": 21,
            "winRate": 0.48,
            "kills": 315,
            "assists": 89,
            "deaths": 282,
            "adr": 77.29,
            "headShotRatio": 0.5,
            "rws": 9.29,
            "mvpCount": 44,
            "summary": "神枪不朽 & 稳定发挥",
            "hotMaps": [
                {"mapName": "炙热沙城Ⅱ", "totalMatch": 13, "winCount": 8},
            ],
            "hotWeapons": [{"weaponName": "AK47", "weaponKill": 138}],
        }
    )

    result = render_stats_html(stats)

    assert "<script>alert" not in result
    assert "&lt;script&gt;alert" in result
    assert "1950" in result
    assert "48.0%" in result
    assert "炙热沙城Ⅱ" in result
    assert "AK47" in result
    assert "神枪不朽 &amp; 稳定发挥" in result
    assert "avatar-fallback" in result


def test_official_stats_card_labels_source_and_omits_pvp_placeholders() -> None:
    stats = build_official_stats_snapshot(
        [
            {
                "matchId": "LOCAL@official",
                "dataSource": 1,
                "mapName": "炼狱小镇",
                "team": 1,
                "winTeam": 1,
                "score1": 13,
                "score2": 8,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 0,
                "rating": 1.3,
                "pvpScore": 0,
                "we": 0,
            }
        ],
        steam_id="76561198929215155",
        profile={"name": "Official Player"},
    )

    result = render_stats_html(stats)

    assert "CS2 OFFICIAL · RECENT MATCHES" in result
    assert "Official Player" in result
    assert "胜-负-平" in result
    assert "完美分数" not in result
    assert "官匹不提供 ELO · WE · RWS" in result
    assert "<label>ADR</label>" not in result
    assert "<label>爆头率</label>" not in result
    assert "DATA SCOPE · 官匹口径" in result
    assert "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD" in result
    assert "暂无武器数据" not in result
    assert 'aria-hidden="true">MM</div>' in result


def test_stats_card_embeds_selected_background_and_theme_colors() -> None:
    stats = build_stats_snapshot({"name": "牛牛", "steamId": "76561198929215155"})

    result = render_stats_html(
        stats,
        background_key="cyber_neon",
        background_data_uri="data:image/png;base64,YmFja2dyb3VuZA==",
    )

    assert "data:image/png;base64,YmFja2dyb3VuZA==" in result
    assert "--accent: #63e6ff" in result
    assert "center / cover no-repeat" in result


def test_stats_card_displays_and_escapes_member_badge() -> None:
    stats = build_stats_snapshot({"name": "牛牛", "steamId": "76561198929215155"})

    result = render_stats_html(stats, member_badge='<script>alert("badge")</script>')
    without_badge = render_stats_html(stats)

    assert '<script>alert("badge")</script>' not in result
    assert "&lt;script&gt;alert(&quot;badge&quot;)&lt;/script&gt;" in result
    assert 'class="member-title"' in result
    assert 'class="member-title"' not in without_badge


def test_generated_stats_background_assets_are_packaged() -> None:
    neon = stats_background_data_uri("cyber_neon")
    desert = stats_background_data_uri("desert_sunset")

    assert neon is not None and neon.startswith("data:image/png;base64,")
    assert desert is not None and desert.startswith("data:image/png;base64,")
    assert stats_background_data_uri("classic") is None


async def test_stats_card_embeds_only_trusted_small_avatar() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "img.wmpvp.com"
        return httpx.Response(
            200,
            headers={"content-type": "image/webp"},
            content=b"safe-avatar",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        avatar = await fetch_avatar_data_uri(
            "https://img.wmpvp.com/pvp/avatar.webp",
            client=client,
        )
        rejected = await fetch_avatar_data_uri(
            "https://example.com/avatar.webp",
            client=client,
        )

    assert avatar == "data:image/webp;base64,c2FmZS1hdmF0YXI="
    assert rejected is None

    stats = build_stats_snapshot(
        {
            "name": "𝓗𝓾𝓷𝓰𝓮𝓻",
            "avatar": "https://img.wmpvp.com/pvp/avatar.webp",
        }
    )
    result = render_stats_html(stats, avatar_data_uri=avatar)

    assert "𝓗𝓾𝓷𝓰𝓮𝓻" in result
    assert 'class="avatar"' in result
    assert 'src="data:image/webp;base64,c2FmZS1hdmF0YXI="' in result


async def test_skin_price_card_embeds_only_trusted_steam_preview() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "community.akamai.steamstatic.com"
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"skin-preview",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        preview = await fetch_skin_image_data_uri(
            "https://community.akamai.steamstatic.com/economy/image/test",
            client=client,
        )
        rejected = await fetch_skin_image_data_uri(
            "https://example.com/skin.png",
            client=client,
        )

    assert preview == "data:image/png;base64,c2tpbi1wcmV2aWV3"
    assert rejected is None

    report = SkinPriceReport(
        (
            SkinReportEntry(
                SkinQuote(
                    '<img src=x onerror="alert(1)">',
                    10,
                    9,
                    7.2,
                ),
                previous_sell_cny=60,
            ),
        ),
        report_date="2026-07-06",
    )
    result = render_skin_price_html(report, image_data_uris=(preview,))

    assert '<img src=x onerror="alert(1)">' not in result
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in result
    assert 'src="data:image/png;base64,c2tpbi1wcmV2aWV3"' in result
    assert "¥72.00" in result
    assert "2026-07-06" in result


def test_match_card_contains_result_and_escapes_api_text() -> None:
    match = build_match_snapshot(
        {
            "matchId": "PVP@test",
            "mapName": '<img src=x onerror="alert(1)">',
            "mode": "天梯组排对局",
            "team": 1,
            "winTeam": 2,
            "score1": 9,
            "score2": 13,
            "kill": 20,
            "assist": 4,
            "death": 15,
            "pwRating": 1.2,
            "pvpScoreChange": -11,
            "endTime": "2026-06-30 21:30:00",
        },
        steam_id="76561198929215155",
        profile={"name": "𝓗𝓾𝓷𝓰𝓮𝓻"},
    )

    result = render_match_html(match)

    assert '<img src=x onerror="alert(1)">' not in result
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in result
    assert "𝓗𝓾𝓷𝓰𝓮𝓻" in result
    assert "失败" in result
    assert "9" in result and "13" in result
    assert "-11" in result
    assert "match-avatar-fallback" in result


def test_match_scoreboard_contains_all_ten_players_and_marks_group_members() -> None:
    players = [
        {
            "playerId": f"765611980000000{index:02d}",
            "nickName": '<script>alert("x")</script>' if index == 0 else f"Player {index + 1}",
            "team": 1 if index < 5 else 2,
            "kill": 20 - index,
            "assist": 4,
            "death": 15,
            "pwRating": 1.2,
            "adpr": 80,
            "we": 10,
            "pvpScore": 1800,
        }
        for index in range(10)
    ]
    match = build_match_snapshot(
        {"matchId": "PVP@test", "team": 1},
        steam_id=players[0]["playerId"],
        group_member_steam_ids={players[0]["playerId"], players[7]["playerId"]},
        detail={
            "base": {
                "matchId": "PVP@test",
                "map": "荒漠迷城",
                "mode": "天梯组排对局",
                "score1": 13,
                "score2": 9,
                "winTeam": 1,
                "team1Name": "队伍 A",
                "team2Name": "队伍 B",
            },
            "players": players,
        },
    )

    result = render_match_html(match)

    assert len(match.players) == 10
    assert sum(player.is_group_member for player in match.players) == 2
    assert '<script>alert("x")</script>' not in result
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in result
    assert result.count('class="player-row') == 10
    assert result.count("群成员") >= 2
    assert "队伍 A" in result and "队伍 B" in result
    assert "共 10 名玩家" in result
    assert "<span>WE</span><span>完美分</span>" in result
    assert "official-grid" not in result


def test_official_match_scoreboard_reuses_shared_player_grid() -> None:
    players = [
        {
            "playerId": f"7656119800000010{index}",
            "nickName": f"Official {index + 1}",
            "team": 1 if index < 5 else 2,
            "kill": 20 - index,
            "assist": 4,
            "death": 15,
            "rating": 1.2,
            "adpr": 80,
        }
        for index in range(10)
    ]
    match = build_match_snapshot(
        {"matchId": "LOCAL@scoreboard", "dataSource": 1, "team": 1},
        steam_id=players[0]["playerId"],
        detail={
            "base": {
                "matchId": "LOCAL@scoreboard",
                "dataSource": 1,
                "score1": 13,
                "score2": 9,
            },
            "players": players,
        },
    )

    result = render_match_html(match)

    assert result.count('class="player-row') == 10
    assert "<span>WE</span><span>完美分</span>" in result
    assert "OFFICIAL MATCH VIA PERFECT WORLD" in result
    assert "official-grid" not in result


def test_match_scoreboard_embeds_and_escapes_ai_commentary() -> None:
    players = [
        {
            "playerId": f"7656119800000000{index}",
            "nickName": f"Player {index}",
            "team": 1 if index < 5 else 2,
            "kill": 20 - index,
            "assist": 4,
            "death": 15,
            "pwRating": 1.2,
            "adpr": 80,
        }
        for index in range(10)
    ]
    match = build_match_snapshot(
        {"matchId": "PVP@commentary", "team": 1},
        steam_id=players[0]["playerId"],
        group_member_steam_ids={players[0]["playerId"]},
        detail={
            "base": {"matchId": "PVP@commentary", "score1": 13, "score2": 9},
            "players": players,
        },
    )

    result = render_match_html(
        match,
        commentary=(
            "🐮 赛后 AI 锐评\n"
            "赛果核对：队伍 1 13:9 队伍 2，胜方为队伍 1\n"
            "Player 0｜胜利｜队伍 1\n"
            "火力在线，<script>alert(1)</script>；下场继续保持补枪距离。\n"
            "不想被点评可发送 #关闭锐评"
        ),
    )

    assert 'data-testid="match-commentary"' in result
    assert "牛牛赛后锐评" in result
    assert "赛果核对：" not in result
    assert "Player 0｜胜利｜队伍 1" in result
    assert "<script>alert(1)</script>" not in result
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result


def test_match_scoreboard_omits_commentary_panel_without_ai_result() -> None:
    match = build_match_snapshot(
        {"matchId": "PVP@plain", "team": 1},
        steam_id="76561198000000000",
        detail={
            "base": {"matchId": "PVP@plain", "score1": 13, "score2": 9},
            "players": [
                {
                    "playerId": "76561198000000000",
                    "nickName": "Player",
                    "team": 1,
                    "kill": 20,
                    "assist": 4,
                    "death": 15,
                }
            ],
        },
    )

    result = render_match_html(match, commentary=None)

    assert 'data-testid="match-commentary"' not in result


def test_official_match_card_uses_official_source_label() -> None:
    match = build_match_snapshot(
        {
            "matchId": "LOCAL@test",
            "dataSource": 1,
            "team": 1,
            "winTeam": 1,
            "score1": 13,
            "score2": 8,
            "kill": 20,
            "assist": 5,
            "death": 10,
            "rating": 1.3,
        },
        steam_id="76561198000000001",
    )

    result = render_match_html(match)

    assert match.mode == "CS2 官匹"
    assert "NEW MATCH · CS2 OFFICIAL" in result
    assert "OFFICIAL MATCH VIA PERFECT WORLD" in result
    assert ">MM</div>" in result
    assert "<label>WE</label>" in result
    assert "<label>完美分</label>" in result
    assert "<label>分数变化</label>" in result
    assert "official-grid" not in result


def test_match_history_card_contains_ten_rows_and_escapes_text() -> None:
    history = build_match_history(
        [
            {
                "matchId": f"PVP@{index}",
                "timeStamp": 1000 - index,
                "endTime": "2026-07-01 20:00:00",
                "mapName": '<img src=x onerror="alert(1)">' if index == 0 else "荒漠迷城",
                "mode": "天梯组排对局",
                "team": 1,
                "winTeam": 1 if index % 2 else 2,
                "score1": 13 if index % 2 else 8,
                "score2": 8 if index % 2 else 13,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 1.2,
                "we": 9.5,
                "pvpScoreChange": 10,
            }
            for index in range(10)
        ],
        steam_id="76561198929215155",
        profile={"name": "History Player"},
    )

    result = render_match_history_html(history)

    assert '<img src=x onerror="alert(1)">' not in result
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in result
    assert result.count('class="history-row"') == 10
    assert "History Player · 完美近十场战绩" in result
    assert "5-5" in result
    assert "+100" in result

    themed = render_match_history_html(
        history,
        background_key="desert_sunset",
        background_data_uri="data:image/png;base64,YmFja2dyb3VuZA==",
    )
    assert "data:image/png;base64,YmFja2dyb3VuZA==" in themed
    assert "--accent: #ffd166" in themed


def test_match_history_card_displays_and_escapes_member_badge() -> None:
    history = build_match_history(
        [],
        steam_id="76561198929215155",
        profile={"name": "History Player"},
    )

    result = render_match_history_html(history, member_badge='<img src=x onerror="x">')
    without_badge = render_match_history_html(history)

    assert '<img src=x onerror="x">' not in result
    assert "&lt;img src=x onerror=&quot;x&quot;&gt;" in result
    assert 'class="history-member-title"' in result
    assert 'class="history-member-title"' not in without_badge


def test_official_match_history_card_reuses_shared_columns() -> None:
    history = build_match_history(
        [
            {
                "matchId": "LOCAL@official",
                "dataSource": 1,
                "timeStamp": 1000,
                "endTime": "2026-07-01 20:00:00",
                "mapName": "炼狱小镇",
                "mode": "国服竞技",
                "team": 1,
                "winTeam": 1,
                "score1": 13,
                "score2": 8,
                "kill": 20,
                "assist": 5,
                "death": 10,
                "pwRating": 0,
                "rating": 1.3,
                "we": 0,
                "pvpScoreChange": 0,
            }
        ],
        steam_id="76561198929215155",
        profile={"name": "Official Player"},
        source="official",
    )

    result = render_match_history_html(history)

    assert "RECENT MATCHES · CS2 OFFICIAL" in result
    assert "Official Player · 官匹近十场战绩" in result
    assert result.count('class="history-row"') == 1
    assert "<span>WE</span>" in result
    assert "<span>ELO</span>" in result
    assert "ELO 变化" in result
    assert '<div class="">-</div>' in result
    assert '<div class="history-negative">-</div>' not in result
    assert "official-grid" not in result
    assert "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD" in result


async def test_match_history_renderer_loads_avatar_and_renders_document(
    tmp_path: Path, monkeypatch
) -> None:
    history = build_match_history(
        [],
        steam_id="76561198929215155",
        profile={"name": "History Player", "avatar": "https://img.wmpvp.com/a.png"},
    )
    output = tmp_path / "history.png"
    output.write_bytes(b"png")
    renderer = StatsCardRenderer(timeout_seconds=15)

    async def fake_avatar(url: str, **kwargs) -> str:
        assert url.endswith("a.png")
        assert kwargs == {"timeout_seconds": 5.0}
        return "data:image/png;base64,eA=="

    async def fake_document(document: str, **kwargs) -> Path:
        assert "data:image/png;base64,eA==" in document
        assert "残局指挥官" in document
        assert kwargs["selector"] == "#history-card"
        return output

    monkeypatch.setattr(stats_card_module, "fetch_avatar_data_uri", fake_avatar)
    monkeypatch.setattr(renderer, "_render_document", fake_document)

    assert await renderer.render_match_history(history, member_badge="残局指挥官") == output


def test_ranking_card_contains_metrics_and_escapes_names() -> None:
    entries = [
        build_ranking_entry(
            {
                "name": '<img src=x onerror="alert(1)">' if index == 0 else f"Player {index}",
                "steamId": f"7656119800000000{index}",
                "seasonId": "S24",
                "cnt": 20,
                "pvpScore": 2000 - index,
                "pwRating": 1.2,
                "winRate": 0.55,
                "adr": 82,
                "headShotRatio": 0.48,
                "radar": {
                    "marksmanship": {
                        "detail": {"rapid_stop_rate_raw": 0.83 - index / 100}
                    }
                },
            },
            qq_id=f"10000{index}",
            steam_id=f"7656119800000000{index}",
        )
        for index in range(4)
    ]
    ranking = build_ranking_snapshot(
        entries,
        metric="rapid_stop",
        total_count=5,
        failed_count=1,
    )

    result = render_ranking_html(ranking)

    assert '<img src=x onerror="alert(1)">' not in result
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in result
    assert result.count('class="ranking-row"') == 4
    assert "急停率" in result
    assert "83.0%" in result
    assert "1 人查询失败" in result
    assert "Steam 76561198000000000" in result
    assert "QQ 100000" not in result


def test_official_ranking_card_only_contains_supported_columns() -> None:
    entry = build_official_ranking_entry(
        [
            {
                "matchId": "LOCAL@official",
                "dataSource": 1,
                "team": 1,
                "winTeam": 1,
                "kill": 20,
                "death": 10,
                "pwRating": 0,
                "rating": 1.3,
                "pvpScore": 0,
                "we": 0,
            }
        ],
        qq_id="100001",
        steam_id="76561198929215155",
        name="Official Player",
    )
    assert entry is not None
    ranking = build_ranking_snapshot(
        [entry],
        metric="rating",
        total_count=1,
        source="official",
    )

    result = render_ranking_html(ranking)

    assert "GROUP LEADERBOARD · CS2 OFFICIAL" in result
    assert "Steam 76561198929215155" in result
    assert "QQ 100001" not in result
    assert "最近官匹汇总" in result
    assert 'class="ranking-row official-grid"' in result
    assert "<span>场次</span>" in result
    assert "<span>Rating</span>" in result
    assert "<span>胜率</span>" in result
    assert "<span>K/D</span>" in result
    assert "<span>ELO</span>" not in result
    assert "<span>ADR</span>" not in result
    assert "<span>急停率</span>" not in result
    assert "<span>爆头率</span>" not in result
    assert "PWCS2 BOT · OFFICIAL DATA VIA PERFECT WORLD" in result


def test_chemistry_network_card_draws_edges_and_escapes_names() -> None:
    shared = [{"matchId": "shared", "team": 1, "winTeam": 1, "pwRating": 1.1}]
    snapshot = build_chemistry_network(
        {"10001": shared, "10002": shared},
        names_by_qq={"10001": "<牛牛>", "10002": "精灵"},
        week_start="2026-07-20",
    )

    result = render_chemistry_network_html(snapshot)

    assert 'id="chemistry-network-card"' in result
    assert "<牛牛>" not in result
    assert "&lt;牛牛&gt;" in result
    assert "最佳搭档" in result
    assert "同队场次" in result


def test_weekly_report_card_contains_medals_and_escapes_names() -> None:
    member = WeeklyReportMember(
        qq_id="10001",
        name="<牛牛>",
        matches=6,
        wins=4,
        losses=2,
        kills=120,
        deaths=80,
        kd=1.5,
        avg_rating=1.23,
        avg_adr=88.2,
        elo_change=66,
        favorite_map="荒漠迷城",
    )
    snapshot = WeeklyReportSnapshot(
        group_id="group",
        week_start="2026-07-20",
        week_end="2026-07-26",
        bound_members=10,
        active_members=1,
        unique_matches=6,
        total_kills=120,
        total_elo_change=66,
        popular_maps=(("荒漠迷城", 6),),
        members=(member,),
        activity_rewards=(("10001", "<牛牛>", 30),),
    )

    document = render_weekly_report_html(snapshot)

    assert 'id="weekly-report-card"' in document
    assert "本周战神" in document
    assert "<牛牛>" not in document
    assert "&lt;牛牛&gt;" in document
    assert "五场活跃奖励" in document
    assert "+30" in document


def test_watch_stage_card_contains_live_teams_and_escapes_names() -> None:
    players = (
        WatchStagePlayer(
            steam_id="76561198000000001",
            side="CT",
            nickname="<牛牛>",
            avatar_url="",
            kills=14,
            assists=3,
            deaths=8,
            score=32,
            adr=91.2,
            headshot="57%",
            alive=True,
            pvp_score=2100,
            avg_rating=1.23,
            avg_we=12.5,
            is_group_member=True,
        ),
        WatchStagePlayer(
            steam_id="76561198000000002",
            side="TERRORIST",
            nickname="精灵",
            avatar_url="",
            kills=10,
            assists=4,
            deaths=10,
            score=25,
            adr=78.4,
            headshot="40%",
            alive=False,
            pvp_score=1900,
            avg_rating=1.08,
            avg_we=None,
        ),
    )
    snapshot = WatchStageSnapshot(
        status="running",
        requested_steam_id="76561198000000001",
        requested_name="<牛牛>",
        platform="2",
        match_id="live-match",
        map_name="de_mirage",
        ct_score=7,
        terrorist_score=5,
        duration="12:34",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=players,
    )

    document = render_watch_stage_html(snapshot)

    assert 'id="watch-stage-card"' in document
    assert "荒漠迷城 · de_mirage" in document
    assert "7 : 5" in document
    assert "<牛牛>" not in document
    assert "&lt;牛牛&gt;" in document
    assert "群友" in document
    assert "ALIVE" in document
    assert "DEAD" in document


def test_official_watch_stage_card_does_not_render_perfect_elo() -> None:
    snapshot = WatchStageSnapshot(
        status="running",
        requested_steam_id="76561198000000001",
        requested_name="官匹牛牛",
        platform="1",
        match_id="1.2.3.4:27015",
        map_name="de_inferno",
        ct_score=2,
        terrorist_score=1,
        duration="02:00",
        warmup=False,
        updated_at="2026-07-27T00:00:00+00:00",
        players=(
            WatchStagePlayer(
                steam_id="76561198000000001",
                side="CT",
                nickname="官匹牛牛",
                avatar_url="",
                kills=4,
                assists=1,
                deaths=2,
                score=9,
                adr=96.0,
                headshot="50%",
                alive=True,
                pvp_score=2100,
                avg_rating=1.3,
                avg_we=12.5,
                is_group_member=True,
            ),
        ),
    )

    document = render_watch_stage_html(snapshot)

    assert "CS2 OFFICIAL · WATCH STAGE" in document
    assert "Rating 1.30" in document
    assert "ELO 2100" not in document
    assert "PERFECT WORLD · WATCH STAGE" not in document


def test_game_status_card_contains_presence_and_escapes_text() -> None:
    snapshot = GameStatusSnapshot(
        entries=(
            GameStatusEntry(
                name='<script>alert("name")</script>',
                steam_id="76561198000000001",
                status_label="正在游戏",
                status_kind="playing",
                game_name="Counter-Strike 2 & Friends",
                detail='<img src=x onerror="alert(1)">',
                app_id="730",
            ),
            GameStatusEntry(
                name="测试成员",
                steam_id="76561198000000002",
                status_label="离开",
                status_kind="away",
            ),
            GameStatusEntry(
                name="Unknown",
                steam_id="",
                status_label="状态不可见",
                status_kind='playing"><script>alert(2)</script>',
            ),
        ),
        total_count=27,
        playing_count=1,
        hidden_count=22,
        stale=True,
        degraded=True,
        title="<群内游戏状态>",
        source_label="STEAM & WATCH STAGE",
        note="<接口暂时不可用>",
    )

    document = render_game_status_html(snapshot)

    assert 'id="game-status-card"' in document
    assert '<script>alert("name")</script>' not in document
    assert "&lt;script&gt;alert(&quot;name&quot;)&lt;/script&gt;" in document
    assert '<img src=x onerror="alert(1)">' not in document
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in document
    assert 'playing"><script>' not in document
    assert "Counter-Strike 2 &amp; Friends" in document
    assert "SteamID 76561198000000001 · AppID 730" in document
    assert "&lt;群内游戏状态&gt;" in document
    assert "STEAM &amp; WATCH STAGE" in document
    assert "&lt;接口暂时不可用&gt;" in document
    assert "缓存状态" in document
    assert "降级数据" in document
    assert "已绑定成员 27 人 · 当前公开 2 人" in document


def test_game_status_card_has_clear_empty_state() -> None:
    document = render_game_status_html(
        GameStatusSnapshot(
            entries=(),
            total_count=5,
            playing_count=0,
            hidden_count=5,
            note="当前没人公开显示正在游戏",
        )
    )

    assert "当前没有可展示的公开状态" in document
    assert "当前没人公开显示正在游戏" in document
    assert "实时数据" in document
    assert "已绑定成员 5 人 · 当前公开 0 人" in document


async def test_game_status_renderer_uses_dedicated_card_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    snapshot = GameStatusSnapshot(
        entries=tuple(
            GameStatusEntry(
                name=f"Player {index}",
                steam_id=f"765611980000000{index:02d}",
                status_label="在线",
                status_kind="online",
            )
            for index in range(5)
        ),
        total_count=5,
        playing_count=0,
    )
    renderer = StatsCardRenderer()
    output = tmp_path / "game-status.png"
    captured: dict[str, object] = {}

    async def fake_render_document(
        document: str,
        *,
        selector: str,
        prefix: str,
        viewport: dict[str, int],
    ) -> Path:
        captured.update(
            document=document,
            selector=selector,
            prefix=prefix,
            viewport=viewport,
        )
        return output

    monkeypatch.setattr(renderer, "_render_document", fake_render_document)

    result = await renderer.render_game_status(snapshot)

    assert result == output
    assert captured["selector"] == "#game-status-card"
    assert captured["prefix"] == "pwcs2-game-status-"
    assert captured["viewport"] == {"width": 1160, "height": 805}
    assert 'id="game-status-card"' in str(captured["document"])


def test_valorant_card_renders_summary_and_limits_history_to_ten() -> None:
    matches = tuple(
        ValorantMatch(
            agent=f"Agent {index}",
            result="胜利" if index % 2 == 0 else "失败",
            score="13:9",
            kda=f"{20 + index}/12/5",
            acs=str(220 + index),
            map_name=f"Map {index}",
            rank="黄金 1",
            rr_change="+19",
            ended_at=f"2026-07-{index + 1:02d} 20:30:00",
            duration="36 分钟",
            highlight=f"亮点 {index}",
        )
        for index in range(12)
    )
    snapshot = ValorantSnapshot(
        riot_id="ExamplePlayer#12345",
        rank="黄金 1",
        record="9胜 1负",
        win_rate="90%",
        kda="1.22",
        acs="222",
        rr_change="+174",
        source_updated_at="近 10 场排位 · 数据同步于 20:40",
        fetched_at="2026-07-31 08:00:00",
        matches=matches,
        cache_status="stale",
        cache_note="第三方源暂时不可用，展示最近成功缓存。",
    )

    document = render_valorant_html(snapshot)

    assert 'id="valorant-card"' in document
    assert "VALORANT · CN RANKED" in document
    assert "ExamplePlayer#12345" in document
    assert "黄金 1" in document
    assert "9胜 1负" in document
    assert "90%" in document
    assert "1.22" in document
    assert "222" in document
    assert "+174" in document
    assert document.count('class="match-row ') == 10
    assert "Agent 9" in document
    assert "Agent 10" not in document
    assert "历史缓存" in document
    assert "第三方源暂时不可用，展示最近成功缓存。" in document


def test_valorant_card_escapes_every_external_text_field() -> None:
    payload = "<img src=x onerror=alert('valorant')>"
    snapshot = ValorantSnapshot(
        riot_id=payload,
        rank=payload,
        record=payload,
        win_rate=payload,
        kda=payload,
        acs=payload,
        rr_change=payload,
        source_updated_at=payload,
        fetched_at=payload,
        matches=(
            ValorantMatch(
                agent=payload,
                result=payload,
                score=payload,
                kda=payload,
                acs=payload,
                map_name=payload,
                rank=payload,
                rr_change=payload,
                ended_at=payload,
                duration=payload,
                highlight=payload,
            ),
        ),
        cache_status="stale",
        cache_note=payload,
    )

    document = render_valorant_html(snapshot)
    escaped = "&lt;img src=x onerror=alert(&#x27;valorant&#x27;)&gt;"

    assert payload not in document
    assert document.count(escaped) == 21
    assert "<img src=x" not in document


async def test_valorant_renderer_uses_dedicated_card_target(
    monkeypatch,
) -> None:
    snapshot = ValorantSnapshot(
        riot_id="ExamplePlayer#12345",
        rank="黄金 1",
        record="1胜 0负",
        win_rate="100%",
        kda="1.20",
        acs="222",
        rr_change="+19",
        source_updated_at="刚刚",
        fetched_at="2026-07-31 08:00:00",
        matches=(
            ValorantMatch(
                agent="捷风",
                result="胜利",
                score="13:9",
                kda="20/12/5",
                acs="222",
                map_name="源工重镇",
                rank="黄金 1",
                rr_change="+19",
                ended_at="2026-07-31 07:00:00",
                duration="36 分钟",
            ),
        ),
    )
    renderer = StatsCardRenderer()
    output = Path("valorant.png")
    captured: dict[str, object] = {}

    async def fake_render_document(
        document: str,
        *,
        selector: str,
        prefix: str,
        viewport: dict[str, int],
    ) -> Path:
        captured.update(
            document=document,
            selector=selector,
            prefix=prefix,
            viewport=viewport,
        )
        return output

    monkeypatch.setattr(renderer, "_render_document", fake_render_document)

    result = await renderer.render_valorant(snapshot)

    assert result == output
    assert captured["selector"] == "#valorant-card"
    assert captured["prefix"] == "pwcs2-valorant-"
    assert captured["viewport"] == {"width": 1160, "height": 760}
    assert 'id="valorant-card"' in str(captured["document"])
