"""Public verification is anonymous, bounded, and tied to published source."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from scripts import verify_public_release as public

VERSION = "v0.4.1"
COMMIT = "a" * 40
IMAGE = "ghcr.io/btfranklin/startunnel@sha256:" + "b" * 64


def public_fixture(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    specification = {"version": VERSION, "supported_from": ["v0.2.0"]}
    record = {
        **specification,
        "schema": 1,
        "source_commit": COMMIT,
        "image": IMAGE,
        "platform": "linux/amd64",
    }
    payload = json.dumps(record).encode()
    release = {
        "tag_name": VERSION,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-25",
        "assets": [
            {"name": "release.json", "digest": "sha256:" + hashlib.sha256(payload).hexdigest()}
        ],
    }
    documents = {
        "docs/setup/README.md": b"Setup guide",
        "docs/setup/maintain.md": b"Update guide",
        f"releases/{VERSION}.md": b"Release notes",
        f"releases/{VERSION}.json": json.dumps(specification).encode(),
        "skills/startunnel/SKILL.md": b"Current agent guide",
    }
    monkeypatch.setattr(public, "git_file", lambda commit, name: documents[name])
    monkeypatch.setattr(subprocess, "check_output", lambda *args, **kwargs: COMMIT + "\n")
    return {
        f"{public.API}/releases/latest": json.dumps(release).encode(),
        f"{public.REPOSITORY}/releases/download/{VERSION}/release.json": payload,
        **{f"{public.RAW}/{VERSION}/{name}": value for name, value in documents.items()},
        "https://startunnel.net/": f'<a href="{public.REPOSITORY}/releases">Releases</a>'.encode(),
        "https://startunnel.net/agents.md": documents["skills/startunnel/SKILL.md"],
    }


def test_complete_public_verification_uses_exact_published_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = public_fixture(monkeypatch)
    pulls: list[tuple[str, str, str]] = []
    report = public.verify_public_release(
        VERSION, fetch=data.__getitem__, pull=lambda *args: pulls.append(args)
    )
    assert report["status"] == "passed" and all(report["checks"].values())
    assert pulls == [(IMAGE, COMMIT, VERSION)]


@pytest.mark.parametrize(
    "condition",
    [
        "draft",
        "prerelease",
        "missing_asset",
        "duplicate_asset",
        "checksum",
        "source",
        "image",
        "platform",
        "specification",
        "guide",
        "homepage",
        "agent",
    ],
)
def test_failure_stops_before_image_pull_and_keeps_only_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    data = public_fixture(monkeypatch)
    api_url = f"{public.API}/releases/latest"
    record_url = f"{public.REPOSITORY}/releases/download/{VERSION}/release.json"
    release = json.loads(data[api_url])
    record = json.loads(data[record_url])
    if condition in {"draft", "prerelease"}:
        release[condition] = True
    elif condition == "missing_asset":
        release["assets"] = []
    elif condition == "duplicate_asset":
        release["assets"] *= 2
    elif condition == "checksum":
        release["assets"][0]["digest"] = "sha256:" + "0" * 64
    elif condition in {"source", "image", "platform", "specification"}:
        field = {
            "source": "source_commit",
            "image": "image",
            "platform": "platform",
            "specification": "supported_from",
        }[condition]
        record[field] = ["private"] if condition == "specification" else "private"
        data[record_url] = json.dumps(record).encode()
        release["assets"][0]["digest"] = "sha256:" + hashlib.sha256(data[record_url]).hexdigest()
    elif condition == "guide":
        data[f"{public.RAW}/{VERSION}/docs/setup/README.md"] = b"private"
    elif condition == "homepage":
        data["https://startunnel.net/"] = b"private"
    else:
        data["https://startunnel.net/agents.md"] = b"private"
    data[api_url] = json.dumps(release).encode()
    report = public.verify_public_release(
        VERSION, fetch=data.__getitem__, pull=lambda *args: pytest.fail("unexpected pull")
    )
    assert report["status"] == "failed"
    assert report["failed_check"] is not None
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "error", [OSError("private"), RuntimeError("private"), KeyboardInterrupt("private")]
)
def test_image_failure_or_interruption_retains_partial_results(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    data = public_fixture(monkeypatch)

    def fail(*args: str) -> None:
        raise error

    report = public.verify_public_release(VERSION, fetch=data.__getitem__, pull=fail)
    assert report["failed_check"] == "anonymous_image"
    assert report["checks"]["deployment_record"]
    assert not report["checks"]["anonymous_image"]
    assert report["failure_type"] == type(error).__name__
    assert "private" not in json.dumps(report)


@pytest.mark.parametrize(
    "site",
    [
        "http://example.test",
        "file:///tmp/data",
        "https://user:secret@example.test",
        "https://example.test/path",
    ],
)
def test_invalid_origin_is_rejected_before_network(site: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        public.verify_public_release(
            VERSION, site_url=site, fetch=lambda *args: pytest.fail("unexpected request")
        )


@pytest.mark.parametrize("fail", [False, True])
def test_anonymous_pull_has_empty_config_explicit_engine_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    configurations: list[Path] = []
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "private-auth")
    monkeypatch.setattr(public, "assert_local_docker", lambda env: "unix:///engine.sock")

    def run(
        command: Sequence[str], *, env: Mapping[str, str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        config = Path(env["DOCKER_CONFIG"])
        configurations.append(config)
        assert list(config.iterdir()) == []
        assert "DOCKER_AUTH_CONFIG" not in env
        assert env["DOCKER_HOST"] == "unix:///engine.sock"
        assert list(command) == ["docker", "pull", "--platform", "linux/amd64", IMAGE]
        if fail:
            raise subprocess.CalledProcessError(1, command, stderr="private")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        public,
        "image_metadata",
        lambda image, env: (
            {
                "org.opencontainers.image.revision": COMMIT,
                "org.opencontainers.image.version": VERSION,
            },
            "linux/amd64",
        ),
    )
    if fail:
        with pytest.raises(ValueError, match="anonymous"):
            public.anonymous_image(IMAGE, COMMIT, VERSION)
    else:
        public.anonymous_image(IMAGE, COMMIT, VERSION)
    assert configurations and not configurations[0].exists()


@pytest.mark.parametrize("condition", ["valid", "oversize", "http_redirect", "non_bytes"])
def test_public_download_is_bounded_https_and_has_no_authorization(
    monkeypatch: pytest.MonkeyPatch,
    condition: str,
) -> None:
    import urllib.request

    class Response:
        url = "http://example.test/" if condition == "http_redirect" else "https://example.test/"

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, amount: int) -> object:
            assert amount == 2_000_001
            if condition == "non_bytes":
                return "private"
            return b"x" * amount if condition == "oversize" else b"public"

    def open_url(request: urllib.request.Request, *, timeout: int) -> Response:
        assert request.get_header("Authorization") is None
        assert timeout == 30
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_url)
    if condition == "valid":
        assert public.fetch_public("https://example.test/") == b"public"
    else:
        with pytest.raises(ValueError):
            public.fetch_public("https://example.test/")
