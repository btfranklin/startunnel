"""Build the promotional website without application settings or a database."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from django.template import Context, Engine
from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
MANIFEST = ".startunnel-build.json"
if not __package__:
    sys.path.insert(0, str(ROOT))


def build(
    output: Path,
    base_path: str = "/",
    repository_url: str = "https://github.com/btfranklin/startunnel",
    site_url: str = "https://startunnel.net",
) -> Path:
    """Render the static site and its public agent resources."""
    from website.glyphs import GATE_GLYPHS

    output = output.resolve()
    if output == ROOT or output in ROOT.parents:
        raise ValueError("Output must not be the repository or one of its parent directories.")
    if output.is_relative_to(ROOT) and output != WEBSITE / "dist":
        raise ValueError("Build inside the repository only at website/dist.")
    if output.exists() and any(path.is_symlink() for path in output.rglob("*")):
        raise ValueError("Output must not contain symbolic links.")
    previous: set[str] = set()
    if output.exists() and any(output.iterdir()):
        manifest = output / MANIFEST
        if not manifest.is_file():
            raise ValueError("Nonempty output is not an owned website build.")
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(recorded, list) or not all(
            isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts
            for name in recorded
        ):
            raise ValueError("Invalid website build manifest.")
        previous = set(recorded)
        actual = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
        if actual - previous - {MANIFEST}:
            raise ValueError("Output contains files not owned by the website build.")
    if not base_path.startswith("/") or ".." in base_path or any(c in base_path for c in "?#\\"):
        raise ValueError("Base path must be an absolute URL path without query or traversal.")
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Repository URL must be an HTTPS URL without query or fragment.")
    origin = urlsplit(site_url)
    if (
        origin.scheme not in {"http", "https"}
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        raise ValueError("Site URL must be an HTTP or HTTPS origin without a path or credentials.")
    base_path = base_path.rstrip("/") + "/"
    repository_url = repository_url.rstrip("/")
    site_root = site_url.rstrip("/") + base_path
    engine = Engine(dirs=[str(WEBSITE / "templates")], autoescape=True)
    context = Context(
        {
            "base_path": base_path,
            "asset_url": base_path + "assets/",
            "repository_url": repository_url,
            "site_root": site_root,
            "agent_guide_url": site_root + "agents.md",
            "gate_glyphs": GATE_GLYPHS,
        },
        use_l10n=False,
        use_tz=False,
    )
    # Prompt files are shared by the visible text and the copy action.
    text_context = Context(context.flatten(), autoescape=False, use_l10n=False, use_tz=False)
    context["prompts"] = {
        name: engine.from_string((WEBSITE / "prompts" / f"{name}.md").read_text(encoding="utf-8"))
        .render(text_context)
        .strip()
        for name in ("start", "setup", "connect")
    }
    guide = (ROOT / "skills/startunnel/SKILL.md").read_text(encoding="utf-8")
    guide_body = guide.split("\n---\n", 1)[1]
    context["guide_html"] = MarkdownIt("commonmark", {"html": False}).render(guide_body)
    rendered = {
        "index.html": engine.get_template("index.html").render(context),
        "agents/index.html": engine.get_template("agents.html").render(context),
        "llms.txt": engine.get_template("llms.txt").render(text_context),
    }
    output.mkdir(parents=True, exist_ok=True)
    sources = {
        "assets/" + str(path.relative_to(WEBSITE / "assets")): path
        for path in (WEBSITE / "assets").rglob("*")
        if path.is_file()
    }
    sources["agents.md"] = ROOT / "skills/startunnel/SKILL.md"
    sources["openapi.json"] = ROOT / "generated/openapi.json"
    # Shared presentation assets have one authored source in the application tree.
    for name in ("css/site.css", "css/tokens.css", "js/site.js", "favicon.svg"):
        sources["assets/" + name] = ROOT / "static" / name
    sources.update(
        {
            "assets/vendor/fonts/" + path.name: path
            for path in (ROOT / "static/vendor/fonts").iterdir()
            if path.is_file()
        }
    )
    current = set(sources) | set(rendered) | {".nojekyll"}
    for name in previous - current:
        (output / name).unlink(missing_ok=True)
    for name, source in sources.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    for name, content in rendered.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    (output / ".nojekyll").touch()
    (output / MANIFEST).write_text(json.dumps(sorted(current)), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=WEBSITE / "dist")
    parser.add_argument("--base-path", default="/")
    parser.add_argument("--repository-url", default="https://github.com/btfranklin/startunnel")
    parser.add_argument("--site-url", default="https://startunnel.net")
    args = parser.parse_args()
    build(args.output, args.base_path, args.repository_url, args.site_url)
    print(f"Website built at {args.output}")


if __name__ == "__main__":
    main()
