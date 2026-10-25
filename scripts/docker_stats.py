"""Read counters and fixed observation commands through the local Docker Engine."""

from __future__ import annotations

import http.client
import json
import re
import socket
import struct
import time
from typing import Any
from urllib.parse import urlsplit

MAX_RESPONSE_BYTES = 1_048_576


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def demultiplex_stdout(raw: bytes) -> str:
    """Read only complete stdout frames; discard stderr and reject damaged streams."""
    output = bytearray()
    offset = 0
    while offset < len(raw):
        if len(raw) - offset < 8:
            raise ValueError("Docker returned an incomplete observation stream.")
        stream, padding, size = struct.unpack(">B3sI", raw[offset : offset + 8])
        offset += 8
        if stream not in (1, 2) or padding != b"\0\0\0" or size > len(raw) - offset:
            raise ValueError("Docker returned an invalid observation stream.")
        if stream == 1:
            output.extend(raw[offset : offset + size])
        offset += size
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("Docker returned invalid observation text.") from error


class DockerStats:
    """Use the already validated local Docker endpoint with bounded responses."""

    def __init__(self, endpoint: str, *, timeout: float = 30) -> None:
        self.endpoint = urlsplit(endpoint)
        self.timeout = timeout
        if self.endpoint.scheme not in {"unix", "tcp"}:
            raise ValueError(
                "Resource measurement requires a local Unix socket or TCP Docker endpoint."
            )

    def _request(
        self, path: str, *, payload: dict[str, object] | None = None, timeout: float | None = None
    ) -> bytes:
        limit = self.timeout if timeout is None else timeout
        if limit <= 0:
            raise ValueError("The Docker observation deadline expired.")
        connection: http.client.HTTPConnection
        if self.endpoint.scheme == "unix":
            connection = (
                UnixConnection(self.endpoint.path)
                if limit == 30
                else UnixConnection(self.endpoint.path, limit)
            )
        else:
            connection = http.client.HTTPConnection(
                self.endpoint.hostname or "localhost", self.endpoint.port, timeout=limit
            )
        try:
            if payload is None:
                connection.request("GET", path)
            else:
                connection.request(
                    "POST", path, json.dumps(payload).encode(), {"Content-Type": "application/json"}
                )
            response = connection.getresponse()
            if response.status not in (200, 201):
                raise ValueError("Docker observation request failed.")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Docker observation response exceeded its size limit.")
            return raw
        except (OSError, http.client.HTTPException) as error:
            raise ValueError("Could not read Docker resource counters.") from error
        finally:
            connection.close()

    def get(self, path: str) -> dict[str, Any]:
        data = json.loads(self._request(path))
        if not isinstance(data, dict):
            raise ValueError("Docker returned invalid resource data.")
        return data

    def get_list(self, path: str) -> list[dict[str, Any]]:
        data = json.loads(self._request(path))
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise ValueError("Docker returned invalid container inventory.")
        return data

    def exec(
        self, identifier: str, command: tuple[str, ...], *, environment: tuple[str, ...] = ()
    ) -> str:
        """Run a fixed observation command without a shell and return stdout only."""
        if not re.fullmatch(r"[a-f0-9]{12,64}", identifier):
            raise ValueError("Invalid observation container identifier.")
        deadline = time.monotonic() + self.timeout
        created = json.loads(
            self._request(
                f"/containers/{identifier}/exec",
                payload={
                    "AttachStdout": True,
                    "AttachStderr": True,
                    "Tty": False,
                    "Cmd": list(command),
                    "Env": list(environment),
                },
                timeout=deadline - time.monotonic(),
            )
        )
        exec_id = created.get("Id") if isinstance(created, dict) else None
        if not isinstance(exec_id, str) or not re.fullmatch(r"[a-f0-9]{12,64}", exec_id):
            raise ValueError("Docker returned an invalid observation command identifier.")
        raw = self._request(
            f"/exec/{exec_id}/start",
            payload={"Detach": False, "Tty": False},
            timeout=deadline - time.monotonic(),
        )
        state = json.loads(
            self._request(f"/exec/{exec_id}/json", timeout=deadline - time.monotonic())
        )
        if not isinstance(state, dict) or state.get("Running") or state.get("ExitCode") != 0:
            raise ValueError("The Docker observation command failed.")
        return demultiplex_stdout(raw)
