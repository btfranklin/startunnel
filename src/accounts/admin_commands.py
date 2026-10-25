"""Typed administrator commands with explicit domain operations."""

from __future__ import annotations

import base64
import hmac
from abc import abstractmethod
from datetime import datetime
from typing import ClassVar
from uuid import UUID

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone
from pydantic import BaseModel, ConfigDict, Field

from agents.models import AgentCredential
from agents.services import create_credential, revoke_credential
from core.errors import InvalidRequest
from core.limits import MAX_SESSION_CHARACTERS, MAX_TUNNEL_RETIREMENTS
from tunnels.admin_actions import (
    TunnelRetirement,
    close_cycle_as_admin,
    retire_tunnel_as_admin,
    retire_tunnels_for_revoked_credential_as_admin,
    rollover_cycle_as_admin,
    rotate_address_as_admin,
    start_cycle_as_admin,
)
from tunnels.beacons import withdraw_beacon_as_admin
from tunnels.models import Tunnel
from tunnels.payloads import MessageContent

from .admin_access import change_admin_state, issue_admin_key
from .admin_data import (
    AdminResourceData,
    admin_resource,
    agent_resource,
    key_resource,
    tunnel_resource,
)
from .models import AdminCredential, AdminReceipt, User


def derive_secret(receipt: AdminReceipt, kind: str) -> str:
    raw = hmac.digest(
        settings.IDEMPOTENCY_SECRET.encode(),
        b"admin-secret:" + kind.encode() + receipt.id.bytes + bytes(receipt.derivation_nonce),
        "sha256",
    )
    return ("sta_" if kind == "admin_key" else "st_") + base64.urlsafe_b64encode(
        raw
    ).decode().rstrip("=")


def _future_expiry(value: datetime | None) -> datetime | None:
    if value is not None and (timezone.is_naive(value) or value <= timezone.now()):
        raise InvalidRequest("Supply a future expiry with a time zone.")
    return value


class AdminCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: ClassVar[str]
    target_type: ClassVar[str]

    @property
    def target_id(self) -> UUID | None:
        return None

    @abstractmethod
    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        raise NotImplementedError


class AccountCreate(AdminCommand):
    operation = "accounts.create"
    target_type = "user"
    username: str
    password: str | None = None
    key_name: str = "initial"

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        username = User.normalize_username(self.username).strip()
        account = User(username=username)
        try:
            account.full_clean(exclude={"password"})
            if self.password:
                validate_password(self.password, user=account)
        except ValidationError as error:
            raise InvalidRequest(" ".join(error.messages)) from error
        account.set_password(self.password or None)
        account.save()
        resource = admin_resource(account)
        if not self.password:
            receipt.secret_kind = "admin_key"
            created, _ = issue_admin_key(
                owner=account,
                name=self.key_name,
                key=derive_secret(receipt, "admin_key"),
            )
            receipt.secret_resource_id = created.id
            resource["initial_key"] = key_resource(created)
        return resource, "user", account.id


class AccountCommand(AdminCommand):
    target_type = "user"
    admin_id: UUID

    @property
    def target_id(self) -> UUID:
        return self.admin_id


class AccountState(AccountCommand):
    operation = "accounts.state"
    active: bool
    expected_active: bool

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        account = change_admin_state(
            actor=actor,
            admin_id=self.admin_id,
            active=self.active,
            expected_active=self.expected_active,
        )
        return admin_resource(account), "user", account.id


class AccountPassword(AccountCommand):
    operation = "accounts.password"
    password: str | None = None

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        account = User.objects.get(pk=self.admin_id)
        if self.password:
            try:
                validate_password(self.password, user=account)
            except ValidationError as error:
                raise InvalidRequest(" ".join(error.messages)) from error
        account.set_password(self.password or None)
        account.save(update_fields=["password"])
        return admin_resource(account), "user", account.id


class KeyCreate(AccountCommand):
    operation = "keys.create"
    name: str
    expires_at: datetime | None = None

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        account = User.objects.get(pk=self.admin_id)
        receipt.secret_kind = "admin_key"
        created, _ = issue_admin_key(
            owner=account,
            name=self.name,
            expires_at=_future_expiry(self.expires_at),
            key=derive_secret(receipt, "admin_key"),
        )
        receipt.secret_resource_id = created.id
        return key_resource(created), "admin_credential", created.id


class KeyRevoke(AdminCommand):
    operation = "keys.revoke"
    target_type = "admin_credential"
    key_id: UUID

    @property
    def target_id(self) -> UUID:
        return self.key_id

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        created = AdminCredential.objects.get(pk=self.key_id)
        created.revoked_at = created.revoked_at or timezone.now()
        created.save(update_fields=["revoked_at"])
        return key_resource(created), "admin_credential", created.id


class AgentCreate(AdminCommand):
    operation = "agents.create"
    target_type = "agent_credential"
    name: str
    expires_at: datetime | None = None

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        receipt.secret_kind = "agent_key"
        issued = create_credential(
            actor=actor,
            name=self.name,
            expires_at=_future_expiry(self.expires_at),
            key=derive_secret(receipt, "agent_key"),
        )
        receipt.secret_resource_id = issued.credential.id
        return agent_resource(issued.credential), "agent_credential", issued.credential.id


