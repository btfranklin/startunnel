"""The decorative gate has a fixed identity and does not change agent addresses."""

import re
from pathlib import Path

from bs4 import BeautifulSoup
from django.test import Client
from website.build import build
from website.glyphs import GATE_GLYPHS

ROOT = Path(__file__).resolve().parents[2]


def test_brand_is_shared_by_service_site_and_style_guide(client: Client, tmp_path: Path) -> None:
    build(tmp_path)
    pages = [
        client.get("/docs/").content.decode(),
        client.get("/accounts/login/").content.decode(),
        (tmp_path / "index.html").read_text(),
        (tmp_path / "agents/index.html").read_text(),
        (ROOT / "ui-style-guide/style-guide.html").read_text(),
        (ROOT / "ui-style-guide/demo.html").read_text(),
    ]
    for html in pages:
        marks = BeautifulSoup(html, "html.parser").select(".wordmark__mark")
        assert marks
        assert all(mark.get_text(strip=True) == "🜎" for mark in marks)
    tokens = (ROOT / "static/css/tokens.css").read_text()
    assert '--font-display: "Space Grotesk"' in tokens
    assert '--font-glyph: "Noto Sans Symbols"' in tokens
    css = (ROOT / "static/css/site.css").read_text()
    assert 'font-family: "Noto Sans Symbols";' in css
    assert "noto-sans-symbols-symbols-400-normal.woff2" in css
    assert (tmp_path / "assets/vendor/fonts/noto-sans-symbols-symbols-400-normal.woff2").is_file()
    assert (tmp_path / "assets/favicon.svg").read_bytes() == (
        ROOT / "static/favicon.svg"
    ).read_bytes()


def test_gate_glyphs_are_exact_unique_and_clockwise() -> None:
    assert "".join(str(glyph["character"]) for glyph in GATE_GLYPHS) == "🜎🜁🜂🜃🜄🜍🜔🜞"
    assert len({glyph["id"] for glyph in GATE_GLYPHS}) == 8
    assert [glyph["angle"] for glyph in GATE_GLYPHS] == [0, 49, 90, 130, 180, 230, 270, 311]
    assert [(glyph["x"], glyph["y"]) for glyph in GATE_GLYPHS] == [
        (402, 84),
        (628, 200),
        (704, 397),
        (628, 593),
        (400, 702),
        (171, 593),
        (92, 396),
        (170, 198),
    ]
    for glyph in GATE_GLYPHS:
        character = str(glyph["character"])
        assert 0x1F700 <= ord(character) <= 0x1F73F
        assert glyph["codepoint"] == f"U+{ord(character):X}"


def test_landing_renders_all_glyphs_without_javascript(tmp_path: Path) -> None:
    html = (build(tmp_path) / "index.html").read_text()
    assert re.findall(r'data-gate-glyph="([^"]+)"', html) == [glyph["id"] for glyph in GATE_GLYPHS]
    assert "data-gate-fallback" in html
    assert "data-gate-toggle-label" in html
    assert "shaders/signal-gate.frag" in html
    assert "signal-chamber.css" in html
    for glyph in GATE_GLYPHS:
        assert f"rotate({glyph['angle']} {glyph['x']} {glyph['y']})" in html


def test_landing_assets_do_not_load_on_documentation(client: Client) -> None:
    html = client.get("/docs/").content.decode()
    assert "signal-chamber.css" not in html
    assert "signal-gate.js" not in html
    assert "data-signal-gate" not in html


def test_gate_assets_remain_small_and_dependency_free() -> None:
    assets = [
        ROOT / "website/assets/js/signal-gate.js",
        ROOT / "website/assets/shaders/signal-gate.vert",
        ROOT / "website/assets/shaders/signal-gate.frag",
        ROOT / "website/templates/partials/signal-gate.html",
    ]
    assert sum(path.stat().st_size for path in assets) < 50000
    assert (ROOT / "website/assets/img/signal-gate-ring.png").stat().st_size < 2_500_000
    script = assets[0].read_text()
    assert "import(" not in script
    assert "requestAnimationFrame" in script
    assert "prefers-reduced-motion" in script
