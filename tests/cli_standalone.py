"""Run standalone client smoke tests with Python 3.10 or later and its standard library."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


class StandaloneClientTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="startunnel-standalone-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = self.directory / "star_tunnel.py"
        shutil.copyfile(ROOT / "cli" / "star_tunnel.py", self.client)
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("STARTUNNEL_")
        }
        self.environment["NO_PROXY"] = "127.0.0.1,localhost"
        self.requests: list[dict[str, Any]] = []
        self.download = self.client.read_bytes()
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def handle_request(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body: Any = json.loads(raw) if raw else None
                test.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": self.headers,
                        "body": body,
                    }
                )
                status = 200
                if self.path == "/downloads/star_tunnel.py":
                    encoded = test.download
                else:
                    value: dict[str, Any]
                    if self.path == "/api/v1/messages":
                        value = {"content": body["content"], "name": "代理"}
                        status = 201
                    elif self.path == "/api/v1/admin/me":
                        value = {"admin": {"id": "test-admin"}}
                    elif self.path == "/api/v1/admin/agents/create":
                        value = {"resource": {"id": "test-agent"}, "secret": "st_issued-test-key"}
                        status = 201
                    else:
                        value = {"error": {"code": "invalid_request"}}
                        status = 404
                    encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close_server() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(close_server)
        self.base_url = f"http://127.0.0.1:{server.server_port}"

    def run_client(
        self,
        *arguments: str,
        body: dict[str, Any] | None = None,
        environment: dict[str, str] | None = None,
        installed: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        command = (
            [str(installed)] if installed is not None else [sys.executable, "-I", str(self.client)]
        )
        return subprocess.run(
            [*command, *arguments],
            cwd=self.directory,
            env={**self.environment, **(environment or {})},
            input=json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None
            else None,
            capture_output=True,
            timeout=15,
            check=False,
        )

    def successful_json(self, result: subprocess.CompletedProcess[bytes]) -> dict[str, Any]:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        return cast(dict[str, Any], json.loads(result.stdout.decode("ascii")))

    def test_offline_version_schema_and_help(self) -> None:
        version = self.successful_json(self.run_client("version"))
        self.assertEqual(version["version"], "unreleased")
        self.assertIsNone(version["source_revision"])
        self.assertEqual(version["command_path"], str(self.client.resolve()))
        self.assertEqual(version["sha256"], hashlib.sha256(self.client.read_bytes()).hexdigest())
        schema = self.successful_json(self.run_client("schema"))
        names = {command["command"] for command in schema["commands"]}
        self.assertTrue({"me", "read-tree", "start", "rollover", "beacon-board"} <= names)
        filtered = self.successful_json(self.run_client("schema", "read-tree"))
        self.assertEqual(
            filtered["commands"], [c for c in schema["commands"] if c["command"] == "read-tree"]
        )
        self.assertEqual(filtered["client"]["version"], version["version"])
        self.assertEqual(filtered["contract_source"], "bundled")
        self.assertFalse(filtered["remote_server_verified"])
        self.assertEqual(
            filtered["commands"][0]["stdout_schemas"]["200"],
            {"$ref": "#/components/schemas/TreeResponse"},
        )
        tree = filtered["components"]["schemas"]["TreeResponse"]
        self.assertIn("next_page_cursor", tree["required"])
        self.assertIn({"type": "null"}, tree["properties"]["next_page_cursor"]["anyOf"])
        admin = self.successful_json(self.run_client("admin", "schema", "agents", "create"))
        output = admin["commands"][0]["stdout_schemas"]["200"]
        self.assertIn("secret_output", output["required"])
        self.assertNotIn("secret", output["properties"])
        help_result = self.run_client("--help")
        self.assertEqual(help_result.returncode, 0)
        self.assertEqual(help_result.stderr, b"")
        self.assertIn(b"--help", help_result.stdout)
        self.assertEqual(self.requests, [])

    def test_offline_config_reports_agent_and_admin_sources(self) -> None:
        agent_file = self.directory / "agent.key"
        admin_file = self.directory / "admin.key"
        agent_file.write_text("st_standalone-agent-key\n", encoding="utf-8")
        admin_file.write_text("sta_standalone-admin-key\n", encoding="utf-8")
        environment = {
            "STARTUNNEL_BASE_URL": self.base_url + "/",
            "STARTUNNEL_AGENT_KEY_FILE": str(agent_file),
            "STARTUNNEL_ADMIN_KEY_FILE": str(admin_file),
        }
        agent = self.successful_json(self.run_client("config", environment=environment))
        admin = self.successful_json(self.run_client("admin", "config", environment=environment))
        self.assertEqual(agent["base_url"], self.base_url)
        self.assertEqual(agent["intended_role"], "agent")
        self.assertEqual(agent["credential_source"], "STARTUNNEL_AGENT_KEY_FILE")
        self.assertEqual(agent["credential_file"], str(agent_file.resolve()))
        self.assertEqual(admin["intended_role"], "admin")
        self.assertEqual(admin["credential_source"], "STARTUNNEL_ADMIN_KEY_FILE")
        self.assertEqual(admin["credential_file"], str(admin_file.resolve()))
        self.assertFalse(agent["authentication_verified"])
        self.assertFalse(admin["authentication_verified"])
        self.assertNotIn(b"standalone-agent-key", json.dumps(agent).encode())
        self.assertNotIn(b"standalone-admin-key", json.dumps(admin).encode())
        self.assertEqual(self.requests, [])

    def test_agent_unicode_request_and_ascii_json_output(self) -> None:
        content = {"type": "text", "text": "Review café with 代理 and 🔭."}
        result = self.run_client(
            "reply",
            "--idempotency-key",
            "standalone-reply-001",
            body={"address": "synthetic-address", "parent_id": "test-parent", "content": content},
            environment={
                "STARTUNNEL_BASE_URL": self.base_url,
                "STARTUNNEL_AGENT_KEY": "st_agent-test-key",
            },
        )
        self.assertEqual(self.successful_json(result), {"content": content, "name": "代理"})
        self.assertEqual(self.requests[0]["body"]["content"], content)
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer st_agent-test-key")
        self.assertEqual(self.requests[0]["headers"]["Idempotency-Key"], "standalone-reply-001")

    def test_admin_key_file_and_private_key_output(self) -> None:
        key_file = self.directory / "admin.key"
        key_file.write_text("sta_admin-test-key\n", encoding="utf-8")
        environment = {
            "STARTUNNEL_BASE_URL": self.base_url,
            "STARTUNNEL_ADMIN_KEY_FILE": str(key_file),
        }
        identity = self.successful_json(self.run_client("admin", "me", environment=environment))
        self.assertEqual(identity, {"admin": {"id": "test-admin"}})
        self.assertEqual(self.requests[0]["headers"]["Authorization"], "Bearer sta_admin-test-key")
        output = self.directory / "agent.key"
        arguments = (
            "admin",
            "agents",
            "create",
            "--idempotency-key",
            "standalone-agent-001",
            "--secret-output",
            str(output),
        )
        created = self.run_client(*arguments, body={"name": "smoke-agent"}, environment=environment)
        self.assertEqual(self.successful_json(created)["resource"]["id"], "test-agent")
        self.assertNotIn(b"st_issued-test-key", created.stdout)
        self.assertEqual(output.read_text(encoding="utf-8"), "st_issued-test-key\n")
        if os.name == "posix":
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        refused = self.run_client(*arguments, body={"name": "smoke-agent"}, environment=environment)
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(refused.stdout, b"")
        self.assertEqual(json.loads(refused.stderr)["error"]["code"], "secret_output_failed")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "st_issued-test-key\n")

    def test_structured_argument_errors_and_invalid_integer(self) -> None:
        for arguments in (
            (),
            ("reply",),
            ("admin", "accounts"),
            ("me", "st_private-stray-value"),
            ("st_private-stray-value",),
            ("admin", "keys", "list", "--limit", "not-an-integer"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_client(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                error = json.loads(result.stderr)["error"]
                self.assertEqual(error["code"], "usage_error")
                self.assertIn("startunnel", error["usage"])
                self.assertNotIn(b"st_private-stray-value", result.stderr)
                self.assertNotIn(b"not-an-integer", result.stderr)
        self.assertEqual(self.requests, [])

    def test_closed_stdout_pipe_is_quiet(self) -> None:
        for argument in ("version", "schema", "--help"):
            with self.subTest(argument=argument):
                read_descriptor, write_descriptor = os.pipe()
                os.close(read_descriptor)
                try:
                    result = subprocess.run(
                        [sys.executable, "-I", str(self.client), argument],
                        cwd=self.directory,
                        env=self.environment,
                        stdout=write_descriptor,
                        stderr=subprocess.PIPE,
                        timeout=15,
                        check=False,
                    )
                finally:
                    os.close(write_descriptor)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, b"")

    @unittest.skipIf(os.name == "nt", "Windows uses handles instead of file descriptors.")
    def test_missing_stdout_fails_before_running_a_command(self) -> None:
        launcher = (
            "import os, sys; os.close(1); "
            "os.execv(sys.executable, [sys.executable, '-I', *sys.argv[1:]])"
        )
        for arguments in (("version",), ("--help",), ("me",)):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, "-I", "-c", launcher, str(self.client), *arguments],
                    cwd=self.directory,
                    env={
                        **self.environment,
                        "STARTUNNEL_BASE_URL": self.base_url,
                        "STARTUNNEL_AGENT_KEY": "st_agent-test-key",
                    },
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, b"")
        self.assertEqual(self.requests, [])

    def test_invalid_json_integer_is_a_structured_request_error(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", "-X", "int_max_str_digits=4300", str(self.client), "read-tree"],
            cwd=self.directory,
            env={
                **self.environment,
                "STARTUNNEL_BASE_URL": self.base_url,
                "STARTUNNEL_AGENT_KEY": "st_agent-test-key",
            },
            input=b'{"value":' + b"1" * 5000 + b"}",
            capture_output=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr)["error"]["code"], "invalid_input")
        self.assertEqual(self.requests, [])

    def test_install_and_update(self) -> None:
        if os.name != "posix":
            for arguments, code in (
                (("install", "--bin-dir", str(self.directory / "bin")), "installation_failed"),
                (("update",), "update_failed"),
            ):
                result = self.run_client(
                    *arguments, environment={"STARTUNNEL_BASE_URL": self.base_url}
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(json.loads(result.stderr)["error"]["code"], code)
            self.assertEqual(self.requests, [])
            return
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cli-smoke-", dir=artifacts) as directory:
            installed = self.successful_json(self.run_client("install", "--bin-dir", directory))
            command = Path(installed["command_path"])
            before = self.successful_json(self.run_client("version", installed=command))
            self.download += b"\n# Standalone update proof.\n"
            updated = self.successful_json(
                self.run_client(
                    "update",
                    installed=command,
                    environment={
                        "STARTUNNEL_BASE_URL": self.base_url,
                        "STARTUNNEL_AGENT_KEY": "st_unused-test-key",
                    },
                )
            )
            self.assertEqual(updated["source_instance"], self.base_url)
            after = self.successful_json(self.run_client("version", installed=command))
            self.assertEqual(command.read_bytes(), self.download)
            self.assertEqual(after["sha256"], hashlib.sha256(self.download).hexdigest())
            self.assertEqual(after["command_path"], str(command.resolve()))
            self.assertNotEqual(before["sha256"], after["sha256"])
            self.assertEqual(self.requests[0]["path"], "/downloads/star_tunnel.py")
            self.assertIsNone(self.requests[0]["headers"].get("Authorization"))

    def test_relative_and_symlink_invocations_report_and_update_the_resolved_file(self) -> None:
        artifacts = ROOT / "artifacts"
        artifacts.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="cli-paths-", dir=artifacts)
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        command = directory / "relative-client.py"
        shutil.copyfile(self.client, command)
        command.chmod(0o755)
        result = subprocess.run(
            [sys.executable, "relative-client.py", "version"],
            cwd=directory,
            env=self.environment,
            capture_output=True,
            timeout=15,
            check=False,
        )
        version = self.successful_json(result)
        self.assertEqual(version["command_path"], str(command.resolve()))
        self.assertEqual(version["sha256"], hashlib.sha256(command.read_bytes()).hexdigest())
        second_command = directory / "second-client.py"
        shutil.copyfile(self.client, second_command)
        second_bytes = second_command.read_bytes()
        second_result = subprocess.run(
            [sys.executable, "-I", str(second_command), "version"],
            cwd=directory,
            env=self.environment,
            capture_output=True,
            timeout=15,
            check=False,
        )
        second = self.successful_json(second_result)
        self.assertEqual(second["command_path"], str(second_command.resolve()))
        self.assertNotEqual(version["command_path"], second["command_path"])

        if os.name != "posix":
            return
        alias = directory / "client-link"
        alias.symlink_to(command)
        before = self.successful_json(self.run_client("version", installed=alias))
        self.assertEqual(before["command_path"], str(command.resolve()))
        self.download += b"\n# Update through a symlink.\n"
        updated = self.successful_json(
            self.run_client(
                "update",
                installed=alias,
                environment={"STARTUNNEL_BASE_URL": self.base_url},
            )
        )
        self.assertEqual(updated["command_path"], str(command.resolve()))
        self.assertEqual(command.read_bytes(), self.download)
        self.assertEqual(second_command.read_bytes(), second_bytes)
        self.assertTrue(alias.is_symlink())
        after = self.successful_json(self.run_client("version", installed=alias))
        self.assertEqual(after["command_path"], str(command.resolve()))
        self.assertEqual(after["sha256"], hashlib.sha256(command.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main(verbosity=2)
