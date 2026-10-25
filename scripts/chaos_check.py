#!/usr/bin/env python3
"""Assertions for the chaos experiments (`make chaos-*`, CI job `chaos`).

Asks Prometheus which alerts fire and the local alert sink which notifications arrived,
so a chaos run can check "this alert fired, was routed here, and that one never fired".
Standard library only: runs on a bare CI runner or a laptop next to `make k8s-forward`.

    chaos_check.py wait-alert TargetDownInSomeRegions target=example region=us-sim
    chaos_check.py wait-delivered warn TargetDownInSomeRegions target=example
    chaos_check.py never-fired TargetDown target=example --window 15m
    chaos_check.py not-firing ProbeAgentsAbsent
    chaos_check.py not-delivered warn ProbeRegionMissing region=eu-sim
    chaos_check.py wait-value 'count(count by (region) (pulsewatch_up))' 2
    chaos_check.py wait-inhibited ProbeRegionMissing region=eu-sim

PROMETHEUS_URL (default http://localhost:9090), ALERTMANAGER_URL (http://localhost:9093) and
SINK_URL (http://localhost:9095) point at the port-forwarded services. `--timeout` goes
before the command. Exit status 0 = the check holds, 1 = it doesn't.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

LABEL_RE = re.compile(r"^([a-zA-Z_][a-zA-Z0-9_]*)=(.*)$")
WINDOW_RE = re.compile(r"^\d+[smh]$")


def parse_labels(pairs: list[str]) -> dict[str, str]:
    """`["target=api", "region=eu"]` -> `{"target": "api", "region": "eu"}`."""
    labels = {}
    for pair in pairs:
        m = LABEL_RE.match(pair)
        if not m:
            raise ValueError(f"expected label=value, got {pair!r}")
        labels[m.group(1)] = m.group(2)
    return labels


def selector(alertname: str, labels: dict[str, str], firing: bool = True) -> str:
    """PromQL selector for one alert in the ALERTS series."""
    matchers = {"alertname": alertname, **labels}
    if firing:
        matchers["alertstate"] = "firing"
    body = ", ".join(f"{k}={json.dumps(v)}" for k, v in sorted(matchers.items()))
    return f"ALERTS{{{body}}}"


def never_fired_query(alertname: str, labels: dict[str, str], window: str) -> str:
    if not WINDOW_RE.match(window):
        raise ValueError(f"window must look like 15m, got {window!r}")
    return f"max_over_time({selector(alertname, labels)}[{window}])"


def matches(alert_labels: dict[str, str], alertname: str, want: dict[str, str]) -> bool:
    if alert_labels.get("alertname") != alertname:
        return False
    return all(alert_labels.get(k) == v for k, v in want.items())


def delivered(
    notifications: list[dict[str, Any]],
    receiver: str,
    alertname: str,
    want: dict[str, str],
    status: str = "firing",
) -> list[dict[str, Any]]:
    """Alerts in the sink's notifications that reached `receiver` with `status`."""
    return [
        a
        for n in notifications
        if n.get("receiver") == receiver
        for a in n.get("payload", {}).get("alerts", [])
        if a.get("status") == status and matches(a.get("labels", {}), alertname, want)
    ]


def inhibited(am_alerts: list[dict[str, Any]], alertname: str, want: dict[str, str]) -> bool:
    """True if Alertmanager holds the alert and every matching instance is inhibited.

    Reads the /api/v2/alerts shape: status.state is "suppressed" and status.inhibitedBy
    lists the fingerprints of the alerts doing the inhibiting.
    """
    hits = [a for a in am_alerts if matches(a.get("labels", {}), alertname, want)]
    return bool(hits) and all(
        a.get("status", {}).get("state") == "suppressed" and a["status"].get("inhibitedBy")
        for a in hits
    )


def _get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


def query(prometheus: str, promql: str) -> list[dict[str, Any]]:
    url = f"{prometheus}/api/v1/query?" + urllib.parse.urlencode({"query": promql})
    body = _get_json(url)
    if body.get("status") != "success":
        raise RuntimeError(f"query failed: {promql}: {body}")
    return body["data"]["result"]


