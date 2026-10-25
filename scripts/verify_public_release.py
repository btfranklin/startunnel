"""Verify a published StarTunnel release without registry or GitHub credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import tempfile
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict
from urllib.parse import urlsplit

if TYPE_CHECKING or __package__:
    from .compose_isolation import assert_local_docker, safe_environment
    from .json_data import json_object, json_string, object_array
    from .prove_production_release import image_metadata, validate_candidate_metadata
    from .release_candidate import OFFICIAL_IMAGE
    from .release_record import ROOT, VERSION, candidate_version
else:
    from compose_isolation import assert_local_docker, safe_environment
    from json_data import json_object, json_string, object_array
    from prove_production_release import image_metadata, validate_candidate_metadata
    from release_candidate import OFFICIAL_IMAGE
    from release_record import ROOT, VERSION, candidate_version

REPOSITORY = "https://github.com/btfranklin/startunnel"
API = "https://api.github.com/repos/btfranklin/startunnel"
RAW = "https://raw.githubusercontent.com/btfranklin/startunnel"


class PublicVerificationError(ValueError):
    """A fixed, public diagnostic without response or credential data."""


class PublicVerification(TypedDict):
    version: str
    source_commit: str | None
    image: str | None
    status: str
    checks: dict[str, bool]
    failed_check: str | None
    failure_type: str | None
    failure: str | None


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.links.update(value for key, value in attrs if key == "href" and value)


def fetch_public(url: str) -> bytes:
    """Read bounded public HTTPS content with no authorization header."""
    if urlsplit(url).scheme != "https":
        raise PublicVerificationError("Public release checks require HTTPS.")
    request = urllib.request.Request(url, headers={"User-Agent": "StarTunnel-release-verifier"})
    # All callers supply fixed GitHub URLs or a validated HTTPS site origin.
    with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
        if urlsplit(response.url).scheme != "https":
            raise PublicVerificationError("A public download left HTTPS.")
        content: object = response.read(2_000_001)
    if not isinstance(content, bytes):
        raise PublicVerificationError("The public resource did not return bytes.")
    if len(content) > 2_000_000:
        raise PublicVerificationError("A public release resource exceeds the read limit.")
    return content


def git_file(commit: str, name: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)  # nosec B603 B607


def anonymous_image(image: str, commit: str, version: str) -> None:
    """Use the current local engine with an empty temporary registry configuration."""
    environment = safe_environment(os.environ)
    endpoint = assert_local_docker(environment)
    for name in ("DOCKER_CONTEXT", "DOCKER_AUTH_CONFIG", "REGISTRY_AUTH_FILE"):
        environment.pop(name, None)
    with tempfile.TemporaryDirectory(prefix="startunnel-public-docker-") as directory:
        environment.update(DOCKER_CONFIG=directory, DOCKER_HOST=endpoint)
        try:
            subprocess.run(  # nosec B603 B607
                ["docker", "pull", "--platform", "linux/amd64", image],
                env=environment,
                check=True,
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise PublicVerificationError("The anonymous image pull did not pass.") from error
        labels, platform = image_metadata(image, environment)
        validate_candidate_metadata(labels, platform=platform, commit=commit, version=version)


def verify_public_release(
    version: str,
    *,
    site_url: str = "https://startunnel.net",
    fetch: Callable[[str], bytes] = fetch_public,
    pull: Callable[[str, str, str], None] = anonymous_image,
) -> PublicVerification:
    """Check immutable release inputs and public entry points; retain safe failures."""
    if not VERSION.fullmatch(version):
        raise PublicVerificationError("Use a vMAJOR.MINOR.PATCH release tag.")
    parsed = urlsplit(site_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PublicVerificationError("Use an HTTPS site origin without credentials or a path.")
    site_url = site_url.rstrip("/")
    checks = {
        name: False
        for name in (
            "published_release",
            "deployment_record",
            "tagged_guides",
            "public_entry_points",
            "anonymous_image",
        )
    }
    report: PublicVerification = {
        "version": version,
        "source_commit": None,
        "image": None,
        "status": "failed",
        "checks": checks,
        "failed_check": None,
        "failure_type": None,
        "failure": None,
    }
    step = "published_release"
    try:
        release = json_object(json.loads(fetch(f"{API}/releases/latest")))
        if (
            release.get("tag_name") != version
            or release.get("draft") is not False
            or release.get("prerelease") is not False
            or not isinstance(release.get("published_at"), str)
        ):
            raise PublicVerificationError(
                "The selected tag is not the latest published stable release."
            )
        assets = [
            asset
            for asset in object_array(release.get("assets", []))
            if asset.get("name") == "release.json"
        ]
        if len(assets) != 1:
            raise PublicVerificationError("The release must have exactly one deployment record.")
        checks[step] = True
        step = "deployment_record"
        content = fetch(f"{REPOSITORY}/releases/download/{version}/release.json")
        if assets[0].get("digest") != "sha256:" + hashlib.sha256(content).hexdigest():
            raise PublicVerificationError("The public deployment record checksum differs.")
        record = json_object(json.loads(content))
        commit = json_string(record.get("source_commit"))
        image = json_string(record.get("image"))
        if (
            record.get("version") != version
            or record.get("platform") != "linux/amd64"
            or record.get("schema") != 1
            or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not OFFICIAL_IMAGE.fullmatch(image)
        ):
            raise PublicVerificationError("The deployment record identity is invalid.")
        expected_commit = subprocess.check_output(  # nosec B603 B607
            ["git", "rev-parse", f"{version}^{{commit}}"],
            cwd=ROOT,
            text=True,
        ).strip()
        if expected_commit != commit:
            raise PublicVerificationError(
                "The deployment record differs from the local release tag."
            )
        specification = json_object(json.loads(git_file(commit, f"releases/{version}.json")))
        if any(record.get(key) != value for key, value in specification.items()):
            raise PublicVerificationError(
                "The deployment record differs from its tagged specification."
            )
        report["source_commit"] = commit
        report["image"] = image
        checks[step] = True
        step = "tagged_guides"
        for name in ("docs/setup/README.md", "docs/setup/maintain.md", f"releases/{version}.md"):
            if fetch(f"{RAW}/{version}/{name}") != git_file(commit, name):
                raise PublicVerificationError("A public tagged guide differs from its source.")
        checks[step] = True
        step = "public_entry_points"
        links = _Links()
        links.feed(fetch(site_url + "/").decode("utf-8"))
        if REPOSITORY + "/releases" not in links.links:
            raise PublicVerificationError("The homepage has no published-release entry point.")
        if fetch(site_url + "/agents.md") != git_file("HEAD", "skills/startunnel/SKILL.md"):
            raise PublicVerificationError("The live agent guide differs from the website source.")
        checks[step] = True
        step = "anonymous_image"
        pull(image, commit, version)
        checks[step] = True
        report["status"] = "passed"
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as error:
        report["failed_check"] = step
        report["failure_type"] = type(error).__name__
        report["failure"] = (
            str(error)
            if isinstance(error, PublicVerificationError)
            else "The public check did not complete."
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--site-url", default="https://startunnel.net")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = verify_public_release(args.tag or candidate_version(), site_url=args.site_url)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Public release {report['version']}: {report['status']}.")
        if report["failed_check"]:
            print(f"Failed check: {report['failed_check']}; {report['failure_type']}.")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
