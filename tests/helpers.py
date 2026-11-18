"""Shared typed helpers for the test suite."""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str) -> ModuleType:
    """Import scripts/<name>.py as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look the module up while loading
    spec.loader.exec_module(module)
    return module


def group(pattern: str, text: str, n: int = 1, flags: int = 0) -> str:
    """re.search that must match; returns the requested group."""
    m = re.search(pattern, text, flags)
    assert m, f"no match for {pattern!r}"
    value = m.group(n)
    assert value is not None, f"group {n} of {pattern!r} did not participate in the match"
    return value


async def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll until condition() is true. Replaces fixed sleeps, which flake on slow CI runners
    (too short) or waste time (too long)."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"condition not met within {timeout}s"
        await asyncio.sleep(0.01)
