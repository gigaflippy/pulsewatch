"""Image scanning: the Trivy gate's policy, and the CI job that runs it."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("trivy_gate", ROOT / "scripts" / "trivy_gate.py")
trivy_gate = importlib.util.module_from_spec(_spec)
sys.modules["trivy_gate"] = trivy_gate  # dataclasses look the module up
_spec.loader.exec_module(trivy_gate)


def _vuln(vid, sev, fixed="", pkg="openssl", installed="3.0.1", title="bad thing"):
    v = {
        "VulnerabilityID": vid,
        "PkgName": pkg,
        "InstalledVersion": installed,
        "Severity": sev,
        "Title": title,
    }
    if fixed:
        v["FixedVersion"] = fixed
    return v


def _report(*results):
    return {
        "SchemaVersion": 2,
        "ArtifactName": "pulsewatch:ci",
        "Results": [{"Target": t, "Vulnerabilities": vs} for t, vs in results],
    }


def _write(tmp_path, report) -> Path:
    p = tmp_path / "trivy.json"
    p.write_text(json.dumps(report))
    return p


def test_fixable_critical_blocks(tmp_path, capsys):
    rep = _report(("debian 12", [_vuln("CVE-1", "CRITICAL", fixed="3.0.2")]))
    assert trivy_gate.main([str(_write(tmp_path, rep))]) == 1
    out = capsys.readouterr().out
    assert "**Blocked:** 1 critical" in out
    assert "::error title=CVE-1 in openssl 3.0.1 (fixed in 3.0.2)::bad thing" in out


@pytest.mark.parametrize(
    "vuln",
    [
        _vuln("CVE-2", "CRITICAL"),  # no fix released yet: reported, not blocking
        _vuln("CVE-3", "HIGH", fixed="1.1"),  # fixable but below the bar
        _vuln("CVE-4", "LOW", fixed="1.1"),
    ],
)
def test_everything_else_is_reported_not_blocking(tmp_path, capsys, vuln):
    rep = _report(("debian 12", [vuln]))
    assert trivy_gate.main([str(_write(tmp_path, rep))]) == 0
    out = capsys.readouterr().out
    assert "**Passed:**" in out
    assert "::error" not in out


def test_clean_and_empty_reports_pass(tmp_path):
    # Trivy writes "Vulnerabilities": null (or leaves it out) for a clean target, and may
    # leave out "Results" entirely when there is nothing to scan.
    clean = {"ArtifactName": "x", "Results": [{"Target": "t", "Vulnerabilities": None}]}
    assert trivy_gate.main([str(_write(tmp_path, clean))]) == 0
    assert trivy_gate.main([str(_write(tmp_path, {"ArtifactName": "x"}))]) == 0


def test_summary_counts_by_severity_and_fix(tmp_path):
    rep = _report(
        ("debian 12", [_vuln("A", "CRITICAL", "2"), _vuln("B", "CRITICAL"), _vuln("C", "HIGH")]),
        ("Python", [_vuln("D", "HIGH", "9", pkg="starlette"), _vuln("E", "weird")]),
    )
    findings = trivy_gate.load(rep)
    assert len(findings) == 5
    text = trivy_gate.summary(findings, "pulsewatch:ci")
    assert "| CRITICAL | 2 | 1 | 1 |" in text
    assert "| HIGH | 2 | 1 | 1 |" in text
    assert "| UNKNOWN | 1 | 0 | 1 |" in text  # unrecognised severity is not dropped
    assert "| A | openssl | 3.0.1 | 2 | debian 12 |" in text


def test_summary_file_is_appended(tmp_path):
    out = tmp_path / "summary.md"
    out.write_text("earlier step\n")
    rep = _report(("debian 12", []))
    trivy_gate.main([str(_write(tmp_path, rep)), "--summary", str(out)])
    text = out.read_text()
    assert text.startswith("earlier step\n")
    assert "### Image scan: `pulsewatch:ci`" in text


def test_annotation_escapes_workflow_command_syntax():
    f = trivy_gate.Finding("CVE-9", "a,b", "1:2", "1:3", "CRITICAL", "t", "50% off\nnext")
    line = trivy_gate.annotation(f)
    title, msg = line.removeprefix("::error title=").split("::", 1)
    assert "," not in title and ":" not in title
    assert "%2C" in title and "%3A" in title
    assert msg == "50%25 off%0Anext. Target: t"


# --- the CI job -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_ci_scans_the_image_and_keeps_the_sbom(ci):
    job = ci["jobs"]["image-scan"]
    script = "\n".join(s.get("run", "") for s in job["steps"])
    assert "docker build -t pulsewatch:ci ." in script
    assert '"$TRIVY_IMAGE"' in script
    assert "--format cyclonedx" in script
    assert "--format json --output /scan/trivy.json pulsewatch:ci" in script
    assert "scripts/trivy_gate.py scan/trivy.json" in script

    upload = next(s for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["if"] == "always()"  # the report is most useful when the gate fails
    assert upload["with"]["path"] == "scan/"


def test_trivy_is_pinned_and_not_a_compromised_release(ci):
    ref = ci["env"]["TRIVY_IMAGE"]
    assert ref.startswith("aquasec/trivy:")
    tag = ref.split(":", 1)[1].split("@", 1)[0]
    # GHSA-69fq-xp46-6x23: these image tags shipped malicious code in March 2026.
    assert tag not in {"0.69.4", "0.69.5", "0.69.6", "latest"}


def test_make_scan_uses_the_ci_trivy_and_gate(ci):
    # `make scan` reads the Trivy ref out of ci.yml with sed, so the two cannot drift; this
    # fails if the env line moves or is re-indented and the sed would come back empty.
    make = (ROOT / "Makefile").read_text()
    assert "sed -n 's/^  TRIVY_IMAGE: //p' .github/workflows/ci.yml" in make
    lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text().splitlines()
    assert [
        ln.removeprefix("  TRIVY_IMAGE: ") for ln in lines if ln.startswith("  TRIVY_IMAGE: ")
    ] == [ci["env"]["TRIVY_IMAGE"]]
    assert "python3 scripts/trivy_gate.py scan/trivy.json" in make
