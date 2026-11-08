from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

from pwcs2_bot.database import BindingRepository


def _payload(highlight_id: str, *, score: int = 60) -> dict[str, object]:
    return {
        "schema_version": 1,
        "highlight_id": highlight_id,
        "match_id": "match-1",
        "player_steam_id": "76561198000000001",
        "score": score,
        "window": {"start_seconds": 10.0, "end_seconds": 30.0},
    }


def test_enqueue_is_idempotent_and_claims_highest_priority(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    repository = BindingRepository(path)
    low, low_created = repository.enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("low"),
        priority=10,
    )
    high, high_created = repository.enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("high", score=100),
        priority=90,
    )
    duplicate, duplicate_created = BindingRepository(path).enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("high", score=1),
        priority=-10,
    )

    assert low_created is True
    assert high_created is True
    assert duplicate_created is False
    assert duplicate.job_id == high.job_id
    assert duplicate.payload["score"] == 100
    assert duplicate.priority == 90

    claimed = repository.claim_highlight_render_jobs("windows-worker", limit=1)
    assert len(claimed) == 1
    assert claimed[0].job_id == high.job_id
    assert claimed[0].status == "running"
    assert claimed[0].attempt_count == 1
    assert claimed[0].lease_token
    assert repository.get_highlight_render_job(low.dedupe_key).status == "pending"


def test_complete_is_lease_safe_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    repository = BindingRepository(path)
    original, _ = repository.enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("complete"),
    )
    leased = repository.claim_highlight_render_jobs("worker-a")[0]

    assert repository.renew_highlight_render_job_lease(leased) is True
    assert repository.complete_highlight_render_job(
        leased,
        {"video_path": "highlights/clip.mp4", "sha256": "abc"},
    )
    assert not repository.complete_highlight_render_job(leased, {"video_path": "again"})

    reopened = BindingRepository(path).get_highlight_render_job(original.dedupe_key)
    assert reopened is not None
    assert reopened.status == "completed"
    assert reopened.output == {"sha256": "abc", "video_path": "highlights/clip.mp4"}
    assert reopened.lease_token == ""
    assert reopened.completed_at


def test_stale_lease_is_reclaimed_and_old_worker_cannot_finish(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    repository.enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("stale"),
    )
    first = repository.claim_highlight_render_jobs("worker-a")[0]
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    with repository._connect() as connection:  # noqa: SLF001 - lease recovery test
        connection.execute(
            "UPDATE highlight_render_jobs SET claimed_at = ? WHERE job_id = ?",
            (stale, first.job_id),
        )

    second = repository.claim_highlight_render_jobs(
        "worker-b",
        lease_seconds=60,
    )[0]
    assert second.job_id == first.job_id
    assert second.attempt_count == 2
    assert second.lease_token != first.lease_token
    assert not repository.complete_highlight_render_job(first, {"video_path": "old.mp4"})
    assert repository.complete_highlight_render_job(second, {"video_path": "new.mp4"})


def test_claim_limit_fails_only_expired_or_pending_exhausted_jobs(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    expired_job, _ = repository.enqueue_highlight_render_job(
        "group",
        "match-expired",
        "76561198000000001",
        _payload("expired"),
        priority=30,
    )
    active_job, _ = repository.enqueue_highlight_render_job(
        "group",
        "match-active",
        "76561198000000001",
        _payload("active"),
        priority=20,
    )
    pending_job, _ = repository.enqueue_highlight_render_job(
        "group",
        "match-pending",
        "76561198000000001",
        _payload("pending"),
        priority=10,
    )
    first = repository.claim_highlight_render_jobs(
        "worker-a", limit=2, max_attempts=2
    )
    assert [item.job_id for item in first] == [expired_job.job_id, active_job.job_id]
    stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
    with repository._connect() as connection:  # noqa: SLF001 - lease boundary setup
        connection.execute(
            """
            UPDATE highlight_render_jobs
            SET attempt_count = 2, claimed_at = ?
            WHERE job_id = ?
            """,
            (stale, expired_job.job_id),
        )
        connection.execute(
            "UPDATE highlight_render_jobs SET attempt_count = 2 WHERE job_id IN (?, ?)",
            (active_job.job_id, pending_job.job_id),
        )

    assert repository.claim_highlight_render_jobs(
        "worker-b", lease_seconds=60, max_attempts=2
    ) == ()
    expired = repository.get_highlight_render_job(expired_job.dedupe_key)
    active = repository.get_highlight_render_job(active_job.dedupe_key)
    pending = repository.get_highlight_render_job(pending_job.dedupe_key)
    assert expired is not None and expired.status == "failed"
    assert "lease expired" in expired.last_error
    assert active is not None and active.status == "running"
    assert active.worker_id == "worker-a"
    assert pending is not None and pending.status == "failed"
    assert "maximum attempts" in pending.last_error


def test_concurrent_highlight_claimers_cannot_lease_the_same_job(tmp_path: Path) -> None:
    path = tmp_path / "bot.db"
    repository = BindingRepository(path)
    repository.enqueue_highlight_render_job(
        "group",
        "match-concurrent",
        "76561198000000001",
        _payload("concurrent"),
    )
    barrier = Barrier(2)

    def claim(worker_id: str):
        barrier.wait(timeout=5)
        return BindingRepository(path).claim_highlight_render_jobs(worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(claim, ("worker-a", "worker-b")))

    claimed = tuple(item for result in results for item in result)
    assert len(claimed) == 1
    assert claimed[0].attempt_count == 1


def test_retry_uses_backoff_then_stops_at_attempt_limit(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    job, _ = repository.enqueue_highlight_render_job(
        "group",
        "match-1",
        "76561198000000001",
        _payload("retry"),
    )
    first = repository.claim_highlight_render_jobs("worker")[0]
    assert repository.retry_highlight_render_job(
        first,
        "renderer offline",
        base_delay_seconds=60,
        max_attempts=2,
    )
    pending = repository.get_highlight_render_job(job.dedupe_key)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.available_at
    assert pending.last_error == "renderer offline"
    assert repository.claim_highlight_render_jobs("worker") == ()

    with repository._connect() as connection:  # noqa: SLF001 - advance due time
        connection.execute(
            "UPDATE highlight_render_jobs SET available_at = '' WHERE job_id = ?",
            (job.job_id,),
        )
    second = repository.claim_highlight_render_jobs("worker")[0]
    assert second.attempt_count == 2
    assert not repository.retry_highlight_render_job(
        second,
        "permanent",
        base_delay_seconds=0,
        max_attempts=2,
    )
    failed = repository.get_highlight_render_job(job.dedupe_key)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.last_error == "permanent"
    assert repository.claim_highlight_render_jobs("worker") == ()


def test_malformed_persisted_payload_fails_before_lease(tmp_path: Path) -> None:
    repository = BindingRepository(tmp_path / "bot.db")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with repository._connect() as connection:  # noqa: SLF001 - corruption test
        connection.execute(
            """
            INSERT INTO highlight_render_jobs
                (dedupe_key, group_id, match_id, steam_id, payload_json,
                 status, created_at, updated_at)
            VALUES ('broken', 'group', 'match', 'steam', '{',
                    'pending', ?, ?)
            """,
            (now, now),
        )

    assert repository.claim_highlight_render_jobs("worker") == ()
    with repository._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            "SELECT status, attempt_count, last_error FROM highlight_render_jobs"
        ).fetchone()
    assert row["status"] == "failed"
    assert row["attempt_count"] == 0
    assert row["last_error"] == "invalid durable highlight payload"
