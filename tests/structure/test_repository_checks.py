"""Repository-specific checks are executable from a fresh clone."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_secrets import OCR_PATTERNS

ROOT = Path(__file__).resolve().parents[2]


def test_ocr_secret_patterns_stay_on_one_rendered_line() -> None:
    pattern = OCR_PATTERNS["StarTunnel key-like OCR"]

    assert pattern.search("s t _ " + "A " * 43)
    assert not pattern.search("st_capture\nCreated 10 Aug 2026\nordinary product text")


def test_local_secret_files_are_ignored() -> None:
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in ignore
    assert ".env.tutorial" in ignore or ".env.*" in ignore


def test_wordmark_uses_the_selected_sulfur_symbol() -> None:
    navigation = (ROOT / "templates/site/partials/navigation.html").read_text(encoding="utf-8")

    assert navigation.count("🜎") == 1
    assert "🜀" not in navigation


def test_product_text_uses_no_symbol_after_the_last_alchemical_pictogram() -> None:
    roots = tuple(
        ROOT / name for name in ("src", "templates", "docs", "examples", "website", "cli")
    )
    suffixes = {".html", ".md", ".py", ".txt"}
    forbidden: list[str] = []

    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in suffixes:
                for character in path.read_text(encoding="utf-8"):
                    if 0x1F774 <= ord(character) <= 0x1F77F:
                        forbidden.append(
                            f"{path.relative_to(ROOT)} contains U+{ord(character):05X}."
                        )

    assert not forbidden, forbidden


def test_pages_have_no_heading_or_section_eyebrows() -> None:
    forbidden_classes = {
        "app-header__context",
        "eyebrow",
        "kicker",
        "overline",
        "page-label",
        "review-flag",
        "section-label",
        "section-tag",
        "trust-line",
    }
    sources = [
        *sorted((ROOT / "templates").rglob("*.html")),
        *sorted((ROOT / "website/templates").rglob("*.html")),
        ROOT / "static/css/site.css",
        ROOT / "website/assets/css/signal-chamber.css",
    ]

    for path in sources:
        content = path.read_text(encoding="utf-8")
        for class_name in forbidden_classes:
            assert class_name not in content, (
                f"{path.relative_to(ROOT)} uses the forbidden eyebrow class {class_name}. "
                "Start the section with its heading."
            )


@pytest.mark.parametrize("path", ["Dockerfile", "compose.yaml", "compose.dev.yaml"])
def test_every_uvicorn_command_disables_implicit_proxy_trust(path: str) -> None:
    content = (ROOT / path).read_text(encoding="utf-8")
    commands = content.split("uvicorn")[1:]
    assert commands, f"{path} must contain a Uvicorn command."
    assert all("--no-proxy-headers" in command for command in commands)


def test_web_worker_and_database_pool_defaults_preserve_headroom() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "${STARTUNNEL_WEB_WORKERS:-1}" in compose
    assert "${STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE:-18}" in compose
    assert "${STARTUNNEL_MAINTENANCE_DATABASE_POOL_MAX_SIZE:-4}" in compose
    assert "STARTUNNEL_WEB_WORKERS=1" in environment
    assert "STARTUNNEL_WEB_DATABASE_POOL_MAX_SIZE=18" in environment
    assert "STARTUNNEL_MAINTENANCE_DATABASE_POOL_MAX_SIZE=4" in environment
    assert 1 * 18 + 4 <= 90


def test_runtime_image_keeps_project_static_sources_and_collected_assets() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    production_settings = (ROOT / "src/startunnel/settings/production.py").read_text(
        encoding="utf-8"
    )

    assert "COPY static ./static" in dockerfile
    assert "manage.py collectstatic --noinput" in dockerfile
    assert "COPY --from=application-builder /app/static /app/static" in dockerfile
    assert "COPY --from=application-builder /app/staticfiles /app/staticfiles" in dockerfile
    assert "STATICFILES_DIRS = []" not in production_settings


def test_runtime_image_removes_unused_package_runtimes() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = dockerfile.split("FROM python-base AS runtime", 1)[1].split(
        "FROM development-dependencies AS development-runtime", 1
    )[0]

    assert "apk add --no-cache ca-certificates tini" in runtime
    assert "apt-get" not in runtime
    assert "/usr/local/lib/python3.14/site-packages/pip" in runtime
    assert "/app/.venv/lib/python3.14/site-packages/pip" in runtime
