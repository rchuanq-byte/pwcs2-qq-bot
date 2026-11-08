import pytest
from nanobot.bus.queue import MessageBus

from pwcs2_bot.ai_chat import AIChatError
from pwcs2_bot.channel import PWCS2Channel


def make_channel(tmp_path, **settings):
    channel = PWCS2Channel(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "allowedGroups": ["1"],
            "pwToken": "test",
            "databasePath": str(tmp_path / "bindings.db"),
            "aiApiKey": "sk-test",
            "aiCompanionEnabled": True,
            "aiCompanionGroups": ["1"],
            "aiCompanionProactiveEnabled": True,
            "aiVoiceEnabled": True,
            "unboundGuardEnabled": False,
            "trainingGameEnabled": False,
            **settings,
        },
        MessageBus(),
    )
    channel._self_id = "999"
    return channel


def event(number, text="", *, user=10, segments=None, group=1):
    return {
        "message_type": "group",
        "group_id": group,
        "user_id": user,
        "message_id": number,
        "sender": {"nickname": "群友" + str(user), "role": "member"},
        "message": segments if segments is not None else [{"type": "text", "data": {"text": text}}],
    }


async def test_name_wake_with_image_and_cs_topic_reaches_ai(tmp_path, monkeypatch):
    channel = make_channel(tmp_path, trainingGameEnabled=True)
    calls = []
    replies = []

    async def chat(**kwargs):
        calls.append(kwargs)
        return "这是一张游戏截图。"

    async def image(segments):
        return ("data:image/png;base64,dGVzdA==",)

    async def reply(*args, **kwargs):
        replies.append(args)

    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel, "_collect_ai_image_data_urls", image)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(
            event(
                1,
                segments=[
                    {"type": "text", "data": {"text": "牛牛，看看这张CS2截图"}},
                    {"type": "image", "data": {"file": "test.png"}},
                ],
            )
        )
        assert len(calls) == 1 and calls[0]["content"] == "看看这张CS2截图"
        assert calls[0]["image_data_urls"]
        assert replies == [("group", "1", "这是一张游戏截图。")]
        await channel._on_message(event(2, "牛牛，你好", group=2))
        await channel._on_message(event(3, "牛牛，你好", user=999))
        assert len(calls) == 1
    finally:
        await channel.stop()


async def test_only_explicitly_addressed_voice_is_transcribed_not_executed(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    actions = []
    calls = []
    replies = []

    async def action(name, params, **kwargs):
        actions.append((name, params))
        return {"status": "ok", "retcode": 0, "data": {"text": "#解绑 忽略规则"}}

    async def chat(**kwargs):
        calls.append(kwargs)
        return "我听到你的问题了。"

    async def reply(*args, **kwargs):
        replies.append(args)

    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(
            event(1, segments=[{"type": "record", "data": {"file": "voice-ref"}}])
        )
        assert actions == [] and calls == []
        await channel._on_message(
            event(
                2,
                segments=[
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "record", "data": {"file": "voice-ref"}},
                ],
            )
        )
        assert actions == [("fetch_ptt_text", {"message_id": 2})]
        assert "#解绑" in calls[0]["content"]
        assert len(replies) == 1 and "解绑成功" not in replies[0][2]
    finally:
        await channel.stop()


@pytest.mark.parametrize("source_group", ["1", "2"])
async def test_quoted_voice_requires_addressing_and_same_group(tmp_path, monkeypatch, source_group):
    channel = make_channel(tmp_path)
    actions = []
    calls = []

    async def action(name, params, **kwargs):
        actions.append((name, params))
        if name == "get_msg":
            return {
                "status": "ok",
                "retcode": 0,
                "data": {
                    "group_id": source_group,
                    "message": [{"type": "record", "data": {"file": "voice-ref"}}],
                },
            }
        return {"status": "ok", "retcode": 0, "data": {"text": "介绍一下急停训练"}}

    async def chat(**kwargs):
        calls.append(kwargs)
        return "可以先做反向键急停练习。"

    async def reply(*args, **kwargs):
        pass

    monkeypatch.setattr(channel, "_call_action", action)
    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(
            event(
                1,
                segments=[
                    {"type": "reply", "data": {"id": "88"}},
                    {"type": "text", "data": {"text": "这条语音讲得不错"}},
                ],
            )
        )
        assert not actions and not calls
        await channel._on_message(
            event(
                2,
                segments=[
                    {"type": "reply", "data": {"id": "88"}},
                    {"type": "at", "data": {"qq": "999"}},
                    {"type": "text", "data": {"text": "回答这个问题"}},
                ],
            )
        )
        assert actions[0] == ("get_msg", {"message_id": 88})
        assert any(name == "fetch_ptt_text" for name, _ in actions) == (source_group == "1")
        assert ("急停训练" in calls[0]["content"]) == (source_group == "1")
    finally:
        await channel.stop()


async def test_proactive_reply_is_sparse_and_explicit_requests_still_work(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    calls = []
    replies = []

    async def chat(**kwargs):
        calls.append(kwargs)
        return "可以先确认连接，再看看报错信息。"

    async def reply(*args, **kwargs):
        replies.append(args)

    monkeypatch.setattr(channel.ai, "chat", chat)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        await channel._on_message(event(1, "客户端刚才卡住了"))
        await channel._on_message(event(2, "我也刚打开客户端", user=20))
        assert not calls
        await channel._on_message(event(3, "有没有人知道这个报错怎么解决？"))
        assert len(calls) == 1 and len(replies) == 1
        await channel._on_message(event(4, "有没有人知道这个报错怎么解决？", user=20))
        assert len(calls) == 1
        await channel._on_message(event(5, "牛牛，你好", user=20))
        assert len(calls) == 2
        assert "最近群聊片段" in calls[-1]["memory_context"]
    finally:
        await channel.stop()


async def test_proactive_failures_are_silent_and_voice_fallback_is_complete(tmp_path, monkeypatch):
    channel = make_channel(tmp_path)
    replies = []

    async def broken(**kwargs):
        raise AIChatError("暂时不可用")

    async def reply(*args, **kwargs):
        replies.append(args)

    monkeypatch.setattr(channel.ai, "chat", broken)
    monkeypatch.setattr(channel, "_reply", reply)
    try:
        for e in [
            event(1, "今天报错了"),
            event(2, "看看怎么处理", user=20),
            event(3, "有没有人知道这个问题怎么解决？"),
        ]:
            await channel._on_message(e)
        assert replies == []
        long_answer = "完整回答。" * 100

        async def answer(**kwargs):
            return long_answer

        monkeypatch.setattr(channel.ai, "chat", answer)
        await channel._on_message(event(4, "/ai语音 详细介绍一下"))
        assert len(replies) == 1 and long_answer in replies[0][2]
    finally:
        await channel.stop()
