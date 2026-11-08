import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pwcs2_bot import bilibili_public as bili

ID = "BV1TstAbCd12"
SHORT = "https://b23.tv/Synthetic1"
PAGES = "/x/player/pagelist"
VIEW = "/x/web-interface/wbi/view"
DESC = "/x/web-interface/archive/desc"
PLAYER = "/x/player/v2"


class Reply:
    def __init__(self, status=200, data=None, location=""):
        self.status = status
        self.headers = {"Location": location}
        self.content = self
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def iter_chunked(self, size):
        raw = json.dumps(self.data).encode()
        for i in range(0, len(raw), size):
            yield raw[i : i + size]


def mock_site(monkeypatch, routes):
    calls = []

    class Session:
        def __init__(self, *, connector, trust_env, **_kwargs):
            assert trust_env is False
            self.connector = connector

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            await self.connector.close()

        def get(self, url, *, params=None, allow_redirects):
            assert not allow_redirects
            calls.append((url, params))
            return routes[url.removeprefix(bili.API)]

    monkeypatch.setattr(bili.aiohttp, "ClientSession", Session)
    return calls


def api(data):
    return Reply(data={"code": 0, "data": data})


async def test_412_falls_back_to_verified_metadata_without_touching_gated_captions(monkeypatch):
    calls = mock_site(
        monkeypatch,
        {
            SHORT: Reply(302, location=f"https://www.bilibili.com/video/{ID}/?p=2&tracking=x"),
            VIEW: Reply(412),
            PAGES: api([{"part": "第一段", "cid": 101}, {"part": "第二段", "cid": 102}]),
            DESC: api("公开视频简介"),
            PLAYER: api(
                {
                    "cid": 102,
                    "need_login_subtitle": True,
                    "subtitle": {"subtitles": [{"subtitle_url": "https://a.hdslb.com/private"}]},
                }
            ),
        },
    )
    result = await bili.read_video(SHORT + "?tracking=discard")
    assert result.ref == bili.VideoRef(ID, 2)
    assert result.part == "第二段" and result.description == "公开视频简介"
    assert not result.caption
    assert "字幕接口需要登录" in result.unavailable_reply()
    assert "公开视频简介" not in result.unavailable_reply()
    assert "不是视频里的发言" in result.render()
    assert calls.count((bili.API + VIEW, {"bvid": ID})) == 1
    assert (bili.API + PLAYER, {"bvid": ID, "cid": 102}) in calls
    assert len(calls) == 5


async def test_reads_public_caption_and_keeps_metadata_separate(monkeypatch):
    url = "https://aisubtitle.hdslb.com/public-caption.json"
    mock_site(
        monkeypatch,
        {
            VIEW: api(
                {
                    "title": "示例视频",
                    "desc": "简介不是原话",
                    "owner": {"name": "示例UP主"},
                    "pages": [{"part": "主视频", "cid": 101, "duration": 36}],
                    "stat": {"view": 120, "like": 12},
                }
            ),
            PLAYER: api(
                {
                    "cid": 101,
                    "subtitle": {
                        "subtitles": [
                            {"lan": "en", "subtitle_url": "https://other.example/subtitle.json"},
                            {"lan": "zh-CN", "subtitle_url": url.removeprefix("https:")},
                        ]
                    },
                }
            ),
            url: Reply(data={"body": [{"content": "字幕中明确说了第一句话。"}]}),
        },
    )
    result = await bili.read_video(ID)
    assert result.caption == "字幕中明确说了第一句话。"
    assert "公开视频字幕摘录" in result.render()
    assert "时长：36秒" in result.render() and "点赞 12" in result.render()


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "https://www.bilibili.com.evil.example/video/BV1TstAbCd12/",
        "https://www.bilibili.com@evil.example/video/BV1TstAbCd12/",
        "https://b23.tv:9999/abcdef",
    ],
)
async def test_short_redirect_must_stay_on_supported_hosts(monkeypatch, target):
    calls = mock_site(monkeypatch, {SHORT: Reply(302, location=target)})
    assert not (await bili.read_video(SHORT)).ref
    assert calls == [(SHORT, None)]


@pytest.mark.parametrize(
    "url",
    [
        "http://aisubtitle.hdslb.com/a",
        "https://aisubtitle.hdslb.com.evil.example/a",
        "https://user@aisubtitle.hdslb.com/a",
        "https://169.254.169.254/a",
    ],
)
def test_untrusted_caption_destinations_are_rejected(url):
    assert not bili._caption_url(url)


def test_content_request_does_not_discard_user_supplied_transcript():
    assert bili.wants_video_words("帮我总结一下这段视频")
    assert not bili.wants_video_words("帮我总结一下，文字稿：今天讨论解说")
    assert not bili.wants_video_words("总结。语音转写（可能有识别误差）：这是我的说法")
    assert not bili.is_video_reference("https://www.bilibili.com/read/cv123")
    assert bili.is_video_reference("av123")


async def test_automatic_link_hook_answers_without_model_when_summary_has_no_captions(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "vendor/dafeiyu/dsh-web/main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = copy.deepcopy(
        next(node for node in ast.walk(tree) if getattr(node, "name", "") == "_attach")
    )
    method.decorator_list = []
    code = compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec")
    namespace = {"_is_bili": bili.is_video_reference}
    exec(code, namespace)
    evidence = bili.VideoEvidence(
        ref=bili.VideoRef(ID),
        part="不能当作原话的标题",
        limitation="这条视频的字幕接口需要登录，机器人没有读取权限",
    )

    async def read(_):
        return evidence

    monkeypatch.setattr(bili, "read_video", read)
    sent = []
    stopped = []

    async def send(message):
        sent.append(message)

    event = SimpleNamespace(
        message_str=f"总结这条视频 {SHORT}",
        get_extra=lambda _key: None,
        send=send,
        plain_result=lambda text: text,
        stop_event=lambda: stopped.append(True),
    )
    req = SimpleNamespace(extra_user_content_parts=[])
    await namespace["_attach"](SimpleNamespace(), req, [SHORT], "", event=event)
    assert sent == [evidence.unavailable_reply()] and stopped == [True]
    assert not req.extra_user_content_parts
    assert "白天" not in sent[0] and evidence.part not in sent[0]
