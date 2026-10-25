"""Checks the isolated Buildx cache and build-provenance contract."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from scripts import proof_build


def test_build_uses_sanitized_compose_build_fields_and_target_scoped_gha_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for filename in ("Dockerfile", "pyproject.toml", "pdm.lock"):
        (tmp_path / filename).write_text(f"ARG {filename}\n", encoding="utf-8")
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setattr(proof_build, "ROOT", tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []

    def run(
        command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        calls.append((list(command), dict(environment)))
        if command[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(command, 0, "linux/amd64", "")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        if command[:3] == ["docker", "version", "--format"]:
            return subprocess.CompletedProcess(command, 0, "29.0", "")
        if command[:3] == ["docker", "buildx", "bake"]:
            bake_path = command[command.index("--file") + 1]
            bake = json.loads(Path(bake_path).read_text(encoding="utf-8"))
            assert "environment" not in bake["target"]["load-profile"]
            assert bake["target"]["load-profile"]["cache-from"] == [
                "type=gha,version=2,scope=startunnel-load-helper-linux-amd64"
            ]
            assert bake["target"]["load-profile"]["cache-to"] == [
                "type=gha,version=2,scope=startunnel-load-helper-linux-amd64,mode=max"
            ]
            assert "must-not-enter-bake" not in json.dumps(bake)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(command, 0, "buildx v0.1", "")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, "sha256:" + "b" * 64 + "|" + "a" * 40 + "|linux/amd64", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(proof_build, "_run", run)
    model = json.dumps(
        {
            "services": {
                "load-profile": {
                    "image": "startunnel-load-tests:local",
                    "build": {
                        "context": ".",
                        "dockerfile": "Dockerfile",
                        "target": "load-helper",
                        "labels": {"net.startunnel.disposable-build": "true"},
                    },
                    "environment": {"PASSWORD": "must-not-enter-bake"},
                }
            }
        }
    )
    environment = {
        "PATH": "/usr/bin",
        "GITHUB_ACTIONS": "true",
        "ACTIONS_CACHE_URL": "https://cache.invalid",
        "ACTIONS_RUNTIME_TOKEN": "cache-token",
        "ACTIONS_RESULTS_URL": "https://results.invalid",
    }

    result = proof_build.build_services(
        ("load-profile",),
        compose_model=model,
        environment=environment,
    )

    build_call = next(call for call in calls if call[0][:3] == ["docker", "buildx", "bake"])
    assert build_call[0][:4] == ["docker", "buildx", "bake", "--load"]
    assert build_call[1]["ACTIONS_RUNTIME_TOKEN"] == "cache-token"
    assert result["cache"] == "gha"
    saved_path = next((tmp_path / "artifacts").glob("proof-build-*.json"))
    saved = json.loads(saved_path.read_text())
    assert saved["hashes"]["Dockerfile"]
    assert "ACTIONS_RUNTIME_TOKEN" not in json.dumps(saved)


def test_build_environment_does_not_forward_cache_credentials_outside_actions() -> None:
    environment = proof_build._build_environment(
        {"GITHUB_ACTIONS": "false", "ACTIONS_RUNTIME_TOKEN": "secret", "PATH": "/usr/bin"}
    )

    assert environment == {"GITHUB_ACTIONS": "false", "PATH": "/usr/bin"}


@pytest.mark.parametrize("no_cache", [False, True])
@pytest.mark.parametrize("different_args", [False, True])
def test_helper_build_shares_equal_inputs_and_retains_distinct_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_cache: bool, different_args: bool
) -> None:
    for filename in ("Dockerfile", "pyproject.toml", "pdm.lock"):
        (tmp_path / filename).write_text(filename, encoding="utf-8")
    monkeypatch.setattr(proof_build, "ROOT", tmp_path)
    inspected: list[str] = []
    services = ("example-tests", "agent-e2e")
    digest = "sha256:" + "b" * 64

    def run(
        command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(command, 0, "linux/aarch64", "")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        if command[:3] == ["docker", "buildx", "bake"]:
            bake = json.loads(Path(command[command.index("--file") + 1]).read_text())
            assert list(bake["target"]) == (
                sorted(services) if different_args else ["example-tests"]
            )
            target = bake["target"]["example-tests"]
            assert list(command[-len(bake["target"]) :]) == (
                list(services) if different_args else ["example-tests"]
            )
            assert target["tags"] == [
                f"startunnel-{name}:local"
                for name in (services[:1] if different_args else services)
            ]
            assert target["args"] == {"APP_UID": "10001"}
            assert target["labels"] == {
                "net.startunnel.disposable-build": "true",
                "net.startunnel.source": "a" * 40,
            }
            if no_cache:
                assert target["no-cache"] is True
                assert "cache-from" not in target and "cache-to" not in target
            else:
                assert target["cache-to"] == [
                    "type=gha,version=2,scope=startunnel-development-linux-arm64,mode=max"
                ]
                if different_args:
                    second = bake["target"]["agent-e2e"]
                    assert second["args"] == {"APP_UID": "10002"}
                    assert second["cache-from"] == target["cache-from"]
                    assert "cache-to" not in second
            Path(command[command.index("--metadata-file") + 1]).write_text(
                json.dumps({name: {"containerimage.digest": digest} for name in bake["target"]})
            )
            return subprocess.CompletedProcess(command, 0, "#1 CACHED", "")
        if command[:3] == ["docker", "image", "inspect"]:
            inspected.append(command[-1])
            return subprocess.CompletedProcess(
                command, 0, digest + "|" + "a" * 40 + "|linux/arm64", ""
            )
        return subprocess.CompletedProcess(command, 0, "tool version", "")

    monkeypatch.setattr(proof_build, "_run", run)
    result = proof_build.build_services(
        services,
        compose_model=json.dumps(
            {
                "services": {
                    name: {
                        "image": f"startunnel-{name}:local",
                        "build": {
                            "context": ".",
                            "dockerfile": "Dockerfile",
                            "target": "development",
                            "args": {
                                "APP_UID": "10002"
                                if different_args and name == "agent-e2e"
                                else "10001"
                            },
                            "labels": {"net.startunnel.disposable-build": "true"},
                        },
                    }
                    for name in services
                }
            }
        ),
        environment={
            "GITHUB_ACTIONS": "true",
            "ACTIONS_RUNTIME_TOKEN": "private-token",
            "ACTIONS_RESULTS_URL": "https://cache.invalid",
        },
        no_cache=no_cache,
    )

    assert inspected == [f"startunnel-{name}:local" for name in services]
    assert result["services"] == list(services)
    assert result["image_ids"] == dict.fromkeys(services, digest)
    assert result["build_metadata"] == {
        name: {"containerimage.digest": digest} for name in services
    }
    assert result["build_arguments"] == {
        name: {"APP_UID": "10002" if different_args and name == "agent-e2e" else "10001"}
        for name in services
    }


@pytest.mark.parametrize(
    "difference",
    [
        {"context": "other"},
        {"dockerfile": "OtherDockerfile"},
        {"target": "proxy-tests"},
        {"args": {"APP_UID": "10002"}},
        {"labels": {"net.startunnel.disposable-build": "false"}},
        {"no-cache": True},
        {"cache-from": ["type=local,src=other"]},
    ],
)
def test_different_build_inputs_keep_separate_targets(difference: proof_build.BakeTarget) -> None:
    target: proof_build.BakeTarget = {
        "context": ".",
        "dockerfile": "Dockerfile",
        "target": "development",
        "args": {"APP_UID": "10001"},
        "labels": {"net.startunnel.disposable-build": "true"},
        "tags": ["first:local"],
    }
    combined, owners = proof_build._combine_targets(
        {"first": target, "second": {**target, **difference, "tags": ["second:local"]}}
    )
    assert list(combined) == ["first", "second"]
    assert owners == {"first": "first", "second": "second"}
    assert target["tags"] == ["first:local"]


def test_proxy_helper_keeps_full_browser_image_separate() -> None:
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    proxy = dockerfile.split("FROM development-dependencies AS proxy-tests\n")[1].split(
        "\nFROM ", maxsplit=1
    )[0]
    assert "playwright install --with-deps chromium " in proxy
    assert "firefox" not in proxy and "webkit" not in proxy
    assert "documentation-tools" not in proxy
    assert "nodejs" not in proxy and "npm" not in proxy
    assert "ARG APP_UID=10001" in proxy and "ARG APP_GID=10001" in proxy
    assert "USER startunnel:startunnel" in proxy
    assert "startunnel-entrypoint" in proxy
    assert "playwright install --with-deps chromium firefox webkit" in dockerfile


def test_cache_failure_rewrites_bake_without_gha_cache_and_retries_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for filename in ("Dockerfile", "pyproject.toml", "pdm.lock"):
        (tmp_path / filename).write_text(filename, encoding="utf-8")
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setattr(proof_build, "ROOT", tmp_path)
    bake_calls: list[dict[str, object]] = []

    def run(
        command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(command, 0, "linux/amd64", "")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        if command[:3] == ["docker", "buildx", "bake"]:
            bake_path = Path(command[command.index("--file") + 1])
            target = json.loads(bake_path.read_text())["target"]["load-profile"]
            bake_calls.append(target)
            if len(bake_calls) == 1:
                assert "cache-from" in target
                assert target["cache-from"][0].startswith("type=gha,version=2,")
                return subprocess.CompletedProcess(command, 1, "", "failed to export cache")
            assert "cache-from" not in target
            assert "cache-to" not in target
            return subprocess.CompletedProcess(command, 0, "#1 [load-helper 1/4] CACHED", "")
        if command[:3] == ["docker", "version", "--format"]:
            return subprocess.CompletedProcess(command, 0, "29.0", "")
        if command[:3] == ["docker", "buildx", "version"]:
            return subprocess.CompletedProcess(command, 0, "buildx v0.1", "")
        if command[:3] == ["docker", "image", "inspect"]:
            return subprocess.CompletedProcess(
                command, 0, "sha256:" + "b" * 64 + "|" + "a" * 40 + "|linux/amd64", ""
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(proof_build, "_run", run)
    result = proof_build.build_services(
        ("load-profile",),
        compose_model=json.dumps(
            {
                "services": {
                    "load-profile": {
                        "image": "startunnel-load-tests:local",
                        "build": {
                            "context": ".",
                            "dockerfile": "Dockerfile",
                            "target": "load-helper",
                        },
                    }
                }
            }
        ),
        environment={
            "PATH": "/usr/bin",
            "GITHUB_ACTIONS": "true",
            "ACTIONS_RUNTIME_TOKEN": "cache-token",
            "ACTIONS_RESULTS_URL": "https://results.invalid",
        },
    )

    assert len(bake_calls) == 2
    assert result["cache_error_fallback"] is True
    assert result["cache_hits"] == 1


def test_generic_build_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for filename in ("Dockerfile", "pyproject.toml", "pdm.lock"):
        (tmp_path / filename).write_text(filename, encoding="utf-8")
    (tmp_path / "artifacts").mkdir()
    monkeypatch.setattr(proof_build, "ROOT", tmp_path)
    bake_calls = 0

    def run(
        command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        nonlocal bake_calls
        if command[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(command, 0, "linux/amd64", "")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        if command[:3] == ["docker", "buildx", "bake"]:
            bake_calls += 1
            return subprocess.CompletedProcess(
                command,
                1,
                "ERROR [load-helper 3/4] safe phase failure\npassword=should-not-leak",
                "failed while running a build step\npostgresql://user:pass@db/private",
            )
        raise AssertionError(command)

    monkeypatch.setattr(proof_build, "_run", run)
    with pytest.raises(proof_build.ProofBuildError, match="phase load-helper"):
        proof_build.build_services(
            ("load-profile",),
            compose_model=json.dumps(
                {
                    "services": {
                        "load-profile": {
                            "image": "startunnel-load-tests:local",
                            "build": {"context": ".", "dockerfile": "Dockerfile"},
                        }
                    }
                }
            ),
            environment={
                "PATH": "/usr/bin",
                "GITHUB_ACTIONS": "true",
                "ACTIONS_RUNTIME_TOKEN": "cache-token",
                "ACTIONS_RESULTS_URL": "https://results.invalid",
            },
        )
    assert bake_calls == 1
    output = capsys.readouterr().out
    assert "phase: load-helper" in output
    assert "safe phase failure" in output
    assert "should-not-leak" not in output
    assert "user:pass" not in output


def test_failure_report_redacts_cache_token_and_limits_output_to_80_lines() -> None:
    output = "\n".join([*(f"line {index}" for index in range(100)), "token=cache-token"])

    safe = proof_build._safe_failure_report(
        output,
        {"ACTIONS_RUNTIME_TOKEN": "cache-token"},
    )

    assert len(safe.splitlines()) == 80
    assert "cache-token" not in safe
    assert "[redacted sensitive build output]" in safe


@pytest.mark.parametrize(
    "compose_model",
    ["[]", "null", '{"services": []}', '{"services": {"load-profile": null}}'],
)
def test_invalid_compose_shape_uses_the_safe_build_error(
    monkeypatch: pytest.MonkeyPatch, compose_model: str
) -> None:
    def run(
        command: Sequence[str], environment: Mapping[str, str], timeout_seconds: int
    ) -> subprocess.CompletedProcess[str]:
        if command[:3] == ["docker", "info", "--format"]:
            return subprocess.CompletedProcess(command, 0, "linux/amd64", "")
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40, "")
        raise AssertionError("An invalid model must fail before the image build.")

    monkeypatch.setattr(proof_build, "_run", run)
    with pytest.raises(proof_build.ProofBuildError):
        proof_build.build_services(("load-profile",), compose_model=compose_model, environment={})
