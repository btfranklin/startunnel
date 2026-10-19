"""Read raw container counters without starting processes in the containers."""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any
from urllib.parse import urlsplit


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=30)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerStats:
    """Use the already validated local Docker endpoint."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = urlsplit(endpoint)
        if self.endpoint.scheme not in {"unix", "tcp"}:
            raise ValueError(
                "Resource measurement requires a local Unix socket or TCP Docker endpoint."
            )

    def get(self, path: str) -> dict[str, Any]:
        connection = (
            UnixConnection(self.endpoint.path)
            if self.endpoint.scheme == "unix"
            else http.client.HTTPConnection(
                self.endpoint.hostname or "localhost", self.endpoint.port, timeout=30
            )
        )
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"Docker resource request returned HTTP {response.status}.")
            data = json.load(response)
            if not isinstance(data, dict):
                raise ValueError("Docker returned invalid resource data.")
            return data
        except (OSError, http.client.HTTPException) as error:
            raise ValueError("Could not read Docker resource counters.") from error
        finally:
            connection.close()
