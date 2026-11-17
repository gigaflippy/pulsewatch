from datetime import UTC, datetime, timedelta

from pulsewatch.probe import ProbeResult
from pulsewatch.storage import Storage, _percentile

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def res(minutes_ago: float, up: bool = True, latency: float | None = 10.0) -> ProbeResult:
    return ProbeResult(
        "t", "r", NOW - timedelta(minutes=minutes_ago), up, 200 if up else 500, latency, None
    )


def test_percentile():
    assert _percentile([], 50) is None
    assert _percentile([5.0], 95) == 5.0
    values = [float(i) for i in range(1, 101)]
    assert _percentile(values, 50) == 50.0
    assert _percentile(values, 95) == 95.0


def test_add_and_recent_newest_first(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.add(res(10))
    s.add(res(1, up=False))
    recent = s.recent("t")
    assert [r.up for r in recent] == [False, True]
    assert recent[0].timestamp == NOW - timedelta(minutes=1)


def test_stats_window_and_uptime(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    for i in range(3):
        s.add(res(i + 1, up=True, latency=10.0 * (i + 1)))
    s.add(res(4, up=False, latency=None))
    s.add(res(120))  # outside a 1h window
    st = s.stats("t", timedelta(hours=1), now=NOW)
    assert st.checks == 4
    assert st.uptime_percent == 75.0
    assert st.latency_p50_ms == 20.0
    assert st.latency_p95_ms == 30.0
    assert st.last is not None and st.last.timestamp == NOW - timedelta(minutes=1)


def test_stats_empty(tmp_path):
    st = Storage(tmp_path / "db.sqlite").stats("nope", timedelta(hours=1), now=NOW)
    assert st.checks == 0
    assert st.uptime_percent is None
    assert st.last is None


def test_prune(tmp_path):
    s = Storage(tmp_path / "db.sqlite")
    s.add(res(60 * 100))
    s.add(res(5))
    assert s.prune(timedelta(hours=72), now=NOW) == 1
    assert len(s.recent("t")) == 1
