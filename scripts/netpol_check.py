#!/usr/bin/env python3
"""Proves the NetworkPolicies are enforced on a live cluster (`make netpol-check`, CI kind jobs).

Opens real TCP connections from inside Pulsewatch pods (`kubectl exec`, the image has
Python) and checks each one against what the policies say:

  * connections the policies forbid time out or are refused,
  * the probe agent still resolves names and reaches the public internet,
  * control: a temporary allow rule opens a forbidden path, and deleting it closes the
    path again. That shows the CNI really enforces NetworkPolicy, so a "blocked" result
    means the policy did it, not a broken network.

    netpol_check.py kind            # deploy/k8s/overlays/kind (one namespace)
    netpol_check.py multi-region    # deploy/k8s/overlays/multi-region

In-cluster destinations are dialled by Service ClusterIP, so a pod with no DNS egress
(the alert sink) still tests the path itself. Exit status 0 = every check holds.
Standard library only.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

MONITORING_NS = "pulsewatch"
CONTROL_POLICY = "netpol-check-control"

# Runs inside the pod: argv = host port. Prints one word: open, refused, timeout, dns, error.
DIAL_PY = """
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
except OSError:
    print("dns"); sys.exit()
try:
    socket.create_connection(addr, timeout=4).close(); print("open")
except ConnectionRefusedError:
    print("refused")
except (socket.timeout, TimeoutError):
    print("timeout")
except OSError:
    print("error")
"""

BLOCKED = {"timeout", "refused", "error"}


@dataclass(frozen=True)
class Check:
    namespace: str  # where the client pod runs
    client: str  # Deployment name of the client (must run the Pulsewatch image)
    dest: str  # "svc/<namespace>/<name>" (dialled by ClusterIP) or a host name / IP
    port: int
    allowed: bool
    why: str

    def label(self) -> str:
        verdict = "allowed" if self.allowed else "blocked"
        return f"{self.namespace}/{self.client} -> {self.dest}:{self.port} {verdict} ({self.why})"


def holds(check: Check, outcome: str) -> bool:
    """`dns` never holds: a check that cannot resolve its destination proves nothing."""
    return outcome == "open" if check.allowed else outcome in BLOCKED


def svc(namespace: str, name: str) -> str:
    return f"svc/{namespace}/{name}"


def checks_for(overlay: str) -> list[Check]:
    m = MONITORING_NS
    if overlay == "kind":
        a = m  # agent and monitoring share one namespace
        # Not checked: agent -> Kubernetes API. On kind the API server listens on the node
        # itself, and traffic from a pod to its own node is not subject to NetworkPolicy
        # (kindnet, like most CNIs). The agent has no API token, so it can't use it.
        return [
            Check(a, "pulsewatch", "example.com", 443, True, "agent: DNS + public internet"),
            Check(a, "pulsewatch", svc(m, "prometheus"), 9090, False, "agent -> Prometheus"),
            Check(a, "pulsewatch", svc(m, "alertmanager"), 9093, False, "agent -> Alertmanager"),
            Check(a, "pulsewatch", svc(m, "grafana"), 3000, False, "agent -> Grafana"),
            Check(a, "pulsewatch", svc(m, "alert-sink"), 9095, False, "agent -> alert sink"),
            Check(m, "alert-sink", svc(a, "pulsewatch"), 8000, False, "only Prometheus scrapes"),
            Check(m, "alert-sink", svc(m, "prometheus"), 9090, False, "sink has no egress"),
            Check(m, "alert-sink", "1.1.1.1", 443, False, "sink has no internet"),
        ]
    if overlay == "multi-region":
        eu, us = "pulsewatch-eu", "pulsewatch-us"
        return [
            Check(eu, "pulsewatch", "example.com", 443, True, "agent: DNS + public internet"),
            Check(us, "pulsewatch", "example.com", 443, True, "agent: DNS + public internet"),
            Check(eu, "pulsewatch", svc(us, "pulsewatch"), 8000, False, "regions are isolated"),
            Check(us, "pulsewatch", svc(eu, "pulsewatch"), 8000, False, "regions are isolated"),
            Check(eu, "pulsewatch", svc(m, "prometheus"), 9090, False, "agent -> Prometheus"),
            Check(us, "pulsewatch", svc(m, "alert-sink"), 9095, False, "agent -> alert sink"),
            Check(m, "alert-sink", svc(eu, "pulsewatch"), 8000, False, "only Prometheus scrapes"),
        ]
    raise ValueError(f"unknown overlay {overlay!r} (kind or multi-region)")


def control_policy(namespace: str) -> str:
    """Temporary allow: probe agent -> Prometheus :9090 (egress for one, ingress for the other).

    Both live in `namespace` for the kind overlay, which is where the control runs.
    """
    rules = []
    for name, selector, direction, peer in (
        ("egress", "pulsewatch", "to", "prometheus"),
        ("ingress", "prometheus", "from", "pulsewatch"),
    ):
        rules.append(
            f"""apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: {CONTROL_POLICY}-{name}
  namespace: {namespace}
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: {selector}
  policyTypes: ["{name.capitalize()}"]
  {name}:
    - {direction}:
        - podSelector:
            matchLabels:
              app.kubernetes.io/name: {peer}
      ports:
        - port: 9090
          protocol: TCP
