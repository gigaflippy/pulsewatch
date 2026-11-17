import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import HttpUrl

from pulsewatch.config import Target
from pulsewatch.metrics import Metrics
from pulsewatch.probe import ProbeResult
from pulsewatch.scheduler import Scheduler
from pulsewatch.storage import Storage
from tests.helpers import wait_until


async def test_scheduler_runs_probes_and_stops(tmp_path, mock_client, monkeypatch):
    monkeypatch.setattr("pulsewatch.scheduler.random.uniform", lambda a, b: 0)
    target = Target(name="good", url=HttpUrl("https://good.test"), interval_seconds=1)
    storage = Storage(tmp_path / "s.db")
    sched = Scheduler([target], storage, Metrics(), "r", timedelta(hours=1), client=mock_client)
    await sched.start()
    await wait_until(lambda: len(storage.recent("good")) >= 1)
    await sched.stop()
    assert sched._tasks == []
    storage.close()


async def test_loop_survives_a_probe_error(tmp_path, mock_client, monkeypatch):
    monkeypatch.setattr("pulsewatch.scheduler.random.uniform", lambda a, b: 0)
    calls = 0

    async def boom(client, target, region):
        nonlocal calls
        calls += 1
        raise RuntimeError("probe exploded")

    monkeypatch.setattr("pulsewatch.scheduler.probe", boom)
    target = Target(name="good", url=HttpUrl("https://good.test"), interval_seconds=1)
    storage = Storage(tmp_path / "s.db")
    sched = Scheduler([target], storage, Metrics(), "r", timedelta(hours=1), client=mock_client)
    await sched.start()
    await wait_until(lambda: calls == 1)
    await asyncio.sleep(0)  # let the loop handle the error before checking it survived
    assert all(not t.done() for t in sched._tasks)  # the loop survived the error
    await sched.stop()
    assert sched._tasks == []
    storage.close()


async def test_scheduler_creates_and_closes_its_own_client(tmp_path):
    target = Target(name="good", url=HttpUrl("https://good.test"))
    storage = Storage(tmp_path / "s.db")
    sched = Scheduler([target], storage, Metrics(), "r", timedelta(hours=1))
    client = sched._ensure_client()
    assert sched._ensure_client() is client  # reused, not recreated
    await sched.stop()
    assert client.is_closed
    assert sched._client is None
    storage.close()


async def test_prune_loop_removes_old_results(tmp_path, mock_client, monkeypatch):
    monkeypatch.setattr("pulsewatch.scheduler.random.uniform", lambda a, b: 0)
    monkeypatch.setattr("pulsewatch.scheduler.PRUNE_EVERY_SECONDS", 0.01)
    storage = Storage(tmp_path / "s.db")
    old = ProbeResult("good", "r", datetime.now(UTC) - timedelta(hours=2), True, 200, 1.0, None)
    storage.add(old)
    target = Target(name="good", url=HttpUrl("https://good.test"), interval_seconds=60)
    sched = Scheduler([target], storage, Metrics(), "r", timedelta(hours=1), client=mock_client)
    cutoff = datetime.now(UTC) - timedelta(hours=1)

    def pruned() -> bool:
        results = storage.recent("good")
        # the loop's own fresh probe is there, and the 2h-old result is gone
        return bool(results) and all(r.timestamp > cutoff for r in results)

    prunes: list[int] = []
    real_prune = storage.prune

    def counting_prune(retention: timedelta) -> int:
        prunes.append(real_prune(retention))
        return prunes[-1]

    monkeypatch.setattr(storage, "prune", counting_prune)
    await sched.start()
    await wait_until(pruned)
    # A later pass finds nothing left to remove; the loop keeps going without logging.
    await wait_until(lambda: len(prunes) >= 2 and prunes[-1] == 0)
    await sched.stop()
    assert prunes[0] == 1
    storage.close()
