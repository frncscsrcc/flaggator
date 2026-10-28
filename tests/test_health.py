"""HTTP tests for the routes this phase serves."""

import io
import json
import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application


def call(method: str, path: str) -> tuple[str, dict, list[tuple[str, str]]]:
    """Invoke the WSGI app in-process and return status, JSON, and headers.

    Does not bind a port. body is empty.
    """
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        """Record the status line and headers. Does not send bytes."""
        captured["status"] = status
        captured["headers"] = headers

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "8080",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": "0",
    }
    payload = b"".join(Application()(environ, start_response))
    status = str(captured["status"])
    headers = captured["headers"]
    return status, json.loads(payload.decode("utf-8")), headers  # type: ignore[arg-type]


class HealthTests(unittest.TestCase):
    def test_get_health(self) -> None:
        """GET /health is 200 and does not claim to have checked the database."""
        status, body, headers = call("GET", "/health")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body, {"status": "ok"})
        header_map = dict(headers)
        self.assertEqual(header_map["Content-Type"], "application/json; charset=utf-8")

    def test_health_rejects_post(self) -> None:
        """POST /health is 405. The route exists and accepts GET."""
        status, body, _headers = call("POST", "/health")
        self.assertEqual(status, "405 Method Not Allowed")
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_unknown_path_without_a_database_is_a_generic_500(self) -> None:
        """A non-health path must authenticate, and that needs a database.

        This application was built without one, so the answer is a generic
        500. It is not a 404 that would skip the credential check.
        """
        status, body, _headers = call("GET", "/v1/projects")
        self.assertEqual(status, "500 Internal Server Error")
        self.assertEqual(body["error"]["code"], "storage_error")
        self.assertNotIn("sqlite", body["error"]["message"].lower())
