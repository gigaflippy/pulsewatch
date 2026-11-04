"""Check (or fix) that every container image the repo pulls is pinned by digest.

A tag like ``prom/prometheus:v2.54.1`` can be re-pushed and silently change under you;
``prom/prometheus:v2.54.1@sha256:...`` cannot. The tag stays for humans (and Dependabot),
the digest is what Docker and Kubernetes actually pull.

    python scripts/pin_images.py            # list unpinned refs, exit 1 if any (CI)
    python scripts/pin_images.py --write    # resolve digests with `docker buildx imagetools`
                                            # and rewrite the files in place (make pin-images)
    python scripts/pin_images.py --check-sync   # list copies that differ from docker-compose.yml
    python scripts/pin_images.py --sync     # copy the docker-compose.yml refs everywhere
                                            # else (make sync-images)

docker-compose.yml is the single source of truth for the monitoring images (Prometheus,
Alertmanager, Grafana): it is the file Dependabot bumps. The same image also appears in
deploy/k8s/monitoring/ and the CI env block, and Dependabot cannot update those copies, so
its PR fails the sync check until ``make sync-images`` has been run on the branch.

The Pulsewatch image itself is built locally and side-loaded into kind, so it is skipped.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Where image references live, and how to spot them in each kind of file.
GLOBS = [
    "Dockerfile",
    "docker-compose.yml",
    "deploy/**/*.yaml",
    "deploy/**/*.yml",
    ".github/workflows/*.yml",
    "infra/terraform/*.tf",
]
DOCKERFILE_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(?P<ref>\S+)", re.I | re.M)
YAML_RE = re.compile(
    r"^\s*(?:-\s*)?(?:image|[A-Z][A-Z0-9_]*_IMAGE):\s*[\"']?(?P<ref>[^\s\"'#]+)", re.M
)
TF_RE = re.compile(
    r"^\s*default\s*=\s*\"(?P<ref>[a-z0-9./_-]+:[\w.-]+(?:@sha256:[0-9a-f]{64})?)\"", re.M
)

DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
LOCAL_IMAGES = {"pulsewatch"}
# Refs here win: every other ref to the same image must be identical (tag and digest).
SOURCE = Path("docker-compose.yml")


@dataclass(frozen=True)
class Ref:
    path: Path
    line: int
    ref: str

    @property
    def pinned(self) -> bool:
        return bool(DIGEST_RE.search(self.ref))


def _name(ref: str) -> str:
    """Image name without tag or digest: ``prom/prometheus:v1@sha256:..`` -> ``prom/prometheus``."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    return ref[: len(ref) - len(last)] + last.split(":", 1)[0]


def find_refs(path: Path, text: str) -> list[Ref]:
    """Every external image reference in one file (local images and build stages skipped)."""
    if path.name == "Dockerfile":
        pattern = DOCKERFILE_RE
        stages = set(re.findall(r"^\s*FROM\s+\S+\s+AS\s+(\S+)", text, re.I | re.M))
    elif path.suffix == ".tf":
        pattern, stages = TF_RE, set()
    else:
        pattern, stages = YAML_RE, set()
    refs = []
    for m in pattern.finditer(text):
        ref = m.group("ref")
        if ref.startswith("$") or ref in stages or _name(ref) in LOCAL_IMAGES:
            continue
        refs.append(Ref(path, text.count("\n", 0, m.start("ref")) + 1, ref))
    return refs


def scan(root: Path = ROOT) -> list[Ref]:
    files = sorted({p for g in GLOBS for p in root.glob(g) if p.is_file()})
    return [r for f in files for r in find_refs(f.relative_to(root), f.read_text())]


def out_of_sync(refs: list[Ref] | None = None) -> list[tuple[Ref, str]]:
    """Copies of a docker-compose.yml image that differ from it, with the ref they should be."""
    refs = scan() if refs is None else refs
    want = {_name(r.ref): r.ref for r in refs if r.path == SOURCE}
    return [
        (r, want[_name(r.ref)])
        for r in refs
        if r.path != SOURCE and _name(r.ref) in want and r.ref != want[_name(r.ref)]
    ]


def _replace(text: str, old: str, new: str) -> str:
    """Replace a whole image ref (not a prefix of a longer one) everywhere in ``text``."""
    return re.sub(rf"(?<![\w./@:-]){re.escape(old)}(?![\w.@:-])", lambda _: new, text)


def sync(stale: list[tuple[Ref, str]]) -> None:
    for path in sorted({r.path for r, _ in stale}):
        text = (ROOT / path).read_text()
        for r, want in stale:
            if r.path == path:
                text = _replace(text, r.ref, want)
        (ROOT / path).write_text(text)


def annotate(stale: list[tuple[Ref, str]]) -> None:
    """One line per stale copy; a GitHub annotation on the exact line when run in Actions."""
    for r, want in stale:
        msg = f"{r.ref} differs from {SOURCE}: {want}. Run `make sync-images` and commit."
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error file={r.path},line={r.line},title=Image out of sync::{msg}")
        else:
            print(f"{r.path}:{r.line}: {msg}")


def resolve(ref: str) -> str:
    """``name:tag`` -> ``name:tag@sha256:...`` (the multi-arch index digest, from the registry)."""
    tagged = ref.split("@", 1)[0]
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", tagged, "--format", "{{.Manifest.Digest}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", out):
        raise RuntimeError(f"unexpected digest for {tagged}: {out!r}")
    return f"{tagged}@{out}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="resolve digests and rewrite files")
    mode.add_argument("--check-sync", action="store_true", help=f"copies that differ from {SOURCE}")
    mode.add_argument("--sync", action="store_true", help=f"copy the {SOURCE} refs everywhere")
    args = ap.parse_args(argv)

    if args.check_sync or args.sync:
        stale = out_of_sync()
        if args.sync:
            sync(stale)
            for r, want in stale:
                print(f"{r.path}:{r.line}: {r.ref} -> {want}")
            return 0
        annotate(stale)
        return 1 if stale else 0

    unpinned = [r for r in scan() if not r.pinned]
    if not args.write:
        for r in unpinned:
            print(f"{r.path}:{r.line}: {r.ref} is not pinned by digest")
        return 1 if unpinned else 0

    pins = {ref: resolve(ref) for ref in sorted({r.ref for r in unpinned})}
    for path in sorted({r.path for r in unpinned}):
        text = (ROOT / path).read_text()
        for r in find_refs(path, text):
            if r.ref in pins:
                text = _replace(text, r.ref, pins[r.ref])
        (ROOT / path).write_text(text)
    for old, new in pins.items():
        print(f"{old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
