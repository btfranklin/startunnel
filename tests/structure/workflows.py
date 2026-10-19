"""Read workflow configuration without YAML 1.1 boolean key conversion."""

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_workflow(name: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader),
    )
