"""The production entry point accepts only immutable application images."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from scripts.start_production import (
    PRODUCTION_SECRET_NAMES,
    compose_command,
    main,
    production_secret_directory,
    read_validated_secret_file,
    rendered_images,
    validate_image_reference,
    validate_production_secret_files,
    validate_rendered_images,
)

ROOT = Path(__file__).resolve().parents[2]


def test_production_image_requires_a_sha256_digest() -> None:
    digest = "a" * 64
    assert validate_image_reference(f"registry.example/startunnel@sha256:{digest}").endswith(digest)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "startunnel:latest",
        "registry.example/startunnel@sha256:short",
        "registry.example/startunnel@sha512:" + "a" * 64,
        "registry.example/startunnel @sha256:" + "a" * 64,
    ],
)
def test_production_image_rejects_mutable_or_malformed_references(value: str) -> None:
    with pytest.raises(ValueError, match="pinned with @sha256"):
        validate_image_reference(value)


def test_production_compose_uses_default_files_and_explicit_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    assert "--profile" not in compose_command(edge=False)
    assert compose_command(edge=False)[-4:] == ["-f", "compose.yaml", "-f", "compose.prod.yaml"]
    assert compose_command(edge=True)[-2:] == ["--profile", "edge"]


def test_production_compose_uses_loaded_custom_files_and_profiles(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("COMPOSE_FILE", "/srv/team/base.yaml:/srv/team/prod.yaml")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "customer-team")
    monkeypatch.setenv("COMPOSE_PROFILES", "metrics")
    config = tmp_path / "team.env"
    assert compose_command(edge=False, env_file=config) == [
        "docker",
        "compose",
        "--env-file",
        str(config),
    ]
    assert compose_command(edge=True, env_file=config)[-2:] == ["--profile", "edge"]


def test_custom_compose_file_resolves_relative_secret_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    compose_directory = tmp_path / "team"
    compose_directory.mkdir()
    monkeypatch.setenv(
        "COMPOSE_FILE",
        os.pathsep.join((str(compose_directory / "base.yaml"), str(tmp_path / "other.yaml"))),
    )
    monkeypatch.setenv("STARTUNNEL_SECRETS_DIR", "runtime/secrets")
    expected = compose_directory / "runtime" / "secrets"
    expected.mkdir(parents=True)
    for name in PRODUCTION_SECRET_NAMES:
        _write_secret(expected / name)

    assert production_secret_directory() == expected
    validate_production_secret_files(production_secret_directory())
    assert production_secret_directory(str(tmp_path / "absolute")) == tmp_path / "absolute"


def test_production_validates_the_images_rendered_by_compose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64

    def compose_result(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(f"registry.example/startunnel@sha256:{digest}\npostgres:latest\n"),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", compose_result)
    images = rendered_images(compose_command(edge=False))

    with pytest.raises(ValueError, match="enabled production service image"):
        validate_rendered_images(images)


def test_all_rendered_production_images_can_be_immutable() -> None:
    digest = "a" * 64
    validate_rendered_images(
        [
            f"registry.example/startunnel@sha256:{digest}",
            f"registry.example/postgres@sha256:{digest}",
            f"registry.example/caddy@sha256:{digest}",
        ]
    )


def _write_secret(path: Path, value: str = "safe-test-secret") -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_production_secret_preflight_accepts_only_complete_safe_sources(
    tmp_path: Path,
) -> None:
    for name in PRODUCTION_SECRET_NAMES:
        _write_secret(tmp_path / name)

    validate_production_secret_files(tmp_path)


def test_production_secret_preflight_inventory_matches_compose() -> None:
    compose_source = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    compose_secret_names = set(
        re.findall(
            r"file: \$\{STARTUNNEL_SECRETS_DIR:-\./secrets\}/([a-z0-9_]+)",
            compose_source,
        )
    )

    assert set(PRODUCTION_SECRET_NAMES) == compose_secret_names


@pytest.mark.parametrize("unsafe_kind", ["empty", "directory"])
def test_production_secret_preflight_rejects_unsafe_sources(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    for name in PRODUCTION_SECRET_NAMES:
        _write_secret(tmp_path / name)
    target = tmp_path / PRODUCTION_SECRET_NAMES[0]
    if unsafe_kind == "empty":
        target.write_text("", encoding="utf-8")
    else:
        target.unlink()
        target.mkdir()

    with pytest.raises(ValueError, match=PRODUCTION_SECRET_NAMES[0]):
        validate_production_secret_files(tmp_path)


def test_production_secret_preflight_accepts_readonly_files_and_managed_links(
    tmp_path: Path,
) -> None:
    for name in PRODUCTION_SECRET_NAMES:
        _write_secret(tmp_path / name)
    first = tmp_path / PRODUCTION_SECRET_NAMES[0]
    first.chmod(0o400)
    second = tmp_path / PRODUCTION_SECRET_NAMES[1]
    second.unlink()
    second.symlink_to(first)
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(tmp_path, target_is_directory=True)

    validate_production_secret_files(linked_directory)
    assert read_validated_secret_file(second) == "safe-test-secret"


def test_secret_reader_rejects_multiline_and_binary_values(tmp_path: Path) -> None:
    secret = tmp_path / "test_secret"
    _write_secret(secret, "first\nsecond")
    with pytest.raises(ValueError, match="one nonempty text value"):
        read_validated_secret_file(secret)

    secret.write_bytes(b"\xff")
    secret.chmod(0o600)
    with pytest.raises(ValueError, match="UTF-8 text"):
        read_validated_secret_file(secret)

    _write_secret(secret, "nul\x00byte")
    with pytest.raises(ValueError, match="one nonempty text value"):
        read_validated_secret_file(secret)


def test_production_start_stops_before_compose_when_preflight_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_compose(*args: object, **kwargs: object) -> None:
        nonlocal called
        del args, kwargs
        called = True

    monkeypatch.setenv("STARTUNNEL_SECRETS_DIR", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", unexpected_compose)

    assert main([]) == 2
    assert called is False


def test_postgres_entrypoint_rejects_equal_role_passwords() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "POSTGRES_PASSWORD": "safe-equal-test-value",
            "STARTUNNEL_DB_RUNTIME_PASSWORD": "safe-equal-test-value",
        }
    )
    result = subprocess.run(
        ["sh", "docker/postgres/entrypoint.sh"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "admin and runtime passwords must be different" in result.stderr


def test_runtime_role_init_passes_arbitrary_password_through_psql_environment() -> None:
    password = "runtime ' \\ $ ü " + "x" * 130
    environment = os.environ.copy()
    environment.pop("PGOPTIONS", None)
    environment.update(
        {
            "POSTGRES_DB": "startunnel",
            "POSTGRES_USER": "startunnel_admin",
            "STARTUNNEL_DB_RUNTIME_PASSWORD": password,
        }
    )

    result = subprocess.run(
        [
            "sh",
            "-c",
            "psql() { printf 'ARGS:%s\\n' \"$*\"; "
            "printf 'PGOPTIONS:%s\\n' \"${PGOPTIONS-unset}\"; cat; }\n"
            ". ./docker/postgres/init/10-runtime-role.sh",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "PGOPTIONS:unset" in result.stdout
    assert "\\getenv runtime_password STARTUNNEL_DB_RUNTIME_PASSWORD" in result.stdout
    assert ":'runtime_password'" in result.stdout
    assert password not in result.stdout + result.stderr
