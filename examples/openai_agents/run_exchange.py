"""Run one bounded tunnel-tree proof with separate OpenAI SDK agents."""

# ruff: noqa: E402 -- resolve the SDK after removal of the colliding local source path.

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# The Django application and the SDK both use the top-level name ``agents``.
# This standalone example does not import Django. Remove only this repository's
# source directory before Python resolves the OpenAI SDK package.
_LOCAL_SOURCE_DIRECTORY = Path(__file__).resolve().parents[2] / "src"
_LOADED_AGENTS_MODULE = sys.modules.get("agents")
_LOADED_AGENTS_FILE = getattr(_LOADED_AGENTS_MODULE, "__file__", None)
if _LOADED_AGENTS_FILE and Path(_LOADED_AGENTS_FILE).resolve().is_relative_to(
    _LOCAL_SOURCE_DIRECTORY
):
    raise RuntimeError(
        "Run this example in a separate process. The current process already loaded Django."
    )
sys.path[:] = [
    entry for entry in sys.path if Path(entry or ".").resolve() != _LOCAL_SOURCE_DIRECTORY
]

from examples.python.startunnel_client import (
    JsonObject,
    StarTunnelClient,
    load_env_file,
    new_idempotency_key,
    require_environment,
)
from pydantic import BaseModel

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    ToolExecutionConfig,
    function_tool,
    set_tracing_disabled,
)

PROMPT_DIRECTORY = Path(__file__).with_name("prompts")
MAX_STAGE_TURNS = 2
REQUIRED_HTTP_EVENTS = {
    "tunnel_and_root_created",
    "receiver_read_root",
    "receiver_reply_posted",
    "sender_read_reply",
    "sender_confirmation_posted",
    "cycle_closed",
    "retained_tree_read",
}
REQUIRED_TOOL_CALLS = [
    "create_tunnel_with_root",
    "read_root_and_reply",
    "read_reply_and_confirm",
]


class AgentStageResult(BaseModel):
    """The small structured result for one agent stage."""

    completed: bool
    summary: str


@dataclass
class ExchangeState:
    """Program-owned evidence that is not part of either agent conversation."""

    sender_credential_id: str
    receiver_credential_id: str
    tunnel_id: str | None = None
    address: str | None = None
    cycle_id: str | None = None
    root_message_id: str | None = None
    reply_message_id: str | None = None
    confirmation_message_id: str | None = None
    cycle_closed: bool = False
    tool_calls: list[str] = field(default_factory=list)
    http_events: list[str] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        resource_ids = (
            self.tunnel_id,
            self.cycle_id,
            self.root_message_id,
            self.reply_message_id,
            self.confirmation_message_id,
        )
        if not all(resource_ids):
            raise RuntimeError("The proof does not have complete resource evidence.")
        return {
            "tunnel_id": self.tunnel_id,
            "cycle_id": self.cycle_id,
            "root_message_id": self.root_message_id,
            "reply_message_id": self.reply_message_id,
            "confirmation_message_id": self.confirmation_message_id,
            "sender_credential_id": self.sender_credential_id,
            "receiver_credential_id": self.receiver_credential_id,
            "tool_calls": self.tool_calls,
            "http_events": self.http_events,
            "structured_results_validated": True,
        }


@dataclass(frozen=True, slots=True)
class AgentClient:
    """Expose only the operations needed by one live agent."""

    client: StarTunnelClient

    async def create(
        self, *, label: str, root_content: JsonObject, correlation_id: str
    ) -> JsonObject:
        return await self.client.create_tunnel(
            label=label,
            cycle_label="Live agent collaboration",
            root_content=root_content,
            root_correlation_id=correlation_id,
            idempotency_key=new_idempotency_key(),
        )

    async def post_reply(
        self,
        *,
        address: str,
        parent_id: str,
        content: JsonObject,
        correlation_id: str,
    ) -> JsonObject:
        return await self.client.post_reply(
            address=address,
            parent_id=parent_id,
            content=content,
            correlation_id=correlation_id,
            idempotency_key=new_idempotency_key(),
        )

    async def read_message(self, *, address: str, cycle_id: str, message_id: str) -> JsonObject:
        return await self.client.read_message(
            address=address, cycle_id=cycle_id, message_id=message_id
        )

    async def read_tree(self, *, address: str, cycle_id: str) -> JsonObject:
        return await self.client.read_tree(address=address, cycle_id=cycle_id)

    async def close_cycle(self, *, address: str, cycle_id: str) -> None:
        await self.client.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )


