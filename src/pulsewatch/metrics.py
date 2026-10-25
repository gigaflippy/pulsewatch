"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from pulsewatch.config import Target
from pulsewatch.probe import ProbeResult

LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry()
        labels = ("target", "region")
        self.up = Gauge(
            "pulsewatch_up",
            "1 if the last probe succeeded, 0 otherwise",
            labels,
            registry=self.registry,
        )
        self.latency = Histogram(
            "pulsewatch_probe_latency_seconds",
            "Probe round-trip latency in seconds (any HTTP response; not timeouts or errors)",
            labels,
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.probes = Counter(
            "pulsewatch_probes_total",
            "Probes run, by outcome",
            (*labels, "result"),
            registry=self.registry,
        )
        self.slo_objective = Gauge(
            "pulsewatch_slo_objective",
            "Availability objective for the target (fraction of time up), from targets.yaml",
            labels,
            registry=self.registry,
        )

    def register_targets(self, targets: list[Target], region: str) -> None:
        """Export each target's SLO objective and start its probe counters at zero.

        Zero-valued counters mean rate() sees the first failure as an increase instead of
        a brand-new series, and dashboards show 0 rather than "No data" before it.
        """
        for t in targets:
            self.slo_objective.labels(t.name, region).set(t.slo_objective)
            for result in ("up", "down"):
                self.probes.labels(t.name, region, result)

    def observe(self, r: ProbeResult) -> None:
        self.up.labels(r.target, r.region).set(1 if r.up else 0)
        self.probes.labels(r.target, r.region, "up" if r.up else "down").inc()
        if r.latency_ms is not None:
            self.latency.labels(r.target, r.region).observe(r.latency_ms / 1000)
