"""The `pulsewatch` entry point: settings from the environment, app handed to uvicorn."""

import uvicorn
from fastapi import FastAPI

import pulsewatch.main


def test_run_builds_app_from_env_and_hands_off_to_uvicorn(tmp_path, monkeypatch):
    targets_file = tmp_path / "targets.yaml"
    targets_file.write_text("targets:\n  - name: good\n    url: https://good.test\n")
    monkeypatch.setenv("PULSEWATCH_TARGETS_FILE", str(targets_file))
    monkeypatch.setenv("PULSEWATCH_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.setenv("PULSEWATCH_HOST", "127.0.0.1")
    monkeypatch.setenv("PULSEWATCH_PORT", "8123")
    captured = {}

    def fake_run(app, host, port):
        captured["app"], captured["host"], captured["port"] = app, host, port

    monkeypatch.setattr(uvicorn, "run", fake_run)
    pulsewatch.main.run()
    assert isinstance(captured["app"], FastAPI)
    assert captured["app"].title == "Pulsewatch"
    assert (captured["host"], captured["port"]) == ("127.0.0.1", 8123)
