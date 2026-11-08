from types import SimpleNamespace

import pytest
from test_astrbot_auto_reply import Event, handler, plugin_method, run_gate
from test_astrbot_transport import make_app

from pwcs2_bot.astrbot_policy import AstrBotOutboundPolicy, OutboundBlocked, classify
from pwcs2_bot.astrbot_runtime import defer_framework_auto_reply
from pwcs2_bot.chat_profiles import (
    ResponsiveChatSelector,
    activate_group_personas,
    conversation_groups,
    group_persona_prompt,
    persona_intervals,
    responsive_groups,
)
from pwcs2_bot.group_companion import CompanionDecision


def test_group_personas_enable_only_the_two_requested_chat_rooms():
    previous = {
        "allowedGroups": ["100000021"],
        "aiChatOnlyGroups": ["100000024"],
        "aiMentionOnlyGroups": ["100000024", "100000023"],
        "aiCompanionResponsiveGroups": ["100000021", "100000024"],
        "aiCompanionReplyTarget": 0.85,
        "aiCompanionGroups": ["100000021"],
        "aiMaxConcurrentRequests": 20,
    }
    settings = dict(previous)
    profiles = {"100000022": "sharp_banter", "100000023": "naruto_mobile"}
    activate_group_personas(settings, profiles)
    assert settings == {
        **previous,
        "aiChatOnlyGroups": ["100000024", "100000022", "100000023"],
        "aiMentionOnlyGroups": ["100000024"],
        "aiCompanionResponsiveGroups": ["100000021", "100000024", "100000022", "100000023"],
        "aiGroupPersonas": profiles,
    }
    snapshot = dict(settings)
    activate_group_personas(settings, profiles)
    assert settings == snapshot
    with pytest.raises(ValueError, match="group IDs"):
        activate_group_personas(settings, {"not-a-qq-group": "sharp_banter"})
    assert settings == snapshot


def test_group_persona_and_automatic_pacing_are_scoped_by_group_id():
    config = SimpleNamespace(
        allowed_groups=["100000021"],
        ai_chat_only_groups=["100000024", "100000022", "100000023"],
        ai_mention_only_groups=["100000024"],
        ai_companion_responsive_groups=["100000021", "100000024", "100000022", "100000023"],
        ai_group_personas={
            "100000022": "sharp_banter",
            "100000023": "naruto_mobile",
            "100000000": "sharp_banter",
        },
    )
    assert "犀利" in group_persona_prompt(config, 100000022)
    assert "火影忍者手游" not in group_persona_prompt(config, 100000022)
    assert "火影忍者手游" in group_persona_prompt(config, 100000023)
    assert "犀利" not in group_persona_prompt(config, 100000023)
    for gid in ("100000021", "100000024", "100000000", ""):
        assert not group_persona_prompt(config, gid)
    now = 0.0
    selector = ResponsiveChatSelector(
        responsive_groups(config), clock=lambda: now, min_intervals=persona_intervals(config)
    )
    assert "100000024" not in selector.groups and "100000000" not in selector.groups
    for gid in ("100000021", "100000022", "100000023"):
        assert selector.select(gid, "自然聊天第一条")
    now = 1.0
    assert selector.select("100000021", "原群第二条不受影响")
    assert not selector.select("100000022", "不连续接话")
    assert not selector.select("100000023", "不连续接话")
    now = 15.0
    assert selector.select("100000023", "决斗场怎么打")
    assert not selector.select("100000022", "互怼群仍有冷却")
    now = 25.0
    assert selector.select("100000022", "冷却已结束")


def test_target_is_85_percent_of_eligible_messages_not_random_luck():
    policy = ResponsiveChatSelector(["1", "2"], target=0.85)
    assert sum(policy.select("1", f"今天聊天话题{i}") for i in range(20)) == 17
    assert sum(policy.select("1", f"新的聊天话题{i}") for i in range(20)) == 17
    assert policy.select("2", "新群的第一个话题")
    assert policy.health()["1"] == {"eligible": 40, "selected": 34, "sent": 0}
    policy.sent("1")
    assert policy.health()["1"]["sent"] == 1


