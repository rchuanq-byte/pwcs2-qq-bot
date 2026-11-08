"""Ground interests in human group discussion, not a prefabricated food persona."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

TOPIC_PATTERNS = {
    "CS2": re.compile(r"CS2|反恐精英|开黑|残局|爆头|压枪|道具|电竞|完美平台|官匹", re.I),
    "游戏": re.compile(r"游戏|Steam|主机|任天堂|独立游戏|联机|无畏契约|瓦罗兰特|原神|怪猎", re.I),
    "AI": re.compile(r"DeepSeek|大模型|模型|本地部署|token|API|prompt|代码|bug|报错|微调", re.I),
    "数码": re.compile(r"电脑|硬件|显卡|键盘|鼠标|耳机|显示器|服务器|数码|手机"),
    "影音": re.compile(r"电影|动漫|动画|电视剧|纪录片|音乐|歌曲|专辑|演唱会"),
    "日常": re.compile(r"上课|作业|考试|校园|课程|运动|跑步|旅游|旅行|健身|通勤"),
    # Food is a normal human topic, at the same weight as other topics, not a bot appetite.
    "美食": re.compile(
        r"火锅|烧烤|奶茶|外卖|食堂|零食|甜点|蛋糕|冰淇淋|炸鸡|麻辣烫|面条|饺子|炒饭|泡面|米饭|夜宵|好吃|吃什么|饿了"
    ),
}
TOPIC_BASE_SCORE = 5
TOPIC_PROFILE_GUARD = (
    "牛牛精灵的人格已改为随本群真实交流成长的群聊伙伴，没有预设吃货、大胃袋、固定口味或定时食欲。"
    "旧历史、旧摘要或动态资料中的‘今日馋’、自称爱吃零食等虚构机器人自述不是当前设定，不要延续。"
    "跟随本轮真实话题，不把无关讨论转向吃喝，也不强行转成CS2或其他固定爱好。"
    "用户正在聊饮食、求推荐或玩相关梗时可以自然参与，但不要虚构自己吃过、饿了或亲身生活经历。"
    "保留已有的群成员真实记忆、关系、幽默和自主表达；不要把这段规则或内部迁移过程说给群友。"
)


def retire_food_defaults(root: Path, groups, *, now=None):
    """Retire only generated food state, retaining history, other drives and all business data."""
    root, groups = Path(root), tuple(str(g) for g in groups)
    changed = {"interest_state": False, "appetite_rows": 0}
    file = root / "dsh_interest_state.json"
    if file.exists():
        state = json.loads(file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Invalid interest state; refusing broad state reset")
        before = json.dumps(state, sort_keys=True, ensure_ascii=False)
        state["mood"], state["last_swap"] = {}, 0.0
        state["topic_origin"] = "group_discussion"
        # Remove retired template categories, not actual human food discussions.
        tables = state.get("hot", {})
        for hot in tables.values() if isinstance(tables, dict) else ():
            if isinstance(hot, dict):
                hot.pop("干饭", None)
                hot.pop("深海", None)
        if before != json.dumps(state, sort_keys=True, ensure_ascii=False):
            mode = file.stat().st_mode & 0o777
            temporary = file.with_name(file.name + ".topic-new")
            temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
            os.chmod(temporary, mode)
            temporary.replace(file)
            changed["interest_state"] = True
    database = root / "dsh_desire.db"
    if database.exists() and groups:
        with sqlite3.connect(database, timeout=5) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(drive_state)")}
            if columns:
                required = {"drive", "group_id", "intensity", "baseline", "phase"}
                if not required <= columns:
                    raise ValueError("Unknown desire schema; refusing broad state reset")
                assignments = {"intensity": 0, "baseline": 0, "phase": "latent"}
                for name, value in {
                    "active_since": 0,
                    "cooldown_until": 0,
                    "updated_at": time.time() if now is None else now,
                    "last_signal": "retired_food_persona",
                }.items():
                    if name in columns:
                        assignments[name] = value
                where = "intensity<>0 OR baseline<>0 OR phase<>'latent'"
                if "last_signal" in columns:
                    where += " OR last_signal<>'retired_food_persona'"
                cursor = connection.execute(
                    "UPDATE drive_state SET "
                    + ",".join(f"{key}=?" for key in assignments)
                    + " WHERE drive='appetite' AND group_id IN ("
                    + ",".join("?" for _ in groups)
                    + ") AND ("
                    + where
                    + ")",
                    (*assignments.values(), *groups),
                )
                changed["appetite_rows"] = cursor.rowcount
    return changed
