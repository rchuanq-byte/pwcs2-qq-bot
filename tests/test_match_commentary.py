import json
from pathlib import Path

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.match_commentary import MatchCommentaryService
from pwcs2_bot.match_formatting import build_match_snapshot


class FakeAI:
    configured = True

    def __init__(self) -> None:
        self.contents: list[str] = []
        self.max_tokens = 0

    async def complete_once(
        self,
        *,
        system_prompt: str,
        content: str,
        max_tokens: int,
    ) -> str:
        assert "不羞辱" in system_prompt
        assert "不得重新判断" in system_prompt
        self.contents.append(content)
        self.max_tokens = max_tokens
        payload = json.loads(content.splitlines()[1])
        targets = [
            player
            for player in payload["players"]
            if player["commentary_target"]
        ]
        return json.dumps(
            {
                "comments": [
                    {
                        "steam_id": player["steam_id"],
                        "comment": (
                            "火力输出稳定，关键回合敢于接战；"
                            "下场注意用道具换取更安全的首杀。"
                        ),
                    }
                    for player in targets
                ]
            },
            ensure_ascii=False,
        )


class MistakenResultAI:
    configured = True

    def __init__(self) -> None:
        self.calls = 0

    async def complete_once(
        self,
        *,
        system_prompt: str,
        content: str,
        max_tokens: int,
    ) -> str:
        del system_prompt, max_tokens
        self.calls += 1
        payload = json.loads(content.splitlines()[1])
        target = next(
            player
            for player in payload["players"]
            if player["commentary_target"]
        )
        return json.dumps(
            {
                "comments": [
                    {
                        "steam_id": target["steam_id"],
                        "comment": "虽然输了，但个人火力不错，下场可以继续主动找机会。",
                    }
                ]
            },
            ensure_ascii=False,
        )


def _match(*, group_member_steam_ids: set[str] | None = None):
    players = [
        {
            "playerId": f"7656119800000000{index}",
            "nickName": f"Player {index}",
            "team": 1 if index < 5 else 2,
            "kill": 20 - index,
            "assist": 4,
            "death": 12,
            "pwRating": 1.1,
            "adpr": 80,
        }
        for index in range(10)
    ]
    return build_match_snapshot(
        {"matchId": "PVP@test", "team": 1},
        steam_id="76561198000000000",
        detail={
            "base": {"matchId": "PVP@test", "score1": 13, "score2": 8},
            "players": players,
        },
        group_member_steam_ids=group_member_steam_ids
        or {"76561198000000000", "76561198000000001"},
    )


async def test_commentary_only_targets_opted_in_group_members(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "10001", "76561198000000000")
    repository.bind("group", "10002", "76561198000000001")
    repository.set_match_commentary_enabled("group", "10002", enabled=False)
    ai = FakeAI()
    service = MatchCommentaryService(repository, ai, max_tokens=400)  # type: ignore[arg-type]

    result = await service.generate("group", _match())
    payload = json.loads(ai.contents[0].split("\n", 1)[1])
    target_ids = {
        player["steam_id"]
        for player in payload["players"]
        if player["commentary_target"]
    }

    assert result is not None and result.startswith("🐮 赛后 AI 锐评")
    assert target_ids == {"76561198000000000"}
    assert payload["verified_result"]["winning_team"] == 1
    assert next(
        player
        for player in payload["players"]
        if player["commentary_target"]
    )["verified_outcome"] == "win"
    assert "赛果核对：" in result
    assert "Player 0｜胜利｜队伍 1" in result
    assert len(ai.contents) == 1
    assert ai.max_tokens == 400


async def test_commentary_labels_opposing_group_members_from_verified_score(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "10001", "76561198000000000")
    repository.bind("group", "10006", "76561198000000005")
    ai = FakeAI()
    service = MatchCommentaryService(repository, ai)  # type: ignore[arg-type]

    result = await service.generate(
        "group",
        _match(
            group_member_steam_ids={
                "76561198000000000",
                "76561198000000005",
            }
        ),
    )
    payload = json.loads(ai.contents[0].split("\n", 1)[1])
    outcomes = {
        player["steam_id"]: player["verified_outcome"]
        for player in payload["players"]
        if player["commentary_target"]
    }

    assert outcomes == {
        "76561198000000000": "win",
        "76561198000000005": "loss",
    }
    assert result is not None
    assert "Player 0｜胜利｜队伍 1" in result
    assert "Player 5｜失败｜队伍 2" in result


async def test_commentary_rejects_ai_result_mistake_without_fake_fallback(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "10001", "76561198000000000")
    ai = MistakenResultAI()
    service = MatchCommentaryService(repository, ai)  # type: ignore[arg-type]

    result = await service.generate(
        "group",
        _match(group_member_steam_ids={"76561198000000000"}),
    )

    assert ai.calls == 2
    assert result is None


async def test_commentary_skips_when_every_group_member_opts_out(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "10001", "76561198000000000")
    repository.bind("group", "10002", "76561198000000001")
    repository.set_match_commentary_enabled("group", "10001", enabled=False)
    repository.set_match_commentary_enabled("group", "10002", enabled=False)
    ai = FakeAI()
    service = MatchCommentaryService(repository, ai)  # type: ignore[arg-type]

    assert await service.generate("group", _match()) is None
    assert ai.contents == []
