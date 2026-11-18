from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from pulsewatch.alertsink import (
    DEADMAN_RECEIVER,
    Heartbeat,
    Inbox,
    make_server,
    report,
    run,
    summarize,
    watch,
)

PAYLOAD = {
    "receiver": "page",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "TargetDown", "target": "api", "severity": "page"},
            "annotations": {"summary": "api is down from every region"},
        }
    ],
}


@pytest.fixture
def sink():
    inbox = Inbox(capacity=3)
    server = make_server("127.0.0.1", 0, inbox)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", inbox
    server.shutdown()
    server.server_close()


def _request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_healthz(sink):
    url, _ = sink
    assert _request(f"{url}/healthz") == (200, {"status": "ok"})


def test_post_is_stored_per_receiver(sink):
    url, _ = sink
    assert _request(f"{url}/page", "POST", json.dumps(PAYLOAD).encode())[0] == 200
    assert _request(f"{url}/warn", "POST", json.dumps({"alerts": []}).encode())[0] == 200

    status, items = _request(f"{url}/notifications")
    assert status == 200
    assert [i["receiver"] for i in items] == ["page", "warn"]
    assert items[0]["payload"] == PAYLOAD
    assert items[0]["received_at"]

    _, only_page = _request(f"{url}/notifications?receiver=page")
    assert [i["receiver"] for i in only_page] == ["page"]


def test_inbox_is_bounded_and_clearable(sink):
    url, inbox = sink
    for n in range(5):
        _request(f"{url}/r{n}", "POST", b"{}")
    assert [i["receiver"] for i in inbox.list()] == ["r2", "r3", "r4"]
    assert _request(f"{url}/notifications", "DELETE")[0] == 200
    assert inbox.list() == []


@pytest.mark.parametrize(
    ("path", "body", "code"),
    [
        ("/page", b"not json", 400),
        ("/page", b"[1, 2]", 400),
        ("/", b"{}", 404),
        ("/a/b", b"{}", 404),
        ("/notifications", b"{}", 404),
    ],
)
def test_rejects_bad_requests(sink, path, body, code):
    url, inbox = sink
    assert _request(f"{url}{path}", "POST", body)[0] == code
    assert inbox.list() == []


def test_unknown_get_is_404(sink):
    url, _ = sink
    assert _request(f"{url}/nope")[0] == 404


def test_summarize_one_line_per_alert():
    [line] = summarize("page", PAYLOAD)
    assert line == (
        "receiver=page status=firing alertname=TargetDown target=api severity=page "
        "api is down from every region"
    )
    assert summarize("warn", {}) == []


def test_delete_unknown_path_is_404(sink):
    url, _ = sink
    assert _request(f"{url}/nope", "DELETE")[0] == 404


def test_rejects_empty_body(sink):
    url, inbox = sink
    assert _request(f"{url}/page", "POST", b"")[0] == 400
    assert inbox.list() == []


