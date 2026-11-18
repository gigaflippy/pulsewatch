"""Local inbox for Alertmanager webhook notifications: `python -m pulsewatch.alertsink`.

Every Alertmanager receiver delivers here, so alert routing works and can be tested
end to end without any credential. Each POST /<receiver> is kept in memory (newest
last, bounded) and logged as one line per alert; GET /notifications lists them.

It is also the dead man's switch for the alerting path. Prometheus fires `Watchdog` all the
time and Alertmanager resends it to POST /heartbeat every minute. When that stops for
ALERTSINK_HEARTBEAT_TIMEOUT seconds (default 300), or Alertmanager reports Watchdog as
resolved (Prometheus stopped sending it), the sink logs an error and adds a firing
`HeartbeatMissing` notification under receiver "deadman"; a resolved one follows when the
heartbeat comes back. GET /heartbeat shows the current state. Heartbeats themselves are not
kept in the inbox, so they can't push real alerts out of it.

Standard library only, so it runs from the Pulsewatch image with no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("pulsewatch.alertsink")

MAX_BODY_BYTES = 1 << 20
DEFAULT_CAPACITY = 500
HEARTBEAT_RECEIVER = "heartbeat"
DEADMAN_RECEIVER = "deadman"
DEFAULT_HEARTBEAT_TIMEOUT = 300.0
# GET paths, and receivers only the sink itself may write to.
RESERVED = {"healthz", "notifications", DEADMAN_RECEIVER}


class Inbox:
    """Thread-safe, bounded list of received notifications."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._items: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def add(self, receiver: str, payload: dict[str, Any]) -> dict[str, Any]:
        item = {
            "received_at": datetime.now(UTC).isoformat(),
            "receiver": receiver,
            "payload": payload,
        }
        with self._lock:
            self._items.append(item)
        return item

    def list(self, receiver: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._items)
        return [i for i in items if receiver is None or i["receiver"] == receiver]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