def test_ineligible_inputs_do_not_inflate_denominator_or_leak_secrets():
    policy = ResponsiveChatSelector(["1"])
    for text in (
        "",
        "嗯",
        "😂😂",
        "#战绩",
        "豆包：你好",
        "sk-abcdefghijklmnop",
        "https://example.com",
    ):
        assert not policy.select("1", text)
    assert not policy.select("1", "你好朋友", mentions=[123], self_id=999)
    assert not policy.select("9", "未授权群的话题")
    assert policy.health()["1"]["eligible"] == 0
    assert policy.select("1", "这是一次正常聊天")
    assert not policy.select("1", "这是一次正常聊天")
    assert policy.health()["1"]["eligible"] == 1


async def test_chat_group_is_not_a_business_group_and_cannot_run_commands(tmp_path):
    app = make_app(tmp_path, aiChatOnlyGroups=["2"], aiCompanionEnabled=True)
    raw = {
        "post_type": "message",
        "message_type": "group",
        "group_id": 2,
        "user_id": 10,
        "message_id": 100,
        "message": "你好朋友",
    }
    try:
        assert conversation_groups(app.config) == {"1", "2"}
        assert app.config.allowed_groups == ["1"]
        assert app.companion.allowed("2")
        assert classify(app, raw).allowed
        assert not classify(app, raw).explicit
        assert classify(app, {**raw, "message": "#ai 你好"}).explicit
        for command in ("#战绩", "/数据", "#签到", "#绑定 76561197960266101", "#帮助"):
            assert not classify(app, {**raw, "message": command}).allowed
            await app._on_message({**raw, "message": command})
        assert not app.bot.calls
        assert not classify(app, {**raw, "post_type": "notice"}).allowed
        assert not classify(app, {**raw, "group_id": 3}).allowed
    finally:
        await app.stop()


async def test_chat_only_native_boundary_blocks_background_files_forward_and_moderation(tmp_path):
    app = make_app(tmp_path, aiChatOnlyGroups=["2"])
    try:
        policy = AstrBotOutboundPolicy(app, lambda: None)
        event = Event(niuniu_ai=True, niuniu_chat_only=True, niuniu_auto_reserved=True)
        event.get_group_id = lambda: "2"
        policy.validate("send_group_msg", {"group_id": 2}, event)
        policy.validate("send_msg", {"message_type": "group", "group_id": 2}, event)
        for action in (
            "send_group_msg",
            "send_group_forward_msg",
            "upload_group_file",
            "set_group_ban",
            "set_group_card",
        ):
            with pytest.raises(OutboundBlocked):
                policy.validate(action, {"group_id": 2})
        for action in (
            "send_group_forward_msg",
            "upload_group_file",
            "set_group_ban",
            "set_group_kick",
        ):
            with pytest.raises(OutboundBlocked):
                policy.validate(action, {"group_id": 2}, event)
        event.get_group_id = lambda: "1"
        with pytest.raises(OutboundBlocked):
            policy.validate("send_group_msg", {"group_id": 2}, event)
        assert not app.bot.calls
    finally:
        await app.stop()


def test_managed_profile_omits_builtin_sampler_without_losing_collectors():
    first, last = handler("niuniu", "route"), handler("niuniu", "responsive_chat")
    builtin = handler("astrbot.builtin_stars.astrbot.main", "on_message")
    memory = handler("dsh-memory", "collect")
    handlers = [first, builtin, memory, last]
    event = Event(activated_handlers=handlers)
    defer_framework_auto_reply(event, omit=True)
    assert handlers == [first, memory, last]


async def test_responsive_gate_remains_automatic_and_cannot_bypass_quiet():
    event = Event(niuniu_ai=True, niuniu_responsive_turn=True)
    _, _, reservations = await run_gate(event)
    assert reservations and not event.get_extra("niuniu_explicit")
    quiet = Event(niuniu_ai=True, niuniu_responsive_turn=True)
    await run_gate(quiet, reserve=False, expect_observed=False)
    assert quiet.stopped


async def test_synthetic_merged_chat_retains_its_group_boundary(tmp_path):
    from pwcs2_bot.chat_profiles import chat_only, conversation_groups
    from pwcs2_bot.dafeiyu_persona import is_synthetic

    app = make_app(tmp_path, aiChatOnlyGroups=["2"])
    try:
        event = Event(niuniu_merged=True)
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
            responsive=ResponsiveChatSelector(["1", "2"]),
            _bind=lambda bot: None,
        )
        await route(main, event)
        assert not event.stopped
        assert event.get_extra("niuniu_ai") and event.get_extra("niuniu_chat_only")
        assert event.get_extra("niuniu_explicit")
    finally:
        await app.stop()


