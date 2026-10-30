"""SDK tokens, evaluation routes, and the evaluation cache."""

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
from repositories.eval_cache import EvaluationCache, cache_key
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase


class EvaluationTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database and issue a management token.

        The application uses a memory cache so a second evaluation can hit
        it. Does not bind a port.
        """
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
        self.memory = MemoryCache()
        self.application = Application(self.database, EvaluationCache(self.memory, 5))
        status, _body = self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.assertEqual(status, "201 Created")

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_evaluate_follows_the_worked_example(self) -> None:
        """POST /v1/evaluate serves the checkout-v2 table, including a missing flag.

        The missing flag is 200 with the caller default. evaluate-all lists
        real flags in key order and leaves unknown keys out.
        """
        self.seed()
        self.add_flag("alpha")
        saved = self.put_checkout()
        alice = self.evaluate("checkout-v2", "alice", {"country": "US"})
        italy = self.evaluate("checkout-v2", "bob", {"country": "IT"})
        other = self.evaluate("checkout-v2", "bob", {"country": "US"})
        missing_status, missing = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("missing", "bob", {}, default="fallback"),
        )
        all_status, all_body = self.call(
            "POST",
            "/v1/evaluate-all",
            {
                "project_key": "shop",
                "environment_key": "prod",
                "context": {"subject": "alice", "attributes": {"country": "US"}},
            },
        )
        self.assertEqual(alice["value"], True)
        self.assertEqual(alice["variation_index"], 1)
        self.assertEqual(alice["reason"], "target_match")
        self.assertIsNone(alice["rule_id"])
        self.assertEqual(alice["version"], saved["version"])
        self.assertEqual(italy["reason"], "rule_match")
        self.assertEqual(italy["rule_id"], saved["rules"][0]["id"])
        self.assertEqual(italy["variation_index"], 0)
        self.assertEqual(other["reason"], "fallthrough")
        self.assertEqual(other["variation_index"], 0)
        self.assertEqual(missing_status, "200 OK")
        self.assertEqual(missing["value"], "fallback")
        self.assertEqual(missing["reason"], "flag_not_found")
        self.assertIsNone(missing["variation_index"])
        self.assertIsNone(missing["version"])
        self.assertEqual(all_status, "200 OK")
        self.assertEqual([item["flag_key"] for item in all_body["decisions"]], ["alpha", "checkout-v2"])
        self.assertNotIn("missing", [item["flag_key"] for item in all_body["decisions"]])

    def test_sdk_token_uses_its_environment_and_revoke_rejects_it(self) -> None:
        """An SDK token evaluates its environment, and a mismatch is 403.

        The list and the audit row contain the prefix and not the secret.
        After revoke the same secret is 401.
        """
        self.seed()
        self.put_checkout()
        created_status, created = self.call(
            "POST",
            "/v1/projects/shop/environments/prod/sdk-tokens",
            {"name": "checkout-api"},
        )
        self.assertEqual(created_status, "201 Created")
        secret = created["secret"]
        self.assertEqual(created["name"], "checkout-api")
        self.assertEqual(created["prefix"], secret[:8])
        listed_status, listed = self.call("GET", "/v1/projects/shop/environments/prod/sdk-tokens")
        duplicate_status, duplicate = self.call(
            "POST",
            "/v1/projects/shop/environments/prod/sdk-tokens",
            {"name": "checkout-api"},
        )
        own_status, own = self.call(
            "POST",
            "/v1/evaluate",
            {
                "flag_key": "checkout-v2",
                "context": {"subject": "alice", "attributes": {}},
                "default": False,
            },
            token=secret,
        )
        mismatch_status, mismatch = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("checkout-v2", "alice", {}, environment_key="other"),
            token=secret,
        )
        missing_keys_status, missing_keys = self.call(
            "POST",
            "/v1/evaluate",
            {
                "flag_key": "checkout-v2",
                "context": {"subject": "alice", "attributes": {}},
                "default": False,
            },
        )
        config_status, config = self.call(
            "GET",
            "/v1/projects/shop/environments/prod/evaluation-config",
            token=secret,
        )
        other_status, other = self.call(
            "GET",
            "/v1/projects/shop/environments/dev/evaluation-config",
            token=secret,
        )
        self.add_environment("dev")
        other_real_status, _other_real = self.call(
            "GET",
            "/v1/projects/shop/environments/dev/evaluation-config",
            token=secret,
        )
        revoked_status, revoked = self.call(
            "DELETE",
            "/v1/projects/shop/environments/prod/sdk-tokens/" + str(created["id"]),
        )
        after_status, after = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("checkout-v2", "alice", {}),
            token=secret,
        )
        created_audit = [row for row in self.audit_rows() if row["action"] == "sdk_token.create"]
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(listed["items"][0]["name"], "checkout-api")
        self.assertEqual(listed["items"][0]["prefix"], created["prefix"])
        self.assertNotIn("secret", listed["items"][0])
        self.assertEqual(duplicate_status, "409 Conflict")
        self.assertEqual(duplicate["error"]["message"], "SDK token name already exists.")
        self.assertEqual(own_status, "200 OK")
        self.assertEqual(own["reason"], "target_match")
        self.assertEqual(mismatch_status, "403 Forbidden")
        self.assertEqual(mismatch["error"]["message"], "The credential is not allowed for this environment.")
        self.assertEqual(missing_keys_status, "400 Bad Request")
        self.assertEqual(config_status, "200 OK")
        self.assertEqual(config["flags"][0]["rules"][0]["serve"]["rollout"]["salt"], "s1")
        self.assertNotIn("secret", json.dumps(config))
        self.assertEqual(other_status, "403 Forbidden")
        self.assertEqual(other_real_status, "403 Forbidden")
        self.assertEqual(revoked_status, "204 No Content")
        self.assertEqual(revoked, {})
        self.assertEqual(after_status, "401 Unauthorized")
        self.assertEqual(len(created_audit), 1)
        self.assertNotIn(secret, created_audit[0]["after_json"])
        self.assertIn(created["prefix"], created_audit[0]["after_json"])

    def test_context_over_the_caps_is_rejected(self) -> None:
        """Too many attributes, an empty subject, and a null value are 400."""
        self.seed()
        attributes = {}
        index = 0
        while index < 51:
            attributes["a" + str(index)] = index
            index = index + 1
        too_many_status, _too_many = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("checkout-v2", "bob", attributes),
        )
        empty_subject_status, _empty_subject = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("checkout-v2", "", {}),
        )
        null_status, null_body = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body("checkout-v2", "bob", {"country": None}),
        )
        self.assertEqual(too_many_status, "400 Bad Request")
        self.assertEqual(empty_subject_status, "400 Bad Request")
        self.assertEqual(null_status, "400 Bad Request")
        self.assertEqual(null_body["error"]["code"], "validation_error")

    def test_a_cached_bundle_survives_a_raw_sql_change_until_a_write(self) -> None:
        """Evaluation reads the cache. Management targeting reads SQL.

        A direct update does not delete the cache, so the next evaluation
        still returns the previous decision. A name-only patch also leaves
        the cache. PUT targeting deletes it after commit, and the following
        evaluation sees the new document.
        """
        self.seed()
        saved = self.put_checkout()
        first = self.evaluate("checkout-v2", "bob", {"country": "US"})
        self.set_enabled(0)
        targeting_status, targeting = self.call(
            "GET",
            "/v1/projects/shop/flags/checkout-v2/environments/prod",
        )
        cached = self.evaluate("checkout-v2", "bob", {"country": "US"})
        renamed_status, _renamed = self.call(
            "PATCH",
            "/v1/projects/shop/flags/checkout-v2",
            {"name": "Checkout renamed"},
        )
        after_rename = self.evaluate("checkout-v2", "bob", {"country": "US"})
        replaced_status, _replaced = self.call(
            "PUT",
            "/v1/projects/shop/flags/checkout-v2/environments/prod",
            {
                "enabled": False,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [],
                "targets": {},
                "prerequisites": [],
                "version": saved["version"],
            },
        )
        fresh = self.evaluate("checkout-v2", "bob", {"country": "US"})
        self.assertEqual(first["reason"], "fallthrough")
        self.assertEqual(first["value"], False)
        self.assertEqual(targeting_status, "200 OK")
        self.assertIs(targeting["enabled"], False)
        self.assertEqual(cached["reason"], "fallthrough")
        self.assertEqual(cached["value"], False)
        self.assertEqual(renamed_status, "200 OK")
        self.assertEqual(after_rename["reason"], "fallthrough")
        self.assertEqual(replaced_status, "200 OK")
        self.assertEqual(fresh["reason"], "off")
        self.assertIs(fresh["value"], False)

    def test_creating_a_flag_drops_every_environment_bundle(self) -> None:
        """A new flag is visible on the next evaluate-all.

        The first evaluate-all stores the bundle. Creating a flag deletes
        that entry after commit.
        """
        self.seed()
        self.put_checkout()
        before_status, before = self.call(
            "POST",
            "/v1/evaluate-all",
            {
                "project_key": "shop",
                "environment_key": "prod",
                "context": {"subject": "bob", "attributes": {}},
            },
        )
        self.add_flag("alpha")
        after_status, after = self.call(
            "POST",
            "/v1/evaluate-all",
            {
                "project_key": "shop",
                "environment_key": "prod",
                "context": {"subject": "bob", "attributes": {}},
            },
        )
        self.assertEqual(before_status, "200 OK")
        self.assertEqual([item["flag_key"] for item in before["decisions"]], ["checkout-v2"])
        self.assertEqual(after_status, "200 OK")
        self.assertEqual([item["flag_key"] for item in after["decisions"]], ["alpha", "checkout-v2"])

    def test_a_bundle_over_one_mebibyte_is_not_stored(self) -> None:
        """A document larger than 1 MiB is returned and left out of the cache.

        A small document is stored under the evaluation key.
        """
        self.seed()
        small = self.load_bundle()
        self.assertIsNotNone(self.memory.get(cache_key("shop", "prod")))
        self.assertEqual(small["project_key"], "shop")
        self.memory.delete(cache_key("shop", "prod"))
        self.store_huge_variation()
        large = self.load_bundle()
        self.assertIsNone(self.memory.get(cache_key("shop", "prod")))
        self.assertEqual(large["flags"][0]["key"], "checkout-v2")

    def seed(self) -> None:
        """Create prod and the checkout-v2 flag. Does not enable it."""
        self.add_environment("prod")
        self.add_flag("checkout-v2")

    def add_environment(self, key: str) -> None:
        """Create an environment in shop."""
        status, _body = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": key, "name": key},
        )
        self.assertEqual(status, "201 Created")

    def add_flag(self, key: str) -> None:
        """Create a flag in shop with variations false and true."""
        status, _body = self.call(
            "POST",
            "/v1/projects/shop/flags",
            {"key": key, "name": key, "description": "", "variations": [False, True]},
        )
        self.assertEqual(status, "201 Created")

    def put_checkout(self) -> dict:
        """Store the worked-example targeting and return the saved document.

        The rule id is assigned by the service. The rollout salt is s1.
        """
        status, body = self.call(
            "PUT",
            "/v1/projects/shop/flags/checkout-v2/environments/prod",
            {
                "enabled": True,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [
                    {
                        "clauses": [
                            {"attribute": "country", "operator": "in", "values": ["IT", "FR"]}
                        ],
                        "serve": {
                            "rollout": {
                                "salt": "s1",
                                "variations": [
                                    {"variation_index": 1, "weight": 10000},
                                    {"variation_index": 0, "weight": 90000},
                                ],
                            }
                        },
                    }
                ],
                "targets": {"1": ["alice"]},
                "prerequisites": [],
                "version": 1,
            },
        )
        self.assertEqual(status, "200 OK")
        return body

    def evaluate(self, flag_key: str, subject: str, attributes: dict) -> dict:
        """Evaluate one flag with the management token and return the decision.

        Raises AssertionError when the status is not 200.
        """
        status, body = self.call(
            "POST",
            "/v1/evaluate",
            self.evaluate_body(flag_key, subject, attributes),
        )
        self.assertEqual(status, "200 OK")
        return body

    def evaluate_body(
        self,
        flag_key: str,
        subject: str,
        attributes: dict,
        default: object = False,
        environment_key: str = "prod",
    ) -> dict:
        """Return an evaluate-one body for shop and environment_key.

        Does not call the API.
        """
        return {
            "project_key": "shop",
            "environment_key": environment_key,
            "flag_key": flag_key,
            "context": {"subject": subject, "attributes": attributes},
            "default": default,
        }

    def set_enabled(self, enabled: int) -> None:
        """Set checkout-v2 in prod to enabled without going through a service.

        This does not register a cache delete. Does not change the version.
        """
        connection = self.database.connect()
        try:
            connection.execute(
                """
                UPDATE flag_configs
                   SET enabled = ?
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
                (enabled, "shop", "checkout-v2", "shop", "prod"),
            )
        finally:
            connection.close()

    def load_bundle(self) -> dict:
        """Load the prod bundle through the evaluation cache.

        Uses the ids stored for shop and prod. Does not evaluate.
        """
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT projects.id AS project_id,
                       environments.id AS environment_id
                  FROM environments
                  JOIN projects ON projects.id = environments.project_id
                 WHERE projects.`key` = ? AND environments.`key` = ?
                """,
                ("shop", "prod"),
            ).fetchone()
            self.assertIsNotNone(row)
            assert row is not None
            cache = EvaluationCache(self.memory, 5)
            return cache.load_document(
                connection,
                int(row["project_id"]),
                int(row["environment_id"]),
                "shop",
                "prod",
            )
        finally:
            connection.close()

    def store_huge_variation(self) -> None:
        """Replace the first checkout variation with a string over 1 MiB.

        The write skips validation so the bundle exceeds the cache cap.
        """
        payload = json.dumps("x" * (1024 * 1024))
        connection = self.database.connect()
        try:
            connection.execute(
                """
                UPDATE flag_variations
                   SET value_json = ?
                 WHERE variation_index = 0
                   AND flag_id = (
                           SELECT flags.id
                             FROM flags
                             JOIN projects ON projects.id = flags.project_id
                            WHERE projects.`key` = ? AND flags.`key` = ?
                       )
                """,
                (payload, "shop", "checkout-v2"),
            )
        finally:
            connection.close()

    def call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> tuple[str, dict]:
        """Invoke the WSGI app in-process.

        body is sent as JSON when present. token defaults to the management
        token. Returns the status line and the JSON body. Does not bind a port.
        """
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line. Does not send bytes."""
            captured["status"] = status

        raw = b""
        if body is not None:
            raw = json.dumps(body).encode("utf-8")
        bearer = self.token if token is None else token
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
            "HTTP_AUTHORIZATION": "Bearer " + bearer,
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
