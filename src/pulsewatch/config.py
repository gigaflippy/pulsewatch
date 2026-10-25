"""Runtime settings and target definitions."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, HttpUrl, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Target(BaseModel):
    """A single endpoint to probe."""

    name: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.-]+$")
    url: HttpUrl
    interval_seconds: float = Field(default=30.0, ge=1.0)
    timeout_seconds: float = Field(default=5.0, gt=0.0)
    expected_status: list[int] = Field(default_factory=lambda: [200])
    method: str = "GET"
    # Availability objective for this target: the fraction of time it must be up from at
    # least one region, over Prometheus' 7-day SLO window. 0.995 = ~50 min of budget a week.
    slo_objective: float = Field(default=0.995, gt=0.0, lt=1.0)

    @field_validator("method")
    @classmethod
    def _upper_method(cls, v: str) -> str:
        v = v.upper()
        if v not in {"GET", "HEAD"}:
            raise ValueError("method must be GET or HEAD")
        return v


class TargetsFile(BaseModel):
    targets: list[Target]

    @field_validator("targets")
    @classmethod
    def _unique_names(cls, v: list[Target]) -> list[Target]:
        names = [t.name for t in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate target names: {sorted(dupes)}")
        return v


class Settings(BaseSettings):
    """Configured via PULSEWATCH_* environment variables."""

    model_config = SettingsConfigDict(env_prefix="PULSEWATCH_")

    targets_file: Path = Path("targets.yaml")
    db_path: Path = Path("pulsewatch.db")
    region: str = "local"
    host: str = "0.0.0.0"
    port: int = 8000
    retention_hours: int = Field(default=72, ge=1)
    scheduler_enabled: bool = True


def load_targets(path: Path) -> list[Target]:
    data = yaml.safe_load(path.read_text()) or {}
    return TargetsFile.model_validate(data).targets
