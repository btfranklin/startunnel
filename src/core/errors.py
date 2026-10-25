"""Transport-neutral domain errors."""

from __future__ import annotations

from typing import Literal

PublicErrorCode = Literal[
    "invalid_request",
    "invalid_cursor",
    "invalid_credential",
    "invalid_address",
    "tunnel_unavailable",
    "cycle_unavailable",
    "payload_too_large",
    "quota_exceeded",
    "idempotency_conflict",
    "cycle_closed",
    "lifecycle_conflict",
    "checkpoint_regression",
    "context_budget_too_small",
    "entry_budget_too_small",
    "rate_limited",
    "dependency_unavailable",
    "internal_error",
]


class DomainError(Exception):
    code: PublicErrorCode = "invalid_request"
    status = 400
    message = "The request is not valid. Correct it and try again."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        self.safe_message = message or self.message


class InvalidRequest(DomainError):
    pass


class ResourceUnavailable(InvalidRequest):
    status = 404


class InvalidCursor(InvalidRequest):
    code = "invalid_cursor"
    message = "The cursor is not valid. Read the current state and try again."


class InvalidCredential(DomainError):
    code = "invalid_credential"
    status = 401
    message = "The agent credential is not valid."


class TunnelUnavailable(DomainError):
    code = "tunnel_unavailable"
    status = 404
    message = "This tunnel is not available. Check the address or create a new tunnel."


class PayloadTooLarge(DomainError):
    code = "payload_too_large"
    status = 413
    message = "The message content is larger than 65,536 bytes."


class QuotaExceeded(DomainError):
    code = "quota_exceeded"
    status = 429
    message = "This operation exceeds an instance limit."


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"
    status = 409
    message = "This idempotency key was already used for a different request."


class CycleClosed(DomainError):
    code = "cycle_closed"
    status = 409
    message = "This tunnel has no writable cycle. Start a new cycle and try again."


class CycleUnavailable(DomainError):
    code = "cycle_unavailable"
    status = 404
    message = "This cycle is not available through the public API."


class LifecycleConflict(DomainError):
    code = "lifecycle_conflict"
    status = 409
    message = "The tunnel lifecycle changed. Read the current status and try again."


class CheckpointRegression(DomainError):
    code = "checkpoint_regression"
    status = 409
    message = "A checkpoint cannot move backward. Use the saved or a later cursor."


class ContextBudgetTooSmall(DomainError):
    code = "context_budget_too_small"
    status = 413
    message = "The context budget is too small for the required branch."


class EntryBudgetTooSmall(DomainError):
    code = "entry_budget_too_small"
    status = 413
    message = "The entry byte budget is too small for the required response."


class RateLimited(DomainError):
    retry_after: int | None = None
    code = "rate_limited"
    status = 429
    message = "The request rate is too high. Wait and try again."


class DependencyUnavailable(DomainError):
    code = "dependency_unavailable"
    status = 503
    message = "A required service is not available. Try again soon."
