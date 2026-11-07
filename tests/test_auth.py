"""Credentials, bearer authentication, and the audit helper."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application
from composition import bootstrap_management_token, load_settings
from domain.clock import utc_timestamp
from domain.jwt import encode_hs256
from domain.models.principal import KIND_SDK, KIND_USER, MANAGEMENT_ROLES, ROLE_READER, ROLE_WRITER
from domain.services.audit import record_change
from domain.errors import Unauthenticated
from domain.services.authentication import JwtSettings, authenticate
from repositories.credentials import find_active_principal, hash_token, insert_credential
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase

_JWT_SECRET = "test-jwt-signing-secret-32b-min!!"
_JWT = JwtSettings(
    secret=_JWT_SECRET,
    issuer="flaggator",
    audience="flaggator",
    ttl_seconds=3600,
)


def call(application: Application, method: str, path: str, authorization: str | None = None) -> tuple[str, dict]:
    """Invoke the WSGI app in-process.

    Does not bind a port. authorization is the header value, or omitted.
    Returns the status line and the JSON body.
    """
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        """Record the status line. Does not send bytes."""
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
    if authorization is not None:
        environ["HTTP_AUTHORIZATION"] = authorization
    payload = b"".join(application(environ, start_response))
    return str(captured["status"]), json.loads(payload.decode("utf-8"))


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_prints_the_secret_once_and_stores_the_hash(self) -> None:
        """The stdout line is the token. The database stores the hash and prefix.

        The notice and the audit log do not contain the secret. A second
        bootstrap inserts another credential and does not reprint the first.
        """
        with tempfile.TemporaryDirectory() as temporary:
            settings = load_settings(
                {
                    "FLAGGATOR_SQLITE_PATH": str(Path(temporary) / "test.db"),
                    "FLAGGATOR_CACHE": "null",
                }
            )
            secret_out = io.StringIO()
            notice_out = io.StringIO()
            bootstrap_management_token(settings, secret_out, notice_out)
            token = secret_out.getvalue().strip()
            self.assertNotIn(token, notice_out.getvalue())
            connection = SqliteDatabase(settings.sqlite_path).connect()
            try:
                row = connection.execute(
                    """
                    SELECT id, kind, secret_hash, prefix, environment_id
                      FROM api_credentials
                    """
                ).fetchone()
                audit = connection.execute(
                    """
                    SELECT actor_kind, actor_id, action, after_json
                      FROM audit_events
                    """
                ).fetchall()
            finally:
                connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["kind"], "management")
        self.assertIsNone(row["environment_id"])
        self.assertEqual(row["secret_hash"], hash_token(token))
        self.assertNotEqual(row["secret_hash"], token)
        self.assertEqual(row["prefix"], token[:8])
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "management_token.create")
        self.assertEqual(audit[0]["actor_kind"], "management")
        self.assertEqual(audit[0]["actor_id"], row["id"])
        self.assertEqual(json.loads(audit[0]["after_json"]), {"id": row["id"], "prefix": token[:8]})
        self.assertNotIn(token, audit[0]["after_json"])

    def test_main_bootstrap_argument_uses_the_sqlite_path(self) -> None:
        """python -m main bootstrap writes a token and does not start the listener."""
        from main import main

        with tempfile.TemporaryDirectory() as temporary:
            previous_path = os.environ.get("FLAGGATOR_SQLITE_PATH")
            previous_cache = os.environ.get("FLAGGATOR_CACHE")
            previous_engine = os.environ.get("FLAGGATOR_ENGINE")
            os.environ["FLAGGATOR_SQLITE_PATH"] = str(Path(temporary) / "test.db")
            os.environ["FLAGGATOR_CACHE"] = "null"
            os.environ["FLAGGATOR_ENGINE"] = "sqlite"
            stdout = io.StringIO()
            stderr = io.StringIO()
            real_stdout = sys.stdout
            real_stderr = sys.stderr
            sys.stdout = stdout
            sys.stderr = stderr
            try:
                main(["bootstrap"])
            finally:
                sys.stdout = real_stdout
                sys.stderr = real_stderr
                if previous_path is None:
                    os.environ.pop("FLAGGATOR_SQLITE_PATH", None)
                else:
                    os.environ["FLAGGATOR_SQLITE_PATH"] = previous_path
                if previous_cache is None:
                    os.environ.pop("FLAGGATOR_CACHE", None)
                else:
                    os.environ["FLAGGATOR_CACHE"] = previous_cache
                if previous_engine is None:
                    os.environ.pop("FLAGGATOR_ENGINE", None)
                else:
                    os.environ["FLAGGATOR_ENGINE"] = previous_engine
        token = stdout.getvalue().strip()
        self.assertNotEqual(token, "")
        self.assertNotIn(token, stderr.getvalue())


class AuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database and issue one management token."""
        self._temporary = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(str(Path(self._temporary.name) / "test.db"))
        apply_migrations(self.database, default_migrations_directory())
        secret_out = io.StringIO()
        settings = load_settings(
            {
                "FLAGGATOR_SQLITE_PATH": str(Path(self._temporary.name) / "test.db"),
                "FLAGGATOR_CACHE": "null",
            }
        )
        bootstrap_management_token(settings, secret_out, io.StringIO())
        self.management_token = secret_out.getvalue().strip()
        self.application = Application(self.database, jwt_settings=_JWT)

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_missing_unknown_and_revoked_share_one_message(self) -> None:
        """The 401 body does not say which of the three failures it was."""
        missing_status, missing_body = call(self.application, "GET", "/v1/projects")
        unknown_status, unknown_body = call(
            self.application,
            "GET",
            "/v1/projects",
            "Bearer not-a-real-token",
        )
        self._revoke(self.management_token)
        revoked_status, revoked_body = call(
            self.application,
            "GET",
            "/v1/projects",
            f"Bearer {self.management_token}",
        )
        self.assertEqual(missing_status, "401 Unauthorized")
        self.assertEqual(unknown_status, missing_status)
        self.assertEqual(revoked_status, missing_status)
        self.assertEqual(missing_body, unknown_body)
        self.assertEqual(missing_body, revoked_body)
        self.assertEqual(missing_body["error"]["code"], "unauthenticated")
        self.assertNotIn("revoked", missing_body["error"]["message"])

    def test_a_bad_jwt_is_the_same_401(self) -> None:
        """A JWT-shaped token that fails verify is the same 401 as a bad opaque token."""
        status, body = call(
            self.application,
            "GET",
            "/v1/projects",
            "Bearer aaa.bbb.ccc",
        )
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(body["error"]["message"], "A valid credential is required.")

    def test_management_token_passes_auth_on_an_unimplemented_route(self) -> None:
        """A real management token is not 401. An unknown route is still 404."""
        status, body = call(
            self.application,
            "GET",
            "/v1/not-a-route",
            f"Bearer {self.management_token}",
        )
        self.assertEqual(status, "404 Not Found")
        self.assertEqual(body["error"]["code"], "not_found")

    def test_sdk_token_cannot_call_a_management_path(self) -> None:
        """An SDK token is 403 on /v1/projects and is authorized on evaluate.

        GET is not the evaluate method, so the response is 405 rather than
        403. That shows authorize let the SDK token through.
        """
        sdk_token = "sdk-token-for-prod-environment"
        self._insert_sdk(sdk_token)
        forbidden_status, forbidden_body = call(
            self.application,
            "GET",
            "/v1/projects",
            f"Bearer {sdk_token}",
        )
        allowed_status, allowed_body = call(
            self.application,
            "GET",
            "/v1/evaluate",
            f"Bearer {sdk_token}",
        )
        self.assertEqual(forbidden_status, "403 Forbidden")
        self.assertEqual(forbidden_body["error"]["code"], "forbidden")
        self.assertEqual(allowed_status, "405 Method Not Allowed")

    def test_record_change_commits_with_the_caller_and_rolls_back_with_it(self) -> None:
        """The audit row is in the same transaction as the caller's other writes."""
        connection = self.database.connect()
        try:
            principal = find_active_principal(connection, self.management_token)
            self.assertIsNotNone(principal)
            assert principal is not None
            with connection.transaction():
                record_change(
                    connection,
                    principal,
                    action="project.create",
                    project_key="shop",
                    environment_key=None,
                    flag_key=None,
                    before_json=None,
                    after_json='{"key":"shop"}',
                )
            stored = connection.execute(
                """
                SELECT actor_kind, actor_id, action, project_key, after_json
                  FROM audit_events
                 WHERE action = 'project.create'
                """
            ).fetchone()
            with self.assertRaises(RuntimeError):
                with connection.transaction():
                    record_change(
                        connection,
                        principal,
                        action="project.create",
                        project_key="other",
                        environment_key=None,
                        flag_key=None,
                        before_json=None,
                        after_json=None,
                    )
                    raise RuntimeError("fail the transaction")
            remaining = connection.execute(
                """
                SELECT project_key FROM audit_events
                 WHERE action = 'project.create'
                """
            ).fetchall()
        finally:
            connection.close()
        assert stored is not None
        self.assertEqual(stored["actor_kind"], "management")
        self.assertEqual(stored["actor_id"], principal.credential_id)
        self.assertEqual(stored["action"], "project.create")
        self.assertEqual(stored["after_json"], '{"key":"shop"}')
        self.assertNotIn(self.management_token, stored["after_json"])
        self.assertEqual([row["project_key"] for row in remaining], ["shop"])

    def test_logout_and_a_missing_generation_reject_the_local_token(self) -> None:
        """Logout bumps gen for every local session. A token without gen is 401.

        A management token cannot call logout. The audit row names the user
        and does not include a secret.
        """
        user_id = self._insert_user("ops@example.com", '["reader"]')
        token = encode_hs256(
            {
                "sub": str(user_id),
                "gen": 0,
                "iss": "flaggator",
                "aud": "flaggator",
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
            },
            _JWT_SECRET,
        )
        stale = encode_hs256(
            {
                "sub": str(user_id),
                "iss": "flaggator",
                "aud": "flaggator",
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
            },
            _JWT_SECRET,
        )
        status, body = call(self.application, "POST", "/v1/auth/logout", f"Bearer {token}")
        denied_status, denied = call(
            self.application,
            "POST",
            "/v1/auth/logout",
            f"Bearer {self.management_token}",
        )
        connection = self.database.connect()
        try:
            with self.assertRaises(Unauthenticated):
                authenticate(connection, f"Bearer {token}", _JWT)
            with self.assertRaises(Unauthenticated):
                authenticate(connection, f"Bearer {stale}", _JWT)
            audit = connection.execute(
                """
                SELECT action, actor_kind, actor_id, project_key, after_json
                  FROM audit_events
                 WHERE action = 'user.logout'
                """
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(status, "204 No Content")
        self.assertEqual(body, {})
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertIn("user session", denied["error"]["message"])
        self.assertIsNotNone(audit)
        assert audit is not None
        self.assertEqual(audit["actor_kind"], "user")
        self.assertEqual(audit["actor_id"], user_id)
        self.assertEqual(audit["project_key"], "")
        self.assertEqual(json.loads(audit["after_json"]), {"id": user_id})

    def _revoke(self, token: str) -> None:
        """Set revoked_at on the credential for token. Commits that write."""
        connection = self.database.connect()
        try:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE api_credentials
                       SET revoked_at = ?
                     WHERE secret_hash = ?
                    """,
                    (utc_timestamp(), hash_token(token)),
                )
        finally:
            connection.close()

    def _insert_sdk(self, token: str) -> None:
        """Insert a project, an environment, and an SDK credential for token.

        Commits the rows. The token is a test value, not a generated secret.
        """
        connection = self.database.connect()
        try:
            with connection.transaction():
                project = connection.execute(
                    "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                    ("shop", "Shop"),
                )
                environment = connection.execute(
                    """
                    INSERT INTO environments (project_id, `key`, name)
                    VALUES (?, ?, ?)
                    """,
                    (project.last_insert_id, "prod", "Production"),
                )
                insert_credential(
                    connection,
                    kind=KIND_SDK,
                    token=token,
                    environment_id=environment.last_insert_id,
                    created_at=utc_timestamp(),
                )
        finally:
            connection.close()

    def test_authenticate_returns_the_principal_without_the_secret(self) -> None:
        """The principal carries the prefix, every management role, and not the token."""
        connection = self.database.connect()
        try:
            principal = authenticate(
                connection,
                f"Bearer {self.management_token}",
                _JWT,
            )
        finally:
            connection.close()
        self.assertEqual(principal.kind, "management")
        self.assertIsNone(principal.environment_id)
        self.assertEqual(principal.prefix, self.management_token[:8])
        self.assertEqual(principal.roles, MANAGEMENT_ROLES)
        self.assertNotIn(self.management_token, repr(principal))

    def test_authenticate_jwt_loads_roles_from_the_user_row(self) -> None:
        """A valid local JWT becomes a user principal; claim roles are ignored."""
        user_id = self._insert_user(
            "ops@example.com",
            '["reader","writer"]',
        )
        token = encode_hs256(
            {
                "sub": str(user_id),
                "roles": ["protected_writer"],
                "gen": 0,
                "iss": "flaggator",
                "aud": "flaggator",
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
            },
            _JWT_SECRET,
        )
        connection = self.database.connect()
        try:
            principal = authenticate(connection, f"Bearer {token}", _JWT)
            missing = self._insert_user("gone@example.com", '["reader"]', disabled=True)
            disabled_token = encode_hs256(
                {
                    "sub": str(missing),
                    "gen": 0,
                "iss": "flaggator",
                    "aud": "flaggator",
                    "iat": 1_700_000_000,
                    "exp": 2_000_000_000,
                },
                _JWT_SECRET,
            )
            with self.assertRaises(Unauthenticated):
                authenticate(connection, f"Bearer {disabled_token}", _JWT)
        finally:
            connection.close()
        self.assertEqual(principal.kind, KIND_USER)
        self.assertEqual(principal.credential_id, user_id)
        self.assertEqual(principal.roles, (ROLE_READER, ROLE_WRITER))
        self.assertIsNone(principal.environment_id)
        self.assertEqual(principal.prefix, "")
        allowed_status, allowed = call(
            self.application,
            "GET",
            "/v1/projects",
            f"Bearer {token}",
        )
        evaluate_status, evaluate = call(
            self.application,
            "POST",
            "/v1/evaluate",
            f"Bearer {token}",
        )
        self.assertEqual(allowed_status, "200 OK")
        self.assertEqual(allowed["items"], [])
        self.assertEqual(evaluate_status, "403 Forbidden")
        self.assertEqual(evaluate["error"]["code"], "forbidden")

    def _insert_user(self, email: str, roles_json: str, disabled: bool = False) -> int:
        """Insert one user row and return its id.

        Commits. password_hash is null. disabled sets disabled_at when true.
        """
        connection = self.database.connect()
        try:
            with connection.transaction():
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        email, name, password_hash, roles_json, created_at, disabled_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        email,
                        "Operator",
                        None,
                        roles_json,
                        utc_timestamp(),
                        utc_timestamp() if disabled else None,
                    ),
                )
                return cursor.last_insert_id
        finally:
            connection.close()
