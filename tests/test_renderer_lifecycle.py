import asyncio
import sys
import types

import pytest

from pwcs2_bot.stats_card import StatsCardRenderer, StatsCardRenderError


class Browser:
    def __init__(self):
        self.closed = asyncio.Event()

    def is_connected(self):
        return not self.closed.is_set()

    async def close(self):
        self.closed.set()


class Playwright:
    def __init__(self):
        self.stopped = False
        self.chromium = self

    async def start(self):
        return self

    async def stop(self):
        self.stopped = True

    async def launch(self, **_kwargs):
        return Browser()


async def test_browser_and_driver_released_after_idle():
    renderer = StatsCardRenderer(idle_timeout_seconds=0.01)
    browser, driver = Browser(), Playwright()
    async with renderer._render_slot():
        renderer._browser, renderer._playwright = browser, driver
    await asyncio.wait_for(browser.closed.wait(), timeout=1)
    await renderer.close()
    assert driver.stopped
    assert renderer._browser is None
    assert renderer._playwright is None
    assert renderer._idle_task is None
    assert renderer._idle_handle is None


async def test_idle_cleanup_never_interrupts_current_or_queued_render():
    renderer = StatsCardRenderer(idle_timeout_seconds=0.01)
    browser = Browser()
    async with renderer._render_slot():
        renderer._browser = browser
    async with renderer._render_slot():
        assert renderer._idle_handle is None
        renderer._last_activity = 0
        cleanup = asyncio.create_task(renderer._close_when_idle())
        await asyncio.sleep(0)
        assert not browser.closed.is_set()
    await cleanup
    assert not browser.closed.is_set()
    await asyncio.wait_for(browser.closed.wait(), timeout=1)
    await renderer.close()


async def test_shutdown_cancels_idle_timer_and_rejects_late_renders():
    renderer = StatsCardRenderer(idle_timeout_seconds=120)
    browser = Browser()
    async with renderer._render_slot():
        renderer._browser = browser
    assert renderer._idle_handle is not None
    await renderer.close()
    assert browser.closed.is_set()
    assert renderer._idle_handle is None
    with pytest.raises(StatsCardRenderError, match="已关闭"):
        async with renderer._render_slot():
            pass


async def test_browser_recycled_between_renders_at_usage_limit(monkeypatch):
    renderer = StatsCardRenderer(max_browser_renders=2)
    old_browser, old_driver, new_driver = Browser(), Playwright(), Playwright()
    renderer._browser, renderer._playwright = old_browser, old_driver
    renderer._browser_render_count = 1
    assert await renderer._ensure_browser() is old_browser
    renderer._browser_render_count = 2
    package = types.ModuleType("playwright")
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: new_driver
    package.async_api = module
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    new_browser = await renderer._ensure_browser()
    assert new_browser is not old_browser
    assert old_browser.closed.is_set() and old_driver.stopped
    assert renderer._browser_render_count == 0
    await renderer.close()
    assert new_browser.closed.is_set() and new_driver.stopped
