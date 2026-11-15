"""Decide whether a container image may ship, from a Trivy JSON report.

Trivy lists every known vulnerability in the image. This script is the policy on top:

* **Fails** on a CRITICAL vulnerability that has a fix (a newer package exists). Those are
  the ones you can act on today, by rebuilding on a newer base image or bumping a lockfile.
* **Reports, but does not fail on**, everything else: lower severities, and criticals with no
  fix yet. Failing on those would block every build until upstream ships a patch, which
  teaches people to ignore the check.

    trivy image --format json --output trivy.json pulsewatch:ci
    python scripts/trivy_gate.py trivy.json                  # exit 1 if blocked
    python scripts/trivy_gate.py trivy.json --summary out.md # also write a markdown table

In GitHub Actions every blocking finding is printed as an ``::error::`` annotation, so it
shows on the PR without opening the job log.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]
BLOCKING = {"CRITICAL"}


@dataclass(frozen=True)
class Finding:
    vuln_id: str
    package: str
    installed: str
    fixed: str
    severity: str
    target: str
    title: str

    @property
    def fixable(self) -> bool:
        return bool(self.fixed)

    @property
    def blocking(self) -> bool:
        return self.severity in BLOCKING and self.fixable


def load(report: dict) -> list[Finding]:
    """Every vulnerability in a Trivy JSON report (schema v2), one entry per package hit."""
    findings = []
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            severity = (v.get("Severity") or "UNKNOWN").upper()
            findings.append(
                Finding(
                    vuln_id=v.get("VulnerabilityID", "?"),
                    package=v.get("PkgName", "?"),
                    installed=v.get("InstalledVersion", ""),
                    fixed=v.get("FixedVersion") or "",
                    severity=severity if severity in SEVERITIES else "UNKNOWN",
                    target=result.get("Target", "?"),
                    title=(v.get("Title") or "").strip(),
                )
            )
    return findings


def summary(findings: list[Finding], image: str) -> str:
    """Markdown: counts per severity (fixable / no fix yet), then the blocking findings."""
    total = Counter(f.severity for f in findings)
    fixable = Counter(f.severity for f in findings if f.fixable)
    blocking = [f for f in findings if f.blocking]
    lines = [
        f"### Image scan: `{image}`",
        "",
        "| Severity | Total | Fix available | No fix yet |",
        "|---|---:|---:|---:|",
    ]
    for s in SEVERITIES:
        lines.append(f"| {s} | {total[s]} | {fixable[s]} | {total[s] - fixable[s]} |")
    lines.append("")
    if blocking:
        lines += [
            f"**Blocked:** {len(blocking)} critical vulnerabilities with a fix available.",
            "",
            "| ID | Package | Installed | Fixed in | Where |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {f.vuln_id} | {f.package} | {f.installed} | {f.fixed} | {f.target} |"
            for f in sorted(blocking, key=lambda f: (f.package, f.vuln_id))
        ]
    else:
        lines.append("**Passed:** no critical vulnerability with a fix available.")
    return "\n".join(lines) + "\n"


def _escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(s: str) -> str:
    return _escape_data(s).replace(":", "%3A").replace(",", "%2C")


def annotation(f: Finding) -> str:
    """A GitHub Actions ``::error::`` workflow command for one blocking finding."""
    title = f"{f.vuln_id} in {f.package} {f.installed} (fixed in {f.fixed})"
    msg = f"{f.title or f.vuln_id}. Target: {f.target}"
    return f"::error title={_escape_property(title)}::{_escape_data(msg)}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("report", type=Path, help="Trivy JSON report (trivy image --format json)")
    ap.add_argument("--summary", type=Path, help="append a markdown summary to this file")
    args = ap.parse_args(argv)

    report = json.loads(args.report.read_text())
    image = report.get("ArtifactName", str(args.report))
    findings = load(report)
    text = summary(findings, image)
    print(text)
    if args.summary:
        with args.summary.open("a") as fh:
            fh.write(text)

    blocking = [f for f in findings if f.blocking]
    for f in blocking:
        print(annotation(f))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
