"""Local inbox for Alertmanager webhook notifications: `python -m pulsewatch.alertsink`.

Every Alertmanager receiver delivers here, so alert routing works and can be tested
end to end without any credential. Each POST /<receiver> is kept in memory (newest
last, bounded) and logged as one line per alert; GET /notifications lists them.

Standard library only, so it runs from the Pulsewatch image with no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("pulsewatch.alertsink")

MAX_BODY_BYTES = 1 << 20
DEFAULT_CAPACITY = 500


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


def make_handler(inbox: Inbox) -> type[BaseHTTPRequestHandler]:
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
            if not receiver or "/" in receiver or receiver in {"healthz", "notifications"}:
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
            inbox.add(receiver, payload)
            for line in summarize(receiver, payload):
                log.info("%s", line)
            self._send(200, {"status": "ok"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass  # one line per alert from do_POST is enough; no access log noise

    return Handler


def make_server(host: str = "0.0.0.0", port: int = 9095, inbox: Inbox | None = None):
    server = ThreadingHTTPServer((host, port), make_handler(inbox or Inbox()))
    server.daemon_threads = True
    return server


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    host = os.environ.get("ALERTSINK_HOST", "0.0.0.0")
    port = int(os.environ.get("ALERTSINK_PORT", "9095"))
    server = make_server(host, port)
    log.info("alert sink listening on %s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
