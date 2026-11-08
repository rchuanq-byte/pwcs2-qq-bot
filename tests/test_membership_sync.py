from pathlib import Path

from pwcs2_bot.database import BindingRepository
from pwcs2_bot.membership_sync import MembershipReconciler


async def test_membership_reconciler_removes_only_absent_group_members(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "100001", "76561198000000001")
    repository.bind("group", "100002", "76561198000000002")
    repository.bind("other", "200001", "76561198000000003")
    queries: list[str] = []

    async def fetch(group_id: str) -> set[str]:
        queries.append(group_id)
        return {"100001"} if group_id == "group" else {"200001"}

    reconciler = MembershipReconciler(
        repository,
        fetch,
        initial_delay_seconds=0,
    )

    removed = await reconciler.run_once()

    assert queries == ["group", "other"]
    assert [(item.group_id, item.qq_id) for item in removed] == [("group", "100002")]
    assert repository.get_by_qq("group", "100001") is not None
    assert repository.get_by_qq("group", "100002") is None


async def test_membership_reconciler_refuses_empty_or_failed_lists(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("empty", "100001", "76561198000000001")
    repository.bind("failed", "100002", "76561198000000002")

    async def fetch(group_id: str) -> set[str]:
        if group_id == "failed":
            raise RuntimeError("temporary OneBot failure")
        return set()

    reconciler = MembershipReconciler(repository, fetch)

    assert await reconciler.run_once() == ()
    assert len(repository.list_bindings()) == 2


async def test_membership_reconciler_discovers_and_removes_valorant_only_binding(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind_valorant("valorant-only", "100001", "ExamplePlayer#12345")
    cleaned: list[tuple[str, str]] = []

    async def fetch(group_id: str) -> set[str]:
        assert group_id == "valorant-only"
        return {"999999"}

    reconciler = MembershipReconciler(
        repository,
        fetch,
        lambda group_id, qq_id: cleaned.append((group_id, qq_id)),
        initial_delay_seconds=0,
    )

    removed = await reconciler.run_once()

    assert removed == ()
    assert repository.get_valorant_by_qq("valorant-only", "100001") is None
    assert cleaned == [("valorant-only", "100001")]


async def test_membership_reconciler_cleans_mixed_bindings_once_per_member(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.bind("group", "100001", "76561198000000001")
    repository.bind_valorant("group", "100001", "ExamplePlayer#12345")
    cleaned: list[tuple[str, str]] = []

    async def fetch(_group_id: str) -> set[str]:
        return {"999999"}

    reconciler = MembershipReconciler(
        repository,
        fetch,
        lambda group_id, qq_id: cleaned.append((group_id, qq_id)),
        initial_delay_seconds=0,
    )

    removed = await reconciler.run_once()

    assert [(binding.group_id, binding.qq_id) for binding in removed] == [
        ("group", "100001")
    ]
    assert repository.get_by_qq("group", "100001") is None
    assert repository.get_valorant_by_qq("group", "100001") is None
    assert cleaned == [("group", "100001")]


async def test_membership_reconciler_discovers_memory_only_group(
    tmp_path: Path,
) -> None:
    repository = BindingRepository(tmp_path / "bindings.db")
    repository.add_group_ai_memory(
        "memory-only",
        "这位成员已经退群",
        created_by_qq="100001",
        created_by_nickname="Former member",
    )
    queries: list[str] = []

    async def fetch(group_id: str) -> set[str]:
        queries.append(group_id)
        return {"999999"}

    reconciler = MembershipReconciler(repository, fetch, initial_delay_seconds=0)

    assert await reconciler.run_once() == ()
    assert queries == ["memory-only"]
    assert repository.list_group_ai_memories("memory-only") == ()
