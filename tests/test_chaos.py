"""Chaos experiments: the checker script, and its wiring into the Makefile and CI.

The experiments themselves run on kind in the CI `chaos` job; these tests keep the job
honest (every alert it waits for exists, with the severity it expects to be routed by)
and cover scripts/chaos_check.py against a fake Prometheus / Alertmanager / alert sink.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "chaos_check.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"
RULES = ROOT / "deploy" / "prometheus" / "rules.yml"
MAKEFILE = ROOT / "Makefile"
ALERTMANAGER = ROOT / "deploy" / "alertmanager" / "alertmanager.yml"

spec = importlib.util.spec_from_file_location("chaos_check", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def _alerts() -> dict[str, dict]:
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return {r["alert"]: r for g in groups for r in g["rules"] if "alert" in r}


def _chaos_commands() -> list[list[str]]:
    """Every `$CHECK ...` invocation in the CI chaos job, as argv lists."""
    job = yaml.safe_load(CI.read_text())["jobs"]["chaos"]
    cmds = []
    for step in job["steps"]:
        for line in step.get("run", "").splitlines():
            line = line.strip()
            if line.startswith("$CHECK "):
                argv = shlex.split(line[len("$CHECK ") :])
                if argv[0] == "--timeout":
                    argv = argv[2:]
                cmds.append(argv)
    return cmds


# --- pure helpers ---------------------------------------------------------------------


def test_parse_labels():
    assert cc.parse_labels(["target=api", "region=eu-sim"]) == {"target": "api", "region": "eu-sim"}
    assert cc.parse_labels(["summary=a=b"]) == {"summary": "a=b"}
    with pytest.raises(ValueError):
        cc.parse_labels(["no-equals"])
    with pytest.raises(ValueError):
        cc.parse_labels(["1bad=x"])


def test_selector_quotes_values_and_adds_firing_state():
    assert (
        cc.selector("TargetDown", {"target": 'we"ird'})
        == 'ALERTS{alertname="TargetDown", alertstate="firing", target="we\\"ird"}'
    )
    assert cc.selector("ProbeAgentsAbsent", {}, firing=False) == (
        'ALERTS{alertname="ProbeAgentsAbsent"}'
    )


def test_never_fired_query_validates_window():
    q = cc.never_fired_query("TargetDown", {"target": "example"}, "15m")
    assert q.startswith("max_over_time(ALERTS{") and q.endswith("}[15m])")
    with pytest.raises(ValueError):
        cc.never_fired_query("TargetDown", {}, "15m] or vector(1")


def _notification(receiver, status, **labels):
    return {"receiver": receiver, "payload": {"alerts": [{"status": status, "labels": labels}]}}


def test_delivered_filters_receiver_status_and_labels():
    inbox = [
        _notification("warn", "firing", alertname="ProbeRegionMissing", region="us-sim"),
        _notification("warn", "resolved", alertname="ProbeRegionMissing", region="eu-sim"),
        _notification("page", "firing", alertname="ProbeAgentsAbsent"),
    ]
    assert cc.delivered(inbox, "warn", "ProbeRegionMissing", {"region": "us-sim"})
    assert not cc.delivered(inbox, "warn", "ProbeRegionMissing", {"region": "eu-sim"})
    assert cc.delivered(inbox, "warn", "ProbeRegionMissing", {"region": "eu-sim"}, "resolved")
    assert not cc.delivered(inbox, "page", "ProbeRegionMissing", {})
    assert cc.delivered(inbox, "page", "ProbeAgentsAbsent", {})


def test_inhibited_needs_every_matching_alert_suppressed_by_something():
    def am(state, inhibited_by, **labels):
        return {"labels": labels, "status": {"state": state, "inhibitedBy": inhibited_by}}

    want = {"region": "eu-sim"}
    assert not cc.inhibited([], "ProbeRegionMissing", want)  # absent is not inhibited
    held = am("suppressed", ["fp1"], alertname="ProbeRegionMissing", region="eu-sim")
    assert cc.inhibited([held], "ProbeRegionMissing", want)
    # silenced, not inhibited
    silenced = am("suppressed", [], alertname="ProbeRegionMissing", region="eu-sim")
    assert not cc.inhibited([silenced], "ProbeRegionMissing", want)
    active = am("active", [], alertname="ProbeRegionMissing", region="eu-sim")
    assert not cc.inhibited([held, active], "ProbeRegionMissing", want)


# --- the CLI against fake services -----------------------------------------------------


class FakeServices:
    """One HTTP server playing Prometheus, Alertmanager and the alert sink."""

    def __init__(self):
        self.series: dict[str, list] = {}  # PromQL string -> result list
        self.queries: list[str] = []
        self.inbox: list[dict] = []
        self.am_alerts: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                if url.path == "/api/v1/query":
                    q = parse_qs(url.query)["query"][0]
                    fake.queries.append(q)
                    body = {"status": "success", "data": {"result": fake.series.get(q, [])}}
                elif url.path == "/notifications":
                    body = fake.inbox
                elif url.path == "/api/v2/alerts":
                    body = fake.am_alerts
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def fake(monkeypatch):
    f = FakeServices()
    for var in ("PROMETHEUS_URL", "SINK_URL", "ALERTMANAGER_URL"):
        monkeypatch.setenv(var, f.url)
    monkeypatch.setattr(cc.time, "sleep", lambda _: None)
    yield f
    f.server.shutdown()


def test_cli_wait_alert(fake):
    sel = cc.selector("TargetDownInSomeRegions", {"target": "example", "region": "us-sim"})
    assert (
        cc.main(
            [
                "--timeout",
                "0",
                "wait-alert",
                *["TargetDownInSomeRegions", "target=example", "region=us-sim"],
            ]
        )
        == 1
    )
    fake.series[sel] = [{"metric": {}, "value": [0, "1"]}]
    assert (
        cc.main(["wait-alert", "TargetDownInSomeRegions", "target=example", "region=us-sim"]) == 0
    )
    assert fake.queries[-1] == sel


def test_cli_not_firing_and_never_fired(fake):
    assert cc.main(["not-firing", "ProbeAgentsAbsent"]) == 0
    assert cc.main(["never-fired", "TargetDown", "target=example", "--window", "15m"]) == 0
    fake.series[cc.never_fired_query("TargetDown", {"target": "example"}, "15m")] = [
        {"metric": {}, "value": [0, "1"]}
    ]
    assert cc.main(["never-fired", "TargetDown", "target=example", "--window", "15m"]) == 1


def test_cli_delivered(fake):
    assert cc.main(["--timeout", "0", "wait-delivered", "page", "ProbeAgentsAbsent"]) == 1
    assert cc.main(["not-delivered", "page", "ProbeAgentsAbsent"]) == 0
    fake.inbox.append(_notification("page", "firing", alertname="ProbeAgentsAbsent"))
    assert cc.main(["wait-delivered", "page", "ProbeAgentsAbsent"]) == 0
    assert cc.main(["not-delivered", "page", "ProbeAgentsAbsent"]) == 1


def test_cli_wait_value_needs_exactly_one_matching_sample(fake):
    q = "count(count by (region) (pulsewatch_up))"
    fake.series[q] = [{"metric": {}, "value": [0, "1"]}]
    assert cc.main(["--timeout", "0", "wait-value", q, "2"]) == 1
    fake.series[q] = [{"metric": {}, "value": [0, "2"]}]
    assert cc.main(["wait-value", q, "2"]) == 0


def test_cli_wait_inhibited(fake):
    assert cc.main(["--timeout", "0", "wait-inhibited", "ProbeRegionMissing"]) == 1
    fake.am_alerts.append(
        {
            "labels": {"alertname": "ProbeRegionMissing", "region": "eu-sim"},
            "status": {"state": "suppressed", "inhibitedBy": ["abc"]},
        }
    )
    assert cc.main(["wait-inhibited", "ProbeRegionMissing", "region=eu-sim"]) == 0


def test_cli_keeps_waiting_through_connection_errors(monkeypatch):
    monkeypatch.setenv("PROMETHEUS_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setattr(cc.time, "sleep", lambda _: None)
    assert cc.main(["--timeout", "0", "wait-alert", "TargetDown"]) == 1


# --- wiring -------------------------------------------------------------------------------

SEVERITY_FOR_RECEIVER = {"page": "page", "warn": "warn"}


def test_ci_chaos_job_only_checks_real_alerts_on_the_right_receiver():
    alerts = _alerts()
    cmds = _chaos_commands()
    assert cmds, "chaos job has no $CHECK steps"
    for argv in cmds:
        cmd = argv[0]
        if cmd in ("wait-alert", "not-firing", "never-fired", "wait-inhibited"):
            assert argv[1] in alerts, argv
        elif cmd in ("wait-delivered", "not-delivered"):
            receiver, name = argv[1], argv[2]
            assert name in alerts, argv
            assert alerts[name]["labels"]["severity"] == SEVERITY_FOR_RECEIVER[receiver], argv
        else:
            assert cmd == "wait-value", argv


def test_ci_chaos_job_exercises_each_failure_mode():
    waited = {argv[1] for argv in _chaos_commands() if argv[0] == "wait-alert"}
    assert {"TargetDownInSomeRegions", "ProbeRegionMissing", "ProbeAgentsAbsent"} <= waited
    inhibited = {argv[1] for argv in _chaos_commands() if argv[0] == "wait-inhibited"}
    am = yaml.safe_load(ALERTMANAGER.read_text())
    sources = {
        rule["source_matchers"][0]: rule["target_matchers"][0] for rule in am["inhibit_rules"]
    }
    assert sources['alertname="ProbeAgentsAbsent"'] == 'alertname="ProbeRegionMissing"'
    assert inhibited == {"ProbeRegionMissing"}


def test_ci_chaos_job_uses_make_targets_that_exist():
    makefile = MAKEFILE.read_text()
    job = yaml.safe_load(CI.read_text())["jobs"]["chaos"]
    used = set()
    for step in job["steps"]:
        used |= set(re.findall(r"\bmake ([a-z0-9-]+)", step.get("run", "")))
    assert {"chaos-partition", "chaos-kill-region", "chaos-heal"} <= used
    for target in used:
        assert re.search(rf"^{target}:", makefile, re.M), target
        assert target in makefile.split(".PHONY:")[1].split("\n\n")[0], f"{target} not .PHONY"


def test_partition_patch_is_valid_json_with_the_host_variable():
    recipe = MAKEFILE.read_text().split("chaos-partition:")[1].split("\n\n")[0]
    [patch] = re.findall(r"'(\{.*\})'", recipe)
    spec = json.loads(patch.replace("$(CHAOS_HOST)", "example.com"))["spec"]["template"]["spec"]
    assert spec["hostAliases"] == [{"ip": "127.0.0.1", "hostnames": ["example.com"]}]


def test_meta_alerts_cover_lost_agents():
    alerts = _alerts()
    assert alerts["ProbeAgentsAbsent"]["labels"]["severity"] == "page"
    assert "absent(pulsewatch_up)" in alerts["ProbeAgentsAbsent"]["expr"]
    assert alerts["ProbeRegionMissing"]["labels"]["severity"] == "warn"
    assert "by (region)" in alerts["ProbeRegionMissing"]["expr"]
