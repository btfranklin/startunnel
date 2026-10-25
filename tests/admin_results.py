"""Validate the resource type selected by an administrator test command."""

from collections.abc import Mapping
from typing import NotRequired, TypedDict, overload
from uuid import uuid4

from pydantic import TypeAdapter

from accounts.admin_commands import (
    AccountCreate,
    AccountPassword,
    AccountState,
    AdminCommand,
    AgentCreate,
    AgentRevoke,
    KeyCreate,
    KeyRevoke,
    TunnelClose,
    TunnelRetire,
    TunnelRollover,
    TunnelRotate,
    TunnelStart,
)
from accounts.admin_data import (
    AccountResourceData,
    AgentResourceData,
    KeyResourceData,
    OperationReference,
    TunnelResourceData,
)
from accounts.admin_services import execute_admin_operation
from accounts.models import User


class OperationResult[T](TypedDict):
    resource: T
    operation: OperationReference
    secret: NotRequired[str]


@overload
def run(
    actor: User,
    command_type: type[AccountCreate | AccountPassword | AccountState],
    data: Mapping[str, object],
    key: str | None = None,
) -> OperationResult[AccountResourceData]: ...


@overload
def run(
    actor: User,
    command_type: type[KeyCreate | KeyRevoke],
    data: Mapping[str, object],
    key: str | None = None,
) -> OperationResult[KeyResourceData]: ...


@overload
def run(
    actor: User,
    command_type: type[AgentCreate | AgentRevoke],
    data: Mapping[str, object],
    key: str | None = None,
) -> OperationResult[AgentResourceData]: ...


@overload
def run(
    actor: User,
    command_type: type[TunnelStart | TunnelRollover | TunnelClose | TunnelRotate | TunnelRetire],
    data: Mapping[str, object],
    key: str | None = None,
) -> OperationResult[TunnelResourceData]: ...


@overload
def run(
    actor: User,
    command_type: type[AdminCommand],
    data: Mapping[str, object],
    key: str | None = None,
) -> Mapping[str, object]: ...


def run(
    actor: User,
    command_type: type[AdminCommand],
    data: Mapping[str, object],
    key: str | None = None,
) -> Mapping[str, object]:
    result = execute_admin_operation(
        actor=actor,
        command=command_type.model_validate(data),
        key=key or str(uuid4()),
        request_data=dict(data),
    )
    if issubclass(command_type, (AccountCreate, AccountPassword, AccountState)):
        return TypeAdapter(OperationResult[AccountResourceData]).validate_python(
            result, strict=True
        )
    if issubclass(command_type, (KeyCreate, KeyRevoke)):
        return TypeAdapter(OperationResult[KeyResourceData]).validate_python(result, strict=True)
    if issubclass(command_type, (AgentCreate, AgentRevoke)):
        return TypeAdapter(OperationResult[AgentResourceData]).validate_python(result, strict=True)
    if issubclass(command_type, (TunnelStart, TunnelClose, TunnelRotate)):
        return TypeAdapter(OperationResult[TunnelResourceData]).validate_python(result, strict=True)
    return result
