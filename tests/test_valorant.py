from __future__ import annotations

import httpx
import pytest

from pwcs2_bot.valorant import (
    ValorantClient,
    ValorantParseError,
    ValorantPlayerNotFound,
    ValorantQueryError,
    parse_valorant_id,
    parse_valorant_result_html,
)

SUCCESS_HTML = """
<!doctype html>
<html lang="zh-CN">
  <body class="theme-dark public-results-page">
    <main>
      <h1 id="result-player-name">ExamplePlayer#12345</h1>
      <p class="result-location">近 10 场排位 · IP属地 安徽</p>
      <div class="result-rank-line">
        <strong>黄金 1</strong>
        <span>9 胜 1 负</span>
      </div>
      <section class="result-summary">
        <div class="result-metric metric-win-rate">
          <span>近 10 场胜率</span><strong>90%</strong>
        </div>
        <div class="result-metric metric-kda">
          <span>近 10 场 KDA</span><strong>1.22</strong>
        </div>
        <div class="result-metric metric-acs">
          <span>近 10 场 ACS</span><strong>222</strong>
        </div>
        <div class="result-metric metric-rr">
          <span>近 10 场 RR</span><strong>+174</strong>
        </div>
      </section>
      <section class="public-match-list">
        <article class="public-match-card win">
          <div class="match-agent"><img alt=""><span>捷风</span></div>
          <span class="match-result">胜利</span>
          <div class="match-outcome"><small>比分</small><strong>14:12</strong></div>
          <div class="match-kda"><small>K / D / A</small><strong>19 / 21 / 5</strong></div>
          <div class="match-acs"><small>ACS</small><strong>208</strong></div>
          <div class="match-map"><small>地图</small><strong>源工重镇</strong></div>
          <div class="match-highlight">首杀 4</div>
          <div class="match-rank"><strong>黄金 1</strong><b>+19 RR</b></div>
          <div class="match-time">
            <strong>2026-07-30</strong><span>22:14:05</span><span>38 分钟</span>
          </div>
          <button data-battle-detail-ref="battle-1">查看十人详情</button>
        </article>
        <article class="public-match-card loss">
          <div class="match-agent"><span>幻棱</span></div>
          <span class="match-result">失败</span>
          <div class="match-outcome"><strong>9:13</strong></div>
          <div class="match-kda"><strong>16/18/4</strong></div>
          <div class="match-acs"><strong>196</strong></div>
          <div class="match-map"><strong>亚海悬城</strong></div>
          <div class="match-rank"><strong>白银 3</strong><b>-16 RR</b></div>
          <div class="match-time">
            <strong>2026-07-30</strong><span>20:01:02</span><span>34 分钟</span>
          </div>
        </article>
      </section>
    </main>
  </body>
</html>
"""


def test_parse_result_uses_only_audited_public_result_selectors() -> None:
    snapshot = parse_valorant_result_html(
        SUCCESS_HTML,
        requested_riot_id="ExamplePlayer#12345",
        fetched_at="2026-07-31T00:00:00+00:00",
    )

    assert snapshot.riot_id == "ExamplePlayer#12345"
    assert snapshot.rank == "黄金 1"
    assert snapshot.record == "9胜1负"
    assert snapshot.win_rate == "90%"
    assert snapshot.kda == "1.22"
    assert snapshot.acs == "222"
    assert snapshot.rr_change == "+174"
    assert snapshot.source_updated_at == "近 10 场排位"
    assert "IP属地" not in snapshot.source_updated_at
    assert len(snapshot.matches) == 2
    first = snapshot.matches[0]
    assert first.agent == "捷风"
    assert first.result == "胜利"
    assert first.score == "14:12"
    assert first.kda == "19/21/5"
    assert first.acs == "208"
    assert first.map_name == "源工重镇"
    assert first.rank == "黄金 1"
    assert first.rr_change == "+19 RR"
    assert first.ended_at == "2026-07-30 22:14:05"
    assert first.duration == "38 分钟"
    assert first.highlight == "首杀 4"


