"""Static checks for deploy/k8s.

Full rendering and schema validation run in CI (kustomize + kubeconform) and the
kind job deploys for real; these tests catch drift between the manifests and the
code/config they depend on, without needing any Kubernetes tooling.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from pulsewatch.api import create_app
from pulsewatch.config import Settings, load_targets

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"
BASE = K8S / "base"
MONITORING = K8S / "monitoring"
OVERLAY = K8S / "overlays" / "kind"
PROM_CONFIG = ROOT / "deploy" / "prometheus" / "prometheus.yml"
PROM_K8S_CONFIG = ROOT / "deploy" / "prometheus" / "prometheus-k8s.yml"
MULTI = K8S / "overlays" / "multi-region"
CLUSTER_SCOPED = {"Namespace", "ClusterRole", "ClusterRoleBinding"}
GRAFANA_DATASOURCE = ROOT / "deploy" / "grafana" / "provisioning" / "datasources" / "prometheus.yml"


def _docs(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


def _find(kind: str, name: str) -> dict:
    for path in [*BASE.glob("*.yaml"), *MONITORING.glob("*.yaml")]:
        for doc in _docs(path):
            if doc.get("kind") == kind and doc["metadata"]["name"] == name:
                return doc
    raise AssertionError(f"{kind}/{name} not found")


def _container(deployment: dict) -> dict:
    [c] = deployment["spec"]["template"]["spec"]["containers"]
    return c


def _env(container: dict) -> dict[str, str]:
    return {e["name"]: e["value"] for e in container.get("env", [])}


def test_in_cluster_targets_file_is_valid():
    assert load_targets(BASE / "targets.yaml")


def test_pulsewatch_container_matches_app_settings(tmp_path):
    c = _container(_find("Deployment", "pulsewatch"))
    env = _env(c)
    # Every PULSEWATCH_* var must be a real setting, or it is silently ignored.
    known = {f"PULSEWATCH_{name.upper()}" for name in Settings.model_fields}
    unknown = {k for k in env if k.startswith("PULSEWATCH_")} - known
    assert not unknown, f"unknown settings in deployment env: {unknown}"

    assert c["ports"][0]["containerPort"] == Settings().port
    mounts = {m["name"]: m["mountPath"] for m in c["volumeMounts"]}
    assert env["PULSEWATCH_TARGETS_FILE"].startswith(mounts["targets"] + "/")
    assert env["PULSEWATCH_DB_PATH"].startswith(mounts["data"] + "/")

    # Probe path must exist in the app.
    app = create_app(
        Settings(db_path=tmp_path / "x.db", scheduler_enabled=False),
        targets=load_targets(BASE / "targets.yaml"),
    )
    routes = {r.path for r in app.routes}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert c[probe]["httpGet"]["path"] in routes


def test_settings_reject_injected_service_env(monkeypatch):
    """Documents why enableServiceLinks must stay false: Kubernetes would inject this."""
    from pydantic import ValidationError

    monkeypatch.setenv("PULSEWATCH_PORT", "tcp://10.96.0.10:8000")
    with pytest.raises(ValidationError):
        Settings()


def test_pulsewatch_runs_as_dockerfile_user():
    dockerfile = (ROOT / "Dockerfile").read_text()
    pod = _find("Deployment", "pulsewatch")["spec"]["template"]["spec"]
    assert f"--uid {pod['securityContext']['runAsUser']}" in dockerfile


def test_single_writer_for_sqlite():
    spec = _find("Deployment", "pulsewatch")["spec"]
    assert spec["replicas"] == 1
    assert spec["strategy"]["type"] == "Recreate"


@pytest.mark.parametrize(
    "name", ["pulsewatch", "prometheus", "grafana", "alertmanager", "alert-sink"]
)
def test_containers_are_hardened(name):
    dep = _find("Deployment", name)
    pod = dep["spec"]["template"]["spec"]
    c = _container(dep)
    assert pod["securityContext"]["runAsNonRoot"] is True
    # Only Prometheus gets an API token, for pod discovery.
    assert pod["automountServiceAccountToken"] is (name == "prometheus")
    # Service env injection would set PULSEWATCH_PORT=tcp://... and crash Settings().
    assert pod["enableServiceLinks"] is False
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert "memory" in c["resources"]["limits"]
    assert "readinessProbe" in c


def test_services_match_shared_config_hostnames():
    """The compose scrape target and the Grafana datasource URL are hostnames; in k8s they
    resolve to these Services, so names and ports must line up."""
    prom = yaml.safe_load(PROM_CONFIG.read_text())
    [scrape_target] = prom["scrape_configs"][0]["static_configs"][0]["targets"]
    host, port = scrape_target.split(":")
    svc = _find("Service", host)
    assert svc["spec"]["ports"][0]["port"] == int(port)

    ds = yaml.safe_load(GRAFANA_DATASOURCE.read_text())["datasources"][0]
    host, port = ds["url"].removeprefix("http://").split(":")
    assert _find("Service", host)["spec"]["ports"][0]["port"] == int(port)


def test_images_pinned_like_compose():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ("prometheus", "grafana", "alertmanager"):
        assert _container(_find("Deployment", name))["image"] == compose[name]["image"]


def test_overlay_pins_local_image():
    k = yaml.safe_load((OVERLAY / "kustomization.yaml").read_text())
    [img] = k["images"]
    # The alert sink runs from the same locally built image as the probe agents.
    for name in ("pulsewatch", "alert-sink"):
        assert img["name"] == _container(_find("Deployment", name))["image"]
        assert _container(_find("Deployment", name))["imagePullPolicy"] == "IfNotPresent"
    multi = yaml.safe_load((MULTI / "monitoring" / "kustomization.yaml").read_text())
    assert multi["images"] == k["images"]


def test_alert_sink_runs_as_dockerfile_user_and_entry_point():
    dockerfile = (ROOT / "Dockerfile").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    dep = _find("Deployment", "alert-sink")
    pod = dep["spec"]["template"]["spec"]
    assert f"--uid {pod['securityContext']['runAsUser']}" in dockerfile
    [cmd] = _container(dep)["command"]
    assert f'{cmd} = "pulsewatch.alertsink:run"' in pyproject


@pytest.mark.skipif(
    not (shutil.which("kustomize") or shutil.which("kubectl")), reason="no kustomize/kubectl"
)
def test_overlay_renders():
    cmd = ["kustomize", "build"] if shutil.which("kustomize") else ["kubectl", "kustomize"]
    out = subprocess.run([*cmd, str(OVERLAY)], check=True, capture_output=True, text=True).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    workloads = {d["metadata"]["name"] for d in docs if d["kind"] == "Deployment"}
    assert workloads == {"pulsewatch", "prometheus", "grafana", "alertmanager", "alert-sink"}
    assert all(
        d["metadata"].get("namespace") == "pulsewatch"
        for d in docs
        if d["kind"] not in CLUSTER_SCOPED
    )


def _kustomize() -> list[str] | None:
    if shutil.which("kustomize"):
        return ["kustomize", "build"]
    if shutil.which("kubectl"):
        return ["kubectl", "kustomize"]
    return None


def test_k8s_service_discovery_matches_pod_template():
    sd = yaml.safe_load(PROM_K8S_CONFIG.read_text())["scrape_configs"][0]
    [selector] = sd["kubernetes_sd_configs"][0]["selectors"]
    key, value = selector["label"].split("=")
    template = _find("Deployment", "pulsewatch")["spec"]["template"]
    assert template["metadata"]["labels"][key] == value
    ann = template["metadata"]["annotations"]
    assert ann["prometheus.io/scrape"] == "true"
    assert (
        int(ann["prometheus.io/port"])
        == _container(_find("Deployment", "pulsewatch"))["ports"][0]["containerPort"]
    )
    assert sd["job_name"] == "pulsewatch"  # dashboard and CI query up{job="pulsewatch"}


def test_prometheus_rbac_is_read_only():
    docs = _docs(MONITORING / "prometheus-rbac.yaml")
    [role] = [d for d in docs if d["kind"] == "ClusterRole"]
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
        assert "secrets" not in rule["resources"]
    pod = _find("Deployment", "prometheus")["spec"]["template"]["spec"]
    [sa] = [d for d in docs if d["kind"] == "ServiceAccount"]
    assert pod["serviceAccountName"] == sa["metadata"]["name"]


@pytest.mark.skipif(_kustomize() is None, reason="no kustomize/kubectl")
def test_multi_region_overlay():
    out = subprocess.run(
        [*_kustomize(), str(MULTI)], check=True, capture_output=True, text=True
    ).stdout
    docs = [d for d in yaml.safe_load_all(out) if d]
    agents = [
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "pulsewatch"
    ]
    regions = {_env(_container(d))["PULSEWATCH_REGION"] for d in agents}
    namespaces = {d["metadata"]["namespace"] for d in agents}
    assert len(agents) >= 2
    assert len(regions) == len(agents), "each agent needs a distinct region"
    assert len(namespaces) == len(agents), "one namespace per region"
    assert "pulsewatch" not in namespaces  # monitoring namespace stays separate

    monitoring = [
        d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] != "pulsewatch"
    ]
    assert {d["metadata"]["namespace"] for d in monitoring} == {"pulsewatch"}
    [binding] = [d for d in docs if d["kind"] == "ClusterRoleBinding"]
    assert binding["subjects"][0]["namespace"] == "pulsewatch"
