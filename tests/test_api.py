import pytest
from fastapi.testclient import TestClient

from pulsewatch.api import create_app
from pulsewatch.config import Settings


@pytest.fixture
def client(tmp_path, targets, mock_client):
    settings = Settings(db_path=tmp_path / "api.db", region="eu-test", scheduler_enabled=False)
    app = create_app(settings, targets=targets, client=mock_client)
    with TestClient(app) as c:
        yield c


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_targets(client):
    names = [t["name"] for t in client.get("/targets").json()]
    assert names == ["good", "bad", "slow"]


def test_check_then_status_and_results(client):
    assert client.post("/targets/good/check").json()["up"] is True
    assert client.post("/targets/bad/check").json()["up"] is False

    status = {s["target"]: s for s in client.get("/status").json()}
    assert status["good"]["uptime_percent"] == 100.0
    assert status["bad"]["uptime_percent"] == 0.0
    assert status["slow"]["checks"] == 0

    results = client.get("/targets/good/results").json()
    assert len(results) == 1 and results[0]["region"] == "eu-test"


def test_unknown_target_404(client):
    assert client.get("/targets/nope/results").status_code == 404
    assert client.post("/targets/nope/check").status_code == 404


def test_metrics_exposed(client):
    client.post("/targets/good/check")
    client.post("/targets/slow/check")
    body = client.get("/metrics").text
    assert 'pulsewatch_up{region="eu-test",target="good"} 1.0' in body
    assert 'pulsewatch_up{region="eu-test",target="slow"} 0.0' in body
    assert 'pulsewatch_probes_total{region="eu-test",result="down",target="slow"} 1.0' in body
    assert "pulsewatch_probe_latency_seconds_bucket" in body


def test_metrics_before_any_probe(client):
    """Objectives are exported and counters start at zero, so the SLO rules have data."""
    body = client.get("/metrics").text
    for name in ("good", "bad", "slow"):
        assert f'pulsewatch_slo_objective{{region="eu-test",target="{name}"}} 0.995' in body
        for result in ("up", "down"):
            line = f'pulsewatch_probes_total{{region="eu-test",result="{result}",target="{name}"}}'
            assert f"{line} 0.0" in body
    # The up gauge stays absent until a probe has run: no fake "down" before the first check.
    assert "pulsewatch_up{" not in body


def test_status_window_validation(client):
    assert client.get("/status?window=10").status_code == 422
