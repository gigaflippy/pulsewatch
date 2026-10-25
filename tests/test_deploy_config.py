"""Static checks for the Prometheus/Grafana config under deploy/.

These catch the mistakes that otherwise only show up as an empty panel in Grafana:
a metric renamed in code but not in the dashboard, a datasource uid typo, a
variable used in a query but never defined.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from pulsewatch.metrics import Metrics

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
DASHBOARD = DEPLOY / "grafana" / "dashboards" / "pulsewatch.json"
DATASOURCES = DEPLOY / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
RULES = DEPLOY / "prometheus" / "rules.yml"
COMPOSE = ROOT / "docker-compose.yml"

# PromQL words that look like metric names but aren't.
PROMQL_WORDS = {
    "sum",
    "count",
    "group",
    "avg",
    "min",
    "max",
    "by",
    "without",
    "or",
    "and",
    "unless",
    "on",
    "ignoring",
    "rate",
    "irate",
    "increase",
    "vector",
    "histogram_quantile",
    "avg_over_time",
    "max_over_time",
    "min_over_time",
    "absent",
    "label_values",
    "le",
    "target",
    "region",
    "result",
    "integration",
}
# Series Prometheus itself creates.
BUILTIN_SERIES = {"ALERTS", "ALERTS_FOR_STATE", "up"}
# Series scraped from Alertmanager (job "alertmanager"), used by the delivery alert.
ALERTMANAGER_SERIES = {"alertmanager_notifications_failed_total"}


def _exported_metric_names() -> set[str]:
    names = set()
    for metric in Metrics().registry.collect():
        names.add(metric.name)
        for sample in metric.samples:
            names.add(sample.name)
        # Histograms/counters expose suffixed series even before any sample exists.
        if metric.type == "histogram":
            names |= {f"{metric.name}_{s}" for s in ("bucket", "sum", "count")}
        if metric.type == "counter":
            names.add(f"{metric.name}_total")
    return names


def _recording_rule_names() -> set[str]:
    data = yaml.safe_load(RULES.read_text())
    return {r["record"] for g in data["groups"] for r in g["rules"] if "record" in r}


def _metric_names_in(expr: str) -> set[str]:
    # Drop label matchers, strings, ranges and Grafana variables, then pick identifiers.
    stripped = re.sub(r"\{[^}]*\}", " ", expr)
    stripped = re.sub(r'"[^"]*"', " ", stripped)
    stripped = re.sub(r"\[[^\]]*\]", " ", stripped)
    stripped = re.sub(r"\$\w+", " ", stripped)
    idents = set(re.findall(r"[a-zA-Z_:][a-zA-Z0-9_:]*", stripped))
    return {i for i in idents if i not in PROMQL_WORDS}


@pytest.fixture(scope="module")
def dashboard() -> dict:
    return json.loads(DASHBOARD.read_text())


def _queries(dashboard: dict) -> list[str]:
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    exprs += [v["query"]["query"] for v in dashboard["templating"]["list"]]
    return exprs


def test_dashboard_panel_ids_unique(dashboard):
    ids = [p["id"] for p in dashboard["panels"]]
    assert len(ids) == len(set(ids))


def test_dashboard_has_stable_uid(dashboard):
    # Links and the compose home-dashboard setting depend on this not changing.
    assert dashboard["uid"] == "pulsewatch-overview"


def test_dashboard_datasource_matches_provisioning(dashboard):
    provisioned = {d["uid"] for d in yaml.safe_load(DATASOURCES.read_text())["datasources"]}
    used = {p["datasource"]["uid"] for p in dashboard["panels"]}
    used |= {t["datasource"]["uid"] for p in dashboard["panels"] for t in p["targets"]}
    used |= {v["datasource"]["uid"] for v in dashboard["templating"]["list"]}
    assert used <= provisioned


def test_dashboard_queries_reference_known_series(dashboard):
    known = _exported_metric_names() | _recording_rule_names() | BUILTIN_SERIES
    for expr in _queries(dashboard):
        unknown = _metric_names_in(expr) - known
        assert not unknown, f"{expr!r} references unknown series {sorted(unknown)}"


def test_dashboard_variables_are_defined(dashboard):
    defined = {v["name"] for v in dashboard["templating"]["list"]}
    builtin = {"__range", "__interval", "__rate_interval"}
    for expr in _queries(dashboard):
        used = set(re.findall(r"\$(\w+)", expr))
        assert used <= defined | builtin, expr


def test_recording_rules_use_known_metrics():
    known = _exported_metric_names()
    data = yaml.safe_load(RULES.read_text())
    recorded = _recording_rule_names()
    for group in data["groups"]:
        for rule in group["rules"]:
            unknown = (
                _metric_names_in(rule["expr"])
                - known
                - recorded
                - ALERTMANAGER_SERIES
                - BUILTIN_SERIES
            )
            assert not unknown, f"{rule!r} references unknown series {sorted(unknown)}"


def test_compose_mounts_exist():
    compose = yaml.safe_load(COMPOSE.read_text())
    for name, svc in compose["services"].items():
        for vol in svc.get("volumes", []):
            src = vol.split(":")[0]
            if src.startswith("./"):
                assert (ROOT / src).exists(), f"{name}: missing mount source {src}"


def test_metric_name_extraction():
    expr = 'sum by (target) (rate(pulsewatch_probes_total{result="down"}[5m])) or vector(0)'
    assert _metric_names_in(expr) == {"pulsewatch_probes_total"}
