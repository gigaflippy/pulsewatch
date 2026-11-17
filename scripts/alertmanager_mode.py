#!/usr/bin/env python3
"""Restarts Alertmanager and waits until the new pod runs in the wanted mode.

    alertmanager_mode.py telegram   # after creating the alertmanager-telegram Secret
    alertmanager_mode.py sink       # after deleting it

Used by `make alerts-telegram` / `make alerts-telegram-off` and the kind CI job.

Why it retries: the Telegram Secret reaches the pod as an optional environment variable,
which the kubelet resolves from its own Secret cache. A restart straight after creating or
deleting the Secret can start a pod that still sees the old answer. CI hit this once
(Telegram still on after `alerts-telegram-off`). Restarting again a few seconds later
fixes it, so the mode is checked from the new pod's start.sh line rather than assumed.

Logs are read from the newest pod by creation time, not `deploy/alertmanager`, which can
pick a pod that is still terminating during a rollout. Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable

EXPECT = {
    "telegram": "Telegram receiver enabled",
    "sink": "alert sink only",
}

SELECTOR = "app.kubernetes.io/name=alertmanager"
NEWEST_LAST = ".metadata.creationTimestamp"

Run = Callable[[list[str]], str]


def kubectl(args: list[str]) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout


def start_line(run: Run, ns: str) -> tuple[str, str]:
    """(newest pod, its start.sh line or "")."""
    query = ["-n", ns, "get", "pods", "-l", SELECTOR, f"--sort-by={NEWEST_LAST}", "-o", "name"]
    pods = run(query).split()
    if not pods:
        return "", ""
    pod = pods[-1]
    lines = [ln for ln in run(["-n", ns, "logs", pod]).splitlines() if "start.sh:" in ln]
    return pod, lines[-1] if lines else ""


def switch(
    mode: str,
    ns: str = "pulsewatch",
    attempts: int = 5,
    run: Run = kubectl,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Restart until the newest pod logs the wanted mode. Returns its start.sh line."""
    expect = EXPECT[mode]
    line = ""
    for attempt in range(1, attempts + 1):
        run(["-n", ns, "rollout", "restart", "deployment/alertmanager"])
        run(["-n", ns, "rollout", "status", "deployment/alertmanager", "--timeout=120s"])
        pod, line = start_line(run, ns)
        if expect in line:
            return line
        print(
            f"attempt {attempt}/{attempts}: {pod or 'no pod'} started with "
            f"{line or 'no start.sh line'!r}; restarting again",
            file=sys.stderr,
        )
        if attempt < attempts:
            sleep(3 * attempt)
    raise RuntimeError(
        f"Alertmanager did not reach mode {mode!r} after {attempts} restarts "
        f"(last: {line or 'no start.sh line'!r})"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=sorted(EXPECT))
    parser.add_argument("--namespace", "-n", default="pulsewatch")
    parser.add_argument("--attempts", type=int, default=5)
    args = parser.parse_args(argv)
    try:
        print(switch(args.mode, args.namespace, args.attempts))
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"alertmanager_mode: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
