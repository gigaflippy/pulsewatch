"""Keeps infra/terraform in sync with the overlays it deploys.

`terraform validate` and a real `terraform apply` against kind run in CI; these checks
need no Terraform binary.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TF = ROOT / "infra" / "terraform"
OVERLAYS = ROOT / "deploy" / "k8s" / "overlays"


def _variable_block(name: str) -> str:
    text = (TF / "variables.tf").read_text()
    m = re.search(rf'variable "{name}" \{{(.*?)\n\}}', text, re.S)
    assert m, f"variable {name} not found"
    return m.group(1)


def test_overlay_validation_lists_every_overlay():
    block = _variable_block("overlay")
    allowed = set(re.findall(r'"([^"]+)"', re.search(r"contains\(\[(.*?)\]", block).group(1)))
    on_disk = {p.name for p in OVERLAYS.iterdir() if (p / "kustomization.yaml").exists()}
    assert allowed == on_disk
    default = re.search(r'default\s*=\s*"([^"]+)"', block).group(1)
    assert default in on_disk


def test_image_default_matches_overlay_pins():
    default = re.search(r'default\s*=\s*"([^"]+)"', _variable_block("image")).group(1)
    for k in OVERLAYS.rglob("kustomization.yaml"):
        for img in yaml.safe_load(k.read_text()).get("images", []):
            assert f"{img['newName']}:{img['newTag']}" == default, k


def test_state_and_kubeconfig_never_committed():
    ignored = (TF / ".gitignore").read_text().split()
    for pattern in ("*.tfstate", "kubeconfig", ".terraform/"):
        assert pattern in ignored


def test_node_image_is_pinned():
    """Unpinned, the node image follows the provider's default, and a newer node broke
    `kind load` (containerd config version 4) without any change in this repo."""
    default = re.search(r'default\s*=\s*"([^"]+)"', _variable_block("node_image")).group(1)
    assert re.fullmatch(r"kindest/node:v\d+\.\d+\.\d+(@sha256:[0-9a-f]{64})?", default), default
    assert "node_image      = var.node_image" in (TF / "main.tf").read_text()


LOCK = TF / ".terraform.lock.hcl"


def _required_providers() -> dict[str, str]:
    """source -> version constraint, from versions.tf."""
    text = (TF / "versions.tf").read_text()
    pairs = re.findall(r'source\s*=\s*"([^"]+)"\s*\n\s*version\s*=\s*"([^"]+)"', text)
    assert pairs, "no required_providers found"
    return dict(pairs)


def _locked() -> dict[str, dict]:
    text = LOCK.read_text()
    out = {}
    blocks = re.findall(r'provider "registry\.terraform\.io/([^"]+)" \{(.*?)\n\}', text, re.S)
    for src, body in blocks:
        out[src] = {
            "version": re.search(r'^\s*version\s*=\s*"([^"]+)"', body, re.M).group(1),
            "constraints": re.search(r'constraints\s*=\s*"([^"]+)"', body).group(1),
            "hashes": re.findall(r'"((?:h1|zh):[^"]+)"', body),
        }
    return out


def _satisfies_pessimistic(version: str, constraint: str) -> bool:
    """`~> X.Y` means >= X.Y and < (X+1).0; `~> X.Y.Z` means >= X.Y.Z and < X.(Y+1).0."""
    m = re.fullmatch(r"~>\s*([\d.]+)", constraint)
    assert m, f"only ~> constraints are used here, got {constraint!r}"
    floor = [int(p) for p in m.group(1).split(".")]
    v = [int(p) for p in version.split(".")]
    ceiling = floor[:-1]
    ceiling[-1] += 1
    return v >= floor and v[: len(ceiling)] < ceiling


def test_provider_lockfile_is_committed_and_matches_constraints():
    """Without a lockfile `terraform init` takes the newest release in range, which is how
    tehcyx/kind v0.13.0 broke CI overnight. CI's terraform-lock job regenerates the file and
    fails on any diff; this catches a stale or hand-edited one before CI does."""
    assert LOCK.exists(), "run `make tf-lock` and commit infra/terraform/.terraform.lock.hcl"
    required, locked = _required_providers(), _locked()
    assert set(locked) == set(required)
    for src, constraint in required.items():
        entry = locked[src]
        assert entry["constraints"] == constraint, src
        assert _satisfies_pessimistic(entry["version"], constraint), (src, entry["version"])
        # one h1 per locked platform plus the registry's zh checksums
        assert sum(h.startswith("h1:") for h in entry["hashes"]) >= 5, src
        assert any(h.startswith("zh:") for h in entry["hashes"]), src


def test_lockfile_is_not_ignored_and_ci_enforces_it():
    assert ".terraform.lock.hcl" not in (TF / ".gitignore").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "terraform init -input=false -lockfile=readonly" in ci
    assert "make tf-lock" in ci
    platforms = re.search(r"TF_LOCK_PLATFORMS \?= (.+)", (ROOT / "Makefile").read_text()).group(1)
    assert {"linux_amd64", "darwin_arm64", "windows_amd64"} <= set(platforms.split())


def test_pessimistic_constraint_helper():
    assert _satisfies_pessimistic("0.13.0", "~> 0.6")
    assert not _satisfies_pessimistic("1.0.0", "~> 0.6")
    assert not _satisfies_pessimistic("0.5.9", "~> 0.6")
    assert _satisfies_pessimistic("0.9.4", "~> 0.9.2")
    assert not _satisfies_pessimistic("0.10.0", "~> 0.9.2")
