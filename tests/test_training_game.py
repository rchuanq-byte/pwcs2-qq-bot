from hashlib import sha256

import pytest

from pwcs2_bot.training_game import (
    TrainingGameRateLimiter,
    build_training_game_system_prompt,
    format_training_game_intro,
    is_training_game_direct_request,
    is_training_game_file_request,
    is_training_game_trigger,
    training_game_html_path,
)


@pytest.mark.parametrize(
    "text",
    (
        "CS2",
        "今晚练枪",
        "来个小游戏",
        "爆头怎么练",
        "压枪练习",
        "反应测试",
        "跟枪",
        "打枪",
        "aim trainer",
        "FPS 要练反应",
        "#训练营",
    ),
)
def test_training_game_matches_requested_keywords(text: str) -> None:
    assert is_training_game_trigger(text)


@pytest.mark.parametrize(
    "text",
    (
        "aiming",
        "cs20",
        "myfpscounter",
        "看看 http://example.com/cs2-training.html",
        "#ai CS2 怎么压枪",
        "/ai fps",
        "普通群聊",
    ),
)
def test_training_game_avoids_embedded_urls_and_explicit_ai(text: str) -> None:
    assert not is_training_game_trigger(text)


def test_training_game_recognizes_direct_link_and_fixed_file_requests() -> None:
    assert is_training_game_direct_request("#训练营")
    assert is_training_game_direct_request("把小游戏链接发我")
    assert not is_training_game_direct_request("今晚练枪")
    assert is_training_game_file_request("#训练文件")
    assert is_training_game_file_request("把 CS2 小游戏原文件发我")
    assert is_training_game_file_request("发原文件", addressed_to_bot=True)
    assert not is_training_game_file_request("发原文件", addressed_to_bot=False)
    assert not is_training_game_file_request("发 C:/secret.txt")


def test_training_game_intro_and_ai_knowledge_use_actual_url() -> None:
    url = "https://game.example.test/cs2-training.html"
    intro = format_training_game_intro(url)
    prompt = build_training_game_system_prompt(url)

    assert url in intro
    assert "爆头 / 跟枪 / 压枪 / 反应测试" in intro
    assert url in prompt
    assert "金色靶 $900" in prompt
    assert "AWP $4750" in prompt
    assert "localStorage" in prompt
    assert "/www/" not in prompt


def test_training_game_rate_limiter_has_group_and_user_boundaries() -> None:
    now = [1000.0]
    limiter = TrainingGameRateLimiter(
        group_cooldown_seconds=300,
        user_cooldown_seconds=60,
        clock=lambda: now[0],
    )

    assert limiter.allow("group", "one", direct=False)
    assert not limiter.allow("group", "two", direct=False)
    assert not limiter.allow("group", "one", direct=True)
    assert limiter.allow("group", "two", direct=True)
    now[0] += 60
    assert limiter.allow("group", "one", direct=True)
    now[0] += 300
    assert limiter.allow("group", "three", direct=False)


def test_training_game_html_is_the_reviewed_single_file_asset() -> None:
    path = training_game_html_path()
    data = path.read_bytes()
    normalized = data[:-1] if data.endswith(b"\n") else data

    assert path.name == "cs2-training.html"
    assert data.startswith(b"<!DOCTYPE html>")
    assert b"<canvas id=\"cv\"></canvas>" in data
    assert b"https://" not in data and b"http://" not in data
    assert sha256(normalized).hexdigest() == (
        "72a1876e22b0a9f37fcb322ca056c88ce46bab5b7bf46e0b2101c4a415753370"
    )
