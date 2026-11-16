from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from pydantic import HttpUrl

from pulsewatch.config import Target


def make_handler(routes: dict[str, int | Exception]) -> Callable[[httpx.Request], httpx.Response]:
    """Build a MockTransport handler: host -> status code or exception to raise."""

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = routes[request.url.host]
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    return handler


@pytest.fixture
def targets() -> list[Target]:
    return [
        Target(name="good", url=HttpUrl("https://good.test")),
        Target(name="bad", url=HttpUrl("https://bad.test")),
        Target(name="slow", url=HttpUrl("https://slow.test"), timeout_seconds=0.1),
    ]


@pytest.fixture
def mock_client() -> httpx.AsyncClient:
    routes: dict[str, int | Exception] = {
        "good.test": 200,
        "bad.test": 503,
        "slow.test": httpx.ReadTimeout("timed out"),
    }
    return httpx.AsyncClient(transport=httpx.MockTransport(make_handler(routes)))