def test_explicit_200_not_found_alert_wins_over_embedded_demo_cards() -> None:
    document = f"""
    <html><body class="home-page">
      <p class="query-message" role="alert">
        没有找到该玩家，请检查昵称和数字 ID。
      </p>
      <section class="home-demo-results">{SUCCESS_HTML}</section>
    </body></html>
    """

    with pytest.raises(ValorantPlayerNotFound, match="没有查到 ExamplePlayer#12345"):
        parse_valorant_result_html(
            document,
            requested_riot_id="ExamplePlayer#12345",
        )


def test_200_temporary_alert_is_generic_and_does_not_echo_diagnostics() -> None:
    document = """
    <html><body class="home-page">
      <p class="query-message" role="alert">
        查询失败：code=UPSTREAM_502，IP属地 安徽。
      </p>
    </body></html>
    """

    with pytest.raises(ValorantQueryError) as captured:
        parse_valorant_result_html(
            document,
            requested_riot_id="ExamplePlayer#12345",
        )

    assert str(captured.value) == "国服战绩源暂不可用，请稍后重试"
    assert "IP属地" not in str(captured.value)
    assert "UPSTREAM_502" not in str(captured.value)


def test_demo_page_without_result_body_is_rejected() -> None:
    document = SUCCESS_HTML.replace(
        'class="theme-dark public-results-page"',
        'class="home-page"',
    )

    with pytest.raises(ValorantParseError, match="不是公开战绩结果页"):
        parse_valorant_result_html(
            document,
            requested_riot_id="ExamplePlayer#12345",
        )


def test_explicit_empty_result_is_valid_without_a_match_list() -> None:
    document = """
    <html><body class="public-results-page">
      <h1 id="result-player-name">新人#60450</h1>
      <div class="result-empty">没有拿到近期排位战绩。</div>
    </body></html>
    """

    snapshot = parse_valorant_result_html(
        document,
        requested_riot_id="新人#60450",
    )

    assert snapshot.riot_id == "新人#60450"
    assert snapshot.rank == "暂无段位"
    assert snapshot.record == "0胜0负"
    assert snapshot.win_rate == "-"
    assert snapshot.matches == ()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" ExamplePlayer#12345 ", "ExamplePlayer#12345"),
        ("ＥｘａｍｐｌｅＰｌａｙｅｒ＃１２３４５", "ExamplePlayer#12345"),
        ("玩家  名字#12345", "玩家 名字#12345"),
        ("玩家#abcde", None),
        ("玩家#12", None),
        ("玩家#123#456", None),
        ("玩家\n名字#12345", None),
    ],
)
def test_parse_valorant_id_normalization(value: str, expected: str | None) -> None:
    assert parse_valorant_id(value) == expected


@pytest.mark.asyncio
async def test_client_percent_encodes_hash_and_parses_html() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
            content=SUCCESS_HTML.encode(),
        )

    client = ValorantClient(transport=httpx.MockTransport(handler))
    try:
        snapshot = await client.query("ExamplePlayer#12345")
    finally:
        await client.close()

    assert snapshot.riot_id == "ExamplePlayer#12345"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "wgval.cloud"
    assert request.url.params["q"] == "ExamplePlayer#12345"
    assert "q=ExamplePlayer%2312345" in str(request.url)
    assert request.url.fragment == ""


@pytest.mark.asyncio
async def test_client_maps_all_httpx_transport_errors_to_safe_query_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError(
            "environment proxy leaked into request",
            request=request,
        )

    client = ValorantClient(
        transport=httpx.MockTransport(handler),
        failure_threshold=1,
    )
    try:
        with pytest.raises(
            ValorantQueryError,
            match="连接超时或网络异常",
        ):
            await client.query("ExamplePlayer#12345")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_client_rejects_http_error_before_parsing_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            request=request,
            headers={"content-type": "text/html"},
            content=SUCCESS_HTML.encode(),
        )

    client = ValorantClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ValorantQueryError, match=r"HTTP 502"):
            await client.query("ExamplePlayer#12345")
    finally:
        await client.close()
