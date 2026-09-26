"""Local liveness state for the maintenance worker."""

from __future__ import annotations

from pathlib import Path

# This marker has no secret data and lives on the worker's private container tmpfs.
MAINTENANCE_HEARTBEAT_PATH = Path("/tmp/startunnel-maintenance-heartbeat")  # nosec B108


def publish_local_maintenance_heartbeat(
    path: Path = MAINTENANCE_HEARTBEAT_PATH,
) -> None:
    """Create or refresh the worker heartbeat on the container tmpfs."""

    path.touch(exist_ok=True)
