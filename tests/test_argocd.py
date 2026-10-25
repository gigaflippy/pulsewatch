"""Keeps deploy/argocd in sync with the manifests Argo CD deploys.

CI renders and schema-checks the bootstrap and runs Argo CD on kind for real; these
checks catch drift between the ApplicationSet, the AppProject and the overlays
without any Kubernetes tooling.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ARGOCD = ROOT / "deploy" / "argocd"
K8S = ROOT / "deploy" / "k8s"
MAKEFILE = ROOT / "Makefile"


def _doc(path: Path) -> dict:
    [doc] = [d for d in yaml.safe_load_all(path.read_text()) if d]
    return doc


def _appset() -> dict:
    return _doc(ARGOCD / "bootstrap" / "applicationset.yaml")


def _project() -> dict:
    return _doc(ARGOCD / "bootstrap" / "project.yaml")


def _app_dirs() -> list[Path]:
    [gen] = _appset()["spec"]["generators"]
    [d] = gen["git"]["directories"]
    return sorted(p for p in ROOT.glob(d["path"]) if p.is_dir())


def _all_docs(directory: Path) -> list[dict]:
    """Every manifest a kustomization pulls in, following local resource paths."""
    k = yaml.safe_load((directory / "kustomization.yaml").read_text())
    docs: list[dict] = []
    for res in k.get("resources", []):
        p = (directory / res).resolve()
        if p.is_dir():
            docs += _all_docs(p)
        else:
            docs += [d for d in yaml.safe_load_all(p.read_text()) if d]
    return docs


def test_applicationset_generates_one_app_per_region_and_monitoring():
    names = {p.name for p in _app_dirs()}
    assert names == {"monitoring", "region-eu", "region-us"}
    for p in _app_dirs():
        assert (p / "kustomization.yaml").exists(), p


def test_generated_apps_cover_the_whole_multi_region_overlay():
    # Whatever `kubectl apply -k overlays/multi-region` deploys, Argo CD deploys too.
    top = yaml.safe_load((K8S / "overlays" / "multi-region" / "kustomization.yaml").read_text())
    assert set(top["resources"]) == {p.name for p in _app_dirs()}


def test_repo_and_revision_are_consistent():
    spec = _appset()["spec"]
    [gen] = spec["generators"]
    source = spec["template"]["spec"]["source"]
    repo = source["repoURL"]
    assert gen["git"]["repoURL"] == repo
    assert _project()["spec"]["sourceRepos"] == [repo]
    # HEAD follows the default branch; the Makefile and CI override both fields together.
    assert gen["git"]["revision"] == source["targetRevision"] == "HEAD"
    make = MAKEFILE.read_text()
    assert f"GITOPS_REPO     ?= {repo}" in make
    assert f"s|{repo}|$(GITOPS_REPO)|" in make
    assert "s|revision: HEAD|revision: $(ARGOCD_REVISION)|" in make
    assert "s|targetRevision: HEAD|targetRevision: $(ARGOCD_REVISION)|" in make


def test_template_uses_project_and_automated_sync():
    tmpl = _appset()["spec"]["template"]
    assert tmpl["spec"]["project"] == _project()["metadata"]["name"]
    assert tmpl["spec"]["source"]["path"] == "{{.path.path}}"
    auto = tmpl["spec"]["syncPolicy"]["automated"]
    assert auto["prune"] and auto["selfHeal"]
    # argocd-wait/argocd-status select applications by this label.
    assert tmpl["metadata"]["labels"]["app.kubernetes.io/part-of"] == "pulsewatch"
    assert "app.kubernetes.io/part-of=pulsewatch" in MAKEFILE.read_text()


def test_project_allows_every_namespace_and_cluster_kind_deployed():
    spec = _project()["spec"]
    [dest] = spec["destinations"]
    allowed_kinds = {(r["group"], r["kind"]) for r in spec["clusterResourceWhitelist"]}
    seen_kinds = set()
    for app_dir in _app_dirs():
        k = yaml.safe_load((app_dir / "kustomization.yaml").read_text())
        ns = k["namespace"]
        assert fnmatch.fnmatch(ns, dest["namespace"]), f"{app_dir.name}: {ns}"
        for d in _all_docs(app_dir):
            if d["kind"] in {"Namespace", "ClusterRole", "ClusterRoleBinding"}:
                group = d["apiVersion"].rpartition("/")[0]
                seen_kinds.add((group, d["kind"]))
                if d["kind"] == "Namespace":
                    assert fnmatch.fnmatch(d["metadata"]["name"], dest["namespace"])
    # Exactly what's needed: nothing missing (sync would fail), nothing extra.
    assert seen_kinds == allowed_kinds


def test_argocd_install_is_pinned_to_a_release():
    k = yaml.safe_load((ARGOCD / "install" / "kustomization.yaml").read_text())
    [remote] = [r for r in k["resources"] if r.startswith("https://")]
    assert re.fullmatch(
        r"https://raw\.githubusercontent\.com/argoproj/argo-cd/v\d+\.\d+\.\d+/manifests/install\.yaml",
        remote,
    ), remote
    assert k["namespace"] == "argocd"
    boot = yaml.safe_load((ARGOCD / "bootstrap" / "kustomization.yaml").read_text())
    assert boot["namespace"] == "argocd"


def test_image_is_side_loaded_not_pulled():
    # Argo CD syncs manifests only; the image must already be in the kind node.
    for app_dir in _app_dirs():
        for d in _all_docs(app_dir):
            if d["kind"] == "Deployment" and d["metadata"]["name"] == "pulsewatch":
                [c] = d["spec"]["template"]["spec"]["containers"]
                assert c["imagePullPolicy"] == "IfNotPresent"
    make = MAKEFILE.read_text()
    up = make[make.index("argocd-up:") :]
    assert up.index("kind load docker-image") < up.index("argocd-bootstrap")


def test_argocd_redis_uses_the_side_loaded_image():
    # public.ecr.aws rate-limits; with upstream's imagePullPolicy: Always the kubelet
    # re-checks the registry even though CI side-loads the image, and redis never starts.
    k = yaml.safe_load((ARGOCD / "install" / "kustomization.yaml").read_text())
    patches = [yaml.safe_load(p["patch"]) for p in k["patches"]]
    [redis] = [p for p in patches if p["metadata"]["name"] == "argocd-redis"]
    [c] = redis["spec"]["template"]["spec"]["containers"]
    assert c == {"name": "redis", "imagePullPolicy": "IfNotPresent"}