def test_rejects_unparseable_content_length(sink):
    url, inbox = sink
    host, _, port = url.removeprefix("http://").partition(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.putrequest("POST", "/page")
        conn.putheader("Content-Length", "banana")
        conn.endheaders()
        assert conn.getresponse().status == 400
    finally:
        conn.close()
    assert inbox.list() == []


def test_run_serves_until_interrupted(monkeypatch):
    monkeypatch.setenv("ALERTSINK_HOST", "127.0.0.1")
    monkeypatch.setenv("ALERTSINK_PORT", "0")  # any free port

    def interrupt(self: ThreadingHTTPServer) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", interrupt)
    run()  # reads the env, serves, and closes the socket on Ctrl-C


# --- dead man's switch ----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _watchdog(status: str = "firing") -> dict[str, Any]:
    return {
        "receiver": "heartbeat",
        "status": status,
        "alerts": [{"status": status, "labels": {"alertname": "Watchdog", "severity": "none"}}],
    }


def test_heartbeat_waits_then_ok_then_missing_after_timeout():
    clock = Clock()
    hb = Heartbeat(timeout=60, clock=clock)
    assert hb.status()["state"] == "waiting"
    assert hb.check() is None  # waiting is not an incident
    hb.record(_watchdog())
    clock.now += 30
    st = hb.status()
    assert st["state"] == "ok" and st["seconds_since_last"] == 30 and st["last_seen"]
    clock.now += 31
    st = hb.status()
    assert st["state"] == "missing" and "no heartbeat for 61s" in st["reason"]


def test_heartbeat_never_seen_turns_missing_after_timeout():
    clock = Clock()
    hb = Heartbeat(timeout=60, clock=clock)
    clock.now += 61
    st = hb.status()
    assert st["state"] == "missing" and "since the sink started" in st["reason"]
    assert st["seconds_since_last"] is None


def test_resolved_watchdog_means_prometheus_stopped():
    clock = Clock()
    hb = Heartbeat(timeout=60, clock=clock)
    hb.record(_watchdog())
    hb.record(_watchdog("resolved"))
    st = hb.status()
    assert st["state"] == "missing" and "Prometheus stopped" in st["reason"]
    hb.record(_watchdog())  # Prometheus is back
    assert hb.status()["state"] == "ok"


def test_heartbeat_ignores_payloads_without_alerts():
    hb = Heartbeat(timeout=60, clock=Clock())
    hb.record({"alerts": "nonsense"})
    hb.record({"alerts": [1, None]})
    hb.record({})
    assert hb.status()["state"] == "waiting"


def test_heartbeat_timeout_must_be_positive():
    with pytest.raises(ValueError, match="must be positive"):
        Heartbeat(timeout=0)


def test_check_reports_each_change_once():
    clock = Clock()
    hb, inbox = Heartbeat(timeout=60, clock=clock), Inbox()
    hb.record(_watchdog())
    assert report(hb, inbox) is None
    clock.now += 61
    fired = report(hb, inbox)
    assert fired is not None and fired["status"] == "firing"
    assert report(hb, inbox) is None  # still missing: no second notification
    hb.record(_watchdog())
    resolved = report(hb, inbox)
    assert resolved is not None and resolved["status"] == "resolved"
    items = inbox.list(DEADMAN_RECEIVER)
    assert [i["payload"]["status"] for i in items] == ["firing", "resolved"]
    [alert] = items[0]["payload"]["alerts"]
    assert alert["labels"] == {"alertname": "HeartbeatMissing", "severity": "page"}
    assert "no heartbeat for 61s" in alert["annotations"]["summary"]


def test_check_logs_missing_as_error(caplog):
    clock = Clock()
    hb = Heartbeat(timeout=1, clock=clock)
    clock.now += 2
    with caplog.at_level("INFO", logger="pulsewatch.alertsink"):
        report(hb, Inbox())
    [record] = caplog.records
    assert record.levelname == "ERROR"
    assert "alertname=HeartbeatMissing" in record.getMessage()
    assert "status=firing" in record.getMessage()


def test_watch_reports_until_stopped():
    clock = Clock()
    hb, inbox = Heartbeat(timeout=1, clock=clock), Inbox()
    clock.now += 2
    stop = threading.Event()
    thread = threading.Thread(target=watch, args=(hb, inbox, stop, 0.01), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not inbox.list(DEADMAN_RECEIVER):
        assert time.monotonic() < deadline, "watch() never reported"
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.fixture
def hb_sink():
    clock = Clock()
    inbox, hb = Inbox(), Heartbeat(timeout=60, clock=clock)
    server = make_server("127.0.0.1", 0, inbox, hb)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", inbox, clock
    server.shutdown()
    server.server_close()


def test_heartbeat_endpoint_is_not_kept_in_the_inbox(hb_sink):
    url, inbox, clock = hb_sink
    assert _request(f"{url}/heartbeat")[1]["state"] == "waiting"
    for _ in range(3):
        assert _request(f"{url}/heartbeat", "POST", json.dumps(_watchdog()).encode())[0] == 200
    assert inbox.list() == []
    status, body = _request(f"{url}/heartbeat")
    assert status == 200 and body["state"] == "ok" and body["timeout_seconds"] == 60
    clock.now += 61
    assert _request(f"{url}/heartbeat")[1]["state"] == "missing"


def test_only_the_sink_writes_the_deadman_receiver(hb_sink):
    url, inbox, _ = hb_sink
    assert _request(f"{url}/deadman", "POST", b"{}")[0] == 404
    assert inbox.list() == []
