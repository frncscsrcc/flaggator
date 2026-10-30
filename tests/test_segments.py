"""Segments through the HTTP API, including target citations and the cache."""

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

_SEGMENTS = "/v1/projects/shop/environments/prod/segments"
_TARGET = "/v1/projects/shop/flags/checkout-v2/environments/prod"


class SegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        """Migrate a temporary database, issue a token, and create one project.

        The application uses a memory cache so a segment update can drop it.
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
        self.application = Application(self.database, EvaluationCache(self.memory, 30))
        status, _body = self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        self.assertEqual(status, "201 Created")

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_create_get_list_and_replace(self) -> None:
        """A segment is stored, listed, and replaced. The audit row has no secret."""
        self.seed()
        created_status, created = self.call(
            "POST",
            _SEGMENTS,
            {"key": "developers", "name": "Developers", "subjects": ["alice", "bob"]},
        )
        fetched_status, fetched = self.call("GET", _SEGMENTS + "/developers")
        listed_status, listed = self.call("GET", _SEGMENTS)
        replaced_status, replaced = self.call(
            "PUT",
            _SEGMENTS + "/developers",
            {"name": "Engineering", "subjects": ["alice"]},
        )
        audits = [row for row in self.audit_rows() if str(row["action"]).startswith("segment.")]
        self.assertEqual(created_status, "201 Created")
        self.assertEqual(created, {"key": "developers", "name": "Developers", "subjects": ["alice", "bob"]})
        self.assertEqual(fetched_status, "200 OK")
        self.assertEqual(fetched, created)
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(listed["items"], [created])
        self.assertIsNone(listed["next_cursor"])
        self.assertEqual(replaced_status, "200 OK")
        self.assertEqual(replaced["name"], "Engineering")
        self.assertEqual(replaced["subjects"], ["alice"])
        self.assertEqual([row["action"] for row in audits], ["segment.create", "segment.update"])
        self.assertEqual(audits[0]["environment_key"], "prod")
        self.assertIsNone(audits[0]["flag_key"])
        self.assertNotIn(self.token, str(audits[0]["after_json"]))

    def test_duplicate_key_and_a_bad_subject_are_rejected(self) -> None:
        """The same key is 409. An empty subject and a repeated subject are 400."""
        self.seed()
        self.call("POST", _SEGMENTS, {"key": "developers", "name": "Developers", "subjects": []})
        duplicate_status, duplicate = self.call(
            "POST",
            _SEGMENTS,
            {"key": "developers", "name": "Other", "subjects": []},
        )
        empty_status, empty = self.call(
            "POST",
            _SEGMENTS,
            {"key": "testers", "name": "Testers", "subjects": [""]},
        )
        repeated_status, repeated = self.call(
            "POST",
            _SEGMENTS,
            {"key": "testers", "name": "Testers", "subjects": ["alice", "alice"]},
        )
        self.assertEqual(duplicate_status, "409 Conflict")
        self.assertEqual(duplicate["error"]["message"], "Segment key already exists.")
        self.assertEqual(empty_status, "400 Bad Request")
        self.assertEqual(empty["error"]["details"][0]["message"], "Use a subject of 1 to 1024 characters.")
        self.assertEqual(repeated_status, "400 Bad Request")
        self.assertEqual(repeated["error"]["details"][0]["message"], "A segment must not repeat a subject.")

    def test_delete_refuses_a_cited_segment_and_removes_an_uncited_one(self) -> None:
        """A target that cites the key is 409. A subject string with the same text is not a citation."""
        self.seed()
        self.call("POST", _SEGMENTS, {"key": "developers", "name": "Developers", "subjects": ["alice"]})
        self.call("POST", _SEGMENTS, {"key": "testers", "name": "Testers", "subjects": []})
        self.put_targets({"1": ["developers"]})
        named_status, named = self.call("DELETE", _SEGMENTS + "/developers")
        self.call("POST", _SEGMENTS, {"key": "developers", "name": "Developers", "subjects": ["alice"]})
        self.put_targets({"1": {"subjects": [], "segments": ["developers"]}})
        cited_status, cited = self.call("DELETE", _SEGMENTS + "/developers")
        free_status, free = self.call("DELETE", _SEGMENTS + "/testers")
        missing_status, missing = self.call("GET", _SEGMENTS + "/testers")
        self.assertEqual(named_status, "204 No Content")
        self.assertEqual(named, {})
        self.assertEqual(cited_status, "409 Conflict")
        self.assertEqual(cited["error"]["message"], "Segment is still referenced.")
        self.assertEqual(free_status, "204 No Content")
        self.assertEqual(missing_status, "404 Not Found")
        self.assertEqual(missing["error"]["message"], "Segment does not exist.")

    def test_a_target_keeps_a_subject_list_and_can_cite_a_segment(self) -> None:
        """An array of subjects is stored as an array. A missing segment is 400.

        The same segment key on two variations is 400. evaluation-config
        returns the membership beside the flags.
        """
        self.seed()
        self.call("POST", _SEGMENTS, {"key": "developers", "name": "Developers", "subjects": ["bob"]})
        array_status, array_body = self.put_targets({"1": ["alice"]})
        missing_status, missing = self.put_targets({"1": {"subjects": [], "segments": ["missing"]}})
        shared_status, shared = self.put_targets(
            {
                "0": {"subjects": [], "segments": ["developers"]},
                "1": {"subjects": [], "segments": ["developers"]},
            }
        )
        cited_status, cited = self.put_targets({"1": {"subjects": ["alice"], "segments": ["developers"]}})
        _config_status, config = self.call("GET", "/v1/projects/shop/environments/prod/evaluation-config")
        alice = self.evaluate("alice")
        bob = self.evaluate("bob")
        self.assertEqual(array_status, "200 OK")
        self.assertEqual(array_body["targets"], {"1": ["alice"]})
        self.assertEqual(missing_status, "400 Bad Request")
        self.assertEqual(missing["error"]["details"][0]["message"], "Segment does not exist.")
        self.assertEqual(shared_status, "400 Bad Request")
        self.assertEqual(shared["error"]["details"][0]["message"], "Target lists must not share a segment.")
        self.assertEqual(cited_status, "200 OK")
        self.assertEqual(cited["targets"]["1"]["segments"], ["developers"])
        self.assertEqual(cited["targets"]["1"]["subjects"], ["alice"])
        self.assertEqual(config["segments"], {"developers": ["bob"]})
        self.assertEqual(alice["reason"], "target_match")
        self.assertEqual(alice["variation_index"], 1)
        self.assertEqual(bob["reason"], "target_match")
        self.assertEqual(bob["variation_index"], 1)

    def test_replacing_members_drops_the_cached_bundle(self) -> None:
        """After a membership change, evaluation uses the new subjects.

        A cached document without a segments map is reloaded from SQL.
        """
        self.seed()
        self.call("POST", _SEGMENTS, {"key": "developers", "name": "Developers", "subjects": ["alice"]})
        self.put_targets({"1": {"subjects": [], "segments": ["developers"]}})
        before = self.evaluate("alice")
        self.assertIsNotNone(self.memory.get(cache_key("shop", "prod")))
        replaced_status, _replaced = self.call(
            "PUT",
            _SEGMENTS + "/developers",
            {"name": "Developers", "subjects": ["bob"]},
        )
        self.assertIsNone(self.memory.get(cache_key("shop", "prod")))
        alice = self.evaluate("alice")
        bob = self.evaluate("bob")
        self.memory.set(
            cache_key("shop", "prod"),
            b'{"environment_key":"prod","flags":[],"project_key":"shop"}',
            30,
        )
        _status, config = self.call("GET", "/v1/projects/shop/environments/prod/evaluation-config")
        self.assertEqual(before["reason"], "target_match")
        self.assertEqual(replaced_status, "200 OK")
        self.assertEqual(alice["reason"], "fallthrough")
        self.assertEqual(bob["reason"], "target_match")
        self.assertEqual(config["segments"]["developers"], ["bob"])
        self.assertEqual(config["flags"][0]["key"], "checkout-v2")

    def test_an_sdk_token_cannot_create_a_segment(self) -> None:
        """An SDK token is 403 on the segment collection. DELETE of a missing method is 405."""
        self.seed()
        created_status, created = self.call(
            "POST",
            "/v1/projects/shop/environments/prod/sdk-tokens",
            {"name": "checkout-api"},
        )
        denied_status, denied = self.call(
            "POST",
            _SEGMENTS,
            {"key": "developers", "name": "Developers", "subjects": []},
            token=created["secret"],
        )
        method_status, method = self.call("DELETE", _SEGMENTS)
        self.assertEqual(created_status, "201 Created")
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertEqual(denied["error"]["message"], "The credential may not call this route.")
        self.assertEqual(method_status, "405 Method Not Allowed")
        self.assertIn("GET or POST", method["error"]["message"])

    def seed(self) -> None:
        """Create the prod environment and the checkout-v2 flag."""
        status, _body = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Prod"},
        )
        self.assertEqual(status, "201 Created")
        status, _body = self.call(
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

    def put_targets(self, targets: dict) -> tuple[str, dict]:
        """Replace prod targeting for checkout-v2 and return the status and body.

        The write sends the version just read. targets replaces that field.
        """
        _status, current = self.call("GET", _TARGET)
        return self.call(
            "PUT",
            _TARGET,
            {
                "enabled": True,
                "off_variation": 0,
                "fallthrough": {"variation": 0},
                "rules": [],
                "targets": targets,
                "prerequisites": [],
                "version": current["version"],
            },
        )

    def evaluate(self, subject: str) -> dict:
        """Return the decision for checkout-v2 and subject. Attributes are empty."""
        _status, body = self.call(
            "POST",
            "/v1/evaluate",
            {
                "project_key": "shop",
                "environment_key": "prod",
                "flag_key": "checkout-v2",
                "context": {"subject": subject, "attributes": {}},
                "default": False,
            },
        )
        return body

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
