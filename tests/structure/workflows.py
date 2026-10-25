"""Read workflow configuration without YAML 1.1 boolean key conversion."""

from pathlib import Path
from typing import TypedDict

import yaml
from pydantic import TypeAdapter
from scripts.json_data import json_object

ROOT = Path(__file__).resolve().parents[2]


WorkflowStep = TypedDict(
    "WorkflowStep",
    {
        "name": str,
        "id": str,
        "uses": str,
        "run": str,
        "if": str,
        "with": dict[str, str],
        "env": dict[str, str],
        "continue-on-error": str,
    },
    total=False,
)


class WorkflowStrategy(TypedDict):
    matrix: dict[str, list[str]]


WorkflowJob = TypedDict(
    "WorkflowJob",
    {
        "steps": list[WorkflowStep],
        "uses": str,
        "env": dict[str, str],
        "with": dict[str, str],
        "needs": str | list[str],
        "if": str,
        "secrets": str | dict[str, str],
        "permissions": dict[str, str],
        "environment": dict[str, str],
        "runs-on": str | list[str],
        "strategy": WorkflowStrategy,
    },
    total=False,
)


class WorkflowInput(TypedDict, total=False):
    default: str
    type: str
    required: str
    description: str


class WorkflowTrigger(TypedDict, total=False):
    inputs: dict[str, WorkflowInput]


class Workflow(TypedDict):
    on: dict[str, WorkflowTrigger]
    jobs: dict[str, WorkflowJob]


def load_workflow(name: str) -> Workflow:
    raw: object = yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)
    # Empty event declarations have no fields. BaseLoader represents them as empty text.
    document = json_object(raw)
    events = json_object(document["on"])
    document["on"] = {event: {} if value == "" else value for event, value in events.items()}
    return TypeAdapter(Workflow).validate_python(document)
