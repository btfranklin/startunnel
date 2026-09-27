"""The promotional artifact is static and supports project-site URL prefixes."""

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from bs4 import BeautifulSoup
from website.build import ROOT, build


def test_build_command_does_not_load_application_settings(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "website/build.py"), "--output", str(tmp_path)],
        cwd=tmp_path,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "unavailable_application_settings"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "index.html").is_file()
    assert not (tmp_path / "src").exists()
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize("base_path", ["/", "/startunnel/"])
def test_static_build_resolves_local_assets(tmp_path: Path, base_path: str) -> None:
    build(tmp_path, base_path, "https://github.com/example/startunnel")
    html = (tmp_path / "index.html").read_text()
    assert "{%" not in html and "{{" not in html
    assert "https://github.com/example/startunnel/blob/main/examples/README.md" in html
    soup = BeautifulSoup(html, "html.parser")
    assert "pricing" not in soup.get_text(" ").lower()
    assert "payment" not in soup.get_text(" ").lower()
    for page in ("index.html", "agents/index.html"):
        page_soup = BeautifulSoup((tmp_path / page).read_text(), "html.parser")
        for element in page_soup.find_all():
            for name in ("href", "src", "data-vertex-shader", "data-fragment-shader"):
                value = element.get(name)
                if not isinstance(value, str) or value.startswith("#") or urlsplit(value).scheme:
                    continue
                assert value.startswith(base_path), value
                relative = value.removeprefix(base_path)
                assert (tmp_path / relative).exists(), value
    assert (tmp_path / "assets/vendor/fonts/inter-latin-400-normal.woff2").is_file()


def test_landing_has_one_detailed_agent_flow(tmp_path: Path) -> None:
    html = (build(tmp_path) / "index.html").read_text()
    soup = BeautifulSoup(html, "html.parser")
    navigation = soup.select_one(".site-nav__links")
    assert navigation is not None
    assert [link.get_text(strip=True) for link in navigation.select("a")] == [
        "Agent guide",
        "Docs",
        "Source",
    ]
    footer_line = soup.select_one(".statement-footer__line")
    assert footer_line is not None
    assert footer_line.get_text(strip=True) == (
        "One meeting place for your agents, on your infrastructure."
    )

    flow = soup.select_one(".signal-flow")
    assert flow is not None
    heading = flow.select_one("h2")
    assert heading is not None
    assert heading.get_text(strip=True) == "An Easy Communication Flow for Agents"
    steps = flow.select(".signal-flow__steps > li")
    assert [step.h3.get_text(strip=True) for step in steps if step.h3] == [
        "Create",
        "Reply",
        "Read context",
        "Close",
    ]
    assert len(steps) == 4
    assert all(step.svg is not None and step.p is not None for step in steps)
    assert all(len(step.p.get_text(" ", strip=True)) > 60 for step in steps if step.p)
    assert soup.select_one(".lifecycle-band") is None
    assert "Global tunnels are unlisted, not confidential." not in html
    assert "Know the boundary before you send." not in html

    css = (tmp_path / "assets/css/signal-chamber.css").read_text()
    assert 'content: "↓"' in css
    assert 'content: "→"' in css


@pytest.mark.parametrize("base_path", ["/", "/startunnel/"])
def test_agent_entry_points_share_sources_and_resolve_from_prompts(
    tmp_path: Path, base_path: str
) -> None:
    origin = "https://preview.example"
    repository = "https://github.com/example/startunnel"
    build(tmp_path, base_path=base_path, site_url=origin, repository_url=repository)
    soup = BeautifulSoup((tmp_path / "index.html").read_text(), "html.parser")
    buttons = soup.select("[data-copy-target]")
    assert len(buttons) == 3
    for button in buttons:
        target = soup.find(id=button["data-copy-target"])
        assert target is not None
        assert not target.has_attr("hidden")
        assert origin + base_path + "agents.md" in target.get_text()
    setup_index = repository + "/blob/main/docs/setup/README.md"
    for prompt_id in ("start-prompt", "setup-prompt"):
        prompt = soup.find(id=prompt_id)
        assert prompt is not None
        assert setup_index in prompt.get_text()
    assert soup.select_one(f'a[href="{setup_index}"]') is not None
    guide = (tmp_path / "agents.md").read_bytes()
    assert guide == (ROOT / "skills/startunnel/SKILL.md").read_bytes()
    assert (tmp_path / "openapi.json").read_bytes() == (
        ROOT / "generated/openapi.json"
    ).read_bytes()
    index = (tmp_path / "llms.txt").read_text()
    assert origin + base_path + "agents.md" in index
    assert origin + base_path + "openapi.json" in index
    assert setup_index in index
    assert "{{" not in index
    assert "brew install" not in soup.get_text()


@pytest.mark.parametrize(
    "origin",
    ["relative", "ftp://example.test", "https://example.test/path", "https://user@example.test"],
)
def test_build_rejects_invalid_site_origin(tmp_path: Path, origin: str) -> None:
    with pytest.raises(ValueError, match="Site URL"):
        build(tmp_path, site_url=origin)


@pytest.mark.parametrize("path", ["relative", "/../", "/x?y", "/x#y"])
def test_build_rejects_invalid_base_path(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="Base path"):
        build(tmp_path, path)


@pytest.mark.parametrize("output", [ROOT, ROOT.parent, ROOT / "src", ROOT / "website/assets"])
def test_build_rejects_source_output(output: Path) -> None:
    with pytest.raises(ValueError, match="repository"):
        build(output)


def test_rebuild_removes_stale_owned_files(tmp_path: Path) -> None:
    build(tmp_path)
    stale = tmp_path / "assets/stale.js"
    stale.write_text("old asset")
    manifest = tmp_path / ".startunnel-build.json"
    manifest.write_text(json.dumps([*json.loads(manifest.read_text()), "assets/stale.js"]))
    build(tmp_path)
    assert not stale.exists()


def test_build_preserves_unowned_files(tmp_path: Path) -> None:
    build(tmp_path)
    unrelated = tmp_path / "private.txt"
    unrelated.write_text("unrelated content")
    with pytest.raises(ValueError, match="not owned"):
        build(tmp_path)
    assert unrelated.read_text() == "unrelated content"
