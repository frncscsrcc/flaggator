"""Public flags and the unauthenticated client evaluation route."""

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
from cache.memory import MemoryCache
from composition import bootstrap_management_token, load_settings
from domain.services.authentication import JwtSettings
from repositories.eval_cache import EvaluationCache, cache_key
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase

_JWT_SECRET = "test-jwt-signing-secret-32b-min!!"
_JWT = JwtSettings(
    secret=_JWT_SECRET,
    issuer="flaggator",
    audience="flaggator",
    ttl_seconds=3600,
)


class PublicFlagTests(unittest.TestCase):
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
                "FLAGGATOR_CACHE": "memory",
                "FLAGGATOR_JWT_SECRET": _JWT_SECRET,
            }
        )
        bootstrap_management_token(settings, secret_out, io.StringIO())
        self.token = secret_out.getvalue().strip()
        self.memory = MemoryCache()
        self.application = Application(
            self.database,
            EvaluationCache(self.memory, 3600),
            jwt_settings=_JWT,
        )

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_create_without_public_stays_private(self) -> None:
        """Omitting public stores false and the flag response includes it."""
        self._project()
        status, body = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {
                "key": "checkout-v2",
                "name": "Checkout",
                "description": "",
                "variations": [False, True],
            },
        )
        listed_status, listed = self.call("GET", "/v1/projects/shop/flags")
        self.assertEqual(status, "201 Created")
        self.assertIs(body["public"], False)
        self.assertEqual(listed_status, "200 OK")
        self.assertIs(listed["items"][0]["public"], False)

    def test_a_public_flag_cannot_depend_on_a_private_one(self) -> None:
        """A public flag may require only public flags, and protecting clears the client id.

        A stored public flag that reaches a private flag is omitted from
        client evaluation. Issuing a new client id lists the public keys.
        """
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        self._flag("inner", public=False)
        self._flag("outer", public=True)
        denied_status, denied = self.call(
            "PUT",
            "/v1/projects/shop/flags/outer/environments/prod",
            self._targeting(1, prerequisites=[{"flag_key": "inner", "variation_index": 1}]),
        )
        made_public_status, _made_public = self.call(
            "PATCH",
            "/v1/projects/shop/flags/inner",
            {"public": True},
        )
        allowed_status, _allowed = self.call(
            "PUT",
            "/v1/projects/shop/flags/outer/environments/prod",
            self._targeting(1, prerequisites=[{"flag_key": "inner", "variation_index": 1}]),
        )
        hide_status, hide = self.call(
            "PATCH",
            "/v1/projects/shop/flags/inner",
            {"public": False},
        )
        issued_status, issued = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        connection = self.database.connect()
        try:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE flags SET public = 0
                     WHERE `key` = 'inner'
                       AND project_id = (SELECT id FROM projects WHERE `key` = 'shop')
                    """
                )
        finally:
            connection.close()
        self.memory.delete(cache_key("shop", "prod"))
        evaluated_status, evaluated = self.call(
            "POST",
            "/v1/client-evaluate",
            {"client_id": issued["client_id"], "context": {"subject": "alice"}},
            authorization=None,
        )
        protected_status, protected = self.call(
            "PATCH",
            "/v1/projects/shop/environments/prod",
            {"protected": True},
        )
        again_status, again = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        self.assertEqual(denied_status, "400 Bad Request")
        self.assertEqual(denied["error"]["details"][0]["field"], "prerequisites")
        self.assertEqual(made_public_status, "200 OK")
        self.assertEqual(allowed_status, "200 OK")
        self.assertEqual(hide_status, "400 Bad Request")
        self.assertEqual(hide["error"]["details"][0]["field"], "public")
        self.assertEqual(issued_status, "200 OK")
        self.assertEqual(evaluated_status, "200 OK")
        self.assertEqual(evaluated["flags"], [])
        self.assertEqual(protected_status, "200 OK")
        self.assertIsNone(protected["client_id"])
        self.assertIs(protected["protected"], True)
        self.assertEqual(again_status, "200 OK")
        self.assertIsNotNone(again["client_id"])

    def test_writer_cannot_issue_a_client_id_for_a_protected_environment(self) -> None:
        """A writer may rotate a normal environment, not a protected one."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "dev", "name": "Dev"},
        )
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production", "protected": True},
        )
        writer = self._writer_token()
        dev_status, dev = self.call(
            "POST",
            "/v1/projects/shop/environments/dev/client-id",
            authorization=f"Bearer {writer}",
        )
        prod_status, prod = self.call(
            "POST",
            "/v1/projects/shop/environments/prod/client-id",
            authorization=f"Bearer {writer}",
        )
        self.assertEqual(dev_status, "200 OK")
        self.assertIsInstance(dev["client_id"], str)
        self.assertGreater(len(dev["client_id"]), 0)
        self.assertEqual(prod_status, "403 Forbidden")
        self.assertIn("protected_writer", prod["error"]["message"])

    def test_public_bit_on_a_protected_project_needs_protected_writer(self) -> None:
        """Turning public on is forbidden for a writer when any environment is protected."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production", "protected": True},
        )
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {
                "key": "banner",
                "name": "Banner",
                "description": "",
                "variations": [False, True],
            },
        )
        writer = self._writer_token()
        denied_status, denied = self.call(
            "PATCH",
            "/v1/projects/shop/flags/banner",
            {"public": True},
            authorization=f"Bearer {writer}",
        )
        renamed_status, renamed = self.call(
            "PATCH",
            "/v1/projects/shop/flags/banner",
            {"name": "Homepage banner"},
            authorization=f"Bearer {writer}",
        )
        allowed_status, allowed = self.call(
            "PATCH",
            "/v1/projects/shop/flags/banner",
            {"public": True},
        )
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertIn("protected_writer", denied["error"]["message"])
        self.assertEqual(renamed_status, "200 OK")
        self.assertEqual(renamed["name"], "Homepage banner")
        self.assertIs(renamed["public"], False)
        self.assertEqual(allowed_status, "200 OK")
        self.assertIs(allowed["public"], True)

    def test_client_evaluate_returns_public_flags_without_a_bearer(self) -> None:
        """The public route omits private flags and reasons. A public prerequisite still applies."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        self._flag("gate", public=True)
        self._flag("show", public=True)
        self.call(
            "PUT",
            "/v1/projects/shop/flags/gate/environments/prod",
            self._targeting(1, fallthrough={"variation": 0}),
        )
        self.call(
            "PUT",
            "/v1/projects/shop/flags/show/environments/prod",
            self._targeting(
                1,
                fallthrough={"variation": 1},
                prerequisites=[{"flag_key": "gate", "variation_index": 1}],
            ),
        )
        issued_status, issued = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        self.assertEqual(issued_status, "200 OK")
        client_id = issued["client_id"]
        hidden_status, hidden = self.call(
            "POST",
            "/v1/client-evaluate",
            {"client_id": client_id, "context": {"subject": "user-1"}},
            authorization=None,
        )
        self.call(
            "PUT",
            "/v1/projects/shop/flags/gate/environments/prod",
            self._targeting(2, fallthrough={"variation": 1}),
        )
        shown_status, shown = self.call(
            "POST",
            "/v1/client-evaluate",
            {"client_id": client_id, "context": {"subject": "user-1"}},
            authorization=None,
        )
        self.assertEqual(hidden_status, "200 OK")
        self.assertEqual(
            hidden["flags"],
            [
                {"key": "gate", "value": False, "variation_index": 0},
                {"key": "show", "value": False, "variation_index": 0},
            ],
        )
        self.assertNotIn("reason", json.dumps(hidden))
        self.assertEqual(shown_status, "200 OK")
        self.assertEqual(
            shown["flags"],
            [
                {"key": "gate", "value": True, "variation_index": 1},
                {"key": "show", "value": True, "variation_index": 1},
            ],
        )

    def test_a_rotated_client_id_is_not_found(self) -> None:
        """The previous client id stops resolving, and a valid id with no public flags is an empty list."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        self._flag("banner", public=False)
        first_status, first = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        empty_status, empty = self.call(
            "POST",
            "/v1/client-evaluate",
            {"client_id": first["client_id"], "context": {}},
            authorization=None,
        )
        second_status, second = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        old_status, old = self.call(
            "POST",
            "/v1/client-evaluate",
            {"client_id": first["client_id"], "context": {}},
            authorization=None,
        )
        self.assertEqual(first_status, "200 OK")
        self.assertEqual(empty_status, "200 OK")
        self.assertEqual(empty["flags"], [])
        self.assertEqual(second_status, "200 OK")
        self.assertNotEqual(first["client_id"], second["client_id"])
        self.assertEqual(old_status, "404 Not Found")
        self.assertEqual(old["error"]["code"], "not_found")

    def test_changing_public_drops_the_cached_bundle(self) -> None:
        """A patch of public is visible on the next client evaluation inside the cache TTL."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        self._flag("banner", public=True)
        issued_status, issued = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        self.assertEqual(issued_status, "200 OK")
        body = {"client_id": issued["client_id"], "context": {}}
        before_status, before = self.call("POST", "/v1/client-evaluate", body, authorization=None)
        patch_status, _patched = self.call(
            "PATCH",
            "/v1/projects/shop/flags/banner",
            {"public": False},
        )
        after_status, after = self.call("POST", "/v1/client-evaluate", body, authorization=None)
        self.assertEqual(before_status, "200 OK")
        self.assertEqual(before["flags"][0]["key"], "banner")
        self.assertEqual(patch_status, "200 OK")
        self.assertEqual(after_status, "200 OK")
        self.assertEqual(after["flags"], [])

    def test_client_evaluate_is_rate_limited_per_address(self) -> None:
        """The third request in a limit of two is 429, without sending sixty calls."""
        self._project()
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        issued_status, issued = self.call("POST", "/v1/projects/shop/environments/prod/client-id")
        self.assertEqual(issued_status, "200 OK")
        limited = Application(
            self.database,
            client_eval_limit=2,
            client_eval_window_seconds=60,
        )
        body = {"client_id": issued["client_id"], "context": {}}
        previous = self.application
        self.application = limited
        try:
            first_status, _first = self.call("POST", "/v1/client-evaluate", body, authorization=None)
            second_status, _second = self.call("POST", "/v1/client-evaluate", body, authorization=None)
            third_status, third = self.call("POST", "/v1/client-evaluate", body, authorization=None)
        finally:
            self.application = previous
        self.assertEqual(first_status, "200 OK")
        self.assertEqual(second_status, "200 OK")
        self.assertEqual(third_status, "429 Too Many Requests")
        self.assertEqual(third["error"]["code"], "rate_limited")

    def _project(self) -> None:
        """Create the shop project. Does not create an environment."""
        status, _body = self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.assertEqual(status, "201 Created")

    def _flag(self, key: str, public: bool) -> None:
        """Create one flag. public is sent explicitly."""
        status, body = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {
                "key": key,
                "name": key,
                "description": "",
                "public": public,
                "variations": [False, True],
            },
        )
        self.assertEqual(status, "201 Created")
        self.assertIs(body["public"], public)

    def _writer_token(self) -> str:
        """Create a writer and return that user's access token."""
        created_status, _created = self.call(
            "POST",
            "/v1/users",
            {
                "email": "writer@example.com",
                "name": "Writer",
                "password": "password-ok-1",
                "roles": ["reader", "writer"],
            },
        )
        self.assertEqual(created_status, "201 Created")
        login_status, logged_in = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "writer@example.com", "password": "password-ok-1"},
            authorization=None,
        )
        self.assertEqual(login_status, "200 OK")
        return str(logged_in["access_token"])

    def _targeting(self, version: int, **overrides: object) -> dict:
        """Return a targeting body. overrides replace top-level fields."""
        body: dict = {
            "enabled": True,
            "off_variation": 0,
            "fallthrough": {"variation": 0},
            "rules": [],
            "targets": {},
            "prerequisites": [],
            "version": version,
        }
        body.update(overrides)
        return body

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

        raw = b""
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REMOTE_ADDR": "203.0.113.8",
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
