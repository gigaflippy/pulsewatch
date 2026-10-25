"""HTTP API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import timedelta

import httpx
from fastapi import FastAPI, HTTPException, Query, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from pulsewatch import __version__
from pulsewatch.config import Settings, Target, load_targets
from pulsewatch.metrics import Metrics
from pulsewatch.probe import ProbeResult
from pulsewatch.scheduler import Scheduler
from pulsewatch.storage import Storage


def _result_dict(r: ProbeResult) -> dict:
    d = asdict(r)
    d["timestamp"] = r.timestamp.isoformat()
    return d


def create_app(
    settings: Settings | None = None,
    targets: list[Target] | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or Settings()
    if targets is None:
        targets = load_targets(settings.targets_file)
    by_name = {t.name: t for t in targets}
    storage = Storage(settings.db_path)
    metrics = Metrics()
    metrics.register_targets(targets, settings.region)
    scheduler = Scheduler(
        targets,
        storage,
        metrics,
        region=settings.region,
        retention=timedelta(hours=settings.retention_hours),
        client=client,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.scheduler_enabled:
            await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            storage.close()

    app = FastAPI(title="Pulsewatch", version=__version__, lifespan=lifespan)
    app.state.storage = storage
    app.state.scheduler = scheduler
    app.state.metrics = metrics

    def _get_target(name: str) -> Target:
        if name not in by_name:
            raise HTTPException(status_code=404, detail=f"unknown target '{name}'")
        return by_name[name]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "region": settings.region, "version": __version__}

    @app.get("/targets")
    def list_targets() -> list[dict]:
        return [t.model_dump(mode="json") for t in targets]

    @app.get("/status")
    def status(window: int = Query(3600, ge=60, le=7 * 24 * 3600)) -> list[dict]:
        out = []
        for t in targets:
            s = storage.stats(t.name, timedelta(seconds=window))
            d = asdict(s)
            d["last"] = _result_dict(s.last) if s.last else None
            out.append(d)
        return out

    @app.get("/targets/{name}/results")
    def results(name: str, limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
        _get_target(name)
        return [_result_dict(r) for r in storage.recent(name, limit)]

    @app.post("/targets/{name}/check")
    async def check_now(name: str) -> dict:
        """Run one probe immediately (useful for testing a target)."""
        target = _get_target(name)
        return _result_dict(await scheduler.run_once(target))

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app
