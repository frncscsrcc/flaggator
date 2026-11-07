"""The operator console is static files on the same process as the API."""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application
from composition import load_settings
from domain.errors import ValidationError


def call(application: Application, method: str, path: str) -> tuple[str, bytes, list[tuple[str, str]]]:
    """Invoke application in-process and return status, body, and headers.

    Does not bind a port. The body is empty.
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
    payload = b"".join(application(environ, start_response))
    return str(captured["status"]), payload, captured["headers"]  # type: ignore[return-value]


class ConsoleSettingsTests(unittest.TestCase):
    def test_headless_defaults_to_serving_the_console(self) -> None:
        """Omitted, empty, and false keep the console on."""
        self.assertFalse(load_settings({}).headless)
        self.assertFalse(load_settings({"FLAGGATOR_HEADLESS": ""}).headless)
        self.assertFalse(load_settings({"FLAGGATOR_HEADLESS": "false"}).headless)
        self.assertIn("headless=False", repr(load_settings({})))

    def test_headless_true_is_accepted_and_other_values_are_not(self) -> None:
        """true turns the console off. Anything else fails at startup."""
        self.assertTrue(load_settings({"FLAGGATOR_HEADLESS": "true"}).headless)
        with self.assertRaises(ValidationError) as caught:
            load_settings({"FLAGGATOR_HEADLESS": "yes"})
        self.assertEqual(caught.exception.details[0]["field"], "headless")


class ConsoleFileTests(unittest.TestCase):
    def setUp(self) -> None:
        """Build a ui tree with one page and a file outside it."""
        self._temp = tempfile.TemporaryDirectory()
        root = Path(self._temp.name)
        self.console = root / "ui"
        self.console.mkdir()
        (self.console / "login.html").write_text("<form id=\"login\">", encoding="utf-8")
        (self.console / "notes.txt").write_text("not a page", encoding="utf-8")
        outside = root / "secret.html"
        outside.write_text("secret", encoding="utf-8")
        (self.console / "escape.html").symlink_to(outside)
        self.application = Application(console_directory=self.console)

    def tearDown(self) -> None:
        """Remove the temporary tree."""
        self._temp.cleanup()

    def test_get_login_page_is_html_without_a_database(self) -> None:
        """The sign-in page is served even when the application has no database."""
        status, body, headers = call(self.application, "GET", "/login.html")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"login", body)
        self.assertEqual(dict(headers)["Content-Type"], "text/html; charset=utf-8")

    def test_a_path_that_leaves_the_console_directory_is_404(self) -> None:
        """.. and a symlink that points outside ui/ are not served."""
        status, body, _headers = call(self.application, "GET", "/../secret.html")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        escaped, escaped_body, _headers = call(self.application, "GET", "/escape.html")
        self.assertEqual(escaped, "404 Not Found")
        self.assertNotIn(b"secret", escaped_body)

    def test_a_file_of_an_unknown_type_is_404(self) -> None:
        """Only the console suffixes are sent. A text file in ui/ is not."""
        status, body, _headers = call(self.application, "GET", "/notes.txt")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")

    def test_health_stays_json(self) -> None:
        """A console directory does not take over GET /health."""
        status, body, headers = call(self.application, "GET", "/health")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body), {"status": "ok"})
        self.assertEqual(dict(headers)["Content-Type"], "application/json; charset=utf-8")

    def test_headless_does_not_read_the_console_directory(self) -> None:
        """FLAGGATOR_HEADLESS=true makes the sign-in page a normal 404."""
        application = Application(console_directory=self.console, headless=True)
        status, body, headers = call(application, "GET", "/login.html")
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        self.assertEqual(dict(headers)["Content-Type"], "application/json; charset=utf-8")

    def test_the_shipped_sign_in_page_is_served_by_default(self) -> None:
        """python -m main finds ui/login.html without a directory argument."""
        status, body, _headers = call(Application(), "GET", "/login.html")
        self.assertEqual(status, "200 OK")
        self.assertIn(b"Sign in", body)
        script, script_body, headers = call(Application(), "GET", "/api.js")
        self.assertEqual(script, "200 OK")
        self.assertIn(b"/v1/auth/login", script_body)
        self.assertEqual(dict(headers)["Content-Type"], "text/javascript; charset=utf-8")
