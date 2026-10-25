import pytest
from pydantic import ValidationError

from pulsewatch.config import Target, TargetsFile, load_targets


def test_load_example_file(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("targets:\n  - name: a\n    url: https://a.test\n    method: head\n")
    [t] = load_targets(p)
    assert t.name == "a"
    assert t.method == "HEAD"
    assert t.interval_seconds == 30
    assert t.expected_status == [200]


def test_example_file_in_repo_is_valid():
    from pathlib import Path

    targets = load_targets(Path(__file__).parent.parent / "targets.example.yaml")
    assert len(targets) >= 1


def test_duplicate_names_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        TargetsFile.model_validate({"targets": [{"name": "a", "url": "https://a.test"}] * 2})


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "has space", "url": "https://a.test"},
        {"name": "a", "url": "not-a-url"},
        {"name": "a", "url": "https://a.test", "interval_seconds": 0.5},
        {"name": "a", "url": "https://a.test", "method": "POST"},
        {"name": "a", "url": "https://a.test", "slo_objective": 1},
        {"name": "a", "url": "https://a.test", "slo_objective": 99.5},
        {"name": "a", "url": "https://a.test", "slo_objective": 0},
    ],
)
def test_invalid_targets(bad):
    with pytest.raises(ValidationError):
        Target.model_validate(bad)


def test_slo_objective_default_and_override():
    assert Target(name="a", url="https://a.test").slo_objective == 0.995
    assert Target(name="a", url="https://a.test", slo_objective=0.99).slo_objective == 0.99
