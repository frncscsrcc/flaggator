"""Flags through the HTTP API."""

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
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase


class FlagTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database, issue a token, and create one project."""
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
        status, _body = self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.assertEqual(status, "201 Created")

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_create_writes_a_disabled_config_in_each_environment(self) -> None:
        """POST stores the variations and a disabled config for every environment.

        The audit row has the flag document and not the token.
        """
        self.call("POST", "/v1/projects/shop/environments", {"key": "dev", "name": "Dev"})
        self.call("POST", "/v1/projects/shop/environments", {"key": "prod", "name": "Prod"})
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
        self.assertEqual(status, "201 Created")
        self.assertEqual(body["key"], "checkout-v2")
        self.assertIs(body["variations"][0], False)
        self.assertIs(body["variations"][1], True)
        self.assertEqual([item["key"] for item in body["environments"]], ["dev", "prod"])
        for item in body["environments"]:
            self.assertIs(item["enabled"], False)
            self.assertEqual(item["off_variation"], 0)
            self.assertEqual(item["fallthrough"], {"variation": 0})
            self.assertEqual(item["rules"], [])
            self.assertEqual(item["targets"], {})
            self.assertEqual(item["prerequisites"], [])
            self.assertEqual(item["version"], 1)
        created = [row for row in self.audit_rows() if row["action"] == "flag.create"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["flag_key"], "checkout-v2")
        self.assertIsNone(created[0]["environment_key"])
        self.assertIsNone(created[0]["before_json"])
        self.assertEqual(
            json.loads(str(created[0]["after_json"]))["variations"],
            [False, True],
        )
        self.assertNotIn(self.token, str(created[0]["after_json"]))

    def test_create_without_environments_has_no_configs(self) -> None:
        """A flag in a project with no environments stores an empty config list."""
        status, body = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "New", "variations": [False, True]},
        )
        self.assertEqual(status, "201 Created")
        self.assertEqual(body["environments"], [])

    def test_duplicate_flag_is_a_conflict(self) -> None:
        """A second POST of the same key is 409 and does not add an audit row."""
        payload = {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False, True]}
        self.call("POST", "/v1/projects/shop/flags", payload)
        status, body = self.call("POST", "/v1/projects/shop/flags", payload)
        self.assertEqual(status, "409 Conflict")
        self.assertEqual(body["error"]["code"], "conflict")
        self.assertEqual(len([row for row in self.audit_rows() if row["action"] == "flag.create"]), 1)

    def test_create_rejects_a_short_list_and_null(self) -> None:
        """One variation, a null value, and a missing project are not stored."""
        short_status, short = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False]},
        )
        null_status, null_body = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False, None]},
        )
        missing_status, missing = self.call(
            "POST",
            "/v1/projects/missing/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False, True]},
        )
        self.assertEqual(short_status, "400 Bad Request")
        self.assertEqual(short["error"]["details"][0]["field"], "variations")
        self.assertEqual(null_status, "400 Bad Request")
        self.assertEqual(null_body["error"]["details"][0]["field"], "variations")
        self.assertEqual(missing_status, "404 Not Found")
        listed_status, listed = self.call("GET", "/v1/projects/shop/flags")
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(listed["items"], [])

    def test_get_returns_every_environment(self) -> None:
        """Fetch includes both configs. A missing flag and a bad key share one 404."""
        self.call("POST", "/v1/projects/shop/environments", {"key": "prod", "name": "Prod"})
        self.call("POST", "/v1/projects/shop/environments", {"key": "dev", "name": "Dev"})
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "New", "variations": [False, True]},
        )
        status, body = self.call("GET", "/v1/projects/shop/flags/checkout-v2")
        missing_status, missing = self.call("GET", "/v1/projects/shop/flags/other")
        invalid_status, invalid = self.call("GET", "/v1/projects/shop/flags/Checkout")
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["description"], "New")
        self.assertEqual([item["key"] for item in body["environments"]], ["dev", "prod"])
        self.assertEqual(missing_status, "404 Not Found")
        self.assertEqual(invalid_status, "404 Not Found")
        self.assertEqual(missing, invalid)

    def test_list_orders_by_key_and_can_include_one_environment(self) -> None:
        """Pages follow the key. The environment query adds that config only."""
        self.call("POST", "/v1/projects/shop/environments", {"key": "prod", "name": "Prod"})
        self.call("POST", "/v1/projects/shop/environments", {"key": "dev", "name": "Dev"})
        for key in ("gamma", "alpha", "beta"):
            self.call(
                "POST",
                "/v1/projects/shop/flags",
                {"key": key, "name": key, "description": "", "variations": [False, True]},
            )
        self.enable("alpha", "prod")
        first_status, first = self.call("GET", "/v1/projects/shop/flags", query="limit=2")
        self.assertEqual(first_status, "200 OK")
        self.assertEqual([item["key"] for item in first["items"]], ["alpha", "beta"])
        self.assertNotIn("environment", first["items"][0])
        second_query = urlencode({"limit": "2", "cursor": first["next_cursor"], "environment": "prod"})
        second_status, second = self.call("GET", "/v1/projects/shop/flags", query=second_query)
        self.assertEqual(second_status, "200 OK")
        self.assertEqual([item["key"] for item in second["items"]], ["gamma"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(
            second["items"][0]["environment"],
            {"key": "prod", "enabled": False, "off_variation": 0, "version": 1},
        )
        filtered_status, filtered = self.call("GET", "/v1/projects/shop/flags", query="environment=prod&limit=1")
        self.assertEqual(filtered_status, "200 OK")
        self.assertEqual(filtered["items"][0]["key"], "alpha")
        self.assertIs(filtered["items"][0]["environment"]["enabled"], True)
        self.assertEqual(filtered["items"][0]["environment"]["version"], 4)
        unknown_status, unknown = self.call("GET", "/v1/projects/shop/flags", query="environment=missing")
        self.assertEqual(unknown_status, "404 Not Found")
        self.assertEqual(unknown["error"]["code"], "not_found")

    def test_patch_appends_a_variation_without_moving_indexes(self) -> None:
        """Name and description change, the new value is last, and configs stay put."""
        self.call("POST", "/v1/projects/shop/environments", {"key": "prod", "name": "Prod"})
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "Old", "variations": [False, True]},
        )
        status, body = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"name": "Checkout v2", "description": "New", "variations": [False, True, "beta"]},
        )
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["name"], "Checkout v2")
        self.assertEqual(body["description"], "New")
        self.assertEqual(body["variations"], [False, True, "beta"])
        self.assertEqual(body["environments"][0]["off_variation"], 0)
        self.assertEqual(body["environments"][0]["fallthrough"], {"variation": 0})
        self.assertEqual(body["environments"][0]["version"], 1)
        patched = [row for row in self.audit_rows() if row["action"] == "flag.patch"]
        self.assertEqual(len(patched), 1)
        before = json.loads(str(patched[0]["before_json"]))
        after = json.loads(str(patched[0]["after_json"]))
        self.assertEqual(before["variations"], [False, True])
        self.assertEqual(after["variations"], [False, True, "beta"])
        self.assertNotIn(self.token, str(patched[0]["after_json"]))

    def test_patch_rejects_reorder_removal_and_a_key_change(self) -> None:
        """A different prefix, a shorter list, and a key are 400. The flag stays."""
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "Old", "variations": [False, True]},
        )
        reorder_status, reorder = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"variations": [True, False]},
        )
        remove_status, remove = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"variations": [False]},
        )
        key_status, key_body = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"key": "other", "name": "Other"},
        )
        self.assertEqual(reorder_status, "400 Bad Request")
        self.assertEqual(reorder["error"]["details"][0]["field"], "variations")
        self.assertEqual(remove_status, "400 Bad Request")
        self.assertEqual(key_status, "400 Bad Request")
        self.assertEqual(key_body["error"]["details"][0]["field"], "key")
        fetched_status, fetched = self.call("GET", "/v1/projects/shop/flags/checkout-v2")
        self.assertEqual(fetched_status, "200 OK")
        self.assertEqual(fetched["name"], "Checkout")
        self.assertEqual(fetched["variations"], [False, True])

    def test_variation_count_and_size_are_capped(self) -> None:
        """Twenty variations are stored. One more, or a 16 KiB value, is 400."""
        variations = [False] * 20
        created_status, created = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": variations},
        )
        extra = variations + ["more"]
        extra_status, extra_body = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"variations": extra},
        )
        huge_status, huge = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {
                "key": "other",
                "name": "Other",
                "description": "",
                "variations": ["x" * (16 * 1024), False],
            },
        )
        self.assertEqual(created_status, "201 Created")
        self.assertEqual(len(created["variations"]), 20)
        self.assertEqual(extra_status, "400 Bad Request")
        self.assertEqual(extra_body["error"]["details"][0]["field"], "variations")
        self.assertEqual(huge_status, "400 Bad Request")
        fetched_status, fetched = self.call("GET", "/v1/projects/shop/flags/checkout-v2")
        self.assertEqual(fetched_status, "200 OK")
        self.assertEqual(len(fetched["variations"]), 20)

    def test_new_environment_after_a_flag_starts_disabled(self) -> None:
        """Creating an environment inserts a disabled config for the flag that exists."""
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False, True]},
        )
        self.call("POST", "/v1/projects/shop/environments", {"key": "prod", "name": "Prod"})
        status, body = self.call("GET", "/v1/projects/shop/flags/checkout-v2")
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(body["environments"]), 1)
        self.assertEqual(body["environments"][0]["key"], "prod")
        self.assertIs(body["environments"][0]["enabled"], False)
        self.assertEqual(body["environments"][0]["off_variation"], 0)

    def test_wrong_method_on_flags_is_rejected(self) -> None:
        """PUT on the collection is 405. POST on one flag is 405."""
        self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": "checkout-v2", "name": "Checkout", "description": "", "variations": [False, True]},
        )
        collection_status, _collection = self.call("PUT", "/v1/projects/shop/flags", {"key": "no"})
        one_status, _one = self.call(
            "POST",
            "/v1/projects/shop/flags/checkout-v2",
            {"name": "No"},
        )
        self.assertEqual(collection_status, "405 Method Not Allowed")
        self.assertEqual(one_status, "405 Method Not Allowed")

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
                SELECT action, project_key, environment_key, flag_key, before_json, after_json
                  FROM audit_events
                 ORDER BY id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def enable(self, flag_key: str, environment_key: str) -> None:
        """Mark one config enabled at version 4, without using the targeting API.

        Commits the row. The flag and the environment must already exist.
        """
        connection = self.database.connect()
        try:
            with connection.transaction():
                connection.execute(
                    """
                    UPDATE flag_configs
                       SET enabled = 1,
                           version = 4
                     WHERE flag_id = (
                               SELECT flags.id
                                 FROM flags
                                 JOIN projects ON projects.id = flags.project_id
                                WHERE projects.`key` = ? AND flags.`key` = ?
                           )
                       AND environment_id = (
                               SELECT environments.id
                                 FROM environments
                                 JOIN projects ON projects.id = environments.project_id
                                WHERE projects.`key` = ? AND environments.`key` = ?
                           )
                    """,
                    ("shop", flag_key, "shop", environment_key),
                )
        finally:
            connection.close()
