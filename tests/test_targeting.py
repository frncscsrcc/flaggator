"""Targeting through the HTTP API."""

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
from composition import bootstrap_management_token, load_settings
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase

_PATH = "/v1/projects/shop/flags/checkout-v2/environments/prod"


class TargetingTests(unittest.TestCase):
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

    def test_get_returns_the_disabled_document(self) -> None:
        """A new flag is off, at version 1, with an empty rule tree."""
        self.seed()
        status, body = self.call("GET", _PATH)
        self.assertEqual(status, "200 OK")
        self.assertEqual(
            body,
            {
                "enabled": False,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [],
                "targets": {},
                "prerequisites": [],
                "version": 1,
            },
        )

    def test_put_replaces_the_document_and_audits(self) -> None:
        """PUT stores the document, increments version, and leaves other environments alone.

        The audit row names the flag and the environment and does not contain
        the token.
        """
        self.seed()
        self.add_environment("dev")
        status, body = self.call("PUT", _PATH, self.document(1, targets={"1": ["alice"]}))
        self.assertEqual(status, "200 OK")
        self.assertIs(body["enabled"], True)
        self.assertEqual(body["targets"], {"1": ["alice"]})
        self.assertEqual(body["version"], 2)
        self.assertNotIn("key", body)
        again_status, again = self.call("GET", _PATH)
        self.assertEqual(again_status, "200 OK")
        self.assertEqual(again, body)
        flag_status, flag = self.call("GET", "/v1/projects/shop/flags/checkout-v2")
        self.assertEqual(flag_status, "200 OK")
        by_key = {item["key"]: item for item in flag["environments"]}
        self.assertEqual(by_key["prod"]["version"], 2)
        self.assertEqual(by_key["prod"]["targets"], {"1": ["alice"]})
        self.assertIs(by_key["dev"]["enabled"], False)
        self.assertEqual(by_key["dev"]["version"], 1)
        replaced = [row for row in self.audit_rows() if row["action"] == "targeting.replace"]
        self.assertEqual(len(replaced), 1)
        self.assertEqual(replaced[0]["environment_key"], "prod")
        self.assertEqual(replaced[0]["flag_key"], "checkout-v2")
        self.assertEqual(json.loads(str(replaced[0]["before_json"]))["version"], 1)
        self.assertEqual(json.loads(str(replaced[0]["after_json"]))["version"], 2)
        self.assertNotIn(self.token, str(replaced[0]["after_json"]))

    def test_stale_version_does_not_write(self) -> None:
        """A second PUT with the old version is 409 and keeps the stored document."""
        self.seed()
        first_status, first = self.call("PUT", _PATH, self.document(1))
        self.assertEqual(first_status, "200 OK")
        stale_status, stale = self.call("PUT", _PATH, self.document(1, enabled=False))
        self.assertEqual(stale_status, "409 Conflict")
        self.assertEqual(stale["error"]["code"], "conflict")
        self.assertEqual(stale["error"]["message"], "The configuration version is stale.")
        _status, body = self.call("GET", _PATH)
        self.assertEqual(body["version"], first["version"])
        self.assertIs(body["enabled"], True)
        self.assertEqual(len([row for row in self.audit_rows() if row["action"] == "targeting.replace"]), 1)

    def test_salt_is_assigned_and_kept_when_omitted(self) -> None:
        """A new rollout gets a salt. Omitting it on a later write keeps it.

        Sending a different salt replaces it. Echoing the rule id keeps the rule.
        """
        self.seed()
        status, saved = self.call("PUT", _PATH, self.rollout_document(1))
        self.assertEqual(status, "200 OK")
        rule_id = saved["rules"][0]["id"]
        rule_salt = saved["rules"][0]["serve"]["rollout"]["salt"]
        fallthrough_salt = saved["fallthrough"]["rollout"]["salt"]
        self.assertIsInstance(rule_id, str)
        self.assertNotEqual(rule_id, "")
        self.assertNotEqual(rule_salt, "")
        self.assertNotEqual(fallthrough_salt, "")
        changed = self.rollout_document(saved["version"], rule_id=rule_id)
        changed["rules"][0]["serve"]["rollout"]["variations"] = [
            {"variation_index": 1, "weight": 20000},
            {"variation_index": 0, "weight": 80000},
        ]
        kept_status, kept = self.call("PUT", _PATH, changed)
        self.assertEqual(kept_status, "200 OK")
        self.assertEqual(kept["rules"][0]["id"], rule_id)
        self.assertEqual(kept["rules"][0]["serve"]["rollout"]["salt"], rule_salt)
        self.assertEqual(kept["fallthrough"]["rollout"]["salt"], fallthrough_salt)
        self.assertEqual(kept["rules"][0]["serve"]["rollout"]["variations"][0]["weight"], 20000)
        reshuffled = self.rollout_document(kept["version"], rule_id=rule_id, rule_salt="reshuffle")
        reshuffle_status, reshuffle = self.call("PUT", _PATH, reshuffled)
        self.assertEqual(reshuffle_status, "200 OK")
        self.assertEqual(reshuffle["rules"][0]["serve"]["rollout"]["salt"], "reshuffle")
        self.assertEqual(reshuffle["fallthrough"]["rollout"]["salt"], fallthrough_salt)

    def test_a_client_cannot_choose_a_new_rule_id(self) -> None:
        """An id that is not already stored is 400, including the first write.

        Two rules cannot share one stored id.
        """
        self.seed()
        chosen = self.rollout_document(1)
        chosen["rules"][0]["id"] = "r1"
        status, body = self.call("PUT", _PATH, chosen)
        self.assertEqual(status, "400 Bad Request")
        self.assertEqual(body["error"]["details"][0]["field"], "rules")
        _get_status, current = self.call("GET", _PATH)
        self.assertEqual(current["version"], 1)
        created_status, created = self.call(
            "PUT",
            _PATH,
            {
                "enabled": True,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [
                    {"clauses": [], "serve": {"variation": 0}},
                    {"clauses": [], "serve": {"variation": 1}},
                ],
                "targets": {},
                "prerequisites": [],
                "version": 1,
            },
        )
        self.assertEqual(created_status, "200 OK")
        first_id = created["rules"][0]["id"]
        duplicate_status, _duplicate = self.call(
            "PUT",
            _PATH,
            {
                "enabled": True,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [
                    {"id": first_id, "clauses": [], "serve": {"variation": 0}},
                    {"id": first_id, "clauses": [], "serve": {"variation": 1}},
                ],
                "targets": {},
                "prerequisites": [],
                "version": created["version"],
            },
        )
        self.assertEqual(duplicate_status, "400 Bad Request")
        _status, after = self.call("GET", _PATH)
        self.assertEqual(len(after["rules"]), 2)
        self.assertNotEqual(after["rules"][0]["id"], after["rules"][1]["id"])

    def test_rejects_invalid_rules_targets_and_weights(self) -> None:
        """Indexes, operators, shared targets, and weight sums are not stored."""
        self.seed()
        rejected = [
            self.document(1, off_variation=3),
            self.document(1, fallthrough={"variation": 4}),
            self.document(1, targets={"0": ["alice"], "1": ["alice"]}),
            self.document(1, targets={"0": ["alice", "alice"]}),
            self.document(1, targets={"01": ["alice"]}),
            self.document(1, targets={"0": [""]}),
            self.document(1, rules=[self.clause_rule("eq", ["IT", "FR"])]),
            self.document(1, rules=[self.clause_rule("in", [])]),
            self.document(1, rules=[self.clause_rule("gt", ["1"])]),
            self.document(1, rules=[self.clause_rule("regex", ["IT"])]),
            self.document(
                1,
                fallthrough={
                    "rollout": {
                        "variations": [
                            {"variation_index": 0, "weight": 10},
                            {"variation_index": 1, "weight": 10},
                        ]
                    }
                },
            ),
            self.document(1, fallthrough={"rollout": {"salt": "", "variations": []}}),
            self.document(1, key="prod"),
        ]
        for payload in rejected:
            status, body = self.call("PUT", _PATH, payload)
            self.assertEqual(status, "400 Bad Request", payload)
            self.assertEqual(body["error"]["code"], "validation_error")
        _status, current = self.call("GET", _PATH)
        self.assertEqual(current["version"], 1)

    def test_prerequisite_cycles_are_per_environment(self) -> None:
        """A cycle in one environment does not use another environment's lists.

        A flag that requires itself is a cycle. A missing prerequisite flag
        is 400.
        """
        self.seed()
        self.add_environment("dev")
        self.add_flag("payments")
        dev_payments = "/v1/projects/shop/flags/payments/environments/dev"
        prod_payments = "/v1/projects/shop/flags/payments/environments/prod"
        dev_status, _dev = self.call(
            "PUT",
            dev_payments,
            self.document(1, prerequisites=[{"flag_key": "checkout-v2", "variation_index": 0}]),
        )
        self.assertEqual(dev_status, "200 OK")
        prod_status, prod = self.call(
            "PUT",
            _PATH,
            self.document(1, prerequisites=[{"flag_key": "payments", "variation_index": 0}]),
        )
        self.assertEqual(prod_status, "200 OK")
        cycle_status, cycle = self.call(
            "PUT",
            prod_payments,
            self.document(1, prerequisites=[{"flag_key": "checkout-v2", "variation_index": 0}]),
        )
        self.assertEqual(cycle_status, "400 Bad Request")
        self.assertEqual(cycle["error"]["details"][0]["field"], "prerequisites")
        _payments_status, payments = self.call("GET", prod_payments)
        self.assertEqual(payments["version"], 1)
        self_status, _self_body = self.call(
            "PUT",
            _PATH,
            self.document(
                prod["version"],
                prerequisites=[{"flag_key": "checkout-v2", "variation_index": 0}],
            ),
        )
        self.assertEqual(self_status, "400 Bad Request")
        missing_status, missing = self.call(
            "PUT",
            _PATH,
            self.document(
                prod["version"],
                prerequisites=[{"flag_key": "missing", "variation_index": 0}],
            ),
        )
        self.assertEqual(missing_status, "400 Bad Request")
        self.assertEqual(missing["error"]["code"], "validation_error")
        index_status, _index = self.call(
            "PUT",
            _PATH,
            self.document(
                prod["version"],
                prerequisites=[{"flag_key": "payments", "variation_index": 5}],
            ),
        )
        self.assertEqual(index_status, "400 Bad Request")
        _status, checkout = self.call("GET", _PATH)
        self.assertEqual(checkout["prerequisites"], [{"flag_key": "payments", "variation_index": 0}])

    def test_rejects_documents_over_the_caps(self) -> None:
        """Rules, clauses, values, targets, and attribute names stop at the security caps.

        A document on the cap is stored. One past the cap is not.
        """
        self.seed()
        over = [
            self.document(1, rules=[{"clauses": [], "serve": {"variation": 0}} for _index in range(101)]),
            self.document(1, rules=[{"clauses": [self.eq_clause("country")] * 21, "serve": {"variation": 0}}]),
            self.document(1, rules=[self.clause_rule("in", [str(index) for index in range(51)])]),
            self.document(1, targets={"0": [f"user-{index}" for index in range(1001)]}),
            self.document(1, rules=[self.clause_rule("eq", ["x"], attribute="a" * 65)]),
        ]
        for payload in over:
            status, _body = self.call("PUT", _PATH, payload)
            self.assertEqual(status, "400 Bad Request", payload)
        accepted_status, accepted = self.call(
            "PUT",
            _PATH,
            self.document(1, rules=[{"clauses": [], "serve": {"variation": 0}} for _index in range(100)]),
        )
        self.assertEqual(accepted_status, "200 OK")
        self.assertEqual(len(accepted["rules"]), 100)
        boundary = self.document(
            accepted["version"],
            rules=[
                {
                    "clauses": [self.eq_clause("a" * 64) for _index in range(19)]
                    + [self.clause("in", [str(index) for index in range(50)])],
                    "serve": {"variation": 0},
                }
            ],
        )
        boundary_status, boundary_body = self.call("PUT", _PATH, boundary)
        self.assertEqual(boundary_status, "200 OK")
        self.assertEqual(len(boundary_body["rules"][0]["clauses"]), 20)
        self.assertEqual(len(boundary_body["rules"][0]["clauses"][19]["values"]), 50)

    def test_missing_keys_and_the_wrong_method(self) -> None:
        """An unknown project, flag, or environment is the same 404. POST is 405."""
        self.seed()
        missing_project, project_body = self.call("GET", "/v1/projects/missing/flags/checkout-v2/environments/prod")
        missing_flag, flag_body = self.call("GET", "/v1/projects/shop/flags/missing/environments/prod")
        bad_flag, bad_flag_body = self.call("GET", "/v1/projects/shop/flags/Checkout/environments/prod")
        missing_environment, environment_body = self.call(
            "GET",
            "/v1/projects/shop/flags/checkout-v2/environments/missing",
        )
        method_status, method_body = self.call("POST", _PATH, self.document(1))
        self.assertEqual(missing_project, "404 Not Found")
        self.assertEqual(project_body["error"]["message"], "Project does not exist.")
        self.assertEqual(missing_flag, "404 Not Found")
        self.assertEqual(flag_body["error"]["message"], "Flag does not exist.")
        self.assertEqual(bad_flag, "404 Not Found")
        self.assertEqual(bad_flag_body["error"]["message"], "Flag does not exist.")
        self.assertEqual(missing_environment, "404 Not Found")
        self.assertEqual(environment_body["error"]["message"], "Environment does not exist.")
        self.assertEqual(method_status, "405 Method Not Allowed")
        self.assertIn("GET or PUT", method_body["error"]["message"])

    def document(self, version: int, **overrides: object) -> dict:
        """Return a valid targeting body, with overrides applied.

        version is the version the write claims to have read. overrides
        replace top-level fields. Does not call the API.
        """
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

    def rollout_document(self, version: int, rule_id: str | None = None, rule_salt: str | None = None) -> dict:
        """Return a body with a fallthrough rollout and one rule rollout.

        rule_id and rule_salt are omitted when None, which is how a new rule
        asks the service to assign them. Does not call the API.
        """
        rule: dict = {
            "clauses": [{"attribute": "country", "operator": "in", "values": ["IT", "FR"]}],
            "serve": {
                "rollout": {
                    "variations": [
                        {"variation_index": 1, "weight": 10000},
                        {"variation_index": 0, "weight": 90000},
                    ]
                }
            },
        }
        if rule_id is not None:
            rule["id"] = rule_id
        if rule_salt is not None:
            rule["serve"]["rollout"]["salt"] = rule_salt
        return self.document(
            version,
            fallthrough={
                "rollout": {
                    "variations": [
                        {"variation_index": 0, "weight": 50000},
                        {"variation_index": 1, "weight": 50000},
                    ]
                }
            },
            rules=[rule],
            targets={"1": ["alice"]},
        )

    def clause_rule(self, operator: str, values: list, attribute: str = "country") -> dict:
        """Return one rule that serves variation 0 when the clause matches.

        Does not call the API.
        """
        return {"clauses": [self.clause(operator, values, attribute)], "serve": {"variation": 0}}

    def eq_clause(self, attribute: str) -> dict:
        """Return an eq clause comparing attribute to the string x.

        Does not call the API.
        """
        return self.clause("eq", ["x"], attribute)

    def clause(self, operator: str, values: list, attribute: str = "country") -> dict:
        """Return one clause. Does not call the API."""
        return {"attribute": attribute, "operator": operator, "values": values}

    def seed(self) -> None:
        """Create the prod environment and the checkout-v2 flag."""
        self.add_environment("prod")
        self.add_flag("checkout-v2")

    def add_environment(self, key: str) -> None:
        """Create an environment in shop. key is its public key."""
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
