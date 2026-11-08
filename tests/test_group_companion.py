import json

from pwcs2_bot.group_companion import GroupCompanion


def companion(clock, **kwargs):
    return GroupCompanion(
        enabled=True, groups=["1"], proactive=True, clock=lambda: clock[0], **kwargs
    )


def observe_topic(c):
    c.observe("1", "10", "甲", "今天客户端好像卡住了")
    c.observe("1", "20", "乙", "先看看连接是不是正常")
    c.observe("1", "10", "甲", "有没有人知道这个报错怎么解决？")


def test_default_disabled_and_groups_are_explicit():
    assert GroupCompanion().decide("1", "牛牛你好") is None
    c = GroupCompanion(enabled=True)
    assert c.decide("1", "牛牛，你好") is None
    c = companion([1000])
    assert c.decide("2", "牛牛，你好") is None


def test_name_wake_is_not_a_mid_sentence_or_shop_keyword():
    c = companion([1000])
    assert c.decide("1", "牛牛精灵，帮我看图").prompt == "帮我看图"
    assert c.decide("1", "牛牛，你好").mode == "wake"
    assert c.decide("1", "牛牛").prompt == "在吗？"
    assert c.decide("1", "我觉得牛牛挺好") is None
    assert c.decide("1", "牛牛商店又上新了") is None


def test_proactive_requires_group_conversation_and_is_rate_limited():
    clock = [1000]
    c = companion(clock, min_interval=300, hourly_limit=2)
    assert c.decide("1", "有没有人知道这个报错怎么解决？") is None
    observe_topic(c)
    d = c.decide("1", "有没有人知道这个报错怎么解决？")
    assert d.mode == "proactive"
    clock[0] += 300
    assert c.decide("1", "有没有人知道这个报错怎么解决？") is None  # pending
    c.release("1")
    assert c.decide("1", "有没有人知道这个报错怎么解决？") is not None
    c.release("1")
    clock[0] += 300
    observe_topic(c)
    assert c.decide("1", "有没有人知道这个报错怎么解决？") is None  # hourly cap


def test_proactive_does_not_cut_into_replies_mentions_or_media():
    c = companion([1000])
    observe_topic(c)
    assert c.decide("1", "有没有人知道这个报错怎么解决？", eligible=False) is None
    assert c.decide("1", "牛牛，你好", eligible=False).mode == "wake"


def test_changed_topic_and_duplicate_answers_are_silent():
    c = companion([1000])
    observe_topic(c)
    d = c.decide("1", "有没有人知道这个报错怎么解决？")
    assert c.prepare_answer("1", "[不接话]", d) == ""
    assert c.prepare_answer("1", "已经检查过连接，先重新打开客户端看看。")
    assert c.prepare_answer("1", "已经检查过连接，先重新打开客户端看看。", d) == ""
    c.observe("1", "20", "乙", "好了别管这个了，今晚开黑")
    assert c.prepare_answer("1", "可以看看网络连接。", d) == ""


def test_context_is_bounded_group_scoped_untrusted_and_not_persistent():
    clock = [1000]
    c = companion(clock)
    for i in range(40):
        c.observe("1", str(i), "不可信昵称", "普通聊天" + str(i))
    c.observe("1", "100", "访客", "api_key: sk-abcdefghijklmnop")
    text = c.context("1", max_chars=600)
    assert len(text) <= 600 and "不可信" in text
    data = json.loads(text.split("\n", 1)[1])
    assert len(data["recent_chat"]) <= 8
    assert "sk-" not in text
    assert c.context("2") == ""
    clock[0] += 601
    assert c.context("1") == ""
    assert companion(clock).context("1") == ""


def test_explicit_long_answers_are_never_truncated_or_deduplicated():
    c = companion([1000])
    text = "详细的回答。" * 1000
    assert c.prepare_answer("1", text) == text
    assert c.prepare_answer("1", text) == text
    assert c.prepare_answer("1", "[[draft]]内部草稿[[/draft]]正式回答") == "正式回答"


def test_daily_limit_is_independent_of_hourly_limit():
    clock = [1000]
    c = companion(clock, min_interval=300, daily_limit=2)
    for _ in range(2):
        observe_topic(c)
        assert c.decide("1", "有没有人知道这个报错怎么解决？") is not None
        c.release("1")
        clock[0] += 3601
    observe_topic(c)
    assert c.decide("1", "有没有人知道这个报错怎么解决？") is None
    assert c.decide("1", "牛牛，你好").mode == "wake"


def test_rate_metadata_survives_restart_without_chat_content(tmp_path):
    clock = [1000]
    path = tmp_path / "limits.json"
    c = companion(clock, state_path=path, hourly_limit=1, min_interval=300)
    observe_topic(c)
    assert c.decide("1", "有没有人知道这个报错怎么解决？")
    assert "客户端" not in path.read_text() and "甲" not in path.read_text()
    clock[0] += 301
    restarted = companion(clock, state_path=path, hourly_limit=1, min_interval=300)
    observe_topic(restarted)
    assert restarted.decide("1", "有没有人知道这个报错怎么解决？") is None
    path.write_text("broken")
    guarded = companion(clock, state_path=path)
    observe_topic(guarded)
    assert guarded.decide("1", "有没有人知道这个报错怎么解决？") is None
    assert guarded.decide("1", "牛牛，你好").mode == "wake"


def test_upstream_managed_turns_do_not_consume_or_obey_extra_local_rate_budget():
    clock = [1000]
    c = companion(clock, hourly_limit=1, daily_limit=1, min_interval=1800)
    assert c.reserve_automatic_reply("1")
    c.release("1")
    assert not c.reserve_automatic_reply("1")
    previous = list(c._state("1").automatic)
    for _ in range(3):
        assert c.reserve_automatic_reply("1", upstream_managed=True)
        assert not c.reserve_automatic_reply("1", upstream_managed=True)  # pending
        c.release("1")
    assert list(c._state("1").automatic) == previous
    assert not c.reserve_automatic_reply("1")  # Legacy mode is still capped.


def test_upstream_managed_turns_preserve_proactive_and_group_eligibility():
    c = companion([1000])
    assert not c.reserve_automatic_reply("2", upstream_managed=True)
    c.proactive = False
    assert not c.reserve_automatic_reply("1", upstream_managed=True)
    c.proactive = True
    c.enabled = False
    assert not c.reserve_automatic_reply("1", upstream_managed=True)
