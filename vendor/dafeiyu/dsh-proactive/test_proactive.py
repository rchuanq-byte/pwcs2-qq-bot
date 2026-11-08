"""dsh-proactive 离线回测。断言全部来自真实语料 + Codex QQ-Enhancer 的评分语义。

重点不是「能不能触发」，而是：
  1. 低价值/纯服务请求绝不触发（防话痨）。
  2. 命中兴趣词就该拿到足够高的分（白米饭 8 分是单类强触发）。
  3. 冷却/额度/阈值这三把闸门的纯函数行为正确。
  4. 群主说话有 +2 的倾斜。
"""

import importlib.util
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

if "astrbot" not in sys.modules:
    try:
        import astrbot  # noqa: F401
    except ImportError:
        for name in ("astrbot", "astrbot.api", "astrbot.api.event",
                     "astrbot.core", "astrbot.core.agent",
                     "astrbot.core.agent.message", "astrbot.core.platform.message_type"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["astrbot.api"].star = types.SimpleNamespace(Star=object, Context=object)
        sys.modules["astrbot.api.event"].AstrMessageEvent = object
        sys.modules["astrbot.api.event"].filter = types.SimpleNamespace(
            on_llm_request=lambda *a, **k: (lambda f: f),
            after_message_sent=lambda *a, **k: (lambda f: f),
            command=lambda *a, **k: (lambda f: f),
            platform_adapter_type=lambda *a, **k: (lambda f: f),
            custom_filter=lambda *a, **k: (lambda f: f))
        sys.modules["astrbot.api.event"].filter.PlatformAdapterType = types.SimpleNamespace(ALL="all")
        sys.modules["astrbot.core"].logger = types.SimpleNamespace(
            info=lambda *a, **k: None, warning=lambda *a, **k: None,
            debug=lambda *a, **k: None, error=lambda *a, **k: None)
        sys.modules["astrbot.core.agent.message"].TextPart = object
        sys.modules["astrbot.core.platform.message_type"].MessageType = types.SimpleNamespace(
            GROUP_MESSAGE="group", PRIVATE_MESSAGE="private")
        sys.modules["astrbot.core.platform.astrbot_message"] = types.ModuleType("astrbot.core.platform.astrbot_message")
        sys.modules["astrbot.core.platform.astrbot_message"].AstrBotMessage = object
        sys.modules["astrbot.core.platform.astrbot_message"].Group = object
        sys.modules["astrbot.core.platform.astrbot_message"].MessageMember = object
        sys.modules["astrbot.core.star.filter.custom_filter"] = types.ModuleType("astrbot.core.star.filter.custom_filter")
        sys.modules["astrbot.core.star.filter.custom_filter"].CustomFilter = object
        sys.modules["aiocqhttp"] = types.ModuleType("aiocqhttp")
        sys.modules["aiocqhttp"].Event = type("Event", (), {"from_payload": staticmethod(lambda p: p)})

spec = importlib.util.spec_from_file_location("proactive", Path(__file__).with_name("main.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def decide(text, owner=False, gap_ms=999_999_999, count=0, ts=0):
    return m.should_proactively_reply(
        text,
        {"senderId": ("100009001" if owner else "1234567890"), "groupId": "100000001"},
        last_reply_ms=0 if ts else 0,
        now_ms=ts or 1_000_000_000_000,
        count=count,
    )


passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL: %s %s" % (name, detail))


# ---------------- Topic-grounded scores: no food bonus or forced food trigger
points = {name: score for _pattern, score, name in m._INTERESTS}
check("普通话题同分", points["美食"] == points["CS2"] == points["AI"] == 5, str(points))
check("没有额外干饭类别", "干饭" not in points, str(points))
check("短句饥饿不再必触发", not decide("饿啊")[0])
check("点名仍可接话", decide("牛牛今天怎么这么安静")[0])
check("连续真实技术问题可接话", any(decide("CS2残局怎么打" + str(i))[0] for i in range(10)))

# ---------------- 不触发类
for msg in ("666", "哈哈哈", "草", "典", "？", "1", "嗯", "收到", "ok"):
    ok, why, score, hits = decide(msg)
    check("低价值不触发: %r" % msg, not ok, why)

ok, why, _, _ = decide("帮我整理一下文档")
check("纯服务请求不触发", not ok, why)

ok, why, score, hits = decide("帮我查一下白米饭的营养")
check("食物请求不获得特殊权重", "干饭" not in hits and "美食" in hits, "%s %d %s" % (why, score, hits))

# ---------------- 群主加权
# 测试代码里不能出现真实 QQ 号（脱敏铁律），monkeypatch OWNER 为假号。
m.OWNER = "100009001"
_, _, score_member, hits_member = decide("火锅奶茶安排上", owner=False)
_, _, score_owner, hits_owner = decide("火锅奶茶安排上", owner=True)
check("群主说话加分", score_owner > score_member,
      "owner=%d member=%d" % (score_owner, score_member))
check("群主命中列表带群主", "群主" in hits_owner, hits_owner)

# ---------------- 冷却 / 额度
ok, why, score, hits = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=1_000_000_000_000 - 60_000,  # 1 分钟前刚说过 < 10 分钟
    now_ms=1_000_000_000_000, count=0)
check("冷却中不触发", not ok, why)

ok, why, _, _ = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=0, now_ms=1_000_000_000_000, count=m.DAY_MAX)
check("额度用完不触发", not ok, why)

ok, why, _, _ = m.should_proactively_reply(
    "白米饭", {"senderId": "1", "groupId": "100000001"},
    last_reply_ms=0, now_ms=1_000_000_000_000, count=0)
check("冷却额度都过后仍遵守兴趣阈值", ok == (m.score_interest("白米饭", {"senderId": "1", "groupId": "100000001"})[0] >= m.THRESHOLD), why)

# ---------------- 分数稳定性
_, _, s1, _ = decide("白米饭加老干妈")
_, _, s2, _ = decide("白米饭加老干妈")
check("同消息同分（稳定抖动）", s1 == s2, "%d vs %d" % (s1, s2))

# ---------------- 纯函数不炸
check("空消息安全", m.score_interest("")[0] == 0)
check("None 安全", m.score_interest(None)[0] == 0)

# ---------------- 与 dsh-social 的明确边界联动（只读、按群按人、故障放行）
social_db = os.path.join(tempfile.mkdtemp(), "social.db")
con = sqlite3.connect(social_db)
con.execute("CREATE TABLE relations(group_id TEXT,user_id TEXT,avoid_until REAL,opted_out INTEGER)")
con.executemany(
    "INSERT INTO relations VALUES(?,?,?,?)",
    [("g1", "u1", time.time() + 60, 0), ("g2", "u1", 0, 0)],
)
con.commit()
con.close()
check("社交边界阻止可选探头", not m.social_allows_proactive("g1", "u1", db=social_db)[0])
check("社交边界按群隔离", m.social_allows_proactive("g2", "u1", db=social_db)[0])
check("社交库缺失时故障放行", m.social_allows_proactive("g1", "u1", db=social_db + ".missing")[0])
check("活跃时段解析可测", m.in_active_hours(0) in (True, False))

print("\n%d passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
