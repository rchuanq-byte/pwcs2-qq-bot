"""Small-group native-mention policy, independent of the original group's AI."""

from types import SimpleNamespace

import pytest
from test_astrbot_auto_reply import Event, handler, plugin_method
from test_astrbot_transport import make_app

from pwcs2_bot.astrbot_policy import (
    AstrBotOutboundPolicy,
    OutboundBlocked,
    classify,
    reserve_automatic_turn,
)
from pwcs2_bot.astrbot_runtime import defer_framework_auto_reply
from pwcs2_bot.chat_profiles import (
    ResponsiveChatSelector,
    automatic_groups,
    chat_only,
    conversation_groups,
    responsive_groups,
)
from pwcs2_bot.dafeiyu_persona import is_synthetic


def raw_message(message, gid=2):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": gid,
        "user_id": 10,
        "message_id": 100,
        "message": message,
    }


def test_bootstrap_disables_only_the_small_group_sampler():
    settings = {
        "allowedGroups": ["1"],
        "aiChatOnlyGroups": ["2"],
        "aiMentionOnlyGroups": ["2"],
    }
    assert automatic_groups(settings) == ["1"]
    assert settings["aiChatOnlyGroups"] == ["2"]
    assert automatic_groups({**settings, "aiMentionOnlyGroups": []}) == ["1", "2"]


@pytest.mark.parametrize(
    "message",
    [
        "ordinary chat",
        "牛牛精灵，出来聊聊",
        "#ai hello",
        "/ai hello",
        "/搜索 example",
        "@牛牛精灵 hello",
        [{"type": "at", "data": {"qq": "all"}}],
        [{"type": "at", "data": {"qq": "12345"}}],
        [{"type": "reply", "data": {"id": "77"}}],
        [{"type": "image", "data": {"url": "https://example.com/pic.png"}}],
        [{"type": "record", "data": {"file": "audio.amr"}}],
    ],
)
async def test_no_model_call_without_a_real_mention_even_for_quotes_and_commands(tmp_path, message):
    app = make_app(tmp_path, aiChatOnlyGroups=["2"], aiMentionOnlyGroups=["2"])
    try:
        app._bot_outbound_ids.append(77)
        assert not classify(app, raw_message(message), quoted_bot=True).allowed
        event = Event(
            activated_handlers=[handler("astrbot.builtin_stars.astrbot.main", "on_message")]
        )
        event.get_group_id = lambda: "2"
        event.get_platform_name = lambda: "aiocqhttp"
        event.message_obj = SimpleNamespace(raw_message=raw_message(message))
        route = plugin_method(
            "route",
            classify=classify,
            chat_only=chat_only,
            conversation_groups=conversation_groups,
            is_synthetic=is_synthetic,
            defer_framework_auto_reply=defer_framework_auto_reply,
        )
        main = SimpleNamespace(
            started=True,
            engine=app,
            responsive=ResponsiveChatSelector(["1"]),
            _bind=lambda bot: None,
        )
        await route(main, event)
        assert event.stopped and not event.get_extra("niuniu_ai")
        assert event.get_extra("activated_handlers") == []
        assert not app.bot.calls
    finally:
        await app.stop()


async def test_actual_mention_is_explicit_and_original_group_keeps_automatic_replies(tmp_path):
    app = make_app(
        tmp_path,
        aiChatOnlyGroups=["2"],
        aiMentionOnlyGroups=["2"],
        aiCompanionResponsiveGroups=["1", "2"],
        aiCompanionEnabled=True,
        aiCompanionGroups=["1"],
        aiCompanionProactiveEnabled=True,
    )
    try:
        at = {"type": "at", "data": {"qq": str(app._self_id)}}
        text = {"type": "text", "data": {"text": "hello"}}
        result = classify(app, raw_message([at, text]))
        assert result.allowed and result.explicit and result.command is None
        assert classify(app, raw_message("ordinary chat", gid=1)).allowed
        assert classify(app, raw_message("#ai hello", gid=1)).explicit
        assert responsive_groups(app.config) == ["1"]
        assert not reserve_automatic_turn(app, "2")
        assert reserve_automatic_turn(app, "1")
    finally:
        await app.stop()


@pytest.mark.parametrize("flag", ["niuniu_merged", "dsh_proactive", "dsh_initiate"])
async def test_synthetic_and_queued_turns_cannot_bypass_native_mention(tmp_path, flag):
    app = make_app(tmp_path, aiChatOnlyGroups=["2"], aiMentionOnlyGroups=["2"])
    try:
        event = Event(**{flag: True})
        event.get_group_id = lambda: "2"
        event.get_platform_name = lambda: "aiocqhttp"
        event.message_obj = SimpleNamespace(raw_message={})
        route = plugin_method(
            "route",
            chat_only=chat_only,
            conversation_groups=conversation_groups,
            is_synthetic=is_synthetic,
            defer_framework_auto_reply=defer_framework_auto_reply,
        )
        main = SimpleNamespace(
            started=True,
            engine=app,
            responsive=ResponsiveChatSelector(["1"]),
            _bind=lambda bot: None,
        )
        await route(main, event)
        assert event.stopped and not event.get_extra("niuniu_ai")
        submit = plugin_method("submit_merged_turn", conversation_groups=conversation_groups)
        # Rejection must precede imports of the real AstrBot runtime.
        assert not await submit(main, "2", "queued prompt", [(10, "hi")], "unused")
    finally:
        await app.stop()


async def test_model_and_native_send_boundaries_require_the_verified_mention(tmp_path):
    app = make_app(tmp_path, aiChatOnlyGroups=["2"], aiMentionOnlyGroups=["2"])
    try:
        observed = []

        async def observe(event, req):
            observed.append(True)

        main = SimpleNamespace(engine=app, _observe_images=observe)
        gate = plugin_method("gate_request")
        policy = AstrBotOutboundPolicy(app, lambda: None)
        for verified in (False, True):
            event = Event(
                niuniu_ai=True,
                niuniu_explicit=True,
                niuniu_chat_only=True,
                niuniu_mentioned=verified,
            )
            event.get_group_id = lambda: "2"
            await gate(main, event, SimpleNamespace())
            assert event.stopped is not verified
            if verified:
                policy.validate("send_group_msg", {"group_id": 2}, event)
            else:
                with pytest.raises(OutboundBlocked, match="real @bot"):
                    policy.validate("send_group_msg", {"group_id": 2}, event)
        assert observed == [True]
        with pytest.raises(OutboundBlocked, match="real @bot"):
            policy.validate("send_group_msg", {"group_id": 2})
    finally:
        await app.stop()
