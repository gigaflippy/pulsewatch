"""Reproducible builds: hash-locked Python dependencies, digest-pinned images,
SHA-pinned actions, and Dependabot to keep every pin current."""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LOCKS = ("requirements.txt", "requirements-build.txt", "requirements-dev.txt")
DIGEST = "@sha256:" + "a" * 64

_spec = importlib.util.spec_from_file_location("pin_images", ROOT / "scripts" / "pin_images.py")
pin_images = importlib.util.module_from_spec(_spec)
sys.modules["pin_images"] = pin_images  # dataclasses look the module up
_spec.loader.exec_module(pin_images)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(lockfile: str) -> dict[str, str]:
    text = (ROOT / lockfile).read_text()
    return {_norm(n): v for n, v in re.findall(r"^([A-Za-z0-9_.-]+)==(\S+)", text, re.M)}


def _requirement_names(reqs: list[str]) -> set[str]:
    return {_norm(re.match(r"[A-Za-z0-9_.-]+", r).group(0)) for r in reqs}


PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


@pytest.mark.parametrize("lockfile", LOCKS)
def test_every_lock_entry_is_exact_and_hashed(lockfile):
    entries = re.split(r"\n(?=[A-Za-z0-9])", (ROOT / lockfile).read_text())
    pkgs = [e for e in entries if re.match(r"[A-Za-z0-9]", e)]
    assert pkgs, f"{lockfile} is empty"
    for entry in pkgs:
        assert re.match(r"[A-Za-z0-9_.-]+==\S+ \\", entry), (
            f"{lockfile}: not an exact pin: {entry[:60]}"
        )
        assert "--hash=sha256:" in entry, f"{lockfile}: no hash for {entry.split()[0]}"
    assert "make lock" in (ROOT / lockfile).read_text()


def test_runtime_lock_covers_pyproject_dependencies():
    assert _requirement_names(PYPROJECT["project"]["dependencies"]) <= set(
        _pins("requirements.txt")
    )


def test_build_lock_covers_build_backend():
    assert _requirement_names(PYPROJECT["build-system"]["requires"]) <= set(
        _pins("requirements-build.txt")
    )


def test_dev_lock_covers_everything_at_the_same_versions():
    dev = _pins("requirements-dev.txt")
    assert _requirement_names(PYPROJECT["project"]["optional-dependencies"]["dev"]) <= set(dev)
    for lockfile in ("requirements.txt", "requirements-build.txt"):
        for name, version in _pins(lockfile).items():
            assert dev.get(name) == version, f"{name}: {lockfile} {version} vs dev {dev.get(name)}"


def test_runtime_lock_ships_no_build_or_dev_tools():
    shipped = set(_pins("requirements.txt"))
    assert not shipped & {"hatchling", "pytest", "ruff", "pip-tools"}


def test_dockerfile_installs_only_from_the_locks():
    text = (ROOT / "Dockerfile").read_text()
    installs = [line for line in text.splitlines() if "pip install" in line]
    assert installs
    for line in installs:
        assert "--require-hashes" in line or "--no-deps" in line, line
    assert "--no-build-isolation" in text  # the locked hatchling builds the wheel, not a fresh one


def test_ci_installs_from_the_lock():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "pip install -e '.[dev]'" not in ci
    assert "pip install ruff" not in ci
    for line in re.findall(r"pip install[^\n]*", ci):
        assert "--require-hashes" in line or "--no-deps" in line, line


def test_actions_are_pinned_to_commit_shas():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)(.*)", ci)
    assert uses
    for ref, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
        assert re.search(r"# v\d+\.\d+\.\d+", comment), f"{ref}: add the version as a comment"


def test_every_pulled_image_is_pinned_by_digest():
    refs = pin_images.scan()
    # The scan really sees the images (a broken regex would pass vacuously).
    names = {pin_images._name(r.ref) for r in refs}
    assert {"python", "prom/prometheus", "prom/alertmanager", "grafana/grafana-oss"} <= names
    assert "kindest/node" in names
    assert [f"{r.path}:{r.line} {r.ref}" for r in refs if not r.pinned] == []


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("Dockerfile", "FROM python:3.13-slim AS build\nFROM build\n", ["python:3.13-slim"]),
        ("Dockerfile", "FROM --platform=$BUILDPLATFORM golang:1.23\n", ["golang:1.23"]),
        ("x.yaml", "      - image: busybox\n        image: 'pulsewatch'\n", ["busybox"]),
        ("x.yaml", "  image: pulsewatch:dev\n  IMAGE: x\n  FOO_IMAGE: a/b:1\n", ["a/b:1"]),
        ("x.yaml", "  image: $ALERTMANAGER_IMAGE\n", []),
        (
            "v.tf",
            'default = "kindest/node:v1.35.0"\ndefault = "pulsewatch:dev"\n',
            ["kindest/node:v1.35.0"],
        ),
        ("v.tf", 'default = "pulsewatch"\n', []),
    ],
)
def test_find_refs(name, text, expected):
    assert [r.ref for r in pin_images.find_refs(Path(name), text)] == expected


def test_pinned_detection_and_names():
    assert pin_images.Ref(Path("x"), 1, "prom/prometheus:v2" + DIGEST).pinned
    assert not pin_images.Ref(Path("x"), 1, "prom/prometheus:v2").pinned
    assert pin_images._name("localhost:5000/a/b:1" + DIGEST) == "localhost:5000/a/b"
    assert pin_images._name("python:3.13-slim") == "python"


def test_write_pins_every_occurrence_once(tmp_path, monkeypatch):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim AS build\nFROM python:3.13-slim\n")
    monkeypatch.setattr(pin_images, "ROOT", tmp_path)
    monkeypatch.setattr(pin_images, "resolve", lambda ref: ref + DIGEST)
    monkeypatch.setattr(
        pin_images,
        "scan",
        lambda: pin_images.find_refs(Path("Dockerfile"), (tmp_path / "Dockerfile").read_text()),
    )
    assert pin_images.main(["--write"]) == 0
    text = (tmp_path / "Dockerfile").read_text()
    assert text.count("python:3.13-slim" + DIGEST) == 2
    assert pin_images.main(["--write"]) == 0  # idempotent
    assert (tmp_path / "Dockerfile").read_text() == text
    assert pin_images.main([]) == 0


def test_dependabot_covers_every_pinned_ecosystem():
    cfg = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text())
    assert cfg["version"] == 2
    got = {(u["package-ecosystem"], u["directory"]) for u in cfg["updates"]}
    assert got == {
        ("pip", "/"),
        ("docker", "/"),
        ("docker-compose", "/"),
        ("github-actions", "/"),
        ("terraform", "/infra/terraform"),
    }
    assert all(u["schedule"]["interval"] for u in cfg["updates"])
