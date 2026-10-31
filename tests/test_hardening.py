"""Controls from the 2026-10-31 security audit."""

import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application, ThreadingWSGIServer, request_from_environ
from cache.memory import MemoryCache
from composition import bootstrap_management_token, load_settings
from domain.errors import ValidationError
from domain.jwt import _jwks_client
from domain.services.authentication import JwtSettings
from repositories.eval_cache import EvaluationCache
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase
from wsgiref.simple_server import make_server

_JWT_SECRET = "test-jwt-signing-secret-32b-min!!"
_JWT = JwtSettings(
    secret=_JWT_SECRET,
    issuer="flaggator",
    audience="flaggator",
    ttl_seconds=3600,
)


class BodyLimitTests(unittest.TestCase):
    def test_oversized_content_length_is_rejected_before_read(self) -> None:
        """A declared body over 256 KiB never touches wsgi.input."""

        class Boom(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("body was read")

        with self.assertRaises(ValidationError):
            request_from_environ(
                {
                    "REQUEST_METHOD": "POST",
                    "PATH_INFO": "/v1/auth/login",
                    "QUERY_STRING": "",
                    "CONTENT_LENGTH": str(300 * 1024),
                    "wsgi.input": Boom(),
                }
            )


class ServerSlotTests(unittest.TestCase):
    def test_a_full_server_answers_503(self) -> None:
        """When every worker slot is taken, the socket gets 503 and is closed."""
        server = make_server(
            "127.0.0.1",
            0,
            Application(None),
            server_class=ThreadingWSGIServer,
        )
        left, right = socket.socketpair()
        try:
            server._in_flight = threading.BoundedSemaphore(0)
            server.process_request(left, ("127.0.0.1", 1))
            data = right.recv(4096)
        finally:
            right.close()
            server.server_close()
        self.assertIn(b"503", data)
        self.assertIn(b"unavailable", data)


class JwksTimeoutTests(unittest.TestCase):
    def test_jwks_client_uses_a_five_second_timeout(self) -> None:
        """IdP key fetch cannot block a worker for the library default."""
        client = _jwks_client("https://idp.example.test/jwks")
        self.assertEqual(client.timeout, 5)


class CacheGenerationTests(unittest.TestCase):
    def test_a_fill_from_before_invalidation_is_dropped(self) -> None:
        """set after drop does not put the stale bytes back."""
        cache = MemoryCache()
        evaluation = EvaluationCache(cache, 5)
        evaluation._store_if_current("eval:shop:prod", 0, b"{}")
        evaluation.drop("eval:shop:prod")
        evaluation._store_if_current("eval:shop:prod", 0, b"stale")
        self.assertIsNone(cache.get("eval:shop:prod"))


class SqliteModeTests(unittest.TestCase):
    def test_database_file_is_owner_only(self) -> None:
        """Connect sets mode 0600 on the SQLite file."""
        temporary = tempfile.TemporaryDirectory()
        try:
            path = str(Path(temporary.name) / "mode.db")
            database = SqliteDatabase(path)
            connection = database.connect()
            connection.close()
            mode = stat.S_IMODE(os.stat(path).st_mode)
        finally:
            temporary.cleanup()
        self.assertEqual(mode, 0o600)


class LoginAndTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database and issue a management token."""
        self._temporary = tempfile.TemporaryDirectory()
        db_path = str(Path(self._temporary.name) / "test.db")
        self.database = SqliteDatabase(db_path)
        apply_migrations(self.database, default_migrations_directory())
        secret_out = io.StringIO()
        settings = load_settings(
            {
                "FLAGGATOR_SQLITE_PATH": db_path,
                "FLAGGATOR_CACHE": "null",
                "FLAGGATOR_JWT_SECRET": _JWT_SECRET,
            }
        )
        bootstrap_management_token(settings, secret_out, io.StringIO())
        self.token = secret_out.getvalue().strip()
        self.application = Application(self.database, jwt_settings=_JWT)

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_login_rate_limit_and_short_password(self) -> None:
        """A short password is 401. The eleventh attempt from one address is 429."""
        first_status, first = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "ops@example.com", "password": "short"},
            authorization=None,
            remote="10.0.0.8",
        )
        for _ in range(9):
            self.call(
                "POST",
                "/v1/auth/login",
                {"email": "ops@example.com", "password": "short"},
                authorization=None,
                remote="10.0.0.8",
            )
        limited_status, limited = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "ops@example.com", "password": "password1"},
            authorization=None,
            remote="10.0.0.8",
        )
        self.assertEqual(first_status, "401 Unauthorized")
        self.assertEqual(first["error"]["code"], "unauthenticated")
        self.assertEqual(limited_status, "429 Too Many Requests")
        self.assertEqual(limited["error"]["code"], "rate_limited")

    def test_writer_cannot_grant_protected_writer(self) -> None:
        """A writer who lacks protected_writer cannot add that role."""
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "writer@example.com",
                "name": "Writer",
                "password": "password1",
                "roles": ["reader", "writer"],
            },
        )
        logged_in = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "writer@example.com", "password": "password1"},
            authorization=None,
        )[1]
        status, body = self.call(
            "POST",
            "/v1/users",
            {
                "email": "other@example.com",
                "name": "Other",
                "password": "password1",
                "roles": ["reader", "writer", "protected_writer"],
            },
            authorization="Bearer " + logged_in["access_token"],
        )
        self.assertEqual(status, "403 Forbidden")
        self.assertIn("protected_writer", body["error"]["message"])

    def test_list_and_revoke_management_token(self) -> None:
        """List shows the prefix. Delete makes the secret fail as unknown."""
        listed_status, listed = self.call("GET", "/v1/management-tokens")
        token_id = listed["items"][0]["id"]
        self.assertEqual(listed_status, "200 OK")
        self.assertNotIn("secret", listed["items"][0])
        self.assertEqual(len(listed["items"][0]["prefix"]), 8)
        revoked_status, _revoked = self.call("DELETE", f"/v1/management-tokens/{token_id}")
        again_status, again = self.call("GET", "/v1/projects")
        self.assertEqual(revoked_status, "204 No Content")
        self.assertEqual(again_status, "401 Unauthorized")
        self.assertEqual(again["error"]["code"], "unauthenticated")

    def test_flag_cap_and_context_array_cap(self) -> None:
        """The second flag is rejected when the cap is 1. A long array is 400."""
        self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": "Shop"},
        )
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        with patch("domain.services.flags.MAX_FLAGS_PER_PROJECT", 1):
            self.call(
                "POST",
                "/v1/projects/shop/flags",
                {
                    "key": "one",
                    "name": "One",
                    "description": "",
                    "variations": [False, True],
                },
            )
            flag_status, flag_body = self.call(
                "POST",
                "/v1/projects/shop/flags",
                {
                    "key": "two",
                    "name": "Two",
                    "description": "",
                    "variations": [False, True],
                },
            )
        evaluate_status, evaluate_body = self.call(
            "POST",
            "/v1/evaluate",
            {
                "project_key": "shop",
                "environment_key": "prod",
                "flag_key": "one",
                "context": {"subject": "a", "attributes": {"country": ["x"] * 51}},
                "default": False,
            },
        )
        self.assertEqual(flag_status, "400 Bad Request")
        self.assertEqual(flag_body["error"]["code"], "validation_error")
        self.assertEqual(evaluate_status, "400 Bad Request")
        self.assertIn("50", evaluate_body["error"]["details"][0]["message"])

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        authorization: str | None = "use-token",
        remote: str = "127.0.0.1",
    ) -> tuple[str, dict]:
        """Invoke the WSGI app in-process."""
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line."""
            captured["status"] = status

        raw = b""
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REMOTE_ADDR": remote,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        if body is not None:
            environ["CONTENT_TYPE"] = "application/json"
        if authorization == "use-token":
            environ["HTTP_AUTHORIZATION"] = f"Bearer {self.token}"
        elif authorization is not None:
            environ["HTTP_AUTHORIZATION"] = authorization
        payload = b"".join(self.application(environ, start_response))
        if captured["status"] == "204 No Content":
            return str(captured["status"]), {}
        return str(captured["status"]), json.loads(payload.decode("utf-8"))