class AgentRevoke(AdminCommand):
    operation = "agents.revoke"
    target_type = "agent_credential"
    credential_id: UUID

    @property
    def target_id(self) -> UUID:
        return self.credential_id

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        agent = AgentCredential.objects.get(pk=self.credential_id)
        revoke_credential(actor=actor, credential=agent)
        agent.refresh_from_db()
        return agent_resource(agent), "agent_credential", agent.id


class BeaconWithdraw(AdminCommand):
    operation = "beacons.withdraw"
    target_type = "beacon"
    beacon_id: UUID

    @property
    def target_id(self) -> UUID:
        return self.beacon_id

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        resource = withdraw_beacon_as_admin(actor=actor, beacon_id=self.beacon_id)
        return resource, "beacon", self.beacon_id


class TunnelCommand(AdminCommand):
    target_type = "tunnel"
    tunnel_id: UUID

    @property
    def target_id(self) -> UUID:
        return self.tunnel_id

    def resource(self) -> tuple[AdminResourceData, str, UUID]:
        return tunnel_resource(Tunnel.objects.get(pk=self.tunnel_id)), "tunnel", self.tunnel_id


class TunnelStart(TunnelCommand):
    operation = "tunnels.start"
    expected_address_generation: int
    root_content: MessageContent
    root_session: str | None = Field(default=None, min_length=1, max_length=MAX_SESSION_CHARACTERS)
    cycle_label: str = ""
    expires_in_seconds: int | None = None

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        start_cycle_as_admin(
            actor=actor,
            tunnel_id=self.tunnel_id,
            expected_address_generation=self.expected_address_generation,
            root_content=self.root_content.model_dump(mode="json"),
            root_session=self.root_session,
            cycle_label=self.cycle_label,
            expires_in_seconds=self.expires_in_seconds,
        )
        return self.resource()


class TunnelRollover(TunnelStart):
    operation = "tunnels.rollover"
    expected_cycle_id: UUID

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        rollover_cycle_as_admin(
            actor=actor,
            tunnel_id=self.tunnel_id,
            expected_cycle_id=self.expected_cycle_id,
            expected_address_generation=self.expected_address_generation,
            root_content=self.root_content.model_dump(mode="json"),
            root_session=self.root_session,
            cycle_label=self.cycle_label,
            expires_in_seconds=self.expires_in_seconds,
        )
        return self.resource()


class TunnelClose(TunnelCommand):
    operation = "tunnels.close"
    expected_cycle_id: UUID

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        close_cycle_as_admin(
            actor=actor, tunnel_id=self.tunnel_id, expected_cycle_id=self.expected_cycle_id
        )
        return self.resource()


class TunnelRotate(TunnelCommand):
    operation = "tunnels.rotate"
    expected_address_generation: int

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        rotate_address_as_admin(
            actor=actor,
            tunnel_id=self.tunnel_id,
            expected_address_generation=self.expected_address_generation,
        )
        return self.resource()


class TunnelRetire(TunnelRotate):
    operation = "tunnels.retire"
    confirmation: str

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        retire_tunnel_as_admin(
            actor=actor,
            tunnel_id=self.tunnel_id,
            confirmation=self.confirmation,
            expected_address_generation=self.expected_address_generation,
        )
        return self.resource()


class TunnelsRetireForCredential(AdminCommand):
    operation = "tunnels.retire-for-credential"
    target_type = "agent_credential"
    credential_id: UUID
    tunnels: list[TunnelRetire] = Field(min_length=1, max_length=MAX_TUNNEL_RETIREMENTS)

    @property
    def target_id(self) -> UUID:
        return self.credential_id

    def perform(self, actor: User, receipt: AdminReceipt) -> tuple[AdminResourceData, str, UUID]:
        retired = retire_tunnels_for_revoked_credential_as_admin(
            actor=actor,
            credential_id=self.credential_id,
            selections=[
                TunnelRetirement(
                    tunnel_id=item.tunnel_id,
                    expected_address_generation=item.expected_address_generation,
                    confirmation=item.confirmation,
                )
                for item in self.tunnels
            ],
        )
        return (
            {
                "credential_id": str(self.credential_id),
                "retired_tunnel_ids": [str(item) for item in retired],
            },
            self.target_type,
            self.credential_id,
        )


ADMIN_OPERATIONS = (
    AccountCreate.operation,
    AccountState.operation,
    AccountPassword.operation,
    KeyCreate.operation,
    KeyRevoke.operation,
    AgentCreate.operation,
    AgentRevoke.operation,
    TunnelStart.operation,
    TunnelClose.operation,
    TunnelRollover.operation,
    TunnelRotate.operation,
    TunnelRetire.operation,
    TunnelsRetireForCredential.operation,
    BeaconWithdraw.operation,
)
