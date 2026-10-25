"""Short diagnostics cannot change the budget or supply promotion evidence."""

import ast
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.capacity_probe_overlay import apply_overlay
from scripts.run_capacity_diagnostic import mark_diagnostic_helper, validate_budget


def valid_report() -> dict[str, object]:
    return {
        "configuration": {
            "duration_seconds": 120,
            "sends_per_second": 50,
            "concurrent_activity_readers": 100,
            "credential_count": 500,
            "web_workers": "4",
            "web_database_pool_max_size": "18",
            "max_in_flight_sends": 256,
            "drain_seconds": 30,
            "tunnel_count": 10,
        },
        "disposable_volumes_removed": True,
        "release_evidence_eligible": False,
        "observations": {"complete": True},
    }


def test_short_diagnostic_accepts_only_the_fixed_budget() -> None:
    report = valid_report()
    validate_budget(report)
    altered = deepcopy(report)
    configuration = altered["configuration"]
    assert isinstance(configuration, dict)
    configuration["max_in_flight_sends"] = 512
    with pytest.raises(ValueError, match="budget differs"):
        validate_budget(altered)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("release_evidence_eligible", True),
        ("disposable_volumes_removed", False),
        ("observations", {"complete": False}),
    ],
)
def test_short_diagnostic_rejects_missing_proof_boundaries(field: str, value: object) -> None:
    report = valid_report()
    report[field] = value
    with pytest.raises(ValueError):
        validate_budget(report)


def test_overlay_preserves_application_and_await_points(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    files = (
        "tests/load/profile.py",
        "tests/load/run_profile.py",
        "scripts/run_full_load_profile.py",
        "scripts/load_observations.py",
        "scripts/proof_pool_reader.py",
    )
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, path)
    driver = tmp_path / "driver.py"
    shutil.copyfile(root / "tests/load/profile.py", driver)
    application = tmp_path / "src/application.py"
    application.parent.mkdir()
    application.write_text("application_source = 'unchanged'\n")
    baseline_awaits = [
        ast.dump(node)
        for node in ast.walk(ast.parse(driver.read_text()))
        if isinstance(node, ast.Await)
    ]
    overlay = apply_overlay(tmp_path, driver)
    assert application.read_text() == "application_source = 'unchanged'\n"
    assert baseline_awaits == [
        ast.dump(node)
        for node in ast.walk(ast.parse((tmp_path / "tests/load/profile.py").read_text()))
        if isinstance(node, ast.Await)
    ]
    assert isinstance(overlay["files"], dict)
    for name in files:
        compile((tmp_path / name).read_text(), name, "exec")
    assert (
        '"STARTUNNEL_LOAD_DURATION_SECONDS": "120"'
        in (tmp_path / "scripts/run_full_load_profile.py").read_text()
    )


@pytest.mark.parametrize("copies", [0, 1, 2])
def test_diagnostic_label_requires_one_exact_anchor(tmp_path: Path, copies: int) -> None:
    dockerfile = tmp_path / "Dockerfile"
    source = 'LABEL net.startunnel.source="${STARTUNNEL_SOURCE_REVISION}"\n' * copies
    dockerfile.write_text(source)
    if copies == 1:
        checksum = mark_diagnostic_helper(dockerfile)
        assert 'net.startunnel.capacity-diagnostic="true"' in dockerfile.read_text()
        assert len(checksum) == 64
    else:
        with pytest.raises(ValueError, match="missing or ambiguous"):
            mark_diagnostic_helper(dockerfile)
        assert dockerfile.read_text() == source
