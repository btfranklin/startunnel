"""Local configuration formatting preserves credentials and extra settings."""

from pathlib import Path

from scripts.bootstrap_env import read_env, render_env, write_env


def test_render_env_preserves_values_without_adding_example_placeholders(tmp_path: Path) -> None:
    values = {
        "OPENAI_MODEL": "local-test-model",
        "LOCAL_SETTING": "'a=b # quoted'",
        "CUSTOM_SECRET": "synthetic-secret",
        "STARTUNNEL_ENV": "development",
        "CUSTOM_SETTING": "synthetic-setting",
    }
    content = render_env(values)
    target = tmp_path / ".env"
    write_env(target, content)

    assert read_env(target) == values
    assert "# Additional local settings" in content
    assert content.index("STARTUNNEL_ENV=") < content.index("OPENAI_MODEL=")
    assert content.index("OPENAI_MODEL=") < content.index("CUSTOM_SECRET=")
    assert "replace-with" not in content
    assert "DATABASE_URL=" not in content
    assert target.stat().st_mode & 0o777 == 0o600
    assert render_env(read_env(target)) == content