def load_prompt(name: str) -> str:
    """Load one complete authored prompt without an inline fallback."""

    return (PROMPT_DIRECTORY / name).read_text(encoding="utf-8")


def render_sender_prompt(task_note: str) -> str:
    """Render the one required placeholder in the external sender prompt."""

    template = load_prompt("sender.md")
    placeholder = "{{ task_note }}"
    if template.count(placeholder) != 1:
        raise RuntimeError("The sender prompt must contain one task-note placeholder.")
    return template.replace(placeholder, task_note)


def _load_protected_credential_file() -> None:
    raw_path = os.getenv("STARTUNNEL_LIVE_CREDENTIALS_FILE")
    if not raw_path:
        return
    path = Path(raw_path)
    if path.is_symlink():
        raise RuntimeError("The live-agent credential file must not be a symbolic link.")
    try:
        file_status = path.stat()
    except OSError as error:
        raise RuntimeError("The live-agent credential file is not available.") from error
    if not stat.S_ISREG(file_status.st_mode):
        raise RuntimeError("The live-agent credential path is not a regular file.")
    mode = file_status.st_mode & 0o777
    if mode != 0o600:
        raise RuntimeError("The live-agent credential file must have mode 0600.")
    load_env_file(path)


def _agent_identity(response: dict[str, Any]) -> str:
    agent = response.get("agent")
    if not isinstance(agent, dict):
        raise RuntimeError("The agent identity response is not valid.")
    return str(agent.get("id", ""))


async def _verify_identities(
    *,
    sender: StarTunnelClient,
    receiver: StarTunnelClient,
    expected_sender_id: str | None,
    expected_receiver_id: str | None,
) -> tuple[str, str]:
    sender_identity, receiver_identity = await asyncio.gather(sender.me(), receiver.me())
    sender_id = _agent_identity(sender_identity)
    receiver_id = _agent_identity(receiver_identity)
    if not sender_id or not receiver_id or sender_id == receiver_id:
        raise RuntimeError("The proof requires two distinct agent credentials.")
    if expected_sender_id and sender_id != expected_sender_id:
        raise RuntimeError("The sender credential does not match its fixture ID.")
    if expected_receiver_id and receiver_id != expected_receiver_id:
        raise RuntimeError("The receiver credential does not match its fixture ID.")
    return sender_id, receiver_id


def _required_object(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"The {description} was not valid.")
    return value


def _validate_stage(result: Any, *, actor: str, stage: str) -> None:
    output = result.final_output
    if not isinstance(output, AgentStageResult) or not output.completed:
        raise RuntimeError(f"The {actor} agent did not complete the {stage} stage.")