"""
        )
    return "---\n".join(rules)


Runner = Callable[[list[str], str | None], str]


def kubectl(args: list[str], stdin: str | None = None) -> str:
    res = subprocess.run(
        ["kubectl", *args], input=stdin, capture_output=True, text=True, timeout=60
    )
    if res.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout.strip()


class Cluster:
    def __init__(self, run: Runner = kubectl, sleep: Callable[[float], None] = time.sleep):
        self.run = run
        self.sleep = sleep
        self._ips: dict[str, str] = {}

    def resolve(self, dest: str) -> str:
        if not dest.startswith("svc/"):
            return dest
        if dest not in self._ips:
            _, ns, name = dest.split("/")
            ip = self.run(["-n", ns, "get", "svc", name, "-o", "jsonpath={.spec.clusterIP}"], None)
            self._ips[dest] = ip
        return self._ips[dest]

    def dial(self, check: Check) -> str:
        host = self.resolve(check.dest)
        args = ["-n", check.namespace, "exec", f"deploy/{check.client}", "--"]
        return self.run([*args, "python", "-c", DIAL_PY, host, str(check.port)], None)

    def wait(self, check: Check, timeout: float, interval: float = 3) -> tuple[bool, str]:
        """Policies are programmed asynchronously: retry until the check holds or time runs out."""
        deadline = time.monotonic() + timeout
        while True:
            outcome = self.dial(check)
            if holds(check, outcome) or time.monotonic() >= deadline:
                return holds(check, outcome), outcome
            self.sleep(interval)


def run(overlay: str, cluster: Cluster, timeout: float) -> list[str]:
    """Runs every check; returns the failures (empty = all good)."""
    failures = []
    for check in checks_for(overlay):
        ok, outcome = cluster.wait(check, timeout)
        print(f"{'ok  ' if ok else 'FAIL'} {check.label()}: {outcome}", flush=True)
        if not ok:
            failures.append(f"{check.label()}: got {outcome}")

    if overlay == "kind":
        # Control: the agent -> Prometheus path checked as blocked above opens with an
        # extra allow rule and closes again without it.
        ns = MONITORING_NS
        path = (ns, "pulsewatch", svc(ns, "prometheus"), 9090)
        opened = Check(*path, True, "control: temporary allow rule applied")
        closed = Check(*path, False, "control: temporary allow rule deleted")
        cluster.run(["apply", "-f", "-"], control_policy(ns))
        try:
            ok, outcome = cluster.wait(opened, timeout)
        finally:
            names = [f"{CONTROL_POLICY}-egress", f"{CONTROL_POLICY}-ingress"]
            cluster.run(["-n", ns, "delete", "networkpolicy", "--ignore-not-found", *names], None)
        print(f"{'ok  ' if ok else 'FAIL'} {opened.label()}: {outcome}", flush=True)
        if not ok:
            failures.append(f"{opened.label()}: got {outcome} (is NetworkPolicy enforced?)")
        ok, outcome = cluster.wait(closed, timeout)
        print(f"{'ok  ' if ok else 'FAIL'} {closed.label()}: {outcome}", flush=True)
        if not ok:
            failures.append(f"{closed.label()}: got {outcome}")
    return failures


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("overlay", choices=["kind", "multi-region"])
    p.add_argument("--timeout", type=float, default=45, help="seconds per check (default 45)")
    args = p.parse_args(argv)
    failures = run(args.overlay, Cluster(), args.timeout)
    if failures:
        print(f"{len(failures)} NetworkPolicy check(s) failed:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    print("NetworkPolicy enforced: every check holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
