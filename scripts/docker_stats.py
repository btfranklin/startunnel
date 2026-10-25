"""Read raw container counters without starting processes in the containers."""

from __future__ import annotations

import http.client
import json
import socket
from typing import Any
from urllib.parse import urlsplit


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 30) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DockerStats:
    """Use the already validated local Docker endpoint."""

    def __init__(self, endpoint: str, *, timeout: float = 30) -> None:
        self.endpoint = urlsplit(endpoint)
        self.timeout = timeout
        if self.endpoint.scheme not in {"unix", "tcp"}:
            raise ValueError(
                "Resource measurement requires a local Unix socket or TCP Docker endpoint."
            )

    def get(self, path: str) -> dict[str, Any]:
        connection: http.client.HTTPConnection
        if self.endpoint.scheme == "unix":
            connection = (
                UnixConnection(self.endpoint.path)
                if self.timeout == 30
                else UnixConnection(self.endpoint.path, self.timeout)
            )
        else:
            connection = http.client.HTTPConnection(
                self.endpoint.hostname or "localhost", self.endpoint.port, timeout=self.timeout
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