def sink(sink_url: str) -> list[dict[str, Any]]:
    return _get_json(f"{sink_url}/notifications")


def wait(check: Callable[[], bool], timeout: float, every: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if check():
                return True
        # Port-forward hiccup, empty or non-JSON body, or a failed query right after a
        # restart: keep waiting until the timeout instead of crashing the chaos job.
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"  (retrying: {exc})", file=sys.stderr)
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--timeout", type=float, default=600, help="seconds, for wait-* commands")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("wait-alert", "not-firing"):
        s = sub.add_parser(name)
        s.add_argument("alertname")
        s.add_argument("labels", nargs="*")
    s = sub.add_parser("never-fired")
    s.add_argument("alertname")
    s.add_argument("labels", nargs="*")
    s.add_argument("--window", default="15m")
    for name in ("wait-delivered", "not-delivered"):
        s = sub.add_parser(name)
        s.add_argument("receiver")
        s.add_argument("alertname")
        s.add_argument("labels", nargs="*")
        s.add_argument("--status", default="firing", choices=["firing", "resolved"])
    s = sub.add_parser("wait-inhibited")
    s.add_argument("alertname")
    s.add_argument("labels", nargs="*")
    s = sub.add_parser("wait-value")
    s.add_argument("promql")
    s.add_argument("expected", type=float)
    args = p.parse_args(argv)

    prom = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
    sink_url = os.environ.get("SINK_URL", "http://localhost:9095").rstrip("/")
    am_url = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093").rstrip("/")
    labels = parse_labels(getattr(args, "labels", []))

    if args.cmd == "wait-alert":
        sel = selector(args.alertname, labels)
        ok = wait(lambda: bool(query(prom, sel)), args.timeout)
        print(f"{'firing' if ok else 'NOT firing after timeout'}: {sel}")
        return 0 if ok else 1
    if args.cmd == "not-firing":
        sel = selector(args.alertname, labels)
        hits = query(prom, sel)
        print(f"{'FIRING' if hits else 'not firing'}: {sel} {hits or ''}")
        return 1 if hits else 0
    if args.cmd == "never-fired":
        q = never_fired_query(args.alertname, labels, args.window)
        hits = query(prom, q)
        print(f"{'FIRED' if hits else 'never fired'} in the last {args.window}: {q} {hits or ''}")
        return 1 if hits else 0
    if args.cmd == "wait-delivered":
        found: list[dict[str, Any]] = []

        def check() -> bool:
            found[:] = delivered(sink(sink_url), args.receiver, args.alertname, labels, args.status)
            return bool(found)

        ok = wait(check, args.timeout)
        what = f"{args.receiver} <- {args.status} {args.alertname} {labels}"
        print(f"{'delivered' if ok else 'NOT delivered after timeout'}: {what}")
        return 0 if ok else 1
    if args.cmd == "not-delivered":
        hits = delivered(sink(sink_url), args.receiver, args.alertname, labels, args.status)
        what = f"{args.receiver} <- {args.status} {args.alertname} {labels}"
        print(f"{'DELIVERED' if hits else 'not delivered'}: {what}")
        return 1 if hits else 0
    if args.cmd == "wait-inhibited":
        ok = wait(
            lambda: inhibited(_get_json(f"{am_url}/api/v2/alerts"), args.alertname, labels),
            args.timeout,
        )
        print(f"{'inhibited' if ok else 'NOT inhibited after timeout'}: {args.alertname} {labels}")
        return 0 if ok else 1
    # wait-value
    seen: list[str] = []

    def check_value() -> bool:
        result = query(prom, args.promql)
        seen[:] = [r["value"][1] for r in result]
        return len(result) == 1 and float(result[0]["value"][1]) == args.expected

    ok = wait(check_value, args.timeout)
    print(f"{'ok' if ok else 'TIMEOUT'}: {args.promql} = {seen} (want {args.expected:g})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
