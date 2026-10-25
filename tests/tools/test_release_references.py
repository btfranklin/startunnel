"""Keep manual current-release references aligned with the release specification."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from scripts import check_release_references as references
from scripts import release_record

VERSION = "v0.9.2"
REPOSITORY = "https://github.com/btfranklin/startunnel/blob"
SUPPORTED = ["v0.2.0", "v0.8.0"]


def write_supported_predecessors(root: Path, versions: list[str]) -> None:
    specification_path = root / f"releases/{VERSION}.json"
    specification = json.loads(specification_path.read_text())
    specification["supported_from"] = versions
    specification_path.write_text(json.dumps(specification))

    maintain_path = root / "docs/setup/maintain.md"
    maintain = maintain_path.read_text()
    lines = maintain.splitlines()
    lines = [
        line
        for line in lines
        if not line.startswith("The supported published predecessors are ")
        and line != "No published predecessor is supported."
    ]
    if versions:
        lines.insert(
            1,
            f"The supported published predecessors are {', '.join(versions)}.",
        )
    else:
        lines.insert(1, "No published predecessor is supported.")
    maintain_path.write_text("\n".join(lines))


@pytest.fixture
def release_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(references, "ROOT", tmp_path)
    monkeypatch.setattr(release_record, "ROOT", tmp_path)
    (tmp_path / "releases").mkdir()
    (tmp_path / "docs" / "setup").mkdir(parents=True)
    (tmp_path / "VERSION").write_text("0.9.2\n")
    (tmp_path / f"releases/{VERSION}.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "supported_from": SUPPORTED,
                "configuration_changes": [],
                "database": "No database changes.",
                "rollback": "Restore the previous release.",
            }
        )
    )
    (tmp_path / f"releases/{VERSION}.md").write_text(f"# StarTunnel {VERSION}\n")
    (tmp_path / "README.md").write_text(
        f"These guides describe {VERSION}. Existing v0.2.0 installations use the old guide.\n"
    )
    (tmp_path / "docs/setup/README.md").write_text(
        f"These guides describe {VERSION}. Older v0.2.0 installations use the old guide.\n"
    )
    (tmp_path / "docs/setup/maintain.md").write_text(
        "\n".join(
            (
                f"These steps update the server and its team's clients to {VERSION}.",
                f"The supported published predecessors are {SUPPORTED[0]} and {SUPPORTED[1]}.",
                f"{REPOSITORY}/{VERSION}/releases/{VERSION}.md",
                f"{REPOSITORY}/{VERSION}/docs/setup/README.md",
                f"https://github.com/btfranklin/startunnel/releases/tag/{VERSION}",
                "The minimum official CLI remains v0.3.0.",
            )
        )
    )
    (tmp_path / "docs/releases.md").write_text(
        f"The current release specification is {VERSION}.\n"
        f"The standard released path to\n`{VERSION}` passes through v0.2.0.\n"
    )
    (tmp_path / f"releases/{VERSION}.md").write_text(
        "\n".join(
            (
                f"# StarTunnel {VERSION}",
                f"{REPOSITORY}/{VERSION}/docs/setup/maintain.md#update-the-application",
                f"{REPOSITORY}/{VERSION}/docs/setup/README.md",
            )
        )
    )
    return tmp_path


def test_current_references_pass_and_older_compatibility_text_is_allowed(
    release_root: Path,
) -> None:
    references.check_references()


def test_stale_current_guide_text_fails_with_file_name(
    release_root: Path,
) -> None:
    path = release_root / "README.md"
    path.write_text(path.read_text().replace(VERSION, "v0.9.1"))

    with pytest.raises(ValueError, match=re.escape("README.md: expected one")):
        references.check_references()


def test_stale_tagged_notes_link_fails_with_file_name(
    release_root: Path,
) -> None:
    path = release_root / f"releases/{VERSION}.md"
    stale_link = f"/{VERSION}/docs/setup/README.md"
    path.write_text(path.read_text().replace(stale_link, "/v0.9.1/docs/setup/README.md"))

    with pytest.raises(
        ValueError,
        match=re.escape(f"releases/{VERSION}.md: expected one current release link"),
    ):
        references.check_references()


def test_stale_supported_predecessors_fail_but_cli_minimum_remains_valid(
    release_root: Path,
) -> None:
    path = release_root / "docs/setup/maintain.md"
    text = path.read_text().replace(
        f"The supported published predecessors are {SUPPORTED[0]} and {SUPPORTED[1]}.",
        "The supported published predecessors are v0.1.0 and v0.8.0.",
    )
    path.write_text(text)

    with pytest.raises(
        ValueError,
        match=re.escape("docs/setup/maintain.md: supported published predecessors"),
    ):
        references.check_references()


@pytest.mark.parametrize(
    "predecessors",
    [[], ["v0.8.0"], ["v0.2.0", "v0.8.0"], ["v0.2.0", "v0.7.0", "v0.8.0"]],
)
def test_supported_predecessor_counts_match_specification(
    release_root: Path, predecessors: list[str]
) -> None:
    write_supported_predecessors(release_root, predecessors)

    references.check_references()


def test_duplicate_stale_current_statement_fails(release_root: Path) -> None:
    path = release_root / "README.md"
    path.write_text(path.read_text() + "These guides describe v0.9.1.\n")

    with pytest.raises(ValueError, match=re.escape("README.md: expected one")):
        references.check_references()


@pytest.mark.parametrize("predecessors", [[], SUPPORTED])
def test_conflicting_predecessor_statements_fail(
    release_root: Path, predecessors: list[str]
) -> None:
    write_supported_predecessors(release_root, predecessors)
    path = release_root / "docs/setup/maintain.md"
    conflict = (
        "No published predecessor is supported."
        if predecessors
        else "The supported published predecessors are v0.8.0."
    )
    path.write_text(path.read_text() + "\n" + conflict + "\n")

    with pytest.raises(ValueError, match=re.escape("docs/setup/maintain.md:")):
        references.check_references()


def test_real_repository_current_references_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(references, "ROOT", Path(__file__).resolve().parents[2])
    monkeypatch.setattr(release_record, "ROOT", references.ROOT)

    references.check_references()