async def test_chat_only_business_tools_are_denied_even_when_invoked_directly(tmp_path):
    import json

    from pwcs2_bot.chat_profiles import chat_only

    app = make_app(tmp_path, aiChatOnlyGroups=["2"])
    try:
        event = Event()
        event.get_group_id = lambda: "2"
        query = plugin_method("_query", chat_only=chat_only, json=json)
        result = json.loads(
            await query(
                SimpleNamespace(engine=app), event, "query_group_statistics", {"kind": "coins"}
            )
        )
        assert result["ok"] is False
        assert not app.bot.calls
    finally:
        await app.stop()


async def test_responsive_late_answer_survives_new_messages_but_still_filters_duplicates(tmp_path):
    app = make_app(tmp_path, aiCompanionEnabled=True, aiCompanionGroups=["1"])
    try:
        app.companion.observe("1", "10", "test", "原来的话题")
        decision = CompanionDecision("responsive", "", 1)
        app.companion.observe("1", "11", "other", "群友接着说话")
        assert app.companion.prepare_answer("1", "这是针对原消息的一条正常回答", decision)
        assert not app.companion.prepare_answer("1", "这是针对原消息的一条正常回答", decision)
    finally:
        await app.stop()


@pytest.mark.parametrize(
    ("persona", "expected"),
    [(None, None), ("sharp_banter", "轻度嘲讽"), ("naruto_mobile", "火影忍者手游")],
)
async def test_chat_only_context_removes_business_tools_and_never_reads_business_memory(
    tmp_path, persona, expected
):
    from pwcs2_bot.chat_profiles import chat_only
    from pwcs2_bot.dafeiyu_persona import is_synthetic
    from pwcs2_bot.upstream_extensions import allowed_tools

    app = make_app(
        tmp_path, aiChatOnlyGroups=["2"], aiGroupPersonas={"2": persona} if persona else {}
    )
    try:

        def forbidden(*args, **kwargs):
            raise AssertionError("Chat group accessed business memory or tools")

        app.ai_group_data.for_request = forbidden
        app.group_ai_memory.build_ai_context = forbidden
        app.member_memory.build_ai_context = forbidden
        prepare = plugin_method(
            "_prepare_context",
            chat_only=chat_only,
            TextPart=SimpleNamespace,
            DEFAULT_CHAT_SYSTEM_PROMPT="business",
            PERMANENT_GROUP_MEMORIES={},
            DYNAMIC_MEMORY_GUARD="guard",
            reference_context=forbidden,
            ALLOWED_AI_TOOLS={"query_group_statistics"},
            is_synthetic=is_synthetic,
            allowed_tools=allowed_tools,
        )
        event = Event(niuniu_link_read=bool(persona))
        event.get_group_id = lambda: "2"
        event.get_sender_id = lambda: "10"
        event.message_str = "你好朋友"
        event.message_obj = SimpleNamespace(raw_message={"message": "你好朋友"})
        req = SimpleNamespace(
            extra_user_content_parts=[
                SimpleNamespace(text="<self_capabilities>business</self_capabilities>"),
                SimpleNamespace(text="<scene>犯困，白天再说</scene>"),
            ],
            func_tool=SimpleNamespace(tools=[SimpleNamespace(name="query_group_statistics")]),
        )
        main = SimpleNamespace(engine=app, ai_contexts=0, image_requests=0, extensions=False)
        await prepare(main, event, req)
        assert not req.func_tool.tools and event.get_extra("niuniu_toolbox") is None
        assert "这个群仅启用自然聊天" in req.system_prompt
        if expected:
            assert expected in req.system_prompt
            assert "不猜具体内容" in req.system_prompt
            assert not any("<scene>" in part.text for part in req.extra_user_content_parts)
        else:
            assert "当前群的发言偏好" not in req.system_prompt
            assert any("<scene>" in part.text for part in req.extra_user_content_parts)
        assert "business" not in "".join(part.text for part in req.extra_user_content_parts)
        assert not app.bot.calls
    finally:
        await app.stop()