async def _run_scenario(
    *,
    base_url: str,
    sender_key: str,
    receiver_key: str,
    model: str,
    expected_sender_id: str | None = None,
    expected_receiver_id: str | None = None,
) -> dict[str, Any]:
    sender_client = StarTunnelClient(base_url, sender_key)
    receiver_client = StarTunnelClient(base_url, receiver_key)
    sender_id, receiver_id = await _verify_identities(
        sender=sender_client,
        receiver=receiver_client,
        expected_sender_id=expected_sender_id,
        expected_receiver_id=expected_receiver_id,
    )
    sender_api = AgentClient(sender_client)
    receiver_api = AgentClient(receiver_client)
    state = ExchangeState(sender_id, receiver_id)
    correlation_id = "openai-agent-tree-proof"
    expected_task_note = load_prompt("task.md").strip()
    if not expected_task_note:
        raise RuntimeError("The external live-proof task is empty.")

    @function_tool
    async def create_tunnel_with_root(task_note: str) -> str:
        """Create a stable tunnel and an immutable structured root message.

        Args:
            task_note: The exact non-sensitive release-note review task.
        """

        state.tool_calls.append("create_tunnel_with_root")
        if task_note != expected_task_note:
            raise RuntimeError("The sender supplied an unexpected task note.")
        if state.tunnel_id is not None:
            raise RuntimeError("The sender already created the tunnel for this proof.")
        created = await sender_api.create(
            label="OpenAI Agents SDK tree proof",
            root_content={
                "type": "json",
                "value": {"action": "review", "note": task_note, "priority": 2},
            },
            correlation_id=correlation_id,
        )
        tunnel = _required_object(created.get("tunnel"), "created tunnel")
        cycle = _required_object(created.get("cycle"), "created cycle")
        root = _required_object(cycle.get("root"), "created root")
        state.tunnel_id = str(tunnel["id"])
        state.address = str(tunnel["address"])
        state.cycle_id = str(cycle["id"])
        state.root_message_id = str(root["id"])
        state.http_events.append("tunnel_and_root_created")
        return json.dumps({"status": "root_created"})

    @function_tool
    async def read_root_and_reply(
        review_status: Literal["approved", "needs_changes"],
    ) -> str:
        """Read the root and post one immutable child reply.

        Args:
            review_status: The result of the bounded review.
        """

        state.tool_calls.append("read_root_and_reply")
        if not state.address or not state.cycle_id or not state.root_message_id:
            raise RuntimeError("The sender has not created the root message.")
        read = await receiver_api.read_message(
            address=state.address,
            cycle_id=state.cycle_id,
            message_id=state.root_message_id,
        )
        message = _required_object(read.get("message"), "root message")
        content = _required_object(message.get("content"), "root content")
        value = _required_object(content.get("value"), "root JSON value")
        sender = _required_object(message.get("sender"), "root sender")
        if value.get("note") != expected_task_note or str(sender.get("id")) != sender_id:
            raise RuntimeError("The receiver read an unexpected root message.")
        state.http_events.append("receiver_read_root")
        posted = await receiver_api.post_reply(
            address=state.address,
            parent_id=state.root_message_id,
            content={
                "type": "json",
                "value": {"status": review_status, "reviewed_by": "receiver-agent"},
            },
            correlation_id=correlation_id,
        )
        reply = _required_object(posted.get("message"), "receiver reply")
        if str(reply.get("parent_id")) != state.root_message_id:
            raise RuntimeError("The receiver reply does not point to the root.")
        state.reply_message_id = str(reply["id"])
        state.http_events.append("receiver_reply_posted")
        return json.dumps({"status": "reply_posted"})

    @function_tool
    async def read_reply_and_confirm() -> str:
        """Read the receiver reply and post one immutable confirmation below it."""

        state.tool_calls.append("read_reply_and_confirm")
        if not state.address or not state.cycle_id or not state.reply_message_id:
            raise RuntimeError("The receiver has not posted the reply.")
        read = await sender_api.read_message(
            address=state.address,
            cycle_id=state.cycle_id,
            message_id=state.reply_message_id,
        )
        message = _required_object(read.get("message"), "receiver reply")
        content = _required_object(message.get("content"), "receiver reply content")
        value = _required_object(content.get("value"), "receiver reply JSON value")
        reply_sender = _required_object(message.get("sender"), "reply sender")
        if value.get("status") != "approved" or str(reply_sender.get("id")) != receiver_id:
            raise RuntimeError("The sender read an unexpected reply.")
        state.http_events.append("sender_read_reply")
        posted = await sender_api.post_reply(
            address=state.address,
            parent_id=state.reply_message_id,
            content={
                "type": "json",
                "value": {"status": "confirmed", "confirmed_by": "sender-agent"},
            },
            correlation_id=correlation_id,
        )
        confirmation = _required_object(posted.get("message"), "sender confirmation")
        if str(confirmation.get("parent_id")) != state.reply_message_id:
            raise RuntimeError("The confirmation does not point to the receiver reply.")
        state.confirmation_message_id = str(confirmation["id"])
        state.http_events.append("sender_confirmation_posted")
        return json.dumps({"status": "confirmation_posted"})

    run_config = RunConfig(
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        tool_execution=ToolExecutionConfig(max_function_tool_concurrency=1),
    )
    model_settings = ModelSettings(
        max_tokens=500,
        parallel_tool_calls=False,
        tool_choice="required",
        store=False,
    )
    sender_agent = Agent(
        name="StarTunnel sender",
        instructions=render_sender_prompt(expected_task_note),
        model=model,
        model_settings=model_settings,
        tools=[create_tunnel_with_root, read_reply_and_confirm],
        output_type=AgentStageResult,
    )
    receiver_agent = Agent(
        name="StarTunnel receiver",
        instructions=load_prompt("receiver.md"),
        model=model,
        model_settings=model_settings,
        tools=[read_root_and_reply],
        output_type=AgentStageResult,
    )

    try:
        sender_start = await Runner.run(
            sender_agent,
            "stage:create",
            max_turns=MAX_STAGE_TURNS,
            run_config=run_config,
        )
        _validate_stage(sender_start, actor="sender", stage="create")
        print("1/3 Sender agent created a tunnel and root            OK")

        receiver_result = await Runner.run(
            receiver_agent,
            "stage:reply",
            max_turns=MAX_STAGE_TURNS,
            run_config=run_config,
        )
        _validate_stage(receiver_result, actor="receiver", stage="reply")
        print("2/3 Receiver agent read the root and replied          OK")

        sender_finish = await Runner.run(
            sender_agent,
            "stage:confirm",
            max_turns=MAX_STAGE_TURNS,
            run_config=run_config,
        )
        _validate_stage(sender_finish, actor="sender", stage="confirm")
        print("3/3 Sender agent read the reply and confirmed         OK")

        if not state.address or not state.cycle_id:
            raise RuntimeError("The exchange did not create a cycle address.")
        await sender_api.close_cycle(address=state.address, cycle_id=state.cycle_id)
        state.cycle_closed = True
        state.http_events.append("cycle_closed")
        retained = await receiver_api.read_tree(address=state.address, cycle_id=state.cycle_id)
        nodes = retained.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 3:
            raise RuntimeError("The retained tree does not contain all three messages.")
        state.http_events.append("retained_tree_read")

        if set(state.http_events) != REQUIRED_HTTP_EVENTS:
            raise RuntimeError("The HTTP evidence is incomplete.")
        if state.tool_calls != REQUIRED_TOOL_CALLS:
            raise RuntimeError("The proof did not use the exact required tool sequence.")
        return state.report()
    finally:
        if state.address and state.cycle_id and not state.cycle_closed:
            await sender_api.close_cycle(address=state.address, cycle_id=state.cycle_id)
        await sender_client.aclose()
        await receiver_client.aclose()


