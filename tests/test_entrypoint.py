"""The process entry listens, from an empty database through one evaluation."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"


class EntrypointTests(unittest.TestCase):
    def test_from_an_empty_database_to_one_evaluation(self) -> None:
        """Bootstrap, listen, create a flag, and evaluate it to true.

        The child is python -m main. GET /health needs no token. The
        decision uses fallthrough variation 1. The child is stopped after
        the response.
        """
        with tempfile.TemporaryDirectory() as temporary:
            port = _free_port()
            environment = _environment(temporary, port)
            bootstrap = subprocess.run(
                [sys.executable, "-m", "main", "bootstrap"],
                cwd=temporary,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            token = bootstrap.stdout.strip()
            self.assertNotEqual(token, "")
            self.assertNotIn(token, bootstrap.stderr)
            process = subprocess.Popen(
                [sys.executable, "-m", "main"],
                cwd=temporary,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            base = "http://127.0.0.1:" + str(port)
            try:
                self.assertEqual(_wait_for_health(process, base), 200)
                self._create_and_evaluate(base, token)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()

    def _create_and_evaluate(self, base: str, token: str) -> None:
        """Create shop, prod, and checkout-v2, then assert the decision.

        Fallthrough variation 1 is true. default is false and is not used.
        Does not start a process.
        """
        _request(
            "POST",
            base + "/v1/projects",
            token,
            {"key": "shop", "name": "Shop"},
        )
        _request(
            "POST",
            base + "/v1/projects/shop/environments",
            token,
            {"key": "prod", "name": "Prod"},
        )
        _request(
            "POST",
            base + "/v1/projects/shop/flags",
            token,
            {
                "key": "checkout-v2",
                "name": "Checkout",
                "description": "",
                "variations": [False, True],
            },
        )
        stored = _request(
            "PUT",
            base + "/v1/projects/shop/flags/checkout-v2/environments/prod",
            token,
            {
                "enabled": True,
                "off_variation": 0,
                "fallthrough": {"variation": 1},
                "rules": [],
                "targets": {},
                "prerequisites": [],
                "version": 1,
            },
        )
        decision = _request(
            "POST",
            base + "/v1/evaluate",
            token,
            {
                "project_key": "shop",
                "environment_key": "prod",
                "flag_key": "checkout-v2",
                "context": {"subject": "user-1", "attributes": {}},
                "default": False,
            },
        )
        self.assertEqual(stored["version"], 2)
        self.assertIs(decision["value"], True)
        self.assertEqual(decision["variation_index"], 1)
        self.assertEqual(decision["reason"], "fallthrough")
        self.assertIsNone(decision["rule_id"])
        self.assertEqual(decision["version"], 2)


def _environment(temporary: str, port: int) -> dict[str, str]:
    """Return the environment for a child process.

    The database file is inside temporary. The port is the one this test
    just released. Does not start a process.
    """
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_SRC)
    environment["FLAGGATOR_ENGINE"] = "sqlite"
    environment["FLAGGATOR_SQLITE_PATH"] = str(Path(temporary) / "flaggator.db")
    environment["FLAGGATOR_CACHE"] = "memory"
    environment["FLAGGATOR_BIND_HOST"] = "127.0.0.1"
    environment["FLAGGATOR_BIND_PORT"] = str(port)
    return environment


def _free_port() -> int:
    """Return a TCP port that was free at the moment it was closed.

    Another process can take it before the child binds. The test then
    fails on startup instead of sharing a port. Does not listen.
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _wait_for_health(process: subprocess.Popen[str], base: str) -> int:
    """Return the /health status once the child answers.

    Raises AssertionError when the child exits or five seconds pass.
    Does not send a credential.
    """
    deadline = time.monotonic() + 5
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            _stderr = ""
            if process.stderr is not None:
                _stderr = process.stderr.read()
            raise AssertionError(_stderr)
        try:
            with urllib.request.urlopen(base + "/health", timeout=0.5) as response:
                return int(response.status)
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            time.sleep(0.05)
    raise AssertionError(last_error)


def _request(method: str, url: str, token: str, body: dict) -> dict:
    """Send one JSON request and return the JSON object.

    Raises AssertionError when the status is not 200 or 201. Does not
    follow a redirect.
    """
    payload = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            status = int(response.status)
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8")
        raise AssertionError(str(error.status) + " " + detail) from error
    if status not in (200, 201):
        raise AssertionError(status)
    if not isinstance(parsed, dict):
        raise AssertionError(parsed)
    return parsed
