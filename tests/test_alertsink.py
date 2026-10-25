from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from pulsewatch.alertsink import Inbox, make_server, summarize

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


def _request(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, object]:
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
