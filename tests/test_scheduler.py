import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import HttpUrl

from pulsewatch.config import Target
from pulsewatch.metrics import Metrics
from pulsewatch.probe import ProbeResult
from pulsewatch.scheduler import Scheduler
from pulsewatch.storage import Storage


async def test_scheduler_runs_probes_and_stops(tmp_path, mock_client, monkeypatch):
    monkeypatch.setattr("pulsewatch.scheduler.random.uniform", lambda a, b: 0)
    target = Target(name="good", url=HttpUrl("https://good.test"), interval_seconds=1)
    storage = Storage(tmp_path / "s.db")
    sched = Scheduler([target], storage, Metrics(), "r", timedelta(hours=1), client=mock_client)
    await sched.start()
    await asyncio.sleep(0.1)
    await sched.stop()
    assert len(storage.recent("good")) >= 1
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
    await asyncio.sleep(0.1)
    assert calls == 1
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
    await sched.start()
    await asyncio.sleep(0.1)
    await sched.stop()
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    results = storage.recent("good")
    assert results  # the loop's own fresh probe is still there
    assert all(r.timestamp > cutoff for r in results)  # the 2h-old result was pruned
    storage.close()