def _write_evidence(path_value: str, report: dict[str, Any]) -> None:
    path = Path(path_value)
    if not path.is_absolute() or not path.parent.is_dir():
        raise RuntimeError("STARTUNNEL_LIVE_EVIDENCE_PATH must use an existing absolute path.")
    if path.exists() or path.is_symlink():
        raise RuntimeError("The live-agent evidence file already exists.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        data = json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


async def run() -> None:
    """Run the live proof after the explicit cost opt-in."""

    if os.getenv("STARTUNNEL_LIVE_AGENT_TESTS") != "1":
        raise RuntimeError(
            "Set STARTUNNEL_LIVE_AGENT_TESTS=1 to confirm this cost-bearing live proof."
        )
    set_tracing_disabled(True)
    load_env_file(".env", names={"OPENAI_API_KEY", "OPENAI_MODEL"})
    _load_protected_credential_file()
    load_env_file()
    common = require_environment("STARTUNNEL_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
    agent_env = require_environment("STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY")
    print("Tunnel proof")
    reports = [
        await _run_scenario(
            base_url=common["STARTUNNEL_BASE_URL"],
            sender_key=agent_env["STARTUNNEL_SENDER_KEY"],
            receiver_key=agent_env["STARTUNNEL_RECEIVER_KEY"],
            model=common["OPENAI_MODEL"],
            expected_sender_id=os.getenv("STARTUNNEL_SENDER_ID"),
            expected_receiver_id=os.getenv("STARTUNNEL_RECEIVER_ID"),
        )
    ]

    report = {"scenarios": reports}
    if evidence_path := os.getenv("STARTUNNEL_LIVE_EVIDENCE_PATH"):
        _write_evidence(evidence_path, report)
    print("\nPublic API evidence is complete. Agent prose was not used as proof.")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    asyncio.run(run())
