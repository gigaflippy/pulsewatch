"""Keeps the Alertmanager routing in deploy/alertmanager consistent with the alert rules,
the compose/Kubernetes wiring and start.sh.

CI also runs `amtool check-config` on both configs, `amtool config routes test`, and an
end-to-end alert (probe -> Prometheus rule -> Alertmanager -> alert sink) in compose and kind.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
AM = ROOT / "deploy" / "alertmanager"
BASE_CONFIG = AM / "alertmanager.yml"
TELEGRAM_CONFIG = AM / "alertmanager-telegram.yml"
START = AM / "start.sh"
RULES = ROOT / "deploy" / "prometheus" / "rules.yml"
MONITORING = ROOT / "deploy" / "k8s" / "monitoring"
COMPOSE = ROOT / "docker-compose.yml"
MAKEFILE = ROOT / "Makefile"


def _yaml(path: Path):
    return yaml.safe_load(path.read_text())


def _alert_rules() -> list[dict]:
    return [r for g in _yaml(RULES)["groups"] for r in g["rules"] if "alert" in r]


def _k8s(kind: str, name: str) -> dict:
    for path in MONITORING.glob("*.yaml"):
        for doc in yaml.safe_load_all(path.read_text()):
            if doc and doc["kind"] == kind and doc["metadata"]["name"] == name:
                return doc
    raise AssertionError(f"{kind}/{name} not found")


def _matcher_value(matchers: list[str], label: str) -> str:
    [m] = [m for m in matchers if m.startswith(f"{label}=")]
    return m.split("=", 1)[1].strip('"').lstrip("~").strip('"')


def test_telegram_config_is_base_plus_telegram():
    """The two files may only differ by telegram_configs and the template include."""
    base = _yaml(BASE_CONFIG)
    tg = _yaml(TELEGRAM_CONFIG)
    assert tg.pop("templates") == ["__CONF_DIR__/telegram.tmpl"]
    with_telegram = []
    for r in tg["receivers"]:
        if r.pop("telegram_configs", None):
            with_telegram.append(r["name"])
    assert tg == base
    assert sorted(with_telegram) == ["page", "warn"]


def test_telegram_token_only_from_file_placeholder():
    tg = _yaml(TELEGRAM_CONFIG)
    for r in tg["receivers"]:
        for t in r.get("telegram_configs", []):
            assert "bot_token" not in t, "token must come from bot_token_file"
            assert t["bot_token_file"] == "__TELEGRAM_BOT_TOKEN_FILE__"
            assert t["chat_id"] == "__TELEGRAM_CHAT_ID__"
            assert t["send_resolved"] is True
    # A real token looks like 123456789:AA...; make sure none was ever pasted in.
    for path in AM.iterdir():
        assert not re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", path.read_text()), path


def test_every_severity_has_a_route():
    routes = _yaml(BASE_CONFIG)["route"]["routes"]
    routed = {_matcher_value(r["matchers"], "severity"): r["receiver"] for r in routes}
    severities = {r["labels"]["severity"] for r in _alert_rules()}
    assert severities <= set(routed), f"unrouted severities: {severities - set(routed)}"
    assert routed["page"] == "page" and routed["warn"] == "warn"


def test_routes_reference_defined_receivers():
    cfg = _yaml(BASE_CONFIG)
    names = {r["name"] for r in cfg["receivers"]}
    used = {cfg["route"]["receiver"]} | {r["receiver"] for r in cfg["route"]["routes"]}
    assert used == names


def test_grouping_labels_exist_on_alerts():
    # One notification per alert per target: every Target* alert must keep the label.
    assert _yaml(BASE_CONFIG)["route"]["group_by"] == ["alertname", "target"]
    for rule in _alert_rules():
        if rule["alert"].startswith("Target"):
            assert "$labels.target" in rule["annotations"]["summary"], rule["alert"]


def test_inhibition_matches_real_alerts():
    """Every inhibit rule: a page silences warnings for the same target (or, for the
    target-less meta alerts, globally), and every name it mentions is a real alert
    (a typo would silently inhibit nothing)."""
    alerts = {r["alert"]: r for r in _alert_rules()}
    found = {}
    for rule in _yaml(BASE_CONFIG)["inhibit_rules"]:
        source = _matcher_value(rule["source_matchers"], "alertname")
        targets = _matcher_value(rule["target_matchers"], "alertname").split("|")
        assert alerts[source]["labels"]["severity"] == "page", source
        for t in targets:
            assert t in alerts, t
            assert alerts[t]["labels"]["severity"] == "warn", t
        if source.startswith("Probe"):
            # Meta alerts carry no target label; `equal: [target]` would never match.
            assert "equal" not in rule, source
        else:
            assert rule["equal"] == ["target"]
        found[source] = set(targets)
    assert found == {
        "TargetDown": {"TargetDownInSomeRegions", "TargetSlow", "ErrorBudgetBurnSlow"},
        "ErrorBudgetBurnFast": {"ErrorBudgetBurnSlow"},
        "ProbeAgentsAbsent": {"ProbeRegionMissing"},
    }


def test_receivers_deliver_to_the_alert_sink():
    svc = _k8s("Service", "alert-sink")
    port = svc["spec"]["ports"][0]["port"]
    compose = _yaml(COMPOSE)["services"]
    assert "alert-sink" in compose
    for r in _yaml(BASE_CONFIG)["receivers"]:
        [hook] = r["webhook_configs"]
        assert hook["url"] == f"http://alert-sink:{port}/{r['name']}"
        assert hook["send_resolved"] is True


def test_prometheus_sends_to_alertmanager_service():
    port = _k8s("Service", "alertmanager")["spec"]["ports"][0]["port"]
    for name in ("prometheus.yml", "prometheus-k8s.yml"):
        cfg = _yaml(ROOT / "deploy" / "prometheus" / name)
        [am] = cfg["alerting"]["alertmanagers"]
        assert am["static_configs"][0]["targets"] == [f"alertmanager:{port}"], name
        jobs = {j["job_name"]: j for j in cfg["scrape_configs"]}
        assert jobs["alertmanager"]["static_configs"][0]["targets"] == [f"alertmanager:{port}"]


def test_configmap_ships_every_file_start_sh_needs():
    k = _yaml(AM / "kustomization.yaml")
    [gen] = k["configMapGenerator"]
    shipped = set(gen["files"])
    on_disk = {p.name for p in AM.iterdir() if p.name != "kustomization.yaml"}
    assert shipped == on_disk
    script = START.read_text()
    for name in ("alertmanager.yml", "alertmanager-telegram.yml"):
        assert name in script
    assert "telegram.tmpl" in TELEGRAM_CONFIG.read_text()


def test_compose_and_k8s_run_alertmanager_the_same_way():
    svc = _yaml(COMPOSE)["services"]["alertmanager"]
    dep = _k8s("Deployment", "alertmanager")
    [c] = dep["spec"]["template"]["spec"]["containers"]
    assert c["image"] == svc["image"]
    assert c["command"] == svc["entrypoint"] == ["/bin/sh", "/etc/alertmanager/start.sh"]
    assert c["args"] == svc["command"]
    mounts = {m["mountPath"]: m["name"] for m in c["volumeMounts"]}
    assert mounts["/etc/alertmanager"] == "config"
    assert "/tmp" in mounts, "start.sh renders the Telegram config into /tmp"
    assert svc["volumes"] == ["./deploy/alertmanager:/etc/alertmanager:ro"]


def test_telegram_secret_is_optional_and_matches_makefile():
    [c] = _k8s("Deployment", "alertmanager")["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in c["env"]}
    assert set(env) == {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}
    assert {ref["name"] for ref in env.values()} == {"alertmanager-telegram"}
    assert all(ref["optional"] is True for ref in env.values())
    make = MAKEFILE.read_text()
    assert "name: alertmanager-telegram" in make
    for ref in env.values():
        assert f"{ref['key']}: " in make
    compose_env = _yaml(COMPOSE)["services"]["alertmanager"]["environment"]
    assert set(compose_env) == set(env)


def test_env_and_secrets_dir_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in ignored
    example = (ROOT / ".env.example").read_text()
    assert "TELEGRAM_BOT_TOKEN=\n" in example and "TELEGRAM_CHAT_ID=\n" in example


# --- start.sh -----------------------------------------------------------------

STUB = """#!/bin/sh
# Stands in for /bin/alertmanager: records its arguments and environment.
printf '%s\\n' "$@" > "$OUT/args"
env > "$OUT/env"
"""


@pytest.fixture
def run_start(tmp_path):
    if not shutil.which("sh"):
        pytest.skip("no POSIX sh")
    stub = tmp_path / "alertmanager"
    stub.write_text(STUB)
    stub.chmod(0o755)
    runtime = tmp_path / "runtime"

    def run(**env: str) -> subprocess.CompletedProcess:
        full = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "OUT": str(tmp_path),
            "ALERTMANAGER_CONF_DIR": str(AM),
            "ALERTMANAGER_RUNTIME_DIR": str(runtime),
            "ALERTMANAGER_BIN": str(stub),
            **env,
        }
        return subprocess.run(
            ["sh", str(START), "--storage.path=/alertmanager"],
            env=full,
            capture_output=True,
            text=True,
        )

    run.tmp = tmp_path
    run.runtime = runtime
    return run


def test_start_without_credentials_uses_base_config(run_start):
    res = run_start()
    assert res.returncode == 0, res.stderr
    assert "alert sink only" in res.stdout
    args = (run_start.tmp / "args").read_text().splitlines()
    assert args == [f"--config.file={BASE_CONFIG}", "--storage.path=/alertmanager"]
    assert not run_start.runtime.exists()


def test_start_with_credentials_renders_telegram_config(run_start):
    token = "123456:test-token-not-real"
    res = run_start(TELEGRAM_BOT_TOKEN=token, TELEGRAM_CHAT_ID=" -100123\n")
    assert res.returncode == 0, res.stderr
    assert token not in res.stdout + res.stderr

    rendered = run_start.runtime / "alertmanager.yml"
    token_file = run_start.runtime / "bot_token"
    args = (run_start.tmp / "args").read_text().splitlines()
    assert args[0] == f"--config.file={rendered}"

    assert token_file.read_text() == token
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600

    text = rendered.read_text()
    assert "__" not in text, "unreplaced placeholder"
    assert token not in text
    cfg = yaml.safe_load(text)
    assert cfg["templates"] == [f"{AM}/telegram.tmpl"]
    for r in cfg["receivers"]:
        for t in r.get("telegram_configs", []):
            assert t["chat_id"] == -100123
            assert t["bot_token_file"] == str(token_file)

    # The token is not left in Alertmanager's environment.
    child_env = (run_start.tmp / "env").read_text()
    assert "TELEGRAM_BOT_TOKEN" not in child_env
    assert token not in child_env


@pytest.mark.parametrize(
    "env",
    [
        {"TELEGRAM_BOT_TOKEN": "123:abc"},
        {"TELEGRAM_CHAT_ID": "42"},
        {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "12a"},
        {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "-"},
        {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "1|x"},
    ],
)
def test_start_rejects_partial_or_bad_credentials(run_start, env):
    res = run_start(**env)
    assert res.returncode == 1
    assert not (run_start.tmp / "args").exists()
