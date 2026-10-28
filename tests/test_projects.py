"""Projects and environments through the HTTP API."""

import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import Application
from composition import bootstrap_management_token, load_settings
from domain.services.authentication import authenticate
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase


def cursor_text(cursor: str) -> str:
    """Decode a list cursor the way a test checks the keyset text.

    Returns the `v1:` payload. Does not call the service.
    """
    padded = cursor + ("=" * (-len(cursor) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


class ProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database and issue one management token."""
        self._temporary = tempfile.TemporaryDirectory()
        database_path = str(Path(self._temporary.name) / "test.db")
        self.database = SqliteDatabase(database_path)
        apply_migrations(self.database, default_migrations_directory())
        secret_out = io.StringIO()
        settings = load_settings(
            {
                "FLAGGATOR_SQLITE_PATH": database_path,
                "FLAGGATOR_CACHE": "null",
                "FLAGGATOR_ENGINE": "sqlite",
            }
        )
        bootstrap_management_token(settings, secret_out, io.StringIO())
        self.token = secret_out.getvalue().strip()
        self.application = Application(self.database)

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_create_get_and_audit(self) -> None:
        """POST stores the project, GET returns it, and the audit row has no secret."""
        created_status, created = self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": "The Shop"},
        )
        fetched_status, fetched = self.call("GET", "/v1/projects/shop")
        audit = self.audit_rows()
        self.assertEqual(created_status, "201 Created")
        self.assertEqual(created, {"key": "shop", "name": "The Shop"})
        self.assertEqual(fetched_status, "200 OK")
        self.assertEqual(fetched, created)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["action"], "project.create")
        self.assertEqual(audit[0]["project_key"], "shop")
        self.assertIsNone(audit[0]["environment_key"])
        self.assertIsNone(audit[0]["flag_key"])
        self.assertIsNone(audit[0]["before_json"])
        self.assertEqual(json.loads(str(audit[0]["after_json"])), {"key": "shop", "name": "The Shop"})
        self.assertNotIn(self.token, str(audit[0]["after_json"]))
        connection = self.database.connect()
        try:
            principal = authenticate(connection, f"Bearer {self.token}")
        finally:
            connection.close()
        self.assertEqual(audit[0]["actor_id"], principal.credential_id)
        self.assertEqual(audit[0]["actor_kind"], "management")

    def test_duplicate_project_is_a_conflict_and_does_not_add_an_audit_row(self) -> None:
        """A second POST of the same key is 409 and leaves the first audit row alone."""
        self.call("POST", "/v1/projects", {"key": "shop", "name": "The Shop"})
        status, body = self.call("POST", "/v1/projects", {"key": "shop", "name": "Other"})
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertEqual(len(self.audit_rows()), 1)
        fetched_status, fetched = self.call("GET", "/v1/projects/shop")
        self.assertEqual(fetched_status, "200 OK")
        self.assertEqual(fetched["name"], "The Shop")

    def test_invalid_project_bodies(self) -> None:
        """A bad key, a blank name, a missing field, and an extra field are 400."""
        uppercase_status, uppercase = self.call(
            "POST",
            "/v1/projects",
            {"key": "Shop", "name": "Shop"},
        )
        blank_status, blank = self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": " Shop"},
        )
        missing_status, missing = self.call("POST", "/v1/projects", {"key": "shop"})
        extra_status, extra = self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": "Shop", "description": "no"},
        )
        self.assertEqual(uppercase_status, "400 Bad Request")
        self.assertEqual(uppercase["error"]["details"][0]["field"], "key")
        self.assertEqual(blank_status, "400 Bad Request")
        self.assertEqual(blank["error"]["details"][0]["field"], "name")
        self.assertEqual(missing_status, "400 Bad Request")
        self.assertEqual(missing["error"]["details"][0]["field"], "name")
        self.assertEqual(extra_status, "400 Bad Request")
        self.assertEqual(extra["error"]["details"][0]["field"], "description")
        listed_status, listed = self.call("GET", "/v1/projects")
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(listed["items"], [])

    def test_list_orders_by_key_and_follows_the_cursor(self) -> None:
        """Pages are ordered by key. The cursor resumes after the last key shown."""
        self.call("POST", "/v1/projects", {"key": "gamma", "name": "Gamma"})
        self.call("POST", "/v1/projects", {"key": "alpha", "name": "Alpha"})
        self.call("POST", "/v1/projects", {"key": "beta", "name": "Beta"})
        first_status, first = self.call("GET", "/v1/projects", query="limit=2")
        self.assertEqual(first_status, "200 OK")
        self.assertEqual([item["key"] for item in first["items"]], ["alpha", "beta"])
        self.assertEqual(cursor_text(str(first["next_cursor"])), "v1:beta")
        second_query = urlencode({"limit": "2", "cursor": first["next_cursor"]})
        second_status, second = self.call("GET", "/v1/projects", query=second_query)
        self.assertEqual(second_status, "200 OK")
        self.assertEqual([item["key"] for item in second["items"]], ["gamma"])
        self.assertIsNone(second["next_cursor"])

    def test_limit_outside_the_cap_is_rejected(self) -> None:
        """A limit of 201 is 400. The service does not silently shorten the page."""
        status, body = self.call("GET", "/v1/projects", query="limit=201")
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body["error"]["code"], "validation_error")
        self.assertEqual(body["error"]["details"][0]["field"], "limit")

    def test_unknown_project_is_not_found(self) -> None:
        """A missing project and a key that cannot exist share one 404 message."""
        missing_status, missing = self.call("GET", "/v1/projects/shop")
        invalid_status, invalid = self.call("GET", "/v1/projects/Shop")
        self.assertEqual(missing_status, "404 Not Found")
        self.assertEqual(invalid_status, "404 Not Found")
        self.assertEqual(missing, invalid)
        self.assertEqual(missing["error"]["code"], "not_found")

    def test_wrong_method_on_a_project_is_rejected(self) -> None:
        """POST on one project is 405. PUT on the collection is 405."""
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        one_status, one = self.call("POST", "/v1/projects/shop", {"key": "shop", "name": "Shop"})
        collection_status, collection = self.call("PUT", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.assertEqual(one_status, "405 Method Not Allowed")
        self.assertEqual(one["error"]["code"], "validation_error")
        self.assertEqual(collection_status, "405 Method Not Allowed")

    def test_create_environment_audits_without_a_secret(self) -> None:
        """An environment response is the key and the name, and the audit matches."""
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        status, body = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        listed_status, listed = self.call("GET", "/v1/projects/shop/environments")
        self.assertEqual(status, "201 Created")
        self.assertEqual(body, {"key": "prod", "name": "Production"})
        self.assertNotIn("secret", body)
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(listed["items"], [body])
        self.assertIsNone(listed["next_cursor"])
        environment_audits = [row for row in self.audit_rows() if row["action"] == "environment.create"]
        self.assertEqual(len(environment_audits), 1)
        self.assertEqual(environment_audits[0]["project_key"], "shop")
        self.assertEqual(environment_audits[0]["environment_key"], "prod")
        self.assertIsNone(environment_audits[0]["flag_key"])
        self.assertNotIn(self.token, str(environment_audits[0]["after_json"]))

    def test_environment_key_is_unique_inside_one_project(self) -> None:
        """The same environment key is a conflict in one project and allowed in another."""
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.call("POST", "/v1/projects", {"key": "warehouse", "name": "Warehouse"})
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        conflict_status, conflict = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Again"},
        )
        other_status, other = self.call(
            "POST",
            "/v1/projects/warehouse/environments",
            {"key": "prod", "name": "Production"},
        )
        missing_status, missing = self.call(
            "POST",
            "/v1/projects/missing/environments",
            {"key": "prod", "name": "Production"},
        )
        self.assertEqual(conflict_status, "409 Conflict")
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(other_status, "201 Created")
        self.assertEqual(other["key"], "prod")
        self.assertEqual(missing_status, "404 Not Found")

    def test_new_environment_disables_existing_flags(self) -> None:
        """Each flag already in the project gets a disabled config. No flags means no rows."""
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.call("POST", "/v1/projects", {"key": "empty", "name": "Empty"})
        self.insert_flag("shop", "checkout-v2")
        self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production"},
        )
        self.call(
            "POST",
            "/v1/projects/empty/environments",
            {"key": "dev", "name": "Development"},
        )
        shop_configs = self.flag_configs("shop")
        empty_configs = self.flag_configs("empty")
        self.assertEqual(len(shop_configs), 1)
        self.assertEqual(shop_configs[0]["enabled"], 0)
        self.assertEqual(shop_configs[0]["off_variation"], 0)
        self.assertEqual(shop_configs[0]["fallthrough_json"], '{"variation":0}')
        self.assertEqual(shop_configs[0]["rules_json"], "[]")
        self.assertEqual(shop_configs[0]["targets_json"], "{}")
        self.assertEqual(shop_configs[0]["prerequisites_json"], "[]")
        self.assertEqual(shop_configs[0]["version"], 1)
        self.assertEqual(empty_configs, [])

    def test_bad_cursor_is_rejected(self) -> None:
        """A cursor that is not a previous page is 400."""
        status, body = self.call("GET", "/v1/projects", query="cursor=not-a-cursor")
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body["error"]["details"][0]["field"], "cursor")

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: str = "",
    ) -> tuple[str, dict]:
        """Invoke the WSGI app in-process.

        body is sent as JSON when present. query is the raw query string.
        Returns the status line and the JSON body. Does not bind a port.
        """
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line. Does not send bytes."""
            captured["status"] = status

        raw = b""
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
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
            "HTTP_AUTHORIZATION": f"Bearer {self.token}",
        }
        if body is not None:
            environ["CONTENT_TYPE"] = "application/json"
        payload = b"".join(self.application(environ, start_response))
        return str(captured["status"]), json.loads(payload.decode("utf-8"))

    def audit_rows(self) -> list[dict]:
        """Return every audit row. An empty log is an empty list."""
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT actor_kind, actor_id, action, project_key,
                       environment_key, flag_key, before_json, after_json
                  FROM audit_events
                 ORDER BY id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def insert_flag(self, project_key: str, flag_key: str) -> None:
        """Insert one flag row directly, because the flag API is a later phase.

        Commits the row. Does not create a configuration.
        """
        connection = self.database.connect()
        try:
            with connection.transaction():
                project = connection.execute(
                    "SELECT id FROM projects WHERE `key` = ?",
                    (project_key,),
                ).fetchone()
                assert project is not None
                connection.execute(
                    """
                    INSERT INTO flags (project_id, `key`, name, description)
                    VALUES (?, ?, ?, ?)
                    """,
                    (project["id"], flag_key, "Checkout", ""),
                )
        finally:
            connection.close()

    def flag_configs(self, project_key: str) -> list[dict]:
        """Return flag configs for project_key. None yet is an empty list."""
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT flag_configs.enabled,
                       flag_configs.off_variation,
                       flag_configs.fallthrough_json,
                       flag_configs.rules_json,
                       flag_configs.targets_json,
                       flag_configs.prerequisites_json,
                       flag_configs.version
                  FROM flag_configs
                  JOIN flags ON flags.id = flag_configs.flag_id
                  JOIN projects ON projects.id = flags.project_id
                 WHERE projects.`key` = ?
                """,
                (project_key,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]
