"""Exercise the production release proof's bounded report and secret boundaries."""

from __future__ import annotations

import base64
import json
import stat
import subprocess
import time
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, cast

import pytest
from scripts import prove_production_release as proof
from scripts.compose_isolation import LaneError, safe_environment

IMAGE = "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64
COMMIT = "a" * 40
VERSION = "v0.4.1"
IMAGE_ID = "sha256:" + "c" * 64
ADMIN_KEY = "sta_" + "A" * 43


class FakeStack:
    def __init__(self, *, fail_transfer: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_transfer = fail_transfer

    def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
        self.calls.append((step, *arguments))
        if "cat" in arguments:
            if self.fail_transfer:
                raise LaneError("bootstrap_admin", "private transfer failed")
            return ADMIN_KEY
        return "bootstrap output"


class FakeProject:
    def __init__(
        self,
        *_args: object,
        cleanup_complete: bool = True,
        **kwargs: object,
    ) -> None:
        source = kwargs.get("source_environment", {})
        self.environment = safe_environment(cast(Mapping[str, str], source))
        self.cleanup_complete = False
        self.will_cleanup = cleanup_complete
        self.calls: list[tuple[str, ...]] = []

    def __enter__(self) -> FakeProject:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        self.cleanup_complete = self.will_cleanup
        return False

    def capture(self, step: str, *arguments: str, timeout_seconds: int = 60) -> str:
        self.calls.append((step, *arguments))
        if step == "render_compose":
            port = int(self.environment["STARTUNNEL_BASE_URL"].rsplit(":", 1)[1])
            directory = Path(self.environment["STARTUNNEL_SECRETS_DIR"])
            return json.dumps(compose_model(directory, port))
        if step == "production_settings":
            return "startunnel.settings.production"
        if "cat" in arguments:
            return ADMIN_KEY
        return ""

    def run(self, step: str, *arguments: str, timeout_seconds: int) -> None:
        self.calls.append((step, *arguments))


def valid_report() -> proof.ReleaseReport:
    report = proof.new_report(COMMIT, VERSION, IMAGE)
    report["checks"] = {name: True for name in proof.CHECK_NAMES}
    report["image_ids"] = {service: IMAGE_ID for service in proof.APP_SERVICES}
    proof.finish_report(report, cleanup_complete=True, private_files_removed=True)
    return report


def compose_model(secret_directory: Path, port: int = 45000) -> dict[str, object]:
    required = set(proof.SECRET_NAMES)
    models: dict[str, object] = {}
    secret_sets = {
        "postgres": {"postgres_admin_password", "postgres_runtime_password"},
        "migrate": required - {"postgres_runtime_password"},
        "web": required - {"postgres_admin_password"},
        "maintenance": required - {"postgres_runtime_password"},
    }
    for name, mounts in secret_sets.items():
        service: dict[str, object] = {"secrets": [{"source": item} for item in sorted(mounts)]}
        if name == "web":
            service["volumes"] = [
                {
                    "type": "bind",
                    "source": str(proof.ROOT / "scripts/proof_runtime.py"),
                    "target": "/proof/runtime.py",
                    "read_only": True,
                }
            ]
        if name in proof.APP_SERVICES:
            service.update(
                {
                    "image": IMAGE,
                    "platform": proof.PLATFORM,
                    "environment": {
                        "DJANGO_SETTINGS_MODULE": "startunnel.settings.production",
                        "STARTUNNEL_ENV": "production",
                    },
                }
            )
        if name == "web":
            service["ports"] = [{"target": 8000, "published": str(port), "host_ip": "127.0.0.1"}]
        models[name] = service
    secrets_config = {name: {"file": str(secret_directory / name)} for name in required}
    return {"services": models, "secrets": secrets_config}


def install_fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cleanup_complete: bool = True,
    fail_admin_authentication: bool = False,
) -> tuple[list[FakeProject], list[tuple[list[str], dict[str, str], str | None]]]:
    projects: list[FakeProject] = []
    cli_calls: list[tuple[list[str], dict[str, str], str | None]] = []

    class Project(FakeProject):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, cleanup_complete=cleanup_complete, **kwargs)
            projects.append(self)

    monkeypatch.setattr(proof, "ProductionComposeProject", Project)
    monkeypatch.setattr(proof, "available_proxy_prefix", lambda _environment: "172.20.20")
    monkeypatch.setattr(proof, "run_process", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        proof,
        "image_metadata",
        lambda *_args, **_kwargs: (
            {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            },
            proof.PLATFORM,
        ),
    )
    monkeypatch.setattr(proof, "required_output", lambda *_args, **_kwargs: IMAGE_ID)
    monkeypatch.setattr(
        proof,
        "runtime_image_ids",
        lambda _stack: {service: IMAGE_ID for service in proof.APP_SERVICES},
    )
    monkeypatch.setattr(proof, "health_ok", lambda _url: True)
    monkeypatch.setattr(proof, "metrics_inodes", lambda _stack: {"worker.db": 17})

    class Download:
        status = 200
        headers = {"Content-Disposition": "attachment; filename=star_tunnel.py"}

        def __enter__(self) -> Download:
            return self

        def __exit__(self, *_args: object) -> Literal[False]:
            return False

        def read(self, _limit: int) -> bytes:
            return b"# downloaded CLI"

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Download())

    def cli_process(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        arguments = list(command[2:])
        raw_environment = kwargs.get("env")
        environment = cast(Mapping[str, str], raw_environment)
        body_input = kwargs.get("input")
        body = json.loads(body_input) if isinstance(body_input, str) else {}
        body_object = cast(dict[str, object], body)
        cli_calls.append(
            (arguments, dict(environment), body_input if isinstance(body_input, str) else None)
        )

        def completed(value: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

        if arguments == ["version"]:
            return completed({"version": VERSION, "source_revision": COMMIT})
        if arguments == ["admin", "config"] or arguments == ["config"]:
            return completed({})
        if arguments == ["admin", "me"]:
            if fail_admin_authentication:
                raise RuntimeError("private failure value sk-secret-value")
            return completed(
                {
                    "admin": {"id": "admin-user"},
                    "credential_id": "admin-credential",
                    "authority": "instance",
                }
            )
        if arguments[:3] == ["admin", "agents", "create"] and "--secret-output" in arguments:
            name = body_object.get("name")
            credential_id = "agent-one" if name == "release-proof-one" else "agent-two"
            secret_path = Path(arguments[arguments.index("--secret-output") + 1])
            secret_path.write_text("st_" + "B" * 43, encoding="utf-8")
            secret_path.chmod(0o600)
            receipt_id = "receipt-one" if credential_id == "agent-one" else "receipt-two"
            audit_id = "audit-one" if credential_id == "agent-one" else "audit-two"
            return completed(
                {
                    "resource": {"id": credential_id},
                    "operation": {"id": receipt_id, "audit_event_id": audit_id},
                }
            )
        if arguments[:3] == ["admin", "operations", "get"]:
            return completed(
                {
                    "id": arguments[3],
                    "result": {
                        "resource": {"id": "agent-one"},
                        "operation": {"id": "receipt-one", "audit_event_id": "audit-one"},
                    },
                }
            )
        if arguments[:3] == ["admin", "audit", "list"]:
            return completed(
                {
                    "items": [
                        {
                            "id": "audit-one",
                            "actor_id": "admin-user",
                            "target_id": "agent-one",
                            "metadata": {"outcome": "success"},
                        }
                    ],
                    "next_cursor": None,
                }
            )
        if arguments == ["me"]:
            agent_id = (
                "agent-one"
                if environment["STARTUNNEL_AGENT_KEY_FILE"].endswith("agent-one.key")
                else "agent-two"
            )
            return completed({"agent": {"id": agent_id, "name": agent_id}})
        if arguments[0] == "create":
            return completed(
                {
                    "tunnel": {"address": "private-glyph-address"},
                    "cycle": {"root": {"id": "root-one"}},
                }
            )
        if arguments[0] == "reply":
            return completed({"message": {"id": "reply-one", "parent_id": "root-one"}})
        if arguments[0] == "read-tree":
            return completed(
                {
                    "root_id": "root-one",
                    "nodes": [
                        {"id": "root-one", "parent_id": None, "sender": {"id": "agent-one"}},
                        {"id": "reply-one", "parent_id": "root-one", "sender": {"id": "agent-two"}},
                    ],
                }
            )
        raise AssertionError(f"Unexpected fake CLI command: {arguments}")

    monkeypatch.setattr(subprocess, "run", cli_process)
    return projects, cli_calls


def test_report_validator_accepts_complete_identity_and_rejects_partial_proof() -> None:
    report = valid_report()
    proof.validate_report(report, commit=COMMIT, image=IMAGE, version=VERSION)

    incomplete = valid_report()
    incomplete["checks"]["health_ready"] = False
    with pytest.raises(ValueError, match="incomplete checks"):
        proof.validate_report(incomplete, commit=COMMIT, image=IMAGE, version=VERSION)

    wrong_image = valid_report()
    with pytest.raises(ValueError, match="identity or status"):
        proof.validate_report(wrong_image, commit=COMMIT, image=IMAGE + "x", version=VERSION)

    for field, value in (
        ("source_commit", "d" * 40),
        ("version", "v9.9.9"),
        ("platform", "linux/arm64"),
    ):
        wrong_identity = valid_report()
        cast(dict[str, object], wrong_identity)[field] = value
        with pytest.raises(ValueError, match="identity or status"):
            proof.validate_report(wrong_identity, commit=COMMIT, image=IMAGE, version=VERSION)


def test_report_validator_rejects_image_id_disagreement_and_incomplete_cleanup() -> None:
    report = valid_report()
    report["image_ids"]["maintenance"] = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="different image IDs"):
        proof.validate_report(report, commit=COMMIT, image=IMAGE, version=VERSION)

    cleanup_failed = valid_report()
    cleanup_failed["disposable_volumes_removed"] = False
    cleanup_failed["release_evidence_eligible"] = False
    with pytest.raises(ValueError, match="cleanup"):
        proof.validate_report(cleanup_failed, commit=COMMIT, image=IMAGE, version=VERSION)

    malformed = valid_report()
    malformed["image_ids"]["web"] = "not-an-image-id"
    with pytest.raises(ValueError, match="image IDs"):
        proof.validate_report(malformed, commit=COMMIT, image=IMAGE, version=VERSION)


@pytest.mark.parametrize(
    ("labels", "platform"),
    [
        (
            {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            },
            "linux/arm64",
        ),
        (
            {
                "org.opencontainers.image.revision": "d" * 40,
                "org.opencontainers.image.version": VERSION,
            },
            proof.PLATFORM,
        ),
        (
            {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": "v9.9.9",
            },
            proof.PLATFORM,
        ),
    ],
)
def test_candidate_metadata_rejects_platform_or_label_mismatch(
    labels: dict[str, str], platform: str
) -> None:
    with pytest.raises(LaneError):
        proof.validate_candidate_metadata(labels, platform=platform, commit=COMMIT, version=VERSION)


def test_production_project_keeps_docker_environment_but_strips_credentials() -> None:
    stack = proof.ProductionComposeProject(
        "stproof-release-unit-test",
        candidate_image=IMAGE,
        source_environment={
            "PATH": "/bin",
            "HOME": "/tmp/home",
            "DOCKER_HOST": "unix:///var/run/docker.sock",
            "STARTUNNEL_SECRET_KEY": "do-not-copy",
            "STARTUNNEL_AGENT_KEY": "do-not-copy",
            "OPENAI_API_KEY": "do-not-copy",
        },
        smoke_overlay=Path("/private/override.yaml"),
    )
    assert stack.environment["PATH"] == "/bin"
    assert stack.environment["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert "STARTUNNEL_SECRET_KEY" not in stack.environment
    assert "STARTUNNEL_AGENT_KEY" not in stack.environment
    assert "OPENAI_API_KEY" not in stack.environment
    proof.set_production_environment(
        stack.environment,
        image=IMAGE,
        base_url="http://127.0.0.1:45000",
        secret_directory=Path("/private/secrets"),
        proxy_prefix="172.20.10",
    )
    assert stack.environment["STARTUNNEL_APP_IMAGE"] == IMAGE
    assert stack.environment["STARTUNNEL_SECRETS_DIR"] == "/private/secrets"
    assert "STARTUNNEL_SECRET_KEY" not in stack.environment
    assert "STARTUNNEL_AGENT_KEY" not in stack.environment
    assert stack.compose_files() == (
        str(proof.ROOT / "compose.yaml"),
        str(proof.ROOT / "compose.prod.yaml"),
        "/private/override.yaml",
    )
    assert "compose.test.yaml" not in stack.compose_files()


def test_rendered_compose_checks_production_platform_loopback_and_private_secrets(
    tmp_path: Path,
) -> None:
    secret_directory = tmp_path / "secrets"
    secret_directory.mkdir()
    rendered = compose_model(secret_directory)
    proof.verify_compose_model(rendered, image=IMAGE, port=45000, secret_directory=secret_directory)

    services = cast(dict[str, object], rendered["services"])
    web = cast(dict[str, object], services["web"])
    web["platform"] = "linux/arm64"
    with pytest.raises(LaneError, match="configuration"):
        proof.verify_compose_model(
            rendered, image=IMAGE, port=45000, secret_directory=secret_directory
        )

    web["platform"] = proof.PLATFORM
    web["ports"] = [{"target": 8000, "published": "45000", "host_ip": "0.0.0.0"}]
    with pytest.raises(LaneError, match="loopback"):
        proof.verify_compose_model(
            rendered, image=IMAGE, port=45000, secret_directory=secret_directory
        )

    web["ports"] = [{"target": 8000, "published": "45000", "host_ip": "127.0.0.1"}]
    secrets_config = cast(dict[str, object], rendered["secrets"])
    secrets_config["postgres_admin_password"] = {"file": str(tmp_path / "outside" / "password")}
    with pytest.raises(LaneError, match="outside private storage"):
        proof.verify_compose_model(
            rendered, image=IMAGE, port=45000, secret_directory=secret_directory
        )


def test_generated_crypto_secrets_are_32_byte_base64url_and_private(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "private"
    proof.make_secrets(secrets_dir)
    for name in proof.SECRET_NAMES:
        path = secrets_dir / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for name in ("startunnel_api_key_pepper", "startunnel_idempotency_secret"):
        token = (secrets_dir / name).read_text(encoding="utf-8").strip()
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        assert len(decoded) == 32


def test_admin_key_uses_exec_cat_and_private_exclusive_host_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stack = FakeStack()
    path = proof.admin_key_from_stack(stack, tmp_path)
    assert path.read_text(encoding="utf-8") == ADMIN_KEY + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stack.calls[0] == (
        "bootstrap_admin",
        "exec",
        "-T",
        "web",
        "env",
        "-u",
        "PROMETHEUS_MULTIPROC_DIR",
        "/usr/local/bin/startunnel-entrypoint",
        "/app/.venv/bin/python",
        "manage.py",
        "create_instance_admin",
        "release-proof-admin",
        "--key-file",
        "/tmp/startunnel-release-admin.key",
    )
    assert stack.calls[1][-2:] == ("cat", "/tmp/startunnel-release-admin.key")
    assert stack.calls[2][-3:] == ("rm", "-f", "/tmp/startunnel-release-admin.key")
    assert not any(ADMIN_KEY in argument for call in stack.calls for argument in call)
    assert all("docker cp" not in argument for call in stack.calls for argument in call)
    assert capsys.readouterr().out == ""


def test_tmpfs_admin_key_transfer_failure_leaves_no_local_key(tmp_path: Path) -> None:
    stack = FakeStack(fail_transfer=True)
    with pytest.raises(LaneError, match="private transfer"):
        proof.admin_key_from_stack(stack, tmp_path)
    assert not (tmp_path / "admin.key").exists()
    assert len(stack.calls) == 2
    assert stack.calls[1][-2:] == ("cat", "/tmp/startunnel-release-admin.key")


def test_runtime_image_id_mismatch_fails_closed() -> None:
    with pytest.raises(LaneError, match="candidate image"):
        proof.verify_runtime_image_ids(
            IMAGE_ID,
            {"web": IMAGE_ID, "migrate": IMAGE_ID, "maintenance": "sha256:" + "d" * 64},
        )


def test_metrics_inode_check_accepts_stable_existing_files_and_new_files() -> None:
    assert proof.metrics_preserved({"worker.db": 7}, {"worker.db": 7, "new.db": 8})
    assert not proof.metrics_preserved({"worker.db": 7}, {"worker.db": 9})


def test_complete_fake_run_bootstraps_once_and_proves_production_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    projects, cli_calls = install_fake_run(monkeypatch)
    report_path = tmp_path / "production-release-proof-success.json"

    report = proof.run_proof(
        image=IMAGE,
        commit=COMMIT,
        version=VERSION,
        project_name="stproof-release-success",
        report_path=report_path,
    )

    proof.validate_report(report, commit=COMMIT, image=IMAGE, version=VERSION)
    assert report["status"] == "passed"
    assert report["release_evidence_eligible"] is True
    assert report["checks"] == {name: True for name in proof.CHECK_NAMES}
    assert report["image_ids"] == {service: IMAGE_ID for service in proof.APP_SERVICES}
    assert report["disposable_volumes_removed"] is True
    assert report["private_files_removed"] is True
    assert report_path.is_file()
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600

    bootstraps = [call for call in projects[0].calls if "create_instance_admin" in call]
    transfers = [
        call
        for call in projects[0].calls
        if call[-2:] == ("cat", "/tmp/startunnel-release-admin.key")
    ]
    assert len(bootstraps) == 1
    assert len(transfers) == 1
    assert "PROMETHEUS_MULTIPROC_DIR" in bootstraps[0]
    assert "-u" in bootstraps[0]
    assert not any("docker cp" in value for call in projects[0].calls for value in call)
    assert report["checks"]["metrics_files_preserved"] is True

    commands = [call[0] for call in cli_calls]
    assert ["admin", "me"] in commands
    assert ["admin", "operations", "get", "receipt-one"] in commands
    assert any(command[:3] == ["admin", "audit", "list"] for command in commands)
    assert ["config"] in commands
    assert ["me"] in commands
    assert any(command[0] == "create" for command in commands)
    assert any(command[0] == "reply" for command in commands)
    assert any(command[0] == "read-tree" for command in commands)
    assert all("OPENAI_API_KEY" not in environment for _, environment, _ in cli_calls)
    assert capsys.readouterr().out == ""


def test_cleanup_duration_includes_actual_context_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_run(monkeypatch)
    clock = [0.0]
    original_exit = FakeProject.__exit__

    def delayed_exit(
        self: FakeProject, exc_type: object, exc: object, traceback: object
    ) -> Literal[False]:
        clock[0] += 7.0
        return original_exit(self, exc_type, exc, traceback)

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(FakeProject, "__exit__", delayed_exit)
    report = proof.run_proof(
        image=IMAGE,
        commit=COMMIT,
        version=VERSION,
        project_name="stproof-cleanup-time",
        report_path=tmp_path / "proof.json",
    )
    cleanup = next(phase for phase in report["phases"] if phase["phase"] == "cleanup")
    assert cleanup["elapsed_seconds"] == 7.0
    assert cleanup["status"] == "passed"


@pytest.mark.parametrize("cleanup_complete", [True, False])
def test_failure_after_bootstrap_keeps_safe_diagnostics_and_cleanup_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cleanup_complete: bool,
) -> None:
    projects, cli_calls = install_fake_run(
        monkeypatch,
        cleanup_complete=cleanup_complete,
        fail_admin_authentication=True,
    )
    report_path = tmp_path / "production-release-proof-failure.json"
    report = proof.run_proof(
        image=IMAGE,
        commit=COMMIT,
        version=VERSION,
        project_name="stproof-release-failure",
        report_path=report_path,
    )

    bootstraps = [call for call in projects[0].calls if "create_instance_admin" in call]
    assert len(bootstraps) == 1
    admin_key_path = Path(cli_calls[-1][1]["STARTUNNEL_ADMIN_KEY_FILE"])
    assert not admin_key_path.exists()
    assert report["status"] == "failed"
    assert report["failure_type"] == "RuntimeError"
    assert report["failed_step"] == "download_cli"
    assert report["private_files_removed"] is True
    assert report["disposable_volumes_removed"] is cleanup_complete
    assert report["release_evidence_eligible"] is False
    assert "sk-secret-value" not in report_path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in capsys.readouterr().out


@pytest.mark.parametrize("cleanup_complete", [True, False])
def test_interruption_writes_safe_failure_report_and_cleans_private_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_complete: bool,
) -> None:
    class Project(FakeProject):
        def __init__(self, *_: object, **__: object) -> None:
            super().__init__(cleanup_complete=cleanup_complete)

    monkeypatch.setattr(proof, "ProductionComposeProject", Project)
    monkeypatch.setattr(proof, "available_proxy_prefix", lambda _environment: "172.20.20")
    monkeypatch.setattr(proof, "verify_compose_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(proof, "run_process", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        proof,
        "image_metadata",
        lambda *_args, **_kwargs: (
            {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            },
            proof.PLATFORM,
        ),
    )
    monkeypatch.setattr(proof, "required_output", lambda *_args, **_kwargs: IMAGE_ID)
    monkeypatch.setattr(
        proof,
        "runtime_image_ids",
        lambda _stack: {service: IMAGE_ID for service in proof.APP_SERVICES},
    )
    monkeypatch.setattr(proof, "health_ok", lambda _url: True)
    monkeypatch.setattr(proof, "metrics_inodes", lambda _stack: {})
    monkeypatch.setattr(
        proof,
        "admin_key_from_stack",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(InterruptedError("sk-secret-value")),
    )
    report_path = tmp_path / "production-release-proof.json"
    report = proof.run_proof(
        image=IMAGE,
        commit=COMMIT,
        version=VERSION,
        project_name="stproof-release-test",
        report_path=report_path,
    )
    serialized = report_path.read_text(encoding="utf-8")
    assert "sk-secret-value" not in serialized
    assert report["failure_type"] == "InterruptedError"
    assert report["failed_step"] == "bootstrap_admin"
    assert report["private_files_removed"] is True
    assert report["disposable_volumes_removed"] is cleanup_complete
    assert report["release_evidence_eligible"] is False
    assert report["status"] == "failed"
    assert any(phase["phase"] == "cleanup" for phase in report["phases"])
    assert not list(tmp_path.glob("startunnel-production-release-*"))


def test_exception_report_preserves_type_without_exception_text() -> None:
    report = valid_report()
    proof.safe_failure(report, RuntimeError("sta_secret_value"), "download_cli")
    proof.finish_report(report, cleanup_complete=True, private_files_removed=True)
    rendered = json.dumps(report)
    assert report["failure_type"] == "RuntimeError"
    assert report["failed_step"] == "download_cli"
    assert report["status"] == "failed"
    assert report["release_evidence_eligible"] is False
    assert "sta_secret_value" not in rendered


def test_report_writer_uses_private_mode_and_never_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "proof.json"
    proof.write_report(target, valid_report())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    original = target.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        proof.write_report(target, valid_report())
    assert target.read_text(encoding="utf-8") == original


def test_default_report_name_is_project_scoped() -> None:
    project_name = "stproof-system-20261025"
    assert proof.output_path(None, project_name) == (
        proof.ROOT / "artifacts" / f"production-release-proof-{project_name}.json"
    )
