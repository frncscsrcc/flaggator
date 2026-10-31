"""Users CRUD and local login through the HTTP API."""

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


class UsersTests(unittest.TestCase):
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

    def test_create_login_and_list_users(self) -> None:
        """Create stores roles, login returns a JWT, list omits passwords."""
        create_status, created = self.call(
            "POST",
            "/v1/users",
            {
                "email": "Ops@Example.com",
                "name": "Ops",
                "password": "password1",
                "roles": ["reader", "writer"],
            },
        )
        login_status, logged_in = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "ops@example.com", "password": "password1"},
            authorization=None,
        )
        list_status, listed = self.call("GET", "/v1/users")
        get_status, fetched = self.call("GET", f"/v1/users/{created['id']}")
        self.assertEqual(create_status, "201 Created")
        self.assertEqual(created["email"], "ops@example.com")
        self.assertEqual(created["roles"], ["reader", "writer"])
        self.assertIsNone(created["disabled_at"])
        self.assertNotIn("password", created)
        self.assertEqual(login_status, "200 OK")
        self.assertEqual(logged_in["token_type"], "bearer")
        self.assertEqual(logged_in["expires_in"], 3600)
        self.assertIn(".", logged_in["access_token"])
        self.assertEqual(list_status, "200 OK")
        self.assertEqual(listed["items"], [created])
        self.assertIsNone(listed["next_cursor"])
        self.assertEqual(get_status, "200 OK")
        self.assertEqual(fetched, created)

    def test_login_failures_share_one_message(self) -> None:
        """Wrong password, unknown email, and disabled user are the same 401."""
        created_status, created = self.call(
            "POST",
            "/v1/users",
            {
                "email": "ops@example.com",
                "name": "Ops",
                "password": "password1",
                "roles": ["reader"],
            },
        )
        self.assertEqual(created_status, "201 Created")
        wrong_status, wrong = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "ops@example.com", "password": "password2"},
            authorization=None,
        )
        missing_status, missing = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "missing@example.com", "password": "password1"},
            authorization=None,
        )
        self.call(
            "PATCH",
            f"/v1/users/{created['id']}",
            {"disabled": True},
        )
        disabled_status, disabled = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "ops@example.com", "password": "password1"},
            authorization=None,
        )
        self.assertEqual(wrong_status, "401 Unauthorized")
        self.assertEqual(missing_status, wrong_status)
        self.assertEqual(disabled_status, wrong_status)
        self.assertEqual(wrong, missing)
        self.assertEqual(wrong, disabled)
        self.assertEqual(wrong["error"]["code"], "unauthenticated")

    def test_patch_roles_and_duplicate_email(self) -> None:
        """PATCH updates roles; a second create with the same email is 409."""
        first_status, first = self.call(
            "POST",
            "/v1/users",
            {
                "email": "ops@example.com",
                "name": "Ops",
                "password": "password1",
                "roles": ["reader"],
            },
        )
        patch_status, patched = self.call(
            "PATCH",
            f"/v1/users/{first['id']}",
            {"name": "Operator", "roles": ["reader", "writer", "protected_writer"]},
        )
        conflict_status, conflict = self.call(
            "POST",
            "/v1/users",
            {
                "email": "ops@example.com",
                "name": "Other",
                "password": "password1",
                "roles": ["reader"],
            },
        )
        self.assertEqual(first_status, "201 Created")
        self.assertEqual(patch_status, "200 OK")
        self.assertEqual(patched["name"], "Operator")
        self.assertEqual(patched["roles"], ["reader", "writer", "protected_writer"])
        self.assertEqual(conflict_status, "409 Conflict")
        self.assertEqual(conflict["error"]["code"], "conflict")

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        authorization: str | None = "use-token",
    ) -> tuple[str, dict]:
        """Invoke the WSGI app in-process."""
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line."""
            captured["status"] = status
            captured["headers"] = headers

        raw = b""
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
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
