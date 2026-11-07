"""Role gates for JWT users on management routes."""

import io
import json
import tempfile
import unittest
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application
from composition import bootstrap_management_token, load_settings
from domain.jwt import encode_hs256
from domain.services.authentication import JwtSettings
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase

_JWT_SECRET = "test-jwt-signing-secret-32b-min!!"
_JWT = JwtSettings(
    secret=_JWT_SECRET,
    issuer="flaggator",
    audience="flaggator",
    ttl_seconds=3600,
)


class AuthzTests(unittest.TestCase):
    def setUp(self) -> None:
        """Bootstrap management token and JWT-capable application."""
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
        self.management = secret_out.getvalue().strip()
        self.application = Application(self.database, jwt_settings=_JWT)

    def tearDown(self) -> None:
        """Delete the temporary database."""
        self._temporary.cleanup()

    def test_reader_can_get_but_not_create(self) -> None:
        """reader lists projects; create is 403."""
        token = self._user_token(["reader"])
        list_status, _listed = self.call("GET", "/v1/projects", token=token)
        create_status, create_body = self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": "Shop"},
            token=token,
        )
        self.assertEqual(list_status, "200 OK")
        self.assertEqual(create_status, "403 Forbidden")
        self.assertEqual(create_body["error"]["code"], "forbidden")

    def test_writer_needs_protected_writer_for_protected_env(self) -> None:
        """writer creates a normal environment; protected create is 403."""
        token = self._user_token(["reader", "writer"])
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"}, token=token)
        ok_status, _ok = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "dev", "name": "Dev"},
            token=token,
        )
        denied_status, denied = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Prod", "protected": True},
            token=token,
        )
        full = self._user_token(["reader", "writer", "protected_writer"])
        allowed_status, created = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Prod", "protected": True},
            token=full,
        )
        self.assertEqual(ok_status, "201 Created")
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertIn("protected_writer", denied["error"]["message"])
        self.assertEqual(allowed_status, "201 Created")
        self.assertIs(created["protected"], True)

    def _user_token(self, roles: list[str]) -> str:
        """Create a user with roles and return a local access JWT."""
        status, user = self.call(
            "POST",
            "/v1/users",
            {
                "email": f"{'-'.join(roles)}@example.com",
                "name": "Operator",
                "password": "password-ok-1",
                "roles": roles,
            },
            token=self.management,
        )
        self.assertEqual(status, "201 Created")
        return encode_hs256(
            {
                "sub": str(user["id"]),
                "roles": roles,
                "iss": "flaggator",
                "aud": "flaggator",
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
            },
            _JWT_SECRET,
        )

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> tuple[str, dict]:
        """Invoke the WSGI app in-process."""
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line."""
            captured["status"] = status

        raw = b"" if body is None else json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8080",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": io.BytesIO(raw),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "CONTENT_LENGTH": str(len(raw)),
            "HTTP_AUTHORIZATION": f"Bearer {token}",
        }
        if body is not None:
            environ["CONTENT_TYPE"] = "application/json"
        payload = b"".join(self.application(environ, start_response))
        return str(captured["status"]), json.loads(payload.decode("utf-8"))