class Heartbeat:
    """Dead man's switch: tracks the Watchdog notifications Alertmanager sends.

    States: "waiting" (none yet, still within the timeout since the sink started), "ok",
    and "missing" (none for longer than the timeout, or Watchdog was resolved).
    """

    def __init__(
        self,
        timeout: float = DEFAULT_HEARTBEAT_TIMEOUT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout <= 0:
            raise ValueError(f"heartbeat timeout must be positive, got {timeout}")
        self.timeout = timeout
        self._clock = clock
        self._started = clock()
        self._last: float | None = None
        self._last_seen: str | None = None
        self._resolved = False
        self._reported_missing = False
        self._lock = threading.Lock()

    def record(self, payload: dict[str, Any]) -> None:
        """One notification for the heartbeat receiver: firing beats, resolved stops."""
        alerts = payload.get("alerts")
        alerts = alerts if isinstance(alerts, list) else []
        statuses = {a.get("status") for a in alerts if isinstance(a, dict)}
        with self._lock:
            if "firing" in statuses:
                self._last = self._clock()
                self._last_seen = datetime.now(UTC).isoformat()
                self._resolved = False
            elif "resolved" in statuses:
                self._resolved = True

    def status(self) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            last, last_seen, resolved = self._last, self._last_seen, self._resolved
        since = None if last is None else round(now - last, 1)
        if resolved:
            state, reason = "missing", "Watchdog was resolved: Prometheus stopped sending it"
        elif last is None:
            waited = now - self._started
            state = "missing" if waited > self.timeout else "waiting"
            reason = f"no heartbeat since the sink started {waited:.0f}s ago"
        elif now - last > self.timeout:
            state, reason = "missing", f"no heartbeat for {now - last:.0f}s"
        else:
            state, reason = "ok", f"last heartbeat {now - last:.0f}s ago"
        return {
            "state": state,
            "reason": reason,
            "last_seen": last_seen,
            "seconds_since_last": since,
            "timeout_seconds": self.timeout,
        }

    def check(self) -> dict[str, Any] | None:
        """A webhook-shaped HeartbeatMissing payload when the state flips, else None."""
        st = self.status()
        missing = st["state"] == "missing"
        with self._lock:
            if missing == self._reported_missing:
                return None
            self._reported_missing = missing
        return deadman_payload("firing" if missing else "resolved", st["reason"])


def deadman_payload(status: str, reason: str) -> dict[str, Any]:
    """Same shape as an Alertmanager webhook, so the inbox, summarize() and the
    CI checks treat it like any other notification."""
    return {
        "receiver": DEADMAN_RECEIVER,
        "status": status,
        "alerts": [
            {
                "status": status,
                "labels": {"alertname": "HeartbeatMissing", "severity": "page"},
                "annotations": {
                    "summary": "No Watchdog heartbeat from Alertmanager: Alertmanager or "
                    f"Prometheus is down, or the heartbeat route is broken ({reason})"
                },
            }
        ],
    }


def report(heartbeat: Heartbeat, inbox: Inbox) -> dict[str, Any] | None:
    """Check once; on a change, file the HeartbeatMissing notification and log it."""
    payload = heartbeat.check()
    if payload is None:
        return None
    inbox.add(DEADMAN_RECEIVER, payload)
    level = logging.ERROR if payload["status"] == "firing" else logging.INFO
    for line in summarize(DEADMAN_RECEIVER, payload):
        log.log(level, "%s", line)
    return payload


def watch(heartbeat: Heartbeat, inbox: Inbox, stop: threading.Event, every: float) -> None:
    """Background loop for run(): report() every `every` seconds until `stop` is set."""
    while not stop.wait(every):
        report(heartbeat, inbox)


def summarize(receiver: str, payload: dict[str, Any]) -> list[str]:
    """One human-readable line per alert in an Alertmanager webhook payload."""
    lines = []
    for alert in payload.get("alerts", []):
        labels = alert.get("labels", {})
        summary = alert.get("annotations", {}).get("summary", "")
        lines.append(
            f"receiver={receiver} status={alert.get('status', '?')} "
            f"alertname={labels.get('alertname', '?')} target={labels.get('target', '-')} "
            f"severity={labels.get('severity', '-')} {summary}".rstrip()
        )
    return lines


def make_handler(inbox: Inbox, heartbeat: Heartbeat) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pulsewatch-alertsink"

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            url = urlsplit(self.path)
            if url.path == "/healthz":
                self._send(200, {"status": "ok"})
            elif url.path == "/notifications":
                receiver = parse_qs(url.query).get("receiver", [None])[0]
                self._send(200, inbox.list(receiver))
            elif url.path == f"/{HEARTBEAT_RECEIVER}":
                self._send(200, heartbeat.status())
            else:
                self._send(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802
            if urlsplit(self.path).path == "/notifications":
                inbox.clear()
                self._send(200, {"status": "cleared"})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            receiver = urlsplit(self.path).path.strip("/")
            if not receiver or "/" in receiver or receiver in RESERVED:
                self._send(404, {"error": "POST /<receiver>"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY_BYTES:
                self._send(400, {"error": "missing or oversized body"})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except json.JSONDecodeError:
                self._send(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return
            if receiver == HEARTBEAT_RECEIVER:
                heartbeat.record(payload)  # not kept: one a minute would flood the inbox
                self._send(200, {"status": "ok"})
                return
            inbox.add(receiver, payload)
            for line in summarize(receiver, payload):
                log.info("%s", line)
            self._send(200, {"status": "ok"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # one line per alert from do_POST is enough; no access log noise

    return Handler


def make_server(
    host: str = "0.0.0.0",
    port: int = 9095,
    inbox: Inbox | None = None,
    heartbeat: Heartbeat | None = None,
) -> ThreadingHTTPServer:
    handler = make_handler(inbox or Inbox(), heartbeat or Heartbeat())
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("ALERTSINK_HOST", "0.0.0.0")
    port = int(os.environ.get("ALERTSINK_PORT", "9095"))
    timeout = float(os.environ.get("ALERTSINK_HEARTBEAT_TIMEOUT", DEFAULT_HEARTBEAT_TIMEOUT))
    inbox, heartbeat = Inbox(), Heartbeat(timeout)
    server = make_server(host, port, inbox, heartbeat)
    stop = threading.Event()
    every = min(10.0, timeout / 4)
    threading.Thread(target=watch, args=(heartbeat, inbox, stop, every), daemon=True).start()
    log.info("alert sink listening on %s:%d (heartbeat timeout %.0fs)", host, port, timeout)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


if __name__ == "__main__":
    run()
