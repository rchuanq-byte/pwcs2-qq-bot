"""Persistent group games: coins, predictions, quiz, museum and revenge."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from pwcs2_bot.appearance import (
    STATS_BACKGROUND_SHOP_ITEMS,
    STATS_BACKGROUNDS,
    resolve_stats_background,
)
from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_formatting import MatchSnapshot
from pwcs2_bot.shop import (
    INVENTORY_LABELS,
    MAX_PURCHASE_QUANTITY,
    SHOP_CATEGORIES,
    SHOP_INVENTORY_KEYS,
    SHOP_ITEMS,
    daily_offers,
    resolve_shop_item,
)


class CommunityError(ValueError):
    """A safe, user-facing group game error."""


CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
CHECKIN_REWARD = 5
CHECKIN_STREAK_DAYS = 7
CHECKIN_STREAK_BONUS = 15
MATCH_REWARD = 5
MATCH_DAILY_LIMIT = 3
FIRST_WIN_BONUS = 5
WEEKLY_ACTIVITY_MATCHES = 5
WEEKLY_ACTIVITY_REWARD = 30


@dataclass(frozen=True, slots=True)
class HistorySource:
    message_id: str
    qq_id: str
    nickname: str
    text: str


@dataclass(frozen=True, slots=True)
class Prediction:
    prediction_id: int
    group_id: str
    question: str
    options: tuple[str, ...]
    created_by: str
    closes_at: str
    status: str


@dataclass(frozen=True, slots=True)
class AutoPredictionRun:
    prediction_id: int
    group_id: str
    prediction_date: str
    qq_id: str
    steam_id: str
    nickname: str
    baseline_match_id: str
    baseline_timestamp: int
    kind: str
    threshold: float | None
    expires_at: str
    status: str
    winning_option: int | None
    result_match_id: str
    result_value: str
    announcement_sent: bool
    result_sent: bool


@dataclass(frozen=True, slots=True)
class SteamPresenceState:
    group_id: str
    steam_id: str
    game_id: str
    server_ip: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class WatchStageState:
    group_id: str
    steam_id: str
    match_id: str
    platform: str
    map_name: str
    ct_score: int | None
    terrorist_score: int | None
    roster_key: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class QuizQuestion:
    key: str
    map_name: str
    prompt: str
    options: tuple[str, ...]
    answer: str
    explanation: str
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class QuizReveal:
    group_id: str
    quiz_date: str
    question: QuizQuestion
    correct_names: tuple[str, ...]
    fastest_name: str
    rewards: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class RevengeEvent:
    winner_qq: str
    winner_name: str
    loser_qq: str
    loser_name: str
    kind: str
    previous_streak: int
    current_streak: int
    horn: bool


@dataclass(frozen=True, slots=True)
class CheckinResult:
    checkin_date: str
    streak: int
    base_reward: int
    streak_bonus: int
    balance: int
    already_checked_in: bool


@dataclass(frozen=True, slots=True)
class MatchCoinReward:
    qq_id: str
    match_id: str
    reward_date: str
    base_reward: int
    win_bonus: int
    balance: int


# Kept only so an already-posted legacy question can still be revealed after an
# upgrade. New quiz runs are always created from PRO_PLAYER_QUESTIONS.
LEGACY_GRENADE_QUESTIONS: tuple[QuizQuestion, ...] = (
    QuizQuestion(
        "smoke-fire",
        "通用机制",
        "哪一种投掷物可以直接熄灭燃烧中的燃烧弹火焰？",
        ("闪光弹", "烟雾弹", "诱饵弹", "高爆手雷"),
        "B",
        "烟雾弹展开后会熄灭范围内的燃烧弹或燃烧瓶火焰。",
    ),
    QuizQuestion(
        "flash-los",
        "通用机制",
        "想让闪光弹充分致盲对手，最关键的条件是什么？",
        ("对手与爆点之间有视线", "必须落到对手脚下", "必须在烟里爆", "必须反弹三次"),
        "A",
        "闪光效果与爆点是否处于视野、朝向和距离密切相关。",
    ),
    QuizQuestion(
        "grenade-limit",
        "竞技模式",
        "标准竞技规则下，一名玩家最多可同时携带多少枚投掷物？",
        ("2 枚", "3 枚", "4 枚", "5 枚"),
        "C",
        "总上限为 4 枚，其中闪光弹最多可携带 2 枚。",
    ),
    QuizQuestion(
        "double-flash",
        "竞技模式",
        "下列哪种投掷物通常可以同时携带两枚？",
        ("烟雾弹", "闪光弹", "高爆手雷", "燃烧弹"),
        "B",
        "标准竞技规则允许携带两枚闪光弹，其他常见投掷物通常各一枚。",
    ),
    QuizQuestion(
        "decoy-sound",
        "通用机制",
        "诱饵弹主要会模仿什么声音来干扰敌人？",
        ("脚步声", "玩家当前武器的射击声", "拆包声", "无线电语音"),
        "B",
        "诱饵弹会模拟投掷者所持武器的射击声，并在结束时爆炸。",
    ),
    QuizQuestion(
        "smoke-gap",
        "CS2 机制",
        "CS2 中想短暂打出烟雾缺口，可以利用什么？",
        ("子弹或高爆手雷", "切刀", "跳跃", "无线电指令"),
        "A",
        "子弹能短暂扰动烟雾，高爆手雷爆炸可暂时清出更明显的缺口。",
    ),
    QuizQuestion(
        "molotov-role",
        "战术基础",
        "进攻方使用燃烧瓶清理近点，最主要的战术目的是什么？",
        ("恢复生命值", "逼迫防守者离开掩体", "提高移速", "隐藏脚步"),
        "B",
        "火焰能封锁区域并逼迫藏在死角的对手移动。",
    ),
    QuizQuestion(
        "he-damage",
        "通用机制",
        "高爆手雷造成伤害时，哪个因素最重要？",
        ("爆炸距离和掩体", "武器皮肤", "玩家昵称", "投掷者金钱"),
        "A",
        "离爆心越近通常伤害越高，墙体等掩体也会影响有效伤害。",
    ),
    QuizQuestion(
        "pop-flash",
        "战术基础",
        "“瞬爆闪”最理想的效果是什么？",
        ("落地后很久才爆", "敌人看到后有充分转身时间", "进入敌人视野后迅速爆炸", "只闪队友"),
        "C",
        "瞬爆闪让对手几乎没有转身规避的反应时间。",
    ),
    QuizQuestion(
        "smoke-purpose",
        "战术基础",
        "进攻时封住防守方关键枪位的烟雾，主要价值是什么？",
        ("切断视野并压缩可用枪线", "增加己方伤害", "自动探测敌人", "让 C4 更快爆炸"),
        "A",
        "烟雾通过切断视野，帮助队伍安全通过或隔离防守位置。",
    ),
    QuizQuestion(
        "team-flash",
        "团队配合",
        "队友准备拉出掩体时，较好的闪光配合方式是什么？",
        ("提前沟通闪光爆点和出手时机", "朝队友脸上扔", "完全不报点", "等队友阵亡后再扔"),
        "A",
        "沟通能让队友同步拉出，同时减少误闪。",
    ),
    QuizQuestion(
        "bounce-use",
        "投掷基础",
        "利用墙面反弹投掷物最常见的目的是什么？",
        ("在不暴露身体的情况下送达目标区域", "增加金币", "永久改变地图", "取消投掷物效果"),
        "A",
        "反弹投掷能让玩家躲在掩体后完成道具投送。",
    ),
)

# Kept so every question already stored in grenade_quiz_runs remains resolvable
# after the curated V2 bank goes live. New runs never select this archive.
LEGACY_PRO_PLAYER_QUESTIONS_V1: tuple[QuizQuestion, ...] = (
    QuizQuestion(
        "pro-bntet-tyloo",
        "跨洲旅程",
        "哪位印度尼西亚选手曾长期效力中国战队 TYLOO，并一度担任指挥？",
        ("xccurate", "BnTeT", "somebody", "Summer"),
        "B",
        "BnTeT 来自印度尼西亚，2017 年加入 TYLOO；那支队伍后来一度升至世界第 10。",
    ),
    QuizQuestion(
        "pro-jimpphat-sergej",
        "亲缘档案",
        "MOUZ 选手 Jimpphat 的哥哥是哪位曾随 ENCE 闯入 2019 卡托维兹 Major 决赛的选手？",
        ("allu", "Aerial", "sergej", "xseveN"),
        "C",
        "Jimpphat 是 sergej 的弟弟；兄弟二人都曾进入 HLTV 年度 Top 20。",
    ),
    QuizQuestion(
        "pro-hen1-lucas1",
        "亲缘档案",
        "巴西选手 HEN1 与下面哪位职业选手是双胞胎兄弟？",
        ("LUCAS1", "kNgV-", "boltz", "fnx"),
        "A",
        "HEN1 与 LUCAS1 是双胞胎，二人还曾一起随 Immortals 打进 2017 克拉科夫 Major 决赛。",
    ),
    QuizQuestion(
        "pro-nivera-scream",
        "亲缘档案",
        "曾为 Vitality 出战六人轮换阵容的 Nivera，是哪位“爆头机器”的弟弟？",
        ("shox", "RpK", "NBK-", "ScreaM"),
        "D",
        "Nivera 是 ScreaM 的弟弟；他希望大家记住自己的昵称，而不只是称他为“ScreaM 的弟弟”。",
    ),
    QuizQuestion(
        "pro-niko-hunter",
        "亲缘档案",
        "G2 的 NiKo 与 huNter- 是什么关系？",
        ("亲兄弟", "叔侄", "表兄弟", "没有亲缘关系"),
        "C",
        "两位 Kovač 是表兄弟；NiKo 还曾鼓励一度远离 CS:GO 的 huNter- 重返赛场。",
    ),
    QuizQuestion(
        "pro-xccurate-forsaken",
        "跨项目亲缘",
        "曾效力 TYLOO 的印尼狙击手 xccurate，是哪位 VALORANT 选手的哥哥？",
        ("f0rsakeN", "something", "Jinggg", "mindfreak"),
        "A",
        "xccurate 的弟弟是 f0rsakeN。后来兄弟二人都转战过 VALORANT。",
    ),
    QuizQuestion(
        "pro-ropz-faceit-hq",
        "成名轶事",
        "哪位选手早年因实力强得被怀疑作弊，曾受邀到 FACEIT 伦敦总部在监督下打 FPL？",
        ("frozen", "ropz", "oskar", "suNny"),
        "B",
        "这名选手是 ropz；他在现场依然打出高水平表现，随后开启了顶级职业生涯。",
    ),
    QuizQuestion(
        "pro-refrezh-liquid-1v5",
        "经典残局",
        "EPL S14 对阵 Liquid 时，谁在 14:15 的赛点局用 Galil 完成 1v5，把 HEROIC 救进加时？",
        ("cadiaN", "stavn", "TeSeS", "refrezh"),
        "D",
        "refrezh 的 Galil 1v5 延续了决胜图，HEROIC 随后在加时淘汰 Liquid。",
    ),
    QuizQuestion(
        "pro-jl-first-cs2-major-mvp",
        "Major 边角料",
        "CS2 历史上第一位 Major MVP 是谁？",
        ("donk", "m0NESY", "jL", "w0nderful"),
        "C",
        "jL 随 NAVI 赢得 2024 哥本哈根 Major，并成为首位 CS2 Major MVP。",
    ),
    QuizQuestion(
        "pro-karrigan-antwerp-oldest",
        "Major 边角料",
        "在 2022 安特卫普 Major 夺冠时，谁以 32 岁成为当时最年长的 Major 冠军？",
        ("karrigan", "rain", "Zeus", "apEX"),
        "A",
        "karrigan 当时以 32 岁 38 天刷新纪录，并带领 FaZe 成为首支夺得 Major 的国际阵容。",
    ),
    QuizQuestion(
        "pro-jame-berlin-rio",
        "两次决赛",
        "哪位指挥先随 AVANGAR 获得 2019 柏林 Major 亚军，后随 Outsiders 赢得 2022 里约 Major？",
        ("Qikert", "Jame", "buster", "AdreN"),
        "B",
        "Jame 两次都担任队伍核心指挥兼狙击手，并在里约夺冠时拿到 Major MVP。",
    ),
    QuizQuestion(
        "pro-zeus-gambit-return",
        "职业转折",
        "哪位指挥离开 NAVI 后带领 Gambit 赢得 2017 克拉科夫 Major，随后又回到了 NAVI？",
        ("ANGE1", "B1ad3", "AdreN", "Zeus"),
        "D",
        "Zeus 在 Gambit 圆了 Major 冠军梦，夺冠后不久便与教练 kane 一起重返 NAVI。",
    ),
    QuizQuestion(
        "pro-bntet-start-age",
        "童年档案",
        "BnTeT 在采访中回忆，自己大约几岁开始和哥哥们玩 CS 1.6？",
        ("11 岁", "14 岁", "17 岁", "20 岁"),
        "A",
        "BnTeT 约 11 岁、读五年级时接触 CS 1.6，最初就是和哥哥及他们的朋友一起玩。",
    ),
    QuizQuestion(
        "pro-jimpphat-fortnite",
        "差点入错行",
        "Jimpphat 最早认真幻想成为职业选手时，想主攻的其实是哪款游戏？",
        ("Dota 2", "PUBG", "Fortnite", "StarCraft II"),
        "C",
        "Jimpphat 小时候一度想成为 Fortnite 职业选手，后来才回到 CS 并迅速打上 FACEIT 10 级。",
    ),
    QuizQuestion(
        "pro-hunter-return",
        "职业转折",
        "是谁鼓励一度不喜欢 CS:GO 的 huNter- 重新认真尝试这款游戏？",
        ("nexa", "emi", "LETN1", "NiKo"),
        "D",
        "鼓励他的人是表兄弟 NiKo；huNter- 后来从巴尔干队伍一路打进世界顶级赛场。",
    ),
    QuizQuestion(
        "pro-nivera-vitality-six",
        "六人阵容考古",
        "2020 年 Nivera 加入哪支队伍，成为其六人轮换阵容的一员？",
        ("G2", "Vitality", "EnVyUs", "LDLC"),
        "B",
        "Nivera 在 2020 年加入 Vitality，当时队伍尝试让第六人参与正式轮换。",
    ),
    QuizQuestion(
        "pro-azr-renegades",
        "大洋洲往事",
        "2019 柏林 Major 上，带领 Renegades 历史性打进四强的澳大利亚指挥是谁？",
        ("AZR", "jks", "Liazz", "Gratisfaction"),
        "A",
        "AZR 是那支 Renegades 的场上指挥；他们在八强 2:0 淘汰了 ENCE。",
    ),
    QuizQuestion(
        "pro-dosia-nade",
        "Major 名场面",
        "2017 克拉科夫 Major 决赛的“Dosia 雷”，主要战术价值是什么？",
        ("抢到一把 AWP", "拖延拆包", "借 C4 爆炸杀死保枪者、破坏对手经济", "制造假脚步"),
        "C",
        "Dosia 先用手雷压低躲在大坑保枪的两人血量，让他们随后被 C4 炸死，重创了经济。",
    ),
    QuizQuestion(
        "pro-jks-katowice-standin",
        "超级替补",
        "2022 IEM 卡托维兹，哪位替补先后顶替 ropz 和 rain，最终随 FaZe 夺冠？",
        ("es3tag", "olofmeister", "cromen", "jks"),
        "D",
        "jks 在同一赛事先后补两个不同位置，FaZe 仍一路夺冠，成为经典替补故事。",
    ),
    QuizQuestion(
        "pro-hobbit-start-age",
        "童年档案",
        "HObbit 回忆自己几岁开始玩 CS，并很快在当地 ASUS 预选赛拿到第三？",
        ("8 岁", "10 岁", "13 岁", "16 岁"),
        "B",
        "HObbit 10 岁开始玩 CS；多年后，他随 Gambit 赢得 2017 克拉科夫 Major。",
    ),
    QuizQuestion(
        "pro-apex-two-majors",
        "跨时代冠军",
        "哪位法国选手分别随 EnVyUs（2015）和 Vitality（2023）赢得 Major？",
        ("shox", "kennyS", "NBK-", "apEX"),
        "D",
        "apEX 先以突破手身份随 EnVyUs 夺冠，八年后又以指挥身份带 Vitality 在巴黎捧杯。",
    ),
    QuizQuestion(
        "pro-autimatic-valorant-return",
        "跨项目回归",
        "哪位 Cloud9 波士顿 Major 冠军转战 VALORANT 一年后，于 2022 年加入 EG 重返 CS？",
        ("tarik", "Skadoodle", "autimatic", "RUSH"),
        "C",
        "autimatic 在 VALORANT 征战约一年后回归 CS，并与部分波士顿冠军队友在 EG 重聚。",
    ),
    QuizQuestion(
        "pro-spinx-paris",
        "小国之光",
        "2023 巴黎 Major 冠军阵容里，哪位选手来自以色列？",
        ("flameZ", "Spinx", "Magisk", "dupreeh"),
        "B",
        "Spinx 来自以色列；他帮助 Vitality 在主场赢得队史首座 Major。",
    ),
    QuizQuestion(
        "pro-gratisfaction-new-zealand",
        "大洋洲往事",
        "2019 柏林 Major 四强 Renegades 阵中，哪位主狙来自新西兰？",
        ("Gratisfaction", "jkaem", "Liazz", "USTILO"),
        "A",
        "Gratisfaction 是新西兰狙击手，也是那支 Renegades 黑马阵容的重要成员。",
    ),
    QuizQuestion(
        "pro-woxic-left-hand",
        "奇特外设",
        "哪位土耳其狙击手以左手握鼠标、把键盘放在右侧的独特姿势闻名？",
        ("XANTARES", "MAJ3R", "woxic", "Calyx"),
        "C",
        "woxic 使用左手握鼠标，桌面布局与多数职业选手正好相反。",
    ),
    QuizQuestion(
        "pro-siuhy-paris-final",
        "青训逆袭",
        "哪位 MOUZ NXT 出身的波兰指挥，后来带领 GamerLegion 闯入 2023 巴黎 Major 决赛？",
        ("Snax", "siuhy", "hades", "dycha"),
        "B",
        "siuhy 从 MOUZ NXT 青训成长，带领 GamerLegion 完成巴黎黑马之旅后又回到 MOUZ。",
    ),
    QuizQuestion(
        "pro-aleksib-two-major-finals",
        "跨时代指挥",
        "哪位芬兰指挥先带 ENCE 打进 2019 卡托维兹 Major 决赛，后带 NAVI 赢得首届 CS2 Major？",
        ("Aleksib", "allu", "suNny", "Aerial"),
        "A",
        "Aleksib 在两套完全不同的国际环境中进入 Major 决赛，并在 2024 年随 NAVI 夺冠。",
    ),
    QuizQuestion(
        "pro-adren-krakow-faze",
        "冠军漂流",
        "2017 克拉科夫 Major MVP 后来还曾短暂效力 FaZe；这位哈萨克斯坦选手是谁？",
        ("mou", "HObbit", "AdreN", "buster"),
        "C",
        "AdreN 是 2017 克拉科夫 Major MVP，2019 年初还曾作为 FaZe 正式阵容成员出战。",
    ),
)


# Curated V2 bank. Sources are retained as maintenance metadata; they are not
# appended to the short QQ reveal message. Keep questions about completed
# events and career history so the answers do not expire with roster changes.
PRO_PLAYER_QUESTIONS: tuple[QuizQuestion, ...] = (
    QuizQuestion(
        "pro-jamyoung-first-cn-mvp",
        "中国突破",
        "哪位选手在 FISSURE Playground 1 成为首位获得 HLTV MVP 奖章的中国选手？",
        ("Starry", "JamYoung", "Moseyuh", "somebody"),
        "B",
        "JamYoung 以赛事 1.40 Rating 获得 HLTV MVP，成为获得该奖章的中国第一人。",
        "https://www.hltv.org/news/42245/jamyoung-secures-fissure-playground-1-mvp",
    ),
    QuizQuestion(
        "pro-starry-g2-1v5",
        "国产残局",
        "Starry 在 2021 BLAST Fall Showdown 的成名 1v5 是面对哪支队伍？",
        ("NAVI", "Liquid", "Astralis", "G2"),
        "D",
        "他效力 Lynn Vision 时以对 G2 的 1v5 首次打响国际名号。",
        "https://www.hltv.org/news/39508/starry-joins-tyloo",
    ),
    QuizQuestion(
        "pro-twistzz-double-igs",
        "双料金条",
        "谁是首位两次赢得 Intel Grand Slam 的选手？",
        ("Twistzz", "ropz", "device", "s1mple"),
        "A",
        "Twistzz 先随 Liquid 赢得第二季，后随 FaZe 赢得第四季，成为首位双冠者。",
        "https://www.hltv.org/news/35865/faze-win-intel-grand-slam-season-4",
    ),
    QuizQuestion(
        "pro-dupreeh-all-csgo-majors",
        "全勤考古",
        "CS:GO 共举办 19 届 Valve Major，唯一全部出席的选手是谁？",
        ("device", "dupreeh", "Xyp9x", "shox"),
        "B",
        "dupreeh 实现 19 届全勤，并在这段旅程中拿到五座 Major 冠军。",
        "https://www.hltv.org/news/38487/from-paris-to-copenhagen-whats-changed",
    ),
    QuizQuestion(
        "pro-donk-broke-kjaerbye",
        "青春纪录",
        "donk 在上海 Major 打破的最年轻冠军兼 MVP 纪录，原保持者是谁？",
        ("JW", "Kjaerbye", "sergej", "frozen"),
        "B",
        "Kjaerbye 2017 年以 18 岁 277 天创下纪录；donk 后来以 17 岁 324 天刷新。",
        "https://www.hltv.org/news/40566/donk-becomes-youngest-and-highest-rated-major-mvp-in-shanghai",
    ),
    QuizQuestion(
        "pro-donk-zywoo-rookie-no1",
        "新秀登顶",
        "donk 在首个完整一线赛季登上 HLTV 年度第一，追平了哪位选手 2019 年的壮举？",
        ("s1mple", "coldzera", "GeT_RiGhT", "ZywOo"),
        "D",
        "ZywOo 与 donk 都在自己的第一个完整顶级赛季拿到 HLTV 年度第一。",
        "https://www.hltv.org/news/40745/top-20-players-of-2024-donk-1",
    ),
    QuizQuestion(
        "pro-bit-first-major",
        "新人无暇冠",
        "哪位 NAVI 青训选手在个人首届 Major 就随队全程不丢一图夺冠？",
        ("b1t", "headtr1ck", "Aunkere", "fear"),
        "A",
        "b1t 在 2021 斯德哥尔摩迎来首届 Major；NAVI 也成为首支全程零丢图夺冠的队伍。",
        "https://www.hltv.org/news/33114/top-20-players-of-2021-b1t-9",
    ),
    QuizQuestion(
        "pro-xantares-oldest-top20-debut",
        "十年磨一榜",
        "谁在 29 岁时成为 HLTV Top 20 历史上年龄最大的初次入榜者？",
        ("MAJ3R", "karrigan", "XANTARES", "Snax"),
        "C",
        "XANTARES 在 2024 年首度入榜第 15，创造最年长初次入榜纪录。",
        "https://www.hltv.org/news/40641/top-20-players-of-2024-xantares-15",
    ),
    QuizQuestion(
        "pro-insani-software-engineer",
        "差点转行",
        "参加 MIBR Academy 海选前，insani 原本打算学习并从事什么职业？",
        ("建筑师", "体育记者", "飞行员", "软件工程师"),
        "D",
        "他一度放下 CS，准备走软件工程道路，直到朋友劝他参加海选。",
        "https://www.hltv.org/news/39010/how-insani-became-brazils-rising-star",
    ),
    QuizQuestion(
        "pro-insani-tryout-three",
        "海选幸存者",
        "MIBR 的 Feitos no Brasil 海选吸引 600 多人，最终有多少人进入首支 Academy 阵容？",
        ("3 人", "5 人", "10 人", "20 人"),
        "A",
        "历经一个多月的多轮筛选，insani 是最终入选的三人之一。",
        "https://www.hltv.org/news/39010/how-insani-became-brazils-rising-star",
    ),
    QuizQuestion(
        "pro-heaton-hockey-injury",
        "命运拐点",
        "哪项运动造成的脚伤，让 HeatoN 被迫宅家苦练 CS 并改变职业轨迹？",
        ("足球", "冰球", "滑雪", "手球"),
        "B",
        "一次冰球脚伤使他无法外出；集中练习后，他很快跻身瑞典强队。",
        "https://www.hltv.org/news/41092/hall-of-fame-heaton",
    ),
    QuizQuestion(
        "pro-potti-quake-standin",
        "零经验冠军",
        "Potti 的 EYEBALLERS 早期在达拉斯 CPL 夺冠时，临时替补 litzer 原本是哪款游戏的选手？",
        ("StarCraft", "Unreal Tournament", "Rainbow Six", "Quake 3"),
        "D",
        "litzer 是 Quake 3 玩家，此前甚至没玩过 CS，队伍仍然夺冠。",
        "https://www.hltv.org/news/40856/hall-of-fame-potti",
    ),
    QuizQuestion(
        "pro-monesy-navi-junior-age",
        "青训神童",
        "m0NESY 加入 NAVI Junior 试训时只有多少岁？",
        ("12 岁", "16 岁", "14 岁", "18 岁"),
        "C",
        "NAVI 在 2020 年让年仅 14 岁的 m0NESY 加入青训试训。",
        "https://www.hltv.org/news/28787/navi-junior-add-m0nesy-on-trial",
    ),
    QuizQuestion(
        "pro-broky-fpl-to-faze",
        "路人局跃迁",
        "无名时期的 broky 主要凭哪种经历获得 FaZe 试训机会？",
        ("大学联赛", "FPL 路人局", "教练亲属推荐", "Major 海选"),
        "B",
        "他在 FPL 与 FaZe 成员交手并单月赢下 69% 对局，随后收到 NiKo 的试训邀请。",
        "https://www.hltv.org/news/33049/top-20-players-of-2021-broky-20",
    ),
    QuizQuestion(
        "pro-mezii-degree",
        "学业并行",
        "mezii 在英国本土队打比赛时，同时攻读的大学专业是什么？",
        ("计算机科学", "体育管理", "心理学", "会计与金融"),
        "D",
        "他效力英国本土队期间，同时完成了 Accounting and Finance 学业。",
        "https://www.hltv.org/news/43534/top-20-players-of-2025-mezii-12",
    ),
    QuizQuestion(
        "pro-regali-academy-rejection",
        "落选转机",
        "regali 在加入 fnatic Rising 前，曾被哪家青训拒绝？",
        ("MOUZ NXT", "NAVI Junior", "Astralis Talent", "BIG Academy"),
        "C",
        "旅居丹麦的 regali 曾被 Astralis Talent 婉拒，后来在 fnatic Rising 找到机会。",
        "https://www.hltv.org/news/32250/regali-on-his-experience-in-kyiv-when-you-see-seriousness-and-professionalism-it-leads-to-a-professional-way-of-living-counter-strike",
    ),
    QuizQuestion(
        "pro-nertz-rookie-24",
        "大龄新秀",
        "2023 HLTV Rookie of the Year NertZ 获奖时大约多少岁？",
        ("17 岁", "19 岁", "21 岁", "24 岁"),
        "D",
        "HLTV 的“新秀”按顶级赛场经验而非年龄判断，NertZ 24 岁仍符合条件。",
        "https://www.hltv.org/news/37937/nertz-wins-rookie-of-the-year-award",
    ),
    QuizQuestion(
        "pro-910-mongolia-first-mvp",
        "草原首章",
        "谁在 YaLLa Compass 2024 拿到蒙古国首枚 HLTV MVP？",
        ("bLitz", "Techno", "910", "Senzu"),
        "C",
        "The MongolZ 主狙 910 成为首位获得 HLTV MVP 奖章的蒙古选手。",
        "https://www.hltv.org/news/39303/who-are-2024s-rookies",
    ),
    QuizQuestion(
        "pro-cogu-first-noneu-hof",
        "名人堂越洋",
        "谁成为 HLTV 名人堂首位来自欧洲以外的选手？",
        ("FalleN", "cogu", "fRoD", "elemeNt"),
        "B",
        "巴西传奇狙击手 cogu 是首位入选 HLTV 名人堂的非欧洲选手。",
        "https://www.hltv.org/news/44055/hall-of-fame-cogu",
    ),
    QuizQuestion(
        "pro-olof-first-graffiti",
        "涂鸦之始",
        "olofmeister 因火中拆包成为首位获游戏内涂鸦纪念的选手，该涂鸦位于哪张地图？",
        ("Cache", "Dust2", "Overpass", "Mirage"),
        "C",
        "2014 科隆 Major 半决赛的燃烧拆包被永久画在 Overpass。",
        "https://www.hltv.org/news/43097/hall-of-fame-unveiling-the-10-nominees-of-2025",
    ),
    QuizQuestion(
        "pro-getright-first-csgo-no1",
        "榜首元年",
        "谁是 HLTV 首届 CS:GO 年度 Top 20（2013）的第一名？",
        ("GeT_RiGhT", "f0rest", "shox", "GuardiaN"),
        "A",
        "NiP 87 连胜时代的 GeT_RiGhT 成为首位 CS:GO 年度第一。",
        "https://www.hltv.org/news/35311/hltv-no-1-players-through-the-years",
    ),
    QuizQuestion(
        "pro-s1mple-first-20-mvps",
        "奖章二十",
        "谁在 2022 BLAST Spring Final 后成为首位累计 20 枚 HLTV MVP 的选手？",
        ("device", "ZywOo", "s1mple", "coldzera"),
        "C",
        "s1mple 的该站 MVP 是生涯第 20 枚，当时超过 device 的 19 枚。",
        "https://www.hltv.org/news/34084/s1mple-breaks-mvp-record-with-blast-spring-medal",
    ),
    QuizQuestion(
        "pro-kyousuke-second-trial",
        "二试成名",
        "kyousuke 首次 Spirit Academy 试训未打动教练后，谁坚持要求给他第二次机会？",
        ("hally", "B1ad3", "Hooch", "OverDrive"),
        "D",
        "球探 OverDrive 请求安排二次试训，kyousuke 随后成功留下。",
        "https://www.hltv.org/news/41047/kyousuke-interview-dream-move-playing-with-donk-and-top-30-ambitions-with-spirit-academy",
    ),
    QuizQuestion(
        "pro-boros-internet-cafe",
        "约旦起点",
        "BOROS 十岁时第一次看到 CS:GO 是在什么地方？",
        ("网吧", "学校机房", "哥哥卧室", "职业队训练室"),
        "A",
        "他在家附近的网吧看到别人玩 CS:GO，随后开始追逐职业梦想。",
        "https://www.hltv.org/news/36192/boros-proved-doubters-wrong-at-the-rmr-now-hes-taking-aim-at-the-major",
    ),
    QuizQuestion(
        "pro-snax-ence-standin",
        "老将救火",
        "Snax 在 2022 IEM Dallas 临时替补哪支队伍并一路打进决赛？",
        ("ENCE", "G2", "MOUZ", "OG"),
        "A",
        "离开一线赛场多年后，Snax 为 ENCE 救火并意外闯进决赛。",
        "https://www.hltv.org/news/37158/snax-joins-gamerlegion",
    ),
    QuizQuestion(
        "pro-zywoo-three-major-mvps",
        "三枚至高章",
        "谁在 2025 布达佩斯成为首位拿到三枚 Major MVP 的选手？",
        ("ZywOo", "device", "coldzera", "s1mple"),
        "A",
        "ZywOo 在布达佩斯收获个人第三枚 Major MVP，成为历史第一位。",
        "https://www.hltv.org/news/43451/zywoo-when-vitality-are-playing-like-this-no-one-can-challenge-us",
    ),
    QuizQuestion(
        "pro-ropz-three-grand-slams",
        "三套金条",
        "谁在 2026 年成为首位赢得三次 ESL/Intel Grand Slam 的选手？",
        ("Twistzz", "ropz", "apEX", "dupreeh"),
        "B",
        "ropz 随 Vitality 赢得第六季后累计三次，超越了 Twistzz 的两次。",
        "https://www.hltv.org/news/44422/vitality-make-history-as-first-team-to-win-two-grand-slams",
    ),
    QuizQuestion(
        "pro-kennys-shanghai-move",
        "东方新章",
        "退役后的 kennyS 在 2024 年搬到哪座城市，参与振兴当地 CS 生态？",
        ("成都", "深圳", "上海", "北京"),
        "C",
        "他搬到上海，并参与打造一个被称作“中国卡托维兹”的项目。",
        "https://www.hltv.org/news/40534/behind-kennys-efforts-to-build-the-chinese-katowice",
    ),
)


BADGES = (
    "荒漠迷城包工头",
    "香蕉道具大师",
    "ECO 局战神",
    "白给艺术家",
    "残局观察员",
    "牛子杯冠军",
    "A 大慈善家",
    "职业气氛组",
)


class CommunityRepository:
    def __init__(self, path: str | Path, *, initial_balance: int = 100) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initial_balance = initial_balance
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS coin_wallets (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    balance INTEGER NOT NULL,
                    badge TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                );
                CREATE TABLE IF NOT EXISTS coin_ledger (
                    ledger_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    reference_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (group_id, qq_id, reference_key)
                );
                CREATE TABLE IF NOT EXISTS coin_inventory (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, item_key)
                );
                CREATE TABLE IF NOT EXISTS member_badges (
                    badge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    badge TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    UNIQUE (group_id, qq_id, badge)
                );
                INSERT OR IGNORE INTO member_badges (group_id, qq_id, badge, acquired_at)
                    SELECT group_id, qq_id, badge, updated_at FROM coin_wallets
                    WHERE trim(badge) <> '';
                CREATE TABLE IF NOT EXISTS shop_daily_purchases (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    shop_date TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, shop_date, item_key)
                );
                CREATE TABLE IF NOT EXISTS shop_receipts (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS daily_checkins (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    checkin_date TEXT NOT NULL,
                    streak INTEGER NOT NULL,
                    base_reward INTEGER NOT NULL,
                    streak_bonus INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, checkin_date)
                );
                CREATE TABLE IF NOT EXISTS match_coin_rewards (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    reward_date TEXT NOT NULL,
                    base_reward INTEGER NOT NULL,
                    win_bonus INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, match_id)
                );
                CREATE INDEX IF NOT EXISTS idx_match_coin_rewards_daily
                    ON match_coin_rewards (group_id, qq_id, reward_date);
                CREATE TABLE IF NOT EXISTS weekly_coin_rewards (
                    group_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    match_count INTEGER NOT NULL,
                    reward INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, week_start, qq_id)
                );
                CREATE TABLE IF NOT EXISTS member_stats_backgrounds (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    theme_key TEXT NOT NULL,
                    selected INTEGER NOT NULL DEFAULT 0,
                    unlocked_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id, theme_key)
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    question TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    closes_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    winning_option INTEGER,
                    created_at TEXT NOT NULL,
                    settled_at TEXT
                );
                CREATE TABLE IF NOT EXISTS prediction_bets (
                    prediction_id INTEGER NOT NULL,
                    qq_id TEXT NOT NULL,
                    option_index INTEGER NOT NULL,
                    stake INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (prediction_id, qq_id),
                    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS auto_prediction_days (
                    group_id TEXT NOT NULL,
                    prediction_date TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, prediction_date)
                );
                CREATE TABLE IF NOT EXISTS auto_prediction_runs (
                    prediction_id INTEGER PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    prediction_date TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    baseline_match_id TEXT NOT NULL,
                    baseline_timestamp INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    threshold REAL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    winning_option INTEGER,
                    result_match_id TEXT NOT NULL DEFAULT '',
                    result_value TEXT NOT NULL DEFAULT '',
                    announcement_sent INTEGER NOT NULL DEFAULT 0,
                    result_sent INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE (group_id, prediction_date),
                    FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
                );
                CREATE TABLE IF NOT EXISTS steam_presence_states (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    game_id TEXT NOT NULL,
                    server_ip TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id)
                );
                CREATE TABLE IF NOT EXISTS watch_stage_states (
                    group_id TEXT NOT NULL,
                    steam_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT '',
                    map_name TEXT NOT NULL DEFAULT '',
                    ct_score INTEGER,
                    terrorist_score INTEGER,
                    roster_key TEXT NOT NULL DEFAULT '',
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, steam_id)
                );
                CREATE TABLE IF NOT EXISTS grenade_quiz_runs (
                    group_id TEXT NOT NULL,
                    quiz_date TEXT NOT NULL,
                    question_key TEXT NOT NULL,
                    question_sent INTEGER NOT NULL DEFAULT 0,
                    revealed INTEGER NOT NULL DEFAULT 0,
                    reveal_sent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, quiz_date)
                );
                CREATE TABLE IF NOT EXISTS grenade_quiz_answers (
                    group_id TEXT NOT NULL,
                    quiz_date TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    nickname TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    answered_at TEXT NOT NULL,
                    reward INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, quiz_date, qq_id)
                );
                CREATE TABLE IF NOT EXISTS grenade_quiz_hints (
                    group_id TEXT NOT NULL,
                    quiz_date TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    choices_json TEXT NOT NULL,
                    used_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, quiz_date, qq_id)
                );
                CREATE TABLE IF NOT EXISTS museum_preferences (
                    group_id TEXT NOT NULL,
                    qq_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, qq_id)
                );
                CREATE TABLE IF NOT EXISTS museum_entries (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    subject_qq TEXT NOT NULL,
                    subject_name TEXT NOT NULL,
                    collector_qq TEXT NOT NULL,
                    quote TEXT NOT NULL,
                    note TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (group_id, source_message_id)
                );
                CREATE TABLE IF NOT EXISTS revenge_rivalries (
                    group_id TEXT NOT NULL,
                    first_qq TEXT NOT NULL,
                    second_qq TEXT NOT NULL,
                    leader_qq TEXT NOT NULL,
                    leader_name TEXT NOT NULL,
                    challenger_name TEXT NOT NULL,
                    streak INTEGER NOT NULL,
                    total_matches INTEGER NOT NULL,
                    last_match_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, first_qq, second_qq)
                );
                CREATE TABLE IF NOT EXISTS revenge_match_runs (
                    group_id TEXT NOT NULL,
                    match_id TEXT NOT NULL,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, match_id)
                );
                CREATE TABLE IF NOT EXISTS weekly_report_runs (
                    group_id TEXT NOT NULL,
                    week_start TEXT NOT NULL,
                    was_sent INTEGER NOT NULL,
                    completed_at TEXT NOT NULL,
                    PRIMARY KEY (group_id, week_start)
                );
                """
            )
            watch_stage_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(watch_stage_states)"
                ).fetchall()
            }
            for column, declaration in (
                ("platform", "TEXT NOT NULL DEFAULT ''"),
                ("map_name", "TEXT NOT NULL DEFAULT ''"),
                ("ct_score", "INTEGER"),
                ("terrorist_score", "INTEGER"),
                ("roster_key", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in watch_stage_columns:
                    connection.execute(
                        f"ALTER TABLE watch_stage_states "
                        f"ADD COLUMN {column} {declaration}"
                    )

    def _ensure_wallet(self, connection: sqlite3.Connection, group_id: str, qq_id: str) -> None:
        now = self._now()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO coin_wallets
                (group_id, qq_id, balance, badge, created_at, updated_at)
            VALUES (?, ?, ?, '', ?, ?)
            """,
            (group_id, qq_id, self.initial_balance, now, now),
        )
        if cursor.rowcount:
            connection.execute(
                """
                INSERT OR IGNORE INTO coin_ledger
                    (group_id, qq_id, delta, reason, reference_key, created_at)
                VALUES (?, ?, ?, '新成员启动资金', 'welcome', ?)
                """,
                (group_id, qq_id, self.initial_balance, now),
            )

    def wallet(self, group_id: str, qq_id: str) -> tuple[int, str]:
        with self._connect() as connection:
            self._ensure_wallet(connection, group_id, qq_id)
            row = connection.execute(
                "SELECT balance, badge FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return int(row["balance"]), str(row["badge"])

    def equipped_badge(self, group_id: str, qq_id: str) -> str:
        """Return the equipped group badge without creating a wallet."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT badge FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return str(row["badge"]) if row is not None else ""

    def peek_wallet(self, group_id: str, qq_id: str) -> tuple[int, str] | None:
        """Read a balance without awarding welcome coins or creating a wallet."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT balance, badge FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return (int(row["balance"]), str(row["badge"])) if row is not None else None

    def coin_ranking(self, group_id: str, limit: int = 10) -> list[tuple[str, int, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, balance, badge FROM coin_wallets
                WHERE group_id = ? ORDER BY balance DESC, qq_id LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()
        return [(str(row["qq_id"]), int(row["balance"]), str(row["badge"])) for row in rows]

    def coin_ledger(
        self, group_id: str, qq_id: str, limit: int = 10
    ) -> list[tuple[int, str, str]]:
        with self._connect() as connection:
            self._ensure_wallet(connection, group_id, qq_id)
            rows = connection.execute(
                """
                SELECT delta, reason, created_at FROM coin_ledger
                WHERE group_id = ? AND qq_id = ?
                ORDER BY ledger_id DESC LIMIT ?
                """,
                (group_id, qq_id, limit),
            ).fetchall()
        return [
            (int(row["delta"]), str(row["reason"]), str(row["created_at"]))
            for row in rows
        ]

    def _change_balance(
        self,
        connection: sqlite3.Connection,
        group_id: str,
        qq_id: str,
        delta: int,
        reason: str,
        reference_key: str,
    ) -> bool:
        self._ensure_wallet(connection, group_id, qq_id)
        now = self._now()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO coin_ledger
                (group_id, qq_id, delta, reason, reference_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (group_id, qq_id, delta, reason, reference_key, now),
        )
        if not cursor.rowcount:
            return False
        if delta < 0:
            updated = connection.execute(
                """
                UPDATE coin_wallets SET balance = balance + ?, updated_at = ?
                WHERE group_id = ? AND qq_id = ? AND balance + ? >= 0
                """,
                (delta, now, group_id, qq_id, delta),
            )
            if not updated.rowcount:
                raise CommunityError("牛币余额不足")
        else:
            connection.execute(
                """
                UPDATE coin_wallets SET balance = balance + ?, updated_at = ?
                WHERE group_id = ? AND qq_id = ?
                """,
                (delta, now, group_id, qq_id),
            )
        return True

    def add_coins(
        self, group_id: str, qq_id: str, amount: int, *, reason: str, reference_key: str
    ) -> bool:
        with self._connect() as connection:
            return self._change_balance(
                connection, group_id, qq_id, amount, reason, reference_key
            )

    def checkin(self, group_id: str, qq_id: str, checkin_date: str) -> CheckinResult:
        current_date = date.fromisoformat(checkin_date)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT streak, base_reward, streak_bonus
                FROM daily_checkins
                WHERE group_id = ? AND qq_id = ? AND checkin_date = ?
                """,
                (group_id, qq_id, checkin_date),
            ).fetchone()
            if existing is not None:
                self._ensure_wallet(connection, group_id, qq_id)
                balance = connection.execute(
                    """
                    SELECT balance FROM coin_wallets
                    WHERE group_id = ? AND qq_id = ?
                    """,
                    (group_id, qq_id),
                ).fetchone()
                return CheckinResult(
                    checkin_date=checkin_date,
                    streak=int(existing["streak"]),
                    base_reward=int(existing["base_reward"]),
                    streak_bonus=int(existing["streak_bonus"]),
                    balance=int(balance["balance"]),
                    already_checked_in=True,
                )

            previous = connection.execute(
                """
                SELECT checkin_date, streak FROM daily_checkins
                WHERE group_id = ? AND qq_id = ? AND checkin_date < ?
                ORDER BY checkin_date DESC LIMIT 1
                """,
                (group_id, qq_id, checkin_date),
            ).fetchone()
            streak = 1
            if (
                previous is not None
                and date.fromisoformat(str(previous["checkin_date"]))
                == current_date - timedelta(days=1)
            ):
                streak = int(previous["streak"]) + 1
            streak_bonus = (
                CHECKIN_STREAK_BONUS
                if streak % CHECKIN_STREAK_DAYS == 0
                else 0
            )
            now = self._now()
            connection.execute(
                """
                INSERT INTO daily_checkins
                    (group_id, qq_id, checkin_date, streak, base_reward,
                     streak_bonus, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    qq_id,
                    checkin_date,
                    streak,
                    CHECKIN_REWARD,
                    streak_bonus,
                    now,
                ),
            )
            total = CHECKIN_REWARD + streak_bonus
            self._change_balance(
                connection,
                group_id,
                qq_id,
                total,
                f"每日签到（连续 {streak} 天）",
                f"checkin:{checkin_date}",
            )
            balance = connection.execute(
                """
                SELECT balance FROM coin_wallets
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return CheckinResult(
            checkin_date=checkin_date,
            streak=streak,
            base_reward=CHECKIN_REWARD,
            streak_bonus=streak_bonus,
            balance=int(balance["balance"]),
            already_checked_in=False,
        )

    def reward_completed_match(
        self,
        group_id: str,
        qq_id: str,
        match_id: str,
        reward_date: str,
        *,
        won: bool,
    ) -> MatchCoinReward | None:
        date.fromisoformat(reward_date)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT 1 FROM match_coin_rewards
                WHERE group_id = ? AND qq_id = ? AND match_id = ?
                """,
                (group_id, qq_id, match_id),
            ).fetchone()
            if existing is not None:
                return None
            daily_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM match_coin_rewards
                    WHERE group_id = ? AND qq_id = ? AND reward_date = ?
                    """,
                    (group_id, qq_id, reward_date),
                ).fetchone()["count"]
            )
            base_reward = MATCH_REWARD if daily_count < MATCH_DAILY_LIMIT else 0
            has_first_win = connection.execute(
                """
                SELECT 1 FROM match_coin_rewards
                WHERE group_id = ? AND qq_id = ? AND reward_date = ?
                  AND win_bonus > 0
                LIMIT 1
                """,
                (group_id, qq_id, reward_date),
            ).fetchone()
            win_bonus = FIRST_WIN_BONUS if won and has_first_win is None else 0
            connection.execute(
                """
                INSERT INTO match_coin_rewards
                    (group_id, qq_id, match_id, reward_date, base_reward,
                     win_bonus, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    group_id,
                    qq_id,
                    match_id,
                    reward_date,
                    base_reward,
                    win_bonus,
                    self._now(),
                ),
            )
            total = base_reward + win_bonus
            if total:
                reason_parts = []
                if base_reward:
                    reason_parts.append(f"当日第 {daily_count + 1} 场完赛")
                if win_bonus:
                    reason_parts.append("今日首胜")
                self._change_balance(
                    connection,
                    group_id,
                    qq_id,
                    total,
                    " + ".join(reason_parts),
                    f"match-income:{match_id}",
                )
            else:
                self._ensure_wallet(connection, group_id, qq_id)
            balance = connection.execute(
                """
                SELECT balance FROM coin_wallets
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchone()
        return MatchCoinReward(
            qq_id=qq_id,
            match_id=match_id,
            reward_date=reward_date,
            base_reward=base_reward,
            win_bonus=win_bonus,
            balance=int(balance["balance"]),
        )

    def award_weekly_activity(
        self,
        group_id: str,
        week_start: str,
        eligible_members: list[tuple[str, int]],
    ) -> None:
        date.fromisoformat(week_start)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for qq_id, match_count in eligible_members:
                if match_count < WEEKLY_ACTIVITY_MATCHES:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO weekly_coin_rewards
                        (group_id, week_start, qq_id, match_count, reward, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        week_start,
                        qq_id,
                        match_count,
                        WEEKLY_ACTIVITY_REWARD,
                        self._now(),
                    ),
                )
                if cursor.rowcount:
                    self._change_balance(
                        connection,
                        group_id,
                        qq_id,
                        WEEKLY_ACTIVITY_REWARD,
                        f"每周活跃奖励（{match_count} 场）",
                        f"weekly-activity:{week_start}",
                    )

    def weekly_activity_rewards(
        self, group_id: str, week_start: str
    ) -> list[tuple[str, int, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, match_count, reward FROM weekly_coin_rewards
                WHERE group_id = ? AND week_start = ?
                ORDER BY match_count DESC, qq_id
                """,
                (group_id, week_start),
            ).fetchall()
        return [
            (str(row["qq_id"]), int(row["match_count"]), int(row["reward"]))
            for row in rows
        ]

    def tip(
        self,
        group_id: str,
        sender_qq: str,
        recipient_qq: str,
        amount: int,
        *,
        request_id: str = "",
    ) -> tuple[int, int, int]:
        if sender_qq == recipient_qq:
            raise CommunityError("不能给自己打赏")
        if type(amount) is not int or not 10 <= amount <= 10_000:
            raise CommunityError("打赏金额必须为 10～10000 牛币")
        fee = max(1, (amount + 9) // 10)
        received = amount - fee
        request_key = self._shop_request_key(request_id)
        payload = json.dumps(["tip", recipient_qq, amount])
        reference = f"tip:{sender_qq}:{request_key}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._shop_receipt(connection, group_id, sender_qq, request_key, payload)
            if previous is not None:
                return previous
            self._change_balance(
                connection,
                group_id,
                sender_qq,
                -amount,
                f"打赏 QQ {recipient_qq}",
                reference,
            )
            self._change_balance(
                connection,
                group_id,
                recipient_qq,
                received,
                f"收到 QQ {sender_qq} 打赏",
                reference,
            )
            row = connection.execute(
                "SELECT balance FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, sender_qq),
            ).fetchone()
            result = received, fee, int(row["balance"])
            self._save_shop_receipt(connection, group_id, sender_qq, request_key, payload, result)
        return result

    def inventory(self, group_id: str, qq_id: str) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT item_key, quantity FROM coin_inventory
                WHERE group_id = ? AND qq_id = ? AND quantity > 0
                """,
                (group_id, qq_id),
            ).fetchall()
        return {str(row["item_key"]): int(row["quantity"]) for row in rows}

    def consume_item(self, group_id: str, qq_id: str, item_key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ?
                WHERE group_id = ? AND qq_id = ? AND item_key = ? AND quantity > 0
                """,
                (self._now(), group_id, qq_id, item_key),
            )
        return bool(cursor.rowcount)

    def _shop_receipt(
        self, connection: sqlite3.Connection, group_id: str, qq_id: str,
        request_key: str, payload: str,
    ) -> tuple | None:
        row = connection.execute(
            "SELECT payload, result_json FROM shop_receipts "
            "WHERE group_id = ? AND qq_id = ? AND request_key = ?",
            (group_id, qq_id, request_key),
        ).fetchone()
        if row is None:
            return None
        if str(row["payload"]) != payload:
            raise CommunityError("同一条消息已处理，不能重复执行不同的牛币操作")
        return tuple(json.loads(str(row["result_json"])))

    def _save_shop_receipt(
        self, connection: sqlite3.Connection, group_id: str, qq_id: str,
        request_key: str, payload: str, result: tuple,
    ) -> None:
        connection.execute(
            "INSERT INTO shop_receipts "
            "(group_id, qq_id, request_key, payload, result_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (group_id, qq_id, request_key, payload, json.dumps(result), self._now()),
        )

    @staticmethod
    def _shop_request_key(request_id: str) -> str:
        if len(request_id) > 160:
            raise CommunityError("商店请求标识过长")
        return f"message:{request_id}" if request_id else f"local:{uuid4().hex}"

    @staticmethod
    def _validate_quantity(quantity: int) -> None:
        if type(quantity) is not int or not 1 <= quantity <= MAX_PURCHASE_QUANTITY:
            raise CommunityError(f"数量必须为 1～{MAX_PURCHASE_QUANTITY} 的整数")

    def daily_purchases(self, group_id: str, qq_id: str, shop_date: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT item_key FROM shop_daily_purchases "
                "WHERE group_id = ? AND qq_id = ? AND shop_date = ?",
                (group_id, qq_id, shop_date),
            ).fetchall()
        return {str(row["item_key"]) for row in rows}

    def purchase(
        self, group_id: str, qq_id: str, item_key: str, *, quantity: int = 1,
        discounted: bool = False, shop_date: str = "", request_id: str = "",
    ) -> tuple[str, int, str]:
        item = SHOP_ITEMS.get(item_key)
        if item is None:
            raise CommunityError("商品编号不存在，请发送 #商店 查看")
        self._validate_quantity(quantity)
        theme_key = STATS_BACKGROUND_SHOP_ITEMS.get(item_key)
        if (theme_key is not None or item_key == "1") and quantity != 1:
            raise CommunityError("永久背景和称号盲盒每次只能购买一份")
        if discounted and quantity != 1:
            raise CommunityError("特惠商品每天每人每款限购一份")
        name, unit_cost, _description = item
        day = date.fromisoformat(shop_date or datetime.now(CHINA_TIMEZONE).date().isoformat())
        request_key = self._shop_request_key(request_id)
        payload = json.dumps(["buy", item_key, quantity, discounted])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._shop_receipt(connection, group_id, qq_id, request_key, payload)
            if previous is not None:
                return previous
            if discounted:
                offer = daily_offers(group_id, day.isoformat()).get(item_key)
                if offer is None:
                    raise CommunityError("该商品不在今日特惠中，请发送 #特惠 查看")
                used = connection.execute(
                    "SELECT 1 FROM shop_daily_purchases "
                    "WHERE group_id = ? AND qq_id = ? AND shop_date = ? AND item_key = ?",
                    (group_id, qq_id, day.isoformat(), item_key),
                ).fetchone()
                if used is not None:
                    raise CommunityError("你今天已经买过这款特惠，可明日再来或按原价购买")
                unit_cost = offer
            cost = unit_cost * quantity
            if theme_key is not None:
                existing = connection.execute(
                    """
                    SELECT 1 FROM member_stats_backgrounds
                    WHERE group_id = ? AND qq_id = ? AND theme_key = ?
                    """,
                    (group_id, qq_id, theme_key),
                ).fetchone()
                if existing is not None:
                    raise CommunityError("你已经永久拥有这套战绩背景，不需要重复购买")
            available_badges = []
            if item_key == "1":
                self._remember_equipped_badge(connection, group_id, qq_id)
                owned = {
                    str(row["badge"]) for row in connection.execute(
                        "SELECT badge FROM member_badges WHERE group_id = ? AND qq_id = ?",
                        (group_id, qq_id),
                    ).fetchall()
                }
                available_badges = [badge for badge in BADGES if badge not in owned]
                if not available_badges:
                    raise CommunityError("盲盒称号已全部集齐，无需再花牛币；#称号 可免费换装")
            changed = self._change_balance(
                connection, group_id, qq_id, -cost,
                f"{'特惠购买' if discounted else '购买'}{name}×{quantity}", f"shop:{request_key}",
            )
            if not changed:
                raise CommunityError("该扣款请求已经处理，请查看牛币流水和背包")
            if theme_key is not None:
                now = self._now()
                connection.execute(
                    """
                    UPDATE member_stats_backgrounds SET selected = 0, updated_at = ?
                    WHERE group_id = ? AND qq_id = ?
                    """,
                    (now, group_id, qq_id),
                )
                connection.execute(
                    """
                    INSERT INTO member_stats_backgrounds
                        (group_id, qq_id, theme_key, selected, unlocked_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (group_id, qq_id, theme_key, now, now),
                )
                theme = STATS_BACKGROUNDS[theme_key]
                detail = f"已永久解锁并切换为「{theme.name}」"
            elif item_key == "1":
                badge = random.SystemRandom().choice(available_badges)
                self._store_badge(connection, group_id, qq_id, badge)
                connection.execute(
                    """
                    UPDATE coin_wallets SET badge = ?, updated_at = ?
                    WHERE group_id = ? AND qq_id = ?
                    """,
                    (badge, self._now(), group_id, qq_id),
                )
                detail = f"抽到新称号「{badge}」，已永久收藏并自动佩戴"
            else:
                self._add_inventory(connection, group_id, qq_id, item_key, quantity)
                detail = f"已放入背包 ×{quantity}"
            if discounted:
                connection.execute(
                    "INSERT INTO shop_daily_purchases VALUES (?, ?, ?, ?)",
                    (group_id, qq_id, day.isoformat(), item_key),
                )
            result = name, cost, detail
            self._save_shop_receipt(connection, group_id, qq_id, request_key, payload, result)
        return result

    def _add_inventory(
        self, connection: sqlite3.Connection, group_id: str, qq_id: str,
        item_key: str, quantity: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO coin_inventory (group_id, qq_id, item_key, quantity, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(group_id, qq_id, item_key) DO UPDATE SET
                quantity = quantity + excluded.quantity, updated_at = excluded.updated_at
            """,
            (group_id, qq_id, SHOP_INVENTORY_KEYS[item_key], quantity, self._now()),
        )

    def gift_item(
        self, group_id: str, qq_id: str, recipient_qq: str, item_key: str,
        *, quantity: int = 1, request_id: str = "",
    ) -> tuple[str, int]:
        self._validate_quantity(quantity)
        if qq_id == recipient_qq:
            raise CommunityError("不能给自己赠送道具")
        if item_key not in SHOP_INVENTORY_KEYS:
            raise CommunityError("只能赠送背包中的消耗道具，称号和永久背景不可转赠")
        request_key = self._shop_request_key(request_id)
        payload = json.dumps(["gift", recipient_qq, item_key, quantity])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._shop_receipt(connection, group_id, qq_id, request_key, payload)
            if previous is not None:
                return previous
            consumed = connection.execute(
                "UPDATE coin_inventory SET quantity = quantity - ?, updated_at = ? "
                "WHERE group_id = ? AND qq_id = ? AND item_key = ? AND quantity >= ?",
                (quantity, self._now(), group_id, qq_id, SHOP_INVENTORY_KEYS[item_key], quantity),
            )
            if not consumed.rowcount:
                raise CommunityError("背包中的该道具数量不足，赠送不会自动扣币购买")
            self._add_inventory(connection, group_id, recipient_qq, item_key, quantity)
            result = SHOP_ITEMS[item_key][0], quantity
            self._save_shop_receipt(connection, group_id, qq_id, request_key, payload, result)
        return result

    def _store_badge(
        self, connection: sqlite3.Connection, group_id: str, qq_id: str, badge: str,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO member_badges (group_id, qq_id, badge, acquired_at) "
            "VALUES (?, ?, ?, ?)", (group_id, qq_id, badge, self._now()),
        )

    def _remember_equipped_badge(
        self, connection: sqlite3.Connection, group_id: str, qq_id: str,
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO member_badges (group_id, qq_id, badge, acquired_at) "
            "SELECT group_id, qq_id, badge, updated_at FROM coin_wallets "
            "WHERE group_id = ? AND qq_id = ? AND trim(badge) <> ''", (group_id, qq_id),
        )

    def owned_badges(self, group_id: str, qq_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT badge FROM member_badges WHERE group_id = ? AND qq_id = ? "
                "ORDER BY badge_id", (group_id, qq_id),
            ).fetchall()
        return [str(row["badge"]) for row in rows]

    def equip_badge(self, group_id: str, qq_id: str, badge: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if badge and connection.execute(
                "SELECT 1 FROM member_badges WHERE group_id = ? AND qq_id = ? AND badge = ?",
                (group_id, qq_id, badge),
            ).fetchone() is None:
                raise CommunityError("你尚未拥有这个称号，请发送 #称号 查看收藏")
            connection.execute(
                "UPDATE coin_wallets SET badge = ?, updated_at = ? "
                "WHERE group_id = ? AND qq_id = ?",
                (badge, self._now(), group_id, qq_id),
            )

    def repair_checkin(
        self, group_id: str, qq_id: str, checkin_date: str, *, request_id: str = "",
    ) -> tuple[str, int, str]:
        today = date.fromisoformat(checkin_date)
        yesterday = (today - timedelta(days=1)).isoformat()
        request_key = self._shop_request_key(request_id)
        payload = json.dumps(["repair_checkin"])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._shop_receipt(connection, group_id, qq_id, request_key, payload)
            if previous is not None:
                return previous
            if connection.execute(
                "SELECT 1 FROM daily_checkins WHERE group_id = ? AND qq_id = ? "
                "AND checkin_date IN (?, ?)", (group_id, qq_id, today.isoformat(), yesterday),
            ).fetchone():
                raise CommunityError("昨日已签到或今日已签到，未消耗道具；请在今日签到前补签")
            before = connection.execute(
                "SELECT streak FROM daily_checkins "
                "WHERE group_id = ? AND qq_id = ? AND checkin_date = ?",
                (group_id, qq_id, (today - timedelta(days=2)).isoformat()),
            ).fetchone()
            if before is None:
                raise CommunityError("补签卡只能修复漏签一天的连续签到，前天没有签到记录")
            consumed = connection.execute(
                "UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ? "
                "WHERE group_id = ? AND qq_id = ? AND item_key = 'checkin_repair' AND quantity > 0",
                (self._now(), group_id, qq_id),
            )
            if not consumed.rowcount:
                raise CommunityError("背包里没有补签卡，请发送 #购买 补签卡")
            streak = int(before["streak"]) + 1
            connection.execute(
                "INSERT INTO daily_checkins "
                "(group_id, qq_id, checkin_date, streak, base_reward, streak_bonus, created_at) "
                "VALUES (?, ?, ?, ?, 0, 0, ?)",
                (group_id, qq_id, yesterday, streak, self._now()),
            )
            result = (
                "补签卡", 0,
                f"已补签 {yesterday}，恢复连续 {streak} 天；不补发牛币。现在可 #签到",
            )
            self._save_shop_receipt(connection, group_id, qq_id, request_key, payload, result)
        return result

    def stats_background(self, group_id: str, qq_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT theme_key FROM member_stats_backgrounds
                WHERE group_id = ? AND qq_id = ? AND selected = 1
                ORDER BY updated_at DESC LIMIT 1
                """,
                (group_id, qq_id),
            ).fetchone()
        key = str(row["theme_key"]) if row is not None else "classic"
        return key if key in STATS_BACKGROUNDS else "classic"

    def unlocked_stats_backgrounds(self, group_id: str, qq_id: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT theme_key FROM member_stats_backgrounds
                WHERE group_id = ? AND qq_id = ?
                """,
                (group_id, qq_id),
            ).fetchall()
        return {"classic", *(str(row["theme_key"]) for row in rows)}

    def select_stats_background(self, group_id: str, qq_id: str, theme_key: str) -> None:
        if theme_key not in STATS_BACKGROUNDS:
            raise CommunityError("战绩背景不存在")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if theme_key != "classic":
                unlocked = connection.execute(
                    """
                    SELECT 1 FROM member_stats_backgrounds
                    WHERE group_id = ? AND qq_id = ? AND theme_key = ?
                    """,
                    (group_id, qq_id, theme_key),
                ).fetchone()
                if unlocked is None:
                    theme = STATS_BACKGROUNDS[theme_key]
                    raise CommunityError(
                        f"尚未解锁「{theme.name}」，请先发送 #购买 {theme.shop_item}"
                    )
            now = self._now()
            connection.execute(
                """
                UPDATE member_stats_backgrounds SET selected = 0, updated_at = ?
                WHERE group_id = ? AND qq_id = ?
                """,
                (now, group_id, qq_id),
            )
            if theme_key != "classic":
                connection.execute(
                    """
                    UPDATE member_stats_backgrounds SET selected = 1, updated_at = ?
                    WHERE group_id = ? AND qq_id = ? AND theme_key = ?
                    """,
                    (now, group_id, qq_id, theme_key),
                )

    def use_custom_badge(self, group_id: str, qq_id: str, badge: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._remember_equipped_badge(connection, group_id, qq_id)
            owned = connection.execute(
                "SELECT 1 FROM member_badges WHERE group_id = ? AND qq_id = ? AND badge = ?",
                (group_id, qq_id, badge),
            ).fetchone()
            if owned is not None:
                connection.execute(
                    "UPDATE coin_wallets SET badge = ?, updated_at = ? "
                    "WHERE group_id = ? AND qq_id = ?", (badge, self._now(), group_id, qq_id),
                )
                return
            consumed = connection.execute(
                """
                UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ?
                WHERE group_id = ? AND qq_id = ? AND item_key = 'custom_badge'
                    AND quantity > 0
                """,
                (self._now(), group_id, qq_id),
            )
            if not consumed.rowcount:
                raise CommunityError("背包里没有自选称号券，请先发送 #购买 5")
            self._ensure_wallet(connection, group_id, qq_id)
            self._store_badge(connection, group_id, qq_id, badge)
            connection.execute(
                """
                UPDATE coin_wallets SET badge = ?, updated_at = ?
                WHERE group_id = ? AND qq_id = ?
                """,
                (badge, self._now(), group_id, qq_id),
            )

    def quiz_hint(
        self,
        group_id: str,
        quiz_date: str,
        qq_id: str,
        question: QuizQuestion,
    ) -> tuple[str, ...]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT choices_json FROM grenade_quiz_hints
                WHERE group_id = ? AND quiz_date = ? AND qq_id = ?
                """,
                (group_id, quiz_date, qq_id),
            ).fetchone()
            if existing is not None:
                return tuple(str(value) for value in json.loads(existing["choices_json"]))
            answered = connection.execute(
                """
                SELECT 1 FROM grenade_quiz_answers
                WHERE group_id = ? AND quiz_date = ? AND qq_id = ?
                """,
                (group_id, quiz_date, qq_id),
            ).fetchone()
            if answered is not None:
                raise CommunityError("你今天已经作答，提示卡留到下一题再用吧")
            consumed = connection.execute(
                """
                UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ?
                WHERE group_id = ? AND qq_id = ? AND item_key = 'quiz_hint'
                    AND quantity > 0
                """,
                (self._now(), group_id, qq_id),
            )
            if not consumed.rowcount:
                raise CommunityError("背包里没有答题提示卡，请先发送 #购买 6")
            letters = tuple(chr(65 + index) for index in range(len(question.options)))
            wrong = tuple(letter for letter in letters if letter != question.answer)
            digest = hashlib.sha256(
                f"{group_id}:{quiz_date}:{qq_id}:{question.key}:hint".encode()
            ).digest()
            decoy = wrong[int.from_bytes(digest[:4], "big") % len(wrong)]
            choices = tuple(sorted((question.answer, decoy)))
            connection.execute(
                """
                INSERT INTO grenade_quiz_hints
                    (group_id, quiz_date, qq_id, choices_json, used_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (group_id, quiz_date, qq_id, json.dumps(choices), self._now()),
            )
        return choices

    def active_prediction(self, group_id: str) -> Prediction | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM predictions
                WHERE group_id = ? AND status = 'open'
                ORDER BY prediction_id DESC LIMIT 1
                """,
                (group_id,),
            ).fetchone()
        return self._prediction(row) if row else None

    def prediction_by_id(self, prediction_id: int) -> Prediction | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM predictions WHERE prediction_id = ?",
                (prediction_id,),
            ).fetchone()
        return self._prediction(row) if row else None

    @staticmethod
    def _prediction(row: sqlite3.Row) -> Prediction:
        options = json.loads(str(row["options_json"]))
        return Prediction(
            prediction_id=int(row["prediction_id"]),
            group_id=str(row["group_id"]),
            question=str(row["question"]),
            options=tuple(str(item) for item in options),
            created_by=str(row["created_by"]),
            closes_at=str(row["closes_at"]),
            status=str(row["status"]),
        )

    def create_prediction(
        self,
        group_id: str,
        qq_id: str,
        question: str,
        options: tuple[str, ...],
        *,
        duration_minutes: int,
    ) -> Prediction:
        if self.active_prediction(group_id) is not None:
            raise CommunityError("本群已有进行中的竞猜，请先开奖或取消")
        now = datetime.now(UTC)
        closes = now + timedelta(minutes=duration_minutes)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO predictions
                    (group_id, question, options_json, created_by, closes_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'open', ?)
                """,
                (
                    group_id,
                    question,
                    json.dumps(options, ensure_ascii=False),
                    qq_id,
                    closes.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            prediction_id = int(cursor.lastrowid)
        return Prediction(
            prediction_id,
            group_id,
            question,
            options,
            qq_id,
            closes.isoformat(timespec="seconds"),
            "open",
        )

    def auto_prediction_day_exists(self, group_id: str, prediction_date: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM auto_prediction_days WHERE group_id = ? AND prediction_date = ?",
                (group_id, prediction_date),
            ).fetchone()
        return row is not None

    def mark_auto_prediction_day(
        self, group_id: str, prediction_date: str, outcome: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO auto_prediction_days
                    (group_id, prediction_date, outcome, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, prediction_date) DO UPDATE SET
                    outcome = excluded.outcome, updated_at = excluded.updated_at
                """,
                (group_id, prediction_date, outcome, self._now()),
            )

    @staticmethod
    def _auto_prediction(row: sqlite3.Row) -> AutoPredictionRun:
        return AutoPredictionRun(
            prediction_id=int(row["prediction_id"]),
            group_id=str(row["group_id"]),
            prediction_date=str(row["prediction_date"]),
            qq_id=str(row["qq_id"]),
            steam_id=str(row["steam_id"]),
            nickname=str(row["nickname"]),
            baseline_match_id=str(row["baseline_match_id"]),
            baseline_timestamp=int(row["baseline_timestamp"]),
            kind=str(row["kind"]),
            threshold=(None if row["threshold"] is None else float(row["threshold"])),
            expires_at=str(row["expires_at"]),
            status=str(row["auto_status"] if "auto_status" in row.keys() else row["status"]),
            winning_option=(
                None if row["winning_option"] is None else int(row["winning_option"])
            ),
            result_match_id=str(row["result_match_id"]),
            result_value=str(row["result_value"]),
            announcement_sent=bool(row["announcement_sent"]),
            result_sent=bool(row["result_sent"]),
        )

    def create_auto_prediction(
        self,
        group_id: str,
        prediction_date: str,
        qq_id: str,
        steam_id: str,
        nickname: str,
        baseline_match_id: str,
        baseline_timestamp: int,
        kind: str,
        threshold: float | None,
        question: str,
        options: tuple[str, ...],
        *,
        duration_minutes: int,
        expires_at: str,
    ) -> tuple[Prediction, AutoPredictionRun]:
        now = datetime.now(UTC)
        closes = now + timedelta(minutes=duration_minutes)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM predictions WHERE group_id = ? AND status = 'open'",
                (group_id,),
            ).fetchone():
                raise CommunityError("本群已有进行中的竞猜，请先开奖或取消")
            cursor = connection.execute(
                """
                INSERT INTO predictions
                    (group_id, question, options_json, created_by, closes_at, status, created_at)
                VALUES (?, ?, ?, 'bot:auto', ?, 'open', ?)
                """,
                (
                    group_id,
                    question,
                    json.dumps(options, ensure_ascii=False),
                    closes.isoformat(timespec="seconds"),
                    now.isoformat(timespec="seconds"),
                ),
            )
            prediction_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO auto_prediction_runs
                    (prediction_id, group_id, prediction_date, qq_id, steam_id, nickname,
                     baseline_match_id, baseline_timestamp, kind, threshold, expires_at,
                     status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?)
                """,
                (
                    prediction_id,
                    group_id,
                    prediction_date,
                    qq_id,
                    steam_id,
                    nickname,
                    baseline_match_id,
                    baseline_timestamp,
                    kind,
                    threshold,
                    expires_at,
                    now.isoformat(timespec="seconds"),
                ),
            )
            connection.execute(
                """
                INSERT INTO auto_prediction_days
                    (group_id, prediction_date, outcome, updated_at)
                VALUES (?, ?, 'created', ?)
                """,
                (group_id, prediction_date, now.isoformat(timespec="seconds")),
            )
        prediction = Prediction(
            prediction_id,
            group_id,
            question,
            options,
            "bot:auto",
            closes.isoformat(timespec="seconds"),
            "open",
        )
        run = self.auto_prediction_by_id(prediction_id)
        if run is None:
            raise RuntimeError("auto prediction was not persisted")
        return prediction, run

    def auto_prediction_by_id(self, prediction_id: int) -> AutoPredictionRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
                FROM auto_prediction_runs WHERE prediction_id = ?
                """,
                (prediction_id,),
            ).fetchone()
        return self._auto_prediction(row) if row else None

    def waiting_auto_prediction(
        self, group_id: str, steam_id: str = ""
    ) -> AutoPredictionRun | None:
        query = """
            SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
            FROM auto_prediction_runs
            WHERE group_id = ? AND status = 'waiting'
        """
        params: list[Any] = [group_id]
        if steam_id:
            query += " AND steam_id = ?"
            params.append(steam_id)
        query += " ORDER BY prediction_id DESC LIMIT 1"
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._auto_prediction(row) if row else None

    def legacy_waiting_auto_predictions(self) -> tuple[AutoPredictionRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
                FROM auto_prediction_runs
                WHERE status = 'waiting' AND prediction_date NOT LIKE 'live:%'
                ORDER BY prediction_id
                """
            ).fetchall()
        return tuple(self._auto_prediction(row) for row in rows)

    def steam_presence_state(
        self, group_id: str, steam_id: str
    ) -> SteamPresenceState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM steam_presence_states
                WHERE group_id = ? AND steam_id = ?
                """,
                (group_id, steam_id),
            ).fetchone()
        if row is None:
            return None
        return SteamPresenceState(
            group_id=str(row["group_id"]),
            steam_id=str(row["steam_id"]),
            game_id=str(row["game_id"]),
            server_ip=str(row["server_ip"]),
            observed_at=str(row["observed_at"]),
        )

    def set_steam_presence_state(
        self,
        group_id: str,
        steam_id: str,
        game_id: str,
        server_ip: str,
        *,
        observed_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO steam_presence_states
                    (group_id, steam_id, game_id, server_ip, observed_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(group_id, steam_id) DO UPDATE SET
                    game_id = excluded.game_id,
                    server_ip = excluded.server_ip,
                    observed_at = excluded.observed_at
                """,
                (group_id, steam_id, game_id, server_ip, observed_at),
            )

    def watch_stage_state(
        self, group_id: str, steam_id: str
    ) -> WatchStageState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM watch_stage_states
                WHERE group_id = ? AND steam_id = ?
                """,
                (group_id, steam_id),
            ).fetchone()
        if row is None:
            return None
        return WatchStageState(
            group_id=str(row["group_id"]),
            steam_id=str(row["steam_id"]),
            match_id=str(row["match_id"]),
            platform=str(row["platform"]),
            map_name=str(row["map_name"]),
            ct_score=None if row["ct_score"] is None else int(row["ct_score"]),
            terrorist_score=(
                None
                if row["terrorist_score"] is None
                else int(row["terrorist_score"])
            ),
            roster_key=str(row["roster_key"]),
            observed_at=str(row["observed_at"]),
        )

    def set_watch_stage_state(
        self,
        group_id: str,
        steam_id: str,
        match_id: str,
        *,
        platform: str = "",
        map_name: str = "",
        ct_score: int | None = None,
        terrorist_score: int | None = None,
        roster_key: str = "",
        observed_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO watch_stage_states
                    (group_id, steam_id, match_id, platform, map_name,
                     ct_score, terrorist_score, roster_key, observed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(group_id, steam_id) DO UPDATE SET
                    match_id = excluded.match_id,
                    platform = excluded.platform,
                    map_name = excluded.map_name,
                    ct_score = excluded.ct_score,
                    terrorist_score = excluded.terrorist_score,
                    roster_key = excluded.roster_key,
                    observed_at = excluded.observed_at
                """,
                (
                    group_id,
                    steam_id,
                    match_id,
                    platform,
                    map_name,
                    ct_score,
                    terrorist_score,
                    roster_key,
                    observed_at,
                ),
            )

    def pending_auto_announcements(self) -> tuple[AutoPredictionRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
                FROM auto_prediction_runs
                WHERE status = 'waiting' AND announcement_sent = 0
                ORDER BY prediction_id
                """
            ).fetchall()
        return tuple(self._auto_prediction(row) for row in rows)

    def pending_auto_results(self) -> tuple[AutoPredictionRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
                FROM auto_prediction_runs
                WHERE status IN ('settled', 'expired', 'void') AND result_sent = 0
                ORDER BY prediction_id
                """
            ).fetchall()
        return tuple(self._auto_prediction(row) for row in rows)

    def mark_auto_announcement_sent(self, prediction_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auto_prediction_runs SET announcement_sent = 1 WHERE prediction_id = ?",
                (prediction_id,),
            )

    def mark_auto_result_sent(self, prediction_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE auto_prediction_runs SET result_sent = 1 WHERE prediction_id = ?",
                (prediction_id,),
            )

    def prediction_bets(self, prediction_id: int) -> list[tuple[str, int, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, option_index, stake FROM prediction_bets
                WHERE prediction_id = ? ORDER BY created_at
                """,
                (prediction_id,),
            ).fetchall()
        return [
            (str(row["qq_id"]), int(row["option_index"]), int(row["stake"]))
            for row in rows
        ]

    def prediction_insurance_refunds(
        self, prediction_id: int
    ) -> list[tuple[str, int]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, delta FROM coin_ledger
                WHERE reference_key = ? AND delta > 0
                ORDER BY ledger_id
                """,
                (f"prediction:{prediction_id}:insurance",),
            ).fetchall()
        return [(str(row["qq_id"]), int(row["delta"])) for row in rows]

    def place_bet(self, group_id: str, qq_id: str, option_index: int, stake: int) -> int:
        prediction = self.active_prediction(group_id)
        if prediction is None:
            raise CommunityError("本群当前没有进行中的竞猜")
        if datetime.now(UTC) >= datetime.fromisoformat(prediction.closes_at):
            raise CommunityError("竞猜已经截止，等待管理员开奖")
        if not 1 <= option_index <= len(prediction.options):
            raise CommunityError("选项编号不存在")
        if not 1 <= stake <= 100_000:
            raise CommunityError("下注数量必须为 1～100000 牛币")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM prediction_bets WHERE prediction_id = ? AND qq_id = ?",
                (prediction.prediction_id, qq_id),
            ).fetchone()
            if existing:
                raise CommunityError("每轮竞猜只能下注一次")
            self._change_balance(
                connection,
                group_id,
                qq_id,
                -stake,
                f"竞猜 #{prediction.prediction_id} 下注",
                f"prediction:{prediction.prediction_id}:bet",
            )
            connection.execute(
                """
                INSERT INTO prediction_bets
                    (prediction_id, qq_id, option_index, stake, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (prediction.prediction_id, qq_id, option_index, stake, self._now()),
            )
            row = connection.execute(
                "SELECT balance FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return int(row["balance"])

    def settle_prediction(
        self, group_id: str, option_index: int
    ) -> tuple[
        Prediction,
        int,
        int,
        list[tuple[str, int]],
        list[tuple[str, int]],
    ]:
        prediction = self.active_prediction(group_id)
        if prediction is None:
            raise CommunityError("本群当前没有待开奖的竞猜")
        if not 1 <= option_index <= len(prediction.options):
            raise CommunityError("正确选项编号不存在")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT qq_id, option_index, stake FROM prediction_bets WHERE prediction_id = ?",
                (prediction.prediction_id,),
            ).fetchall()
            total_pool = sum(int(row["stake"]) for row in rows)
            winners = [row for row in rows if int(row["option_index"]) == option_index]
            payouts: list[tuple[str, int]] = []
            insurance_refunds: list[tuple[str, int]] = []
            if winners:
                winning_stake = sum(int(row["stake"]) for row in winners)
                remaining = total_pool
                for index, row in enumerate(sorted(winners, key=lambda item: str(item["qq_id"]))):
                    payout = (
                        remaining
                        if index == len(winners) - 1
                        else total_pool * int(row["stake"]) // winning_stake
                    )
                    remaining -= payout
                    qq_id = str(row["qq_id"])
                    self._change_balance(
                        connection,
                        group_id,
                        qq_id,
                        payout,
                        f"竞猜 #{prediction.prediction_id} 奖金",
                        f"prediction:{prediction.prediction_id}:payout",
                    )
                    payouts.append((qq_id, payout))
                losers = [
                    row for row in rows if int(row["option_index"]) != option_index
                ]
                for row in losers:
                    qq_id = str(row["qq_id"])
                    consumed = connection.execute(
                        """
                        UPDATE coin_inventory
                        SET quantity = quantity - 1, updated_at = ?
                        WHERE group_id = ? AND qq_id = ?
                            AND item_key = 'prediction_insurance' AND quantity > 0
                        """,
                        (self._now(), group_id, qq_id),
                    )
                    if not consumed.rowcount:
                        continue
                    refund = min(50, max(1, (int(row["stake"]) + 1) // 2))
                    self._change_balance(
                        connection,
                        group_id,
                        qq_id,
                        refund,
                        f"竞猜 #{prediction.prediction_id} 保险理赔",
                        f"prediction:{prediction.prediction_id}:insurance",
                    )
                    insurance_refunds.append((qq_id, refund))
            else:
                for row in rows:
                    qq_id = str(row["qq_id"])
                    stake = int(row["stake"])
                    self._change_balance(
                        connection,
                        group_id,
                        qq_id,
                        stake,
                        f"竞猜 #{prediction.prediction_id} 无人猜中退款",
                        f"prediction:{prediction.prediction_id}:refund",
                    )
                    payouts.append((qq_id, stake))
            connection.execute(
                """
                UPDATE predictions SET status = 'settled', winning_option = ?, settled_at = ?
                WHERE prediction_id = ? AND status = 'open'
                """,
                (option_index, self._now(), prediction.prediction_id),
            )
            connection.execute(
                """
                UPDATE auto_prediction_runs
                SET status = 'settled', result_value = '管理员手动开奖', result_sent = 1,
                    completed_at = ?
                WHERE prediction_id = ? AND status = 'waiting'
                """,
                (self._now(), prediction.prediction_id),
            )
        return prediction, total_pool, len(winners), payouts, insurance_refunds

    def cancel_prediction(self, group_id: str) -> tuple[Prediction, int]:
        prediction = self.active_prediction(group_id)
        if prediction is None:
            raise CommunityError("本群当前没有可取消的竞猜")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT qq_id, stake FROM prediction_bets WHERE prediction_id = ?",
                (prediction.prediction_id,),
            ).fetchall()
            for row in rows:
                self._change_balance(
                    connection,
                    group_id,
                    str(row["qq_id"]),
                    int(row["stake"]),
                    f"竞猜 #{prediction.prediction_id} 取消退款",
                    f"prediction:{prediction.prediction_id}:cancel",
                )
            connection.execute(
                """
                UPDATE predictions SET status = 'cancelled', settled_at = ?
                WHERE prediction_id = ? AND status = 'open'
                """,
                (self._now(), prediction.prediction_id),
            )
            connection.execute(
                """
                UPDATE auto_prediction_runs
                SET status = 'void', result_value = '管理员手动取消', result_sent = 1,
                    completed_at = ?
                WHERE prediction_id = ? AND status = 'waiting'
                """,
                (self._now(), prediction.prediction_id),
            )
        return prediction, len(rows)

    def cancel_all_open_predictions(self) -> tuple[int, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT group_id FROM predictions
                WHERE status = 'open'
                ORDER BY prediction_id
                """
            ).fetchall()
        cancelled = 0
        refunded = 0
        for row in rows:
            try:
                _prediction, refund_count = self.cancel_prediction(str(row["group_id"]))
            except CommunityError:
                continue
            cancelled += 1
            refunded += refund_count
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auto_prediction_runs
                SET status = 'void', result_value = '竞猜功能已关闭',
                    result_sent = 1, completed_at = ?
                WHERE status = 'waiting'
                """,
                (self._now(),),
            )
        return cancelled, refunded

    def settle_auto_prediction(
        self,
        prediction_id: int,
        option_index: int,
        *,
        match_id: str,
        result_value: str,
    ) -> tuple[
        Prediction,
        int,
        int,
        list[tuple[str, int]],
        list[tuple[str, int]],
    ]:
        run = self.auto_prediction_by_id(prediction_id)
        if run is None or run.status != "waiting":
            raise CommunityError("自动竞猜已经处理")
        active = self.active_prediction(run.group_id)
        if active is None or active.prediction_id != prediction_id:
            raise CommunityError("自动竞猜已不再开放")
        result = self.settle_prediction(run.group_id, option_index)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auto_prediction_runs
                SET status = 'settled', winning_option = ?, result_match_id = ?, result_value = ?,
                    result_sent = 0, completed_at = ?
                WHERE prediction_id = ?
                """,
                (option_index, match_id, result_value, self._now(), prediction_id),
            )
        return result

    def cancel_auto_prediction(
        self,
        prediction_id: int,
        *,
        status: str,
        result_value: str,
    ) -> tuple[Prediction, int]:
        if status not in {"expired", "void"}:
            raise ValueError("invalid auto prediction cancellation status")
        run = self.auto_prediction_by_id(prediction_id)
        if run is None or run.status != "waiting":
            raise CommunityError("自动竞猜已经处理")
        active = self.active_prediction(run.group_id)
        if active is None or active.prediction_id != prediction_id:
            raise CommunityError("自动竞猜已不再开放")
        result = self.cancel_prediction(run.group_id)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE auto_prediction_runs
                SET status = ?, result_value = ?, result_sent = 0, completed_at = ?
                WHERE prediction_id = ?
                """,
                (status, result_value, self._now(), prediction_id),
            )
        return result

    def expired_auto_predictions(self, now: str) -> tuple[AutoPredictionRun, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT auto_prediction_runs.*, auto_prediction_runs.status AS auto_status
                FROM auto_prediction_runs
                WHERE status = 'waiting' AND expires_at <= ?
                ORDER BY prediction_id
                """,
                (now,),
            ).fetchall()
        return tuple(self._auto_prediction(row) for row in rows)

    def ensure_quiz_run(self, group_id: str, quiz_date: str, question_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO grenade_quiz_runs
                    (group_id, quiz_date, question_key, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (group_id, quiz_date, question_key, self._now()),
            )

    def quiz_run(self, group_id: str, quiz_date: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM grenade_quiz_runs WHERE group_id = ? AND quiz_date = ?",
                (group_id, quiz_date),
            ).fetchone()

    def mark_quiz_question_sent(self, group_id: str, quiz_date: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE grenade_quiz_runs SET question_sent = 1, updated_at = ?
                WHERE group_id = ? AND quiz_date = ?
                """,
                (self._now(), group_id, quiz_date),
            )

    def skip_quiz(self, group_id: str, quiz_date: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE grenade_quiz_runs
                SET revealed = 1, reveal_sent = 1, updated_at = ?
                WHERE group_id = ? AND quiz_date = ?
                """,
                (self._now(), group_id, quiz_date),
            )

    def submit_quiz_answer(
        self, group_id: str, quiz_date: str, qq_id: str, nickname: str, choice: str
    ) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO grenade_quiz_answers
                        (group_id, quiz_date, qq_id, nickname, choice, answered_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (group_id, quiz_date, qq_id, nickname, choice, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise CommunityError("今天已经作答，不能修改答案") from exc

    def reveal_quiz(
        self,
        group_id: str,
        quiz_date: str,
        question: QuizQuestion,
        *,
        correct_reward: int,
        fastest_bonus: int,
    ) -> QuizReveal:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT revealed FROM grenade_quiz_runs WHERE group_id = ? AND quiz_date = ?",
                (group_id, quiz_date),
            ).fetchone()
            if run is None or int(run["revealed"]):
                raise CommunityError("今日题目已经揭晓")
            answers = connection.execute(
                """
                SELECT qq_id, nickname, choice FROM grenade_quiz_answers
                WHERE group_id = ? AND quiz_date = ? ORDER BY answered_at, qq_id
                """,
                (group_id, quiz_date),
            ).fetchall()
            correct = [row for row in answers if str(row["choice"]) == question.answer]
            rewards: list[tuple[str, int]] = []
            for index, row in enumerate(correct):
                qq_id = str(row["qq_id"])
                amount = correct_reward + (fastest_bonus if index == 0 else 0)
                doubled = connection.execute(
                    """
                    UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ?
                    WHERE group_id = ? AND qq_id = ? AND item_key = 'double_quiz'
                        AND quantity > 0
                    """,
                    (self._now(), group_id, qq_id),
                ).rowcount
                if doubled:
                    amount *= 2
                self._change_balance(
                    connection,
                    group_id,
                    qq_id,
                    amount,
                    f"{quiz_date} 职业选手答题奖励",
                    f"quiz:{quiz_date}",
                )
                connection.execute(
                    """
                    UPDATE grenade_quiz_answers SET reward = ?
                    WHERE group_id = ? AND quiz_date = ? AND qq_id = ?
                    """,
                    (amount, group_id, quiz_date, qq_id),
                )
                rewards.append((qq_id, amount))
            connection.execute(
                """
                UPDATE grenade_quiz_runs SET revealed = 1, updated_at = ?
                WHERE group_id = ? AND quiz_date = ?
                """,
                (self._now(), group_id, quiz_date),
            )
        names = tuple(str(row["nickname"]) for row in correct)
        return QuizReveal(
            group_id=group_id,
            quiz_date=quiz_date,
            question=question,
            correct_names=names,
            fastest_name=names[0] if names else "",
            rewards=tuple(rewards),
        )

    def quiz_reveal_snapshot(
        self, group_id: str, quiz_date: str, question: QuizQuestion
    ) -> QuizReveal:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT qq_id, nickname, reward FROM grenade_quiz_answers
                WHERE group_id = ? AND quiz_date = ? AND reward > 0
                ORDER BY answered_at, qq_id
                """,
                (group_id, quiz_date),
            ).fetchall()
        names = tuple(str(row["nickname"]) for row in rows)
        return QuizReveal(
            group_id=group_id,
            quiz_date=quiz_date,
            question=question,
            correct_names=names,
            fastest_name=names[0] if names else "",
            rewards=tuple((str(row["qq_id"]), int(row["reward"])) for row in rows),
        )

    def mark_quiz_reveal_sent(self, group_id: str, quiz_date: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE grenade_quiz_runs SET reveal_sent = 1, updated_at = ?
                WHERE group_id = ? AND quiz_date = ? AND revealed = 1
                """,
                (self._now(), group_id, quiz_date),
            )

    def museum_enabled(self, group_id: str, qq_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT enabled FROM museum_preferences WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
        return row is None or bool(row["enabled"])

    def set_museum_enabled(self, group_id: str, qq_id: str, *, enabled: bool) -> int:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO museum_preferences (group_id, qq_id, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, qq_id) DO UPDATE SET
                    enabled = excluded.enabled, updated_at = excluded.updated_at
                """,
                (group_id, qq_id, int(enabled), self._now()),
            )
            deleted = 0
            if not enabled:
                deleted = connection.execute(
                    "DELETE FROM museum_entries WHERE group_id = ? AND subject_qq = ?",
                    (group_id, qq_id),
                ).rowcount
        return deleted

    def add_museum_entry(
        self,
        group_id: str,
        collector_qq: str,
        source: HistorySource,
        note: str,
    ) -> int:
        if not self.museum_enabled(group_id, source.qq_id):
            raise CommunityError("对方已关闭黑历史收录")
        if self.consume_item(group_id, source.qq_id, "history_shield"):
            raise CommunityError("黑历史保护伞生效：本次收录已被拦截")
        quote = " ".join(source.text.split())[:300]
        if len(quote) < 2:
            raise CommunityError("回复消息里没有可收录的纯文本")
        with self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO museum_entries
                        (group_id, subject_qq, subject_name, collector_qq, quote, note,
                         source_message_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        group_id,
                        source.qq_id,
                        source.nickname,
                        collector_qq,
                        quote,
                        " ".join(note.split())[:80],
                        source.message_id,
                        self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CommunityError("这条消息已经被收录过了") from exc
        return int(cursor.lastrowid)

    def museum_entries(
        self, group_id: str, *, subject_qq: str = "", entry_id: int | None = None
    ) -> list[sqlite3.Row]:
        query = "SELECT * FROM museum_entries WHERE group_id = ?"
        params: list[Any] = [group_id]
        if entry_id is not None:
            query += " AND entry_id = ?"
            params.append(entry_id)
        if subject_qq:
            query += " AND subject_qq = ?"
            params.append(subject_qq)
        query += " ORDER BY created_at DESC LIMIT 100"
        with self._connect() as connection:
            return connection.execute(query, params).fetchall()

    def delete_museum_entry(
        self, group_id: str, entry_id: int, qq_id: str, *, is_admin: bool
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT subject_qq, collector_qq FROM museum_entries
                WHERE group_id = ? AND entry_id = ?
                """,
                (group_id, entry_id),
            ).fetchone()
            if row is None:
                return False
            if not is_admin and qq_id not in {
                str(row["subject_qq"]),
                str(row["collector_qq"]),
            }:
                raise CommunityError("只有当事人、收录者或管理员可以删除")
            connection.execute(
                "DELETE FROM museum_entries WHERE group_id = ? AND entry_id = ?",
                (group_id, entry_id),
            )
        return True

    def process_revenge(
        self,
        group_id: str,
        match_id: str,
        outcomes: list[tuple[str, str, str, str]],
    ) -> tuple[RevengeEvent, ...]:
        if not outcomes:
            return ()
        events: list[RevengeEvent] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO revenge_match_runs (group_id, match_id, processed_at)
                VALUES (?, ?, ?)
                """,
                (group_id, match_id, self._now()),
            )
            if not inserted.rowcount:
                return ()
            for winner_qq, winner_name, loser_qq, loser_name in outcomes:
                first_qq, second_qq = sorted((winner_qq, loser_qq))
                row = connection.execute(
                    """
                    SELECT * FROM revenge_rivalries
                    WHERE group_id = ? AND first_qq = ? AND second_qq = ?
                    """,
                    (group_id, first_qq, second_qq),
                ).fetchone()
                previous_streak = int(row["streak"]) if row else 0
                old_leader = str(row["leader_qq"]) if row else ""
                if not row:
                    kind = "new"
                    streak = 1
                elif old_leader == winner_qq:
                    kind = "streak"
                    streak = previous_streak + 1
                else:
                    kind = "revenge"
                    streak = 1
                horn = bool(
                    connection.execute(
                        """
                        UPDATE coin_inventory SET quantity = quantity - 1, updated_at = ?
                        WHERE group_id = ? AND qq_id = ? AND item_key = 'revenge_horn'
                            AND quantity > 0
                        """,
                        (self._now(), group_id, loser_qq),
                    ).rowcount
                )
                connection.execute(
                    """
                    INSERT INTO revenge_rivalries
                        (group_id, first_qq, second_qq, leader_qq, leader_name,
                         challenger_name, streak, total_matches, last_match_id, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(group_id, first_qq, second_qq) DO UPDATE SET
                        leader_qq = excluded.leader_qq,
                        leader_name = excluded.leader_name,
                        challenger_name = excluded.challenger_name,
                        streak = excluded.streak,
                        total_matches = total_matches + 1,
                        last_match_id = excluded.last_match_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        group_id,
                        first_qq,
                        second_qq,
                        winner_qq,
                        winner_name,
                        loser_name,
                        streak,
                        match_id,
                        self._now(),
                    ),
                )
                events.append(
                    RevengeEvent(
                        winner_qq,
                        winner_name,
                        loser_qq,
                        loser_name,
                        kind,
                        previous_streak,
                        streak,
                        horn,
                    )
                )
        return tuple(events)

    def revenge_ranking(self, group_id: str, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as connection:
            return connection.execute(
                """
                SELECT * FROM revenge_rivalries WHERE group_id = ?
                ORDER BY streak DESC, total_matches DESC, updated_at DESC LIMIT ?
                """,
                (group_id, limit),
            ).fetchall()

    def weekly_report_processed(self, group_id: str, week_start: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM weekly_report_runs WHERE group_id = ? AND week_start = ?",
                (group_id, week_start),
            ).fetchone()
        return row is not None

    def mark_weekly_report(
        self, group_id: str, week_start: str, *, was_sent: bool
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO weekly_report_runs
                    (group_id, week_start, was_sent, completed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(group_id, week_start) DO UPDATE SET
                    was_sent = excluded.was_sent, completed_at = excluded.completed_at
                """,
                (group_id, week_start, int(was_sent), self._now()),
            )
            connection.execute(
                "DELETE FROM weekly_report_runs WHERE completed_at < ?",
                ((datetime.now(UTC) - timedelta(days=180)).isoformat(timespec="seconds"),),
            )

    def remove_member(self, group_id: str, qq_id: str) -> None:
        with self._connect() as connection:
            binding_row = connection.execute(
                "SELECT steam_id FROM bindings WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            ).fetchone()
            connection.execute(
                "DELETE FROM coin_wallets WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM coin_inventory WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM daily_checkins WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM match_coin_rewards WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM weekly_coin_rewards WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM member_stats_backgrounds WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM member_badges WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM shop_daily_purchases WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM museum_preferences WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM museum_entries WHERE group_id = ? AND subject_qq = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM grenade_quiz_answers WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                "DELETE FROM grenade_quiz_hints WHERE group_id = ? AND qq_id = ?",
                (group_id, qq_id),
            )
            connection.execute(
                """
                DELETE FROM revenge_rivalries
                WHERE group_id = ? AND (first_qq = ? OR second_qq = ?)
                """,
                (group_id, qq_id, qq_id),
            )
            if binding_row is not None:
                connection.execute(
                    "DELETE FROM steam_presence_states WHERE group_id = ? AND steam_id = ?",
                    (group_id, str(binding_row["steam_id"])),
                )
                connection.execute(
                    "DELETE FROM watch_stage_states WHERE group_id = ? AND steam_id = ?",
                    (group_id, str(binding_row["steam_id"])),
                )


class GroupPlayService:
    def __init__(
        self,
        repository: CommunityRepository,
        bindings: BindingRepository,
        *,
        prediction_enabled: bool = True,
        prediction_duration_minutes: int = 30,
        manager_qq_ids: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        self.repository = repository
        self.bindings = bindings
        self.prediction_enabled = prediction_enabled
        self.prediction_duration_minutes = prediction_duration_minutes
        self.manager_qq_ids = {str(value) for value in (manager_qq_ids or ()) if str(value)}

    def _is_admin(self, role: str, qq_id: str = "") -> bool:
        return role.casefold() in {"owner", "admin"} or qq_id in self.manager_qq_ids

    def _require_admin(self, role: str, qq_id: str) -> None:
        if not self._is_admin(role, qq_id):
            raise CommunityError("只有群主、管理员或机器人授权管理员可以执行此操作")

    def _require_prediction_enabled(self) -> None:
        if not self.prediction_enabled:
            raise CommunityError("竞猜功能已关闭")

    def wallet(self, group_id: str, qq_id: str) -> str:
        balance, badge = self.repository.wallet(group_id, qq_id)
        suffix = f"｜称号：{badge}" if badge else ""
        return f"🪙 你的牛币：{balance}{suffix}"

    def checkin(self, group_id: str, qq_id: str, *, checkin_date: str) -> str:
        current_date = checkin_date or datetime.now(CHINA_TIMEZONE).date().isoformat()
        result = self.repository.checkin(group_id, qq_id, current_date)
        if result.already_checked_in:
            return (
                f"📅 今天已经签到过了｜连续 {result.streak} 天\n"
                f"当前余额：{result.balance} 牛币"
            )
        lines = [
            f"📅 签到成功｜连续 {result.streak} 天",
            f"获得 +{result.base_reward} 牛币",
        ]
        if result.streak_bonus:
            lines.append(
                f"🔥 连续 {CHECKIN_STREAK_DAYS} 天奖励 "
                f"+{result.streak_bonus} 牛币"
            )
        lines.append(f"当前余额：{result.balance} 牛币")
        return "\n".join(lines)

    def coin_earning_guide(self) -> str:
        guide = (
            "🪙 牛币获取方式\n"
            f"• #签到：每天 +{CHECKIN_REWARD}；每连续 "
            f"{CHECKIN_STREAK_DAYS} 天额外 +{CHECKIN_STREAK_BONUS}\n"
            f"• 完成比赛：每天前 {MATCH_DAILY_LIMIT} 场各 +{MATCH_REWARD}\n"
            f"• 今日首胜：额外 +{FIRST_WIN_BONUS}\n"
            f"• 每周完成 {WEEKLY_ACTIVITY_MATCHES} 场：周报时 +"
            f"{WEEKLY_ACTIVITY_REWARD}\n"
            "• 职业选手题：答对 +20，最快答对额外 +10\n"
            "• #挑战 领取：战绩达标主动领奖；完美/官匹各每天最多 +35"
        )
        if self.prediction_enabled:
            guide += "\n• 竞猜只重新分配奖池，不会凭空增加牛币"
        return guide

    def coin_ranking(self, group_id: str) -> str:
        rows = self.repository.coin_ranking(group_id)
        if not rows:
            return "本群还没有牛币账户"
        names = self._names(group_id)
        lines = ["🪙 牛币富豪榜"]
        for index, (qq_id, balance, badge) in enumerate(rows, 1):
            name = names.get(qq_id) or f"QQ {qq_id}"
            suffix = f"「{badge}」" if badge else ""
            lines.append(f"{index}. {name}{suffix}：{balance}")
        return "\n".join(lines)

    def coin_ledger(self, group_id: str, qq_id: str) -> str:
        rows = self.repository.coin_ledger(group_id, qq_id)
        balance, _badge = self.repository.wallet(group_id, qq_id)
        lines = [f"📒 牛币流水｜余额 {balance}"]
        for delta, reason, created_at in rows:
            timestamp = (
                datetime.fromisoformat(created_at)
                .astimezone(CHINA_TIMEZONE)
                .strftime("%m-%d %H:%M")
            )
            lines.append(f"{timestamp}｜{delta:+d}｜{reason}")
        return "\n".join(lines)

    def shop(
        self, group_id: str, qq_id: str, category: str = "", *, shop_date: str = "",
    ) -> str:
        category = category.strip()
        if category == "特惠":
            return self.specials(group_id, qq_id, shop_date=shop_date)
        if category and category not in SHOP_CATEGORIES:
            raise CommunityError("商店分类：#商店 道具 / 称号 / 背景 / 特惠")
        balance, _badge = self.repository.wallet(group_id, qq_id)
        unlocked = self.repository.unlocked_stats_backgrounds(group_id, qq_id)
        lines = [f"🛒 牛币商店 2.0｜余额 {balance}"]
        for section, keys in SHOP_CATEGORIES.items():
            if category and category != section:
                continue
            lines.append(f"\n【{section}】")
            for key in keys:
                if key == "7" and not self.prediction_enabled:
                    continue
                name, cost, description = SHOP_ITEMS[key]
                owned = "｜已拥有" if STATS_BACKGROUND_SHOP_ITEMS.get(key) in unlocked else ""
                lines.append(f"{key}. {name}｜{cost} 牛币{owned}\n   {description}")
        lines.extend([
            "\n购买：#购买 <编号或名称> [数量]；消耗品最多 20 份",
            "每日八折：#特惠｜赠送：#赠送 @群友 道具 [数量]",
            "收藏与换装：#称号 / #佩戴称号 / #战绩背景",
        ])
        return "\n".join(lines)

    @staticmethod
    def _shop_order(argument: str) -> tuple[str, int]:
        value = argument.strip()
        quantity = 1
        match = re.fullmatch(r"(.+?)(?:\s+|\s*[x×*]\s*)(\d{1,2})", value)
        if match:
            value, number = match.groups()
            quantity = int(number)
        CommunityRepository._validate_quantity(quantity)
        return resolve_shop_item(value), quantity

    def specials(self, group_id: str, qq_id: str, *, shop_date: str = "") -> str:
        day = shop_date or datetime.now(CHINA_TIMEZONE).date().isoformat()
        offers = daily_offers(group_id, day)
        bought = self.repository.daily_purchases(group_id, qq_id, day)
        balance, _badge = self.repository.wallet(group_id, qq_id)
        lines = [f"🏷 今日特惠｜{day}｜余额 {balance}"]
        for key, price in offers.items():
            name, original, _description = SHOP_ITEMS[key]
            state = "已购买" if key in bought else "可购买 1 份"
            lines.append(f"{key}. {name}｜{original} → {price} 牛币｜{state}")
        lines.extend([
            "#购买 特惠 <编号或名称>",
            "每天每人每款限 1 份，北京时间 00:00 更新；不会自动扣币。",
        ])
        return "\n".join(lines)

    def buy(
        self, group_id: str, qq_id: str, argument: str, *, shop_date: str = "",
        request_id: str = "",
    ) -> str:
        value = argument.strip()
        discounted = value.startswith("特惠")
        if discounted:
            value = value[2:].strip()
        key, quantity = self._shop_order(value)
        if key == "7" and not self.prediction_enabled:
            raise CommunityError("竞猜功能已关闭，竞猜保险暂不出售")
        name, cost, result = self.repository.purchase(
            group_id, qq_id, key, quantity=quantity, discounted=discounted,
            shop_date=shop_date, request_id=request_id,
        )
        balance, _badge = self.repository.wallet(group_id, qq_id)
        return f"购买成功：{name}（-{cost}）\n{result}｜余额 {balance} 牛币"

    def gift(self, group_id: str, qq_id: str, argument: str, *, request_id: str = "") -> str:
        match = re.fullmatch(r"\s*@?([1-9]\d{4,11})\s+(.+)", argument)
        if not match:
            raise CommunityError("格式：#赠送 @群友 <道具编号或名称> [数量]")
        recipient, item = match.groups()
        names = self._names(group_id)
        if recipient not in names:
            raise CommunityError("赠送对象不是当前群成员")
        key, quantity = self._shop_order(item)
        if key == "7" and not self.prediction_enabled:
            raise CommunityError("竞猜功能已关闭，竞猜保险暂不支持赠送")
        name, quantity = self.repository.gift_item(
            group_id, qq_id, recipient, key, quantity=quantity, request_id=request_id,
        )
        return (
            f"🎁 已将背包中的 {name} ×{quantity} "
            f"赠送给 {names[recipient] or recipient}，未扣牛币。"
        )

    def badge_collection(self, group_id: str, qq_id: str) -> str:
        badges = self.repository.owned_badges(group_id, qq_id)
        current = self.equipped_badge(group_id, qq_id)
        lines = [f"🏷 我的称号收藏｜{len(badges)} 个｜当前：{current or '未佩戴'}"]
        lines.extend(
            f"{index}. {badge}{'（佩戴中）' if badge == current else ''}"
            for index, badge in enumerate(badges, 1)
        )
        if not badges:
            lines.append("暂无收藏；可买称号盲盒或使用自选称号券。")
        lines.append("免费换装：#佩戴称号 <编号或完整称号>；#佩戴称号 0 卸下")
        return "\n".join(lines)

    def equip_badge(self, group_id: str, qq_id: str, argument: str) -> str:
        value = argument.strip()
        if not value:
            return self.badge_collection(group_id, qq_id)
        badges = self.repository.owned_badges(group_id, qq_id)
        if value == "0":
            badge = ""
        elif value in badges:
            badge = value
        elif value.isascii() and value.isdigit() and len(value) <= 6:
            index = int(value)
            if not 1 <= index <= len(badges):
                raise CommunityError("称号编号不存在，请发送 #称号 查看")
            badge = badges[index - 1]
        else:
            raise CommunityError("尚未拥有该称号，请发送 #称号 查看收藏")
        self.repository.equip_badge(group_id, qq_id, badge)
        return f"🏷 已免费佩戴「{badge}」" if badge else "已卸下称号，收藏仍保留。"

    def repair_checkin(
        self, group_id: str, qq_id: str, *, checkin_date: str = "", request_id: str = "",
    ) -> str:
        day = checkin_date or datetime.now(CHINA_TIMEZONE).date().isoformat()
        return self.repository.repair_checkin(group_id, qq_id, day, request_id=request_id)[2]

    def inventory(self, group_id: str, qq_id: str) -> str:
        items = self.repository.inventory(group_id, qq_id)
        unlocked_backgrounds = self.repository.unlocked_stats_backgrounds(
            group_id, qq_id
        )
        selected_background = self.repository.stats_background(group_id, qq_id)
        lines = ["🎒 你的背包"]
        lines.extend(
            f"• {INVENTORY_LABELS.get(key, key)} × {quantity}"
            for key, quantity in sorted(items.items())
        )
        cosmetic_names = [
            STATS_BACKGROUNDS[key].name
            for key in STATS_BACKGROUNDS
            if key != "classic" and key in unlocked_backgrounds
        ]
        if cosmetic_names:
            lines.append(f"• 永久背景：{'、'.join(cosmetic_names)}")
            lines.append(
                f"• 当前使用：{STATS_BACKGROUNDS[selected_background].name}"
            )
        badges = self.repository.owned_badges(group_id, qq_id)
        if badges:
            lines.append(f"• 永久称号：{len(badges)} 个；#称号 查看和换装")
        if len(lines) == 1:
            lines.append("空空如也，发送 #商店 看看")
        lines.append("#赠送 @群友 道具 [数量]；补签卡使用 #补签（先补签再签到）")
        return "\n".join(lines)

    def stats_background(self, group_id: str, qq_id: str, argument: str) -> str:
        target = argument.strip()
        if target:
            theme_key = resolve_stats_background(target)
            if theme_key is None:
                raise CommunityError("未知背景，可发送 #战绩背景 查看编号")
            self.repository.select_stats_background(group_id, qq_id, theme_key)
            theme = STATS_BACKGROUNDS[theme_key]
            return f"🎨 战绩背景已切换为「{theme.name}」"

        current = self.repository.stats_background(group_id, qq_id)
        unlocked = self.repository.unlocked_stats_backgrounds(group_id, qq_id)
        lines = [f"🎨 我的战绩背景｜当前：{STATS_BACKGROUNDS[current].name}"]
        for theme in STATS_BACKGROUNDS.values():
            if theme.key in unlocked:
                state = "✅ 已拥有"
            else:
                state = f"🔒 {theme.price} 牛币（#购买 {theme.shop_item}）"
            lines.append(f"{theme.number}. {theme.name}｜{state}")
        lines.append("切换：#战绩背景 <编号或名称>")
        return "\n".join(lines)

    def stats_background_key(self, group_id: str, qq_id: str) -> str:
        return self.repository.stats_background(group_id, qq_id)

    def equipped_badge(self, group_id: str, qq_id: str) -> str:
        return self.repository.equipped_badge(group_id, qq_id)

    def tip(self, group_id: str, qq_id: str, argument: str, *, request_id: str = "") -> str:
        match = re.fullmatch(r"\s*@?([1-9]\d{4,11})\s+(\d+)\s*", argument)
        if not match:
            raise CommunityError("格式：#打赏 @群友 <牛币>")
        recipient_qq, raw_amount = match.groups()
        names = self._names(group_id)
        if recipient_qq not in names:
            raise CommunityError("打赏对象不是当前群成员")
        amount = int(raw_amount)
        received, fee, balance = self.repository.tip(
            group_id, qq_id, recipient_qq, amount, request_id=request_id,
        )
        name = names.get(recipient_qq) or f"QQ {recipient_qq}"
        return (
            f"🎁 已打赏 {name} {received} 牛币｜手续费 {fee}\n"
            f"你的余额：{balance} 牛币"
        )

    def use_custom_badge(self, group_id: str, qq_id: str, argument: str) -> str:
        badge = re.sub(r"\s+", " ", argument).strip()
        if not 2 <= len(badge) <= 12:
            raise CommunityError("自选称号应为 2～12 个字符")
        if any(character in badge for character in "@#|[]<>"):
            raise CommunityError("称号不能包含 @、#、竖线或括号控制字符")
        self.repository.use_custom_badge(group_id, qq_id, badge)
        return f"🏷️ 已佩戴专属称号「{badge}」，永久收藏；以后可免费换装。"

    def quiz_hint(self, group_id: str, qq_id: str, *, quiz_date: str) -> str:
        run = self.repository.quiz_run(group_id, quiz_date)
        if run is None or not int(run["question_sent"]):
            raise CommunityError("今天的职业选手题目还没发布")
        question = question_by_key(str(run["question_key"]))
        if int(run["revealed"]):
            raise CommunityError("今天的题目已经揭晓，提示卡留到下一题再用吧")
        choices = self.repository.quiz_hint(group_id, quiz_date, qq_id, question)
        options = "；".join(
            f"{letter}. {question.options[ord(letter) - ord('A')]}"
            for letter in choices
        )
        return f"💡 提示：答案只可能是以下两项\n{options}"

    def prediction_status(self, group_id: str) -> str:
        self._require_prediction_enabled()
        prediction = self.repository.active_prediction(group_id)
        if prediction is None:
            return "本群当前没有竞猜。管理员可发送：#发起竞猜 题目 | 选项A | 选项B"
        bets = self.repository.prediction_bets(prediction.prediction_id)
        counts = [0] * len(prediction.options)
        pools = [0] * len(prediction.options)
        for _qq, option, stake in bets:
            counts[option - 1] += 1
            pools[option - 1] += stake
        closes = datetime.fromisoformat(prediction.closes_at).astimezone().strftime("%H:%M")
        lines = [f"🎲 竞猜 #{prediction.prediction_id}｜{prediction.question}"]
        for index, option in enumerate(prediction.options, 1):
            lines.append(f"{index}. {option}｜{counts[index-1]} 人 / {pools[index-1]} 牛币")
        lines.append(f"截止 {closes}｜发送 #下注 <编号> <牛币>")
        return "\n".join(lines)

    def create_prediction(self, group_id: str, qq_id: str, role: str, argument: str) -> str:
        self._require_prediction_enabled()
        self._require_admin(role, qq_id)
        parts = tuple(" ".join(part.split()) for part in argument.split("|") if part.strip())
        if len(parts) < 3:
            raise CommunityError("格式：#发起竞猜 题目 | 选项A | 选项B（最多 6 项）")
        if len(parts) > 7:
            raise CommunityError("竞猜最多设置 6 个选项")
        question, options = parts[0][:100], tuple(part[:50] for part in parts[1:])
        prediction = self.repository.create_prediction(
            group_id,
            qq_id,
            question,
            options,
            duration_minutes=self.prediction_duration_minutes,
        )
        return (
            f"竞猜已发起，{self.prediction_duration_minutes} 分钟后截止。\n"
            + self.prediction_status(prediction.group_id)
        )

    def bet(self, group_id: str, qq_id: str, argument: str) -> str:
        self._require_prediction_enabled()
        match = re.fullmatch(r"\s*(\d+)\s+(\d+)\s*", argument)
        if not match:
            raise CommunityError("格式：#下注 <选项编号> <牛币>")
        option, stake = (int(value) for value in match.groups())
        balance = self.repository.place_bet(group_id, qq_id, option, stake)
        return f"下注成功：选项 {option}，{stake} 牛币｜余额 {balance}"

    def settle_prediction(self, group_id: str, qq_id: str, role: str, argument: str) -> str:
        self._require_prediction_enabled()
        self._require_admin(role, qq_id)
        if not argument.strip().isdigit():
            raise CommunityError("格式：#开奖 <正确选项编号>")
        option = int(argument.strip())
        (
            prediction,
            pool,
            winner_count,
            payouts,
            insurance_refunds,
        ) = self.repository.settle_prediction(group_id, option)
        lines = [
            f"🏆 竞猜 #{prediction.prediction_id} 开奖",
            f"答案：{option}. {prediction.options[option - 1]}",
        ]
        if winner_count:
            lines.append(f"奖池 {pool} 牛币，由 {winner_count} 位赢家按下注比例瓜分。")
            names = self._names(group_id)
            lines.extend(
                f"• {names.get(qq_id) or f'QQ {qq_id}'}：+{payout}"
                for qq_id, payout in payouts[:10]
            )
        else:
            lines.append("无人猜中，所有下注已原路退回。")
        if insurance_refunds:
            names = self._names(group_id)
            detail = "、".join(
                f"{names.get(refund_qq) or f'QQ {refund_qq}'} +{amount}"
                for refund_qq, amount in insurance_refunds[:6]
            )
            lines.append(f"🛡️ 竞猜保险理赔：{detail}")
        return "\n".join(lines)

    def cancel_prediction(self, group_id: str, qq_id: str, role: str) -> str:
        self._require_prediction_enabled()
        self._require_admin(role, qq_id)
        prediction, refunds = self.repository.cancel_prediction(group_id)
        return f"竞猜 #{prediction.prediction_id} 已取消，已退款 {refunds} 人。"

    def quiz_answer(
        self,
        group_id: str,
        qq_id: str,
        nickname: str,
        argument: str,
        *,
        quiz_date: str,
    ) -> str:
        run = self.repository.quiz_run(group_id, quiz_date)
        if run is None or not int(run["question_sent"]):
            return "今天的职业选手题目还没发布，默认 19:30 开题。"
        question = question_by_key(str(run["question_key"]))
        if int(run["revealed"]):
            return format_quiz_question(question, revealed=True)
        choice = argument.strip().upper()
        if not choice:
            return format_quiz_question(question)
        if choice not in tuple(chr(65 + index) for index in range(len(question.options))):
            raise CommunityError("请发送 #选手 A、B、C 或 D")
        self.repository.submit_quiz_answer(
            group_id, quiz_date, qq_id, nickname or f"QQ {qq_id}", choice
        )
        return f"答案 {choice} 已锁定，21:00 统一揭晓，不能修改。"

    def add_history(
        self,
        group_id: str,
        qq_id: str,
        argument: str,
        source: HistorySource | None,
    ) -> str:
        if source is None:
            raise CommunityError("请回复一条群消息并发送 #收录黑历史 [备注]")
        entry_id = self.repository.add_museum_entry(group_id, qq_id, source, argument)
        quote = " ".join(source.text.split())[:80]
        return f"🏛️ 已收录为黑历史 #{entry_id}｜{source.nickname}：“{quote}”"

    def history(self, group_id: str, argument: str) -> str:
        target = argument.strip()
        entry_id: int | None = None
        subject_qq = ""
        if target.isdigit() and len(target) <= 4:
            entry_id = int(target)
        elif target:
            subject_qq = _target_qq(target)
            if not subject_qq:
                raise CommunityError("格式：#黑历史 [编号/@群友/QQ号]")
        rows = self.repository.museum_entries(
            group_id, subject_qq=subject_qq, entry_id=entry_id
        )
        if not rows:
            return "🏛️ 黑历史博物馆暂时空空如也。"
        row = rows[0] if entry_id is not None else random.SystemRandom().choice(rows)
        note = f"\n馆长备注：{row['note']}" if str(row["note"]) else ""
        return (
            f"🏛️ 黑历史 #{row['entry_id']}｜{row['subject_name']}\n"
            f"“{row['quote']}”{note}\n"
            f"收录于 {str(row['created_at'])[:10]}｜当事人可随时 "
            f"#删除黑历史 {row['entry_id']}"
        )

    def delete_history(self, group_id: str, qq_id: str, role: str, argument: str) -> str:
        if not argument.strip().isdigit():
            raise CommunityError("格式：#删除黑历史 <编号>")
        deleted = self.repository.delete_museum_entry(
            group_id,
            int(argument.strip()),
            qq_id,
            is_admin=self._is_admin(role, qq_id),
        )
        return "黑历史已删除。" if deleted else "没有找到这个黑历史编号。"

    def set_history(self, group_id: str, qq_id: str, *, enabled: bool) -> str:
        deleted = self.repository.set_museum_enabled(group_id, qq_id, enabled=enabled)
        if enabled:
            return "已允许群友收录你的黑历史。"
        return f"已关闭黑历史收录，并清除你的 {deleted} 条历史记录。"

    def revenge_status(self, group_id: str) -> str:
        rows = self.repository.revenge_ranking(group_id)
        if not rows:
            return "⚔️ 本群还没有群友对位产生的复仇关系。"
        lines = ["⚔️ 本群复仇榜"]
        for index, row in enumerate(rows, 1):
            lines.append(
                f"{index}. {row['leader_name']} 已连续压制 {row['challenger_name']} "
                f"{row['streak']} 次｜交手 {row['total_matches']} 场"
            )
        return "\n".join(lines)

    def process_match(self, group_id: str, match: MatchSnapshot) -> str:
        try:
            score1, score2 = int(match.team1_score), int(match.team2_score)
        except ValueError:
            return ""
        if score1 == score2:
            return ""
        winning_team = 1 if score1 > score2 else 2
        players_by_team = {
            team: [
                player
                for player in match.players
                if player.is_group_member and player.team == team
            ]
            for team in (1, 2)
        }
        outcomes: list[tuple[str, str, str, str]] = []
        for winner in players_by_team[winning_team]:
            winner_binding = self.bindings.get_by_steam(group_id, winner.steam_id)
            if winner_binding is None:
                continue
            for loser in players_by_team[3 - winning_team]:
                loser_binding = self.bindings.get_by_steam(group_id, loser.steam_id)
                if loser_binding is None:
                    continue
                outcomes.append(
                    (winner_binding.qq_id, winner.name, loser_binding.qq_id, loser.name)
                )
        events = self.repository.process_revenge(group_id, match.match_id, outcomes)
        if not events:
            return ""
        lines = ["⚔️ 复仇档案更新"]
        for event in events[:4]:
            horn = "📣 " if event.horn else ""
            if event.kind == "revenge":
                lines.append(
                    f"{horn}{event.winner_name} 复仇成功！终结了 {event.loser_name} "
                    f"此前 {event.previous_streak} 连压制。"
                )
            elif event.kind == "streak":
                lines.append(
                    f"{horn}{event.winner_name} 再次击败 {event.loser_name}，"
                    f"压制扩大到 {event.current_streak} 连。"
                )
            else:
                lines.append(
                    f"{horn}{event.loser_name} 已将 {event.winner_name} 列入复仇名单。"
                )
        lines.append("发送 #复仇 查看本群复仇榜。")
        return "\n".join(lines)

    def reward_match(self, group_id: str, match: MatchSnapshot) -> str:
        try:
            score1, score2 = int(match.team1_score), int(match.team2_score)
        except ValueError:
            score1 = score2 = 0
        winning_team = 0
        if score1 != score2:
            winning_team = 1 if score1 > score2 else 2

        names = self._names(group_id)
        candidates: dict[str, tuple[str, bool]] = {}
        for player in match.players:
            if not player.is_group_member:
                continue
            binding = self.bindings.get_by_steam(group_id, player.steam_id)
            if binding is None:
                continue
            name = names.get(binding.qq_id) or player.name or f"QQ {binding.qq_id}"
            won = bool(winning_team and player.team == winning_team)
            previous = candidates.get(binding.qq_id)
            candidates[binding.qq_id] = (
                name,
                won or bool(previous and previous[1]),
            )

        if not candidates:
            binding = self.bindings.get_by_steam(group_id, match.steam_id)
            if binding is not None:
                candidates[binding.qq_id] = (
                    names.get(binding.qq_id) or match.name or f"QQ {binding.qq_id}",
                    match.result == "win",
                )
        if not candidates:
            return ""

        try:
            reward_date = date.fromisoformat(match.ended_at[:10]).isoformat()
        except ValueError:
            reward_date = datetime.now(CHINA_TIMEZONE).date().isoformat()

        rewarded: list[str] = []
        for qq_id, (name, won) in candidates.items():
            result = self.repository.reward_completed_match(
                group_id,
                qq_id,
                match.match_id,
                reward_date,
                won=won,
            )
            if result is None:
                continue
            total = result.base_reward + result.win_bonus
            if not total:
                continue
            details = []
            if result.base_reward:
                details.append(f"完赛 +{result.base_reward}")
            if result.win_bonus:
                details.append(f"今日首胜 +{result.win_bonus}")
            rewarded.append(f"• {name} +{total}（{'、'.join(details)}）")
        if not rewarded:
            return ""
        return "\n".join(["🪙 完赛奖励", *rewarded])

    def _names(self, group_id: str) -> dict[str, str]:
        return {
            memory.qq_id: memory.nickname
            for memory in self.bindings.list_group_member_memories(group_id)
        }


def _target_qq(value: str) -> str:
    match = re.fullmatch(r"@?([1-9]\d{4,11})", value.strip())
    return match.group(1) if match else ""


def question_for(group_id: str, quiz_date: date | str) -> QuizQuestion:
    quiz_day = (
        quiz_date if isinstance(quiz_date, date) else date.fromisoformat(quiz_date)
    )
    # Each group receives its own stable permutation. Advancing by one index per
    # calendar day guarantees a full bank cycle without a repeat, unlike the V1
    # date hash which could select the same question on consecutive days.
    questions = sorted(
        PRO_PLAYER_QUESTIONS,
        key=lambda question: hashlib.sha256(
            f"{group_id}:{question.key}:pro-player-v2".encode()
        ).digest(),
    )
    return questions[quiz_day.toordinal() % len(questions)]


def question_by_key(key: str) -> QuizQuestion:
    for question in (
        *PRO_PLAYER_QUESTIONS,
        *LEGACY_PRO_PLAYER_QUESTIONS_V1,
        *LEGACY_GRENADE_QUESTIONS,
    ):
        if question.key == key:
            return question
    raise CommunityError("题库版本已变化，请等待下一题")


def format_quiz_question(question: QuizQuestion, *, revealed: bool = False) -> str:
    lines = [f"🏆 CS 职业选手每日一题｜{question.map_name}", question.prompt]
    lines.extend(
        f"{chr(65 + index)}. {option}" for index, option in enumerate(question.options)
    )
    if revealed:
        lines.extend([f"答案：{question.answer}", question.explanation])
    else:
        lines.append("发送 #选手 <A/B/C/D> 作答，21:00 揭晓。")
    return "\n".join(lines)


def format_quiz_reveal(reveal: QuizReveal) -> str:
    lines = [
        f"🎓 CS 职业选手每日一题揭晓｜答案 {reveal.question.answer}",
        reveal.question.explanation,
    ]
    if reveal.correct_names:
        lines.append("答对：" + "、".join(reveal.correct_names))
        lines.append(f"手速王：{reveal.fastest_name}（额外奖励）")
    else:
        lines.append("今日无人答对，牛币奖池安全了。")
    return "\n".join(lines)
