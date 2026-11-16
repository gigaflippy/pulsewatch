"""Runs probes on a fixed interval per target."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from datetime import timedelta

import httpx

from pulsewatch.config import Target
from pulsewatch.metrics import Metrics
from pulsewatch.probe import ProbeResult, probe
from pulsewatch.storage import Storage

log = logging.getLogger(__name__)

PRUNE_EVERY_SECONDS = 3600


class Scheduler:
    def __init__(
        self,
        targets: list[Target],
        storage: Storage,
        metrics: Metrics,
        region: str,
        retention: timedelta,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.targets = targets
        self.storage = storage
        self.metrics = metrics
        self.region = region
        self.retention = retention
        self._client = client
        self._owns_client = client is None
        self._tasks: list[asyncio.Task[None]] = []

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"User-Agent": "pulsewatch/0.1"})
        return self._client

    async def run_once(self, target: Target) -> ProbeResult:
        client = self._ensure_client()
        result = await probe(client, target, self.region)
        self.storage.add(result)
        self.metrics.observe(result)
        if not result.up:
            log.warning("target %s down: %s", target.name, result.error)
        return result

    async def _loop(self, target: Target) -> None:
        # Spread start times so targets don't all fire at once.
        await asyncio.sleep(random.uniform(0, min(target.interval_seconds, 5)))
        while True:
            try:
                await self.run_once(target)
            except Exception:  # keep the loop alive no matter what
                log.exception("probe loop error for %s", target.name)
            await asyncio.sleep(target.interval_seconds)

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(PRUNE_EVERY_SECONDS)
            removed = self.storage.prune(self.retention)
            if removed:
                log.info("pruned %d old results", removed)

    async def start(self) -> None:
        self._ensure_client()
        self._tasks = [asyncio.create_task(self._loop(t)) for t in self.targets]
        self._tasks.append(asyncio.create_task(self._prune_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
