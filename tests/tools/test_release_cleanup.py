"""Keep release cache cleanup narrow, checked, and safe before deletion."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scripts import release_cleanup


@pytest.fixture
def artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(release_cleanup, "ROOT", tmp_path)
    directory = tmp_path / "artifacts"
    directory.mkdir()
    return directory


def write(path: Path, content: str = "cache") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_preview_lists_known_caches_once_in_path_order_and_keeps_unknown_files(
    artifacts: Path,
) -> None:
    first = write(artifacts / "release/scanners/tool", "binary")
    write(artifacts / "release/scanners/__pycache__/helper.pyc", "bytecode")
    second = write(artifacts / "release/trivy-cache/db/metadata.json", "{}")
    report = write(artifacts / "release/load-run.json", '{"status": "failed"}')
    unknown = write(artifacts / "unknown/keep.bin")

    result = release_cleanup.cleanup()

    assert result == {
        "applied": False,
        "targets": [
            {"path": "artifacts/release/scanners", "bytes": 14},
            {"path": "artifacts/release/trivy-cache", "bytes": 2},
        ],
        "total_bytes": 16,
    }
    assert all(path.exists() for path in [first, second, report, unknown])


def test_apply_removes_only_known_caches_and_is_idempotent(artifacts: Path) -> None:
    cache = write(artifacts / "one/__pycache__/helper.pyc")
    outside_scope = write(artifacts / "two/scanners/tool")
    report = write(artifacts / "one/proof-system.json", '{"status": "passed"}')

    result = release_cleanup.cleanup(Path("artifacts/one"), apply=True)

    assert result["applied"]
    assert result["total_bytes"] == 5
    assert not cache.parent.exists()
    assert report.exists() and outside_scope.exists()
    assert release_cleanup.cleanup(Path("artifacts/one"), apply=True)["targets"] == []


@pytest.mark.parametrize("relative", [".env", "secrets/key", "private.pem", "credentials.json"])
def test_credentials_stop_all_deletion_before_the_first_cache(
    artifacts: Path, relative: str
) -> None:
    safe = write(artifacts / "a/scanners/tool")
    private = write(artifacts / "z/trivy-cache" / relative)

    with pytest.raises(ValueError, match="credentials"):
        release_cleanup.cleanup(apply=True)

    assert safe.exists() and private.exists()


@pytest.mark.parametrize("name", ["failed.json", "observations.jsonl", "report.sarif", "scan.log"])
def test_report_inside_a_cache_stops_cleanup(artifacts: Path, name: str) -> None:
    report = write(artifacts / "scanners" / name, "{}")
    with pytest.raises(ValueError, match="report"):
        release_cleanup.cleanup(apply=True)
    assert report.exists()


def test_retained_manifest_protects_referenced_cache_files(artifacts: Path) -> None:
    protected = write(artifacts / "attempt/scanners/tool")
    write(
        artifacts / "attempt/proof-load.json",
        json.dumps({"files": {"scanners/tool": "checksum"}}),
    )
    with pytest.raises(ValueError, match="proof evidence"):
        release_cleanup.cleanup(apply=True)
    assert protected.exists()


def test_unsafe_manifest_reference_stops_cleanup_before_deletion(artifacts: Path) -> None:
    safe = write(artifacts / "scanners/tool")
    write(artifacts / "proof-load.json", json.dumps({"files": {"../outside": "checksum"}}))
    with pytest.raises(ValueError, match="unsafe file reference"):
        release_cleanup.cleanup(apply=True)
    assert safe.exists()


@pytest.mark.parametrize("link_type", ["cache", "parent", "file", "manifest"])
def test_symlinks_cannot_escape_the_artifact_tree(artifacts: Path, link_type: str) -> None:
    safe = write(artifacts / "a/scanners/tool")
    external = write(artifacts.parent / "external/keep")
    selected = Path("artifacts")
    if link_type == "cache":
        (artifacts / "scanners").symlink_to(external.parent, target_is_directory=True)
    elif link_type == "parent":
        (artifacts / "linked").symlink_to(external.parent, target_is_directory=True)
        selected = Path("artifacts/linked/scanners")
    elif link_type == "file":
        directory = artifacts / "z/scanners"
        directory.mkdir(parents=True)
        (directory / "linked").symlink_to(external)
    else:
        (artifacts / "proof-load.json").symlink_to(external)
    with pytest.raises(ValueError, match="symbolic links"):
        release_cleanup.cleanup(selected, apply=True)
    assert external.read_text() == "cache" and safe.exists()


def test_artifacts_root_cannot_be_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_cleanup, "ROOT", tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (tmp_path / "artifacts").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic links"):
        release_cleanup.cleanup(apply=True)


@pytest.mark.parametrize("directory", [Path("other"), Path("artifacts/../other")])
def test_cleanup_refuses_locations_outside_artifacts(artifacts: Path, directory: Path) -> None:
    with pytest.raises(ValueError, match="inside repository artifacts"):
        release_cleanup.cleanup(directory, apply=True)


def test_cli_json_preview_has_only_relative_targets(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = write(artifacts / "release/scanners/tool")
    monkeypatch.setattr(sys, "argv", ["release-cleanup", "--json"])
    assert release_cleanup.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "applied": False,
        "targets": [{"path": "artifacts/release/scanners", "bytes": 5}],
        "total_bytes": 5,
    }
    assert tool.exists()


def test_cli_human_apply_reports_removed_paths(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = write(artifacts / "scanners/tool")
    monkeypatch.setattr(sys, "argv", ["release-cleanup", "--apply"])
    assert release_cleanup.main() == 0
    output = capsys.readouterr().out
    assert "Removed caches:" in output
    assert "artifacts/scanners: 5 bytes" in output
    assert not tool.exists()


def test_cli_rejects_an_unsafe_target_before_any_deletion(
    artifacts: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tool = write(artifacts / "scanners/tool")
    monkeypatch.setattr(
        sys, "argv", ["release-cleanup", "--directory", "artifacts/../other", "--apply"]
    )
    with pytest.raises(SystemExit) as stopped:
        release_cleanup.main()
    assert stopped.value.code == 2
    assert "inside repository artifacts" in capsys.readouterr().err
    assert tool.exists()


def test_missing_artifacts_is_an_empty_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_cleanup, "ROOT", tmp_path)
    assert release_cleanup.cleanup() == {"applied": False, "targets": [], "total_bytes": 0}
