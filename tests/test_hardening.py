"""Controls from the 2026-10-31 security audit."""

import io
import ipaddress
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.http import (
    Application,
    ThreadingWSGIServer,
    _MAX_READERS_PER_ADDRESS,
    _REQUEST_DEADLINE_SECONDS,
    request_from_environ,
)
from api.login_limit import LoginLimiter
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
        """When every work slot is taken, the response is 503.

        The slot is taken after the body has been read, so this calls the
        application directly. A half-open connection does not use the slot.
        """
        application = Application(None)
        application._in_flight = threading.BoundedSemaphore(0)
        captured: dict[str, str] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line. Does not send bytes."""
            captured["status"] = status

        body = b"".join(
            application(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": "/health",
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
                },
                start_response,
            )
        )
        self.assertIn("503", captured["status"])
        self.assertIn(b"unavailable", body)

    def test_reader_limits_and_the_accept_deadline(self) -> None:
        """Fifteen half-open connections leave /health at 200. The seventeenth is 503.

        The production deadline is 30 seconds from accept. This server uses
        a short one so the test can watch the sockets close. A trusted proxy
        is exempt from the per-address cap, and a global cap of zero refuses
        the next reader.
        """
        self.assertEqual(_REQUEST_DEADLINE_SECONDS, 30)
        application = Application(None)
        server = make_server(
            "127.0.0.1",
            0,
            application,
            server_class=ThreadingWSGIServer,
        )
        server.request_deadline_seconds = 2.0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        holders: list[socket.socket] = []
        try:
            for _index in range(15):
                holders.append(_partial_connection(host, port))
            _wait_for(lambda: server._readers_by_address.get("127.0.0.1", 0) >= 15)
            health = _exchange(host, port, b"GET /health HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
            self.assertIn(b"200", health)
            _wait_for(lambda: server._readers_by_address.get("127.0.0.1", 0) == 15)
            holders.append(_partial_connection(host, port))
            _wait_for(lambda: server._readers_by_address.get("127.0.0.1", 0) >= 16)
            denied = _read_refusal(host, port)
            self.assertIn(b"503", denied)
            self.assertEqual(len(holders), _MAX_READERS_PER_ADDRESS)
            time.sleep(2.5)
            for held in holders:
                held.settimeout(1)
                self.assertEqual(held.recv(64), b"")
        finally:
            for held in holders:
                held.close()
            server.shutdown()
            server.server_close()

    def test_a_trusted_proxy_is_exempt_and_the_global_cap_is_not(self) -> None:
        """Seventeen readers from a trusted peer are accepted. A zero global cap is 503."""
        application = Application(None)
        application.trusted_proxies = (ipaddress.ip_network("10.1.0.9/32"),)
        server = make_server(
            "127.0.0.1",
            0,
            application,
            server_class=ThreadingWSGIServer,
        )
        server.request_deadline_seconds = 5
        pairs: list[tuple[socket.socket, socket.socket]] = []
        try:
            for index in range(17):
                left, right = socket.socketpair()
                right.settimeout(0.2)
                pairs.append((left, right))
                server.process_request(left, ("10.1.0.9", index))
            for _left, right in pairs:
                with self.assertRaises(TimeoutError):
                    right.recv(32)
        finally:
            for left, right in pairs:
                right.close()
                left.close()
            server.server_close()
        blocked = make_server(
            "127.0.0.1",
            0,
            Application(None),
            server_class=ThreadingWSGIServer,
        )
        blocked._readers = threading.BoundedSemaphore(0)
        left, right = socket.socketpair()
        try:
            blocked.process_request(left, ("192.0.2.1", 1))
            data = right.recv(4096)
        finally:
            right.close()
            blocked.server_close()
        self.assertIn(b"503", data)


class ClientAddressTests(unittest.TestCase):
    def test_forwarded_for_is_used_only_from_a_trusted_peer(self) -> None:
        """An untrusted peer is counted itself. A trusted peer yields the client hop.

        A hop that is not an IP, and a missing header, share the bucket
        unknown. Two IPv6 addresses in one /64 share a bucket.
        """
        trusted = (ipaddress.ip_network("10.0.0.1/32"),)
        direct = request_from_environ(
            _bare_environ("192.0.2.5", "203.0.113.8"),
            trusted,
        )
        forwarded = request_from_environ(
            _bare_environ("10.0.0.1", "203.0.113.8, 10.0.0.1"),
            trusted,
        )
        garbage = request_from_environ(_bare_environ("10.0.0.1", "not-an-ip"), trusted)
        missing = request_from_environ(_bare_environ("10.0.0.1", None), trusted)
        first = request_from_environ(_bare_environ("2001:db8:1:2::10", None), ())
        second = request_from_environ(_bare_environ("2001:db8:1:2::99", None), ())
        self.assertEqual(direct.client_address, "192.0.2.5")
        self.assertEqual(forwarded.client_address, "203.0.113.8")
        self.assertEqual(garbage.client_address, "unknown")
        self.assertEqual(missing.client_address, "unknown")
        self.assertEqual(first.client_address, second.client_address)
        self.assertEqual(first.client_address, "2001:db8:1:2::")

    def test_a_full_map_keeps_the_new_key(self) -> None:
        """Past the cap the quietest key is dropped and the new key stays."""
        clock = {"now": 0.0}

        def tick() -> float:
            """Return the test clock. Does not read the database."""
            return clock["now"]

        limiter = LoginLimiter(limit=10, window_seconds=60, clock=tick)
        with patch("api.login_limit._MAX_KEYS", 2):
            clock["now"] = 1
            self.assertTrue(limiter.allow("oldest"))
            clock["now"] = 2
            self.assertTrue(limiter.allow("middle"))
            clock["now"] = 3
            self.assertTrue(limiter.allow("newest"))
        self.assertNotIn("oldest", limiter._hits)
        self.assertIn("newest", limiter._hits)
        clock["now"] = 120
        self.assertFalse(limiter.over_limit("middle"))
        self.assertNotIn("middle", limiter._hits)


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
            {"email": "ops@example.com", "password": "password-ok-1"},
            authorization=None,
            remote="10.0.0.8",
        )
        self.assertEqual(first_status, "401 Unauthorized")
        self.assertEqual(first["error"]["code"], "unauthenticated")
        self.assertEqual(limited_status, "429 Too Many Requests")
        self.assertEqual(limited["error"]["code"], "rate_limited")

    def test_failed_logins_are_limited_per_email(self) -> None:
        """Ten failures for one email close that login from every address.

        A success is not a failure. An email the domain rejects does not
        enter the map. A different email still answers 401.
        """
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "ops@example.com",
                "name": "Ops",
                "password": "password-ok-1",
                "roles": ["reader"],
            },
        )
        for index in range(9):
            status, _body = self.call(
                "POST",
                "/v1/auth/login",
                {"email": "ops@example.com", "password": "password-ok-1"},
                authorization=None,
                remote="192.0.2." + str(index + 1),
            )
            self.assertEqual(status, "200 OK")
        for index in range(10):
            status, _body = self.call(
                "POST",
                "/v1/auth/login",
                {"email": "ops@example.com", "password": "wrong-password"},
                authorization=None,
                remote="198.51.100." + str(index + 1),
            )
            self.assertEqual(status, "401 Unauthorized")
        blocked_status, blocked = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "OPS@example.com", "password": "password-ok-1"},
            authorization=None,
            remote="203.0.113.9",
        )
        other_status, other = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "other@example.com", "password": "wrong-password"},
            authorization=None,
            remote="203.0.113.9",
        )
        for _index in range(10):
            self.call(
                "POST",
                "/v1/auth/login",
                {"email": "not-an-email", "password": "wrong-password"},
                authorization=None,
                remote="203.0.113.10",
            )
        still_status, _still = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "other@example.com", "password": "wrong-password"},
            authorization=None,
            remote="203.0.113.11",
        )
        self.assertEqual(blocked_status, "429 Too Many Requests")
        self.assertEqual(blocked["error"]["code"], "rate_limited")
        self.assertEqual(other_status, "401 Unauthorized")
        self.assertEqual(still_status, "401 Unauthorized")

    def test_only_admin_manages_users(self) -> None:
        """Writer cannot create users. Admin can grant every role and write flags."""
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "writer@example.com",
                "name": "Writer",
                "password": "password-ok-1",
                "roles": ["reader", "writer"],
            },
        )
        writer = self._login("writer@example.com")
        denied_status, denied = self.call(
            "POST",
            "/v1/users",
            {
                "email": "other@example.com",
                "name": "Other",
                "password": "password-ok-1",
                "roles": ["reader"],
            },
            authorization="Bearer " + writer,
        )
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "admin@example.com",
                "name": "Admin",
                "password": "password-ok-1",
                "roles": ["admin"],
            },
        )
        admin = self._login("admin@example.com")
        created_status, created = self.call(
            "POST",
            "/v1/users",
            {
                "email": "reader@example.com",
                "name": "Reader",
                "password": "password-ok-1",
                "roles": ["reader", "writer"],
            },
            authorization="Bearer " + admin,
        )
        grant_status, grant = self.call(
            "POST",
            "/v1/users",
            {
                "email": "prot@example.com",
                "name": "Prot",
                "password": "password-ok-1",
                "roles": ["reader", "writer", "protected_writer"],
            },
            authorization="Bearer " + admin,
        )
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "boss@example.com",
                "name": "Boss",
                "password": "password-ok-1",
                "roles": ["reader", "admin"],
            },
        )
        boss = self._login("boss@example.com")
        granted_status, _granted = self.call(
            "POST",
            "/v1/users",
            {
                "email": "second@example.com",
                "name": "Second",
                "password": "password-ok-1",
                "roles": ["reader", "admin"],
            },
            authorization="Bearer " + boss,
        )
        list_status, _listed = self.call("GET", "/v1/users", authorization="Bearer " + admin)
        reader_list_status, _reader_list = self.call(
            "GET",
            "/v1/users",
            authorization="Bearer " + self._login("reader@example.com"),
        )
        patch_status, _patched = self.call(
            "PATCH",
            f"/v1/users/{created['id']}",
            {"name": "Renamed"},
            authorization="Bearer " + writer,
        )
        project_status, _project = self.call(
            "POST",
            "/v1/projects",
            {"key": "shop", "name": "Shop"},
            authorization="Bearer " + admin,
        )
        environment_status, environment = self.call(
            "POST",
            "/v1/projects/shop/environments",
            {"key": "prod", "name": "Production", "protected": True},
            authorization="Bearer " + admin,
        )
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertIn("admin", denied["error"]["message"])
        self.assertEqual(created_status, "201 Created")
        self.assertEqual(grant_status, "201 Created")
        self.assertEqual(grant["roles"], ["reader", "writer", "protected_writer"])
        self.assertEqual(granted_status, "201 Created")
        self.assertEqual(list_status, "200 OK")
        self.assertEqual(reader_list_status, "200 OK")
        self.assertEqual(patch_status, "403 Forbidden")
        self.assertEqual(project_status, "201 Created")
        self.assertEqual(environment_status, "201 Created")
        self.assertIs(environment["protected"], True)

    def _login(self, email: str) -> str:
        """Return a local access token for email. Password is password-ok-1."""
        _status, body = self.call(
            "POST",
            "/v1/auth/login",
            {"email": email, "password": "password-ok-1"},
            authorization=None,
        )
        return str(body["access_token"])

    def test_server_audit_records_login_and_hides_it_from_a_project(self) -> None:
        """Login rows are on GET /v1/audit and absent from a project audit.

        A failed attempt for an unknown email does not store that text.
        The bootstrap row names the new token id and prefix, not the secret.
        reader plus protected_writer cannot revoke a management token.
        """
        failed_status, _failed = self.call(
            "POST",
            "/v1/auth/login",
            {"email": "nobody@example.com", "password": "password-ok-1"},
            authorization=None,
            remote="203.0.113.9",
        )
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "ops@example.com",
                "name": "Ops",
                "password": "password-ok-1",
                "roles": ["reader"],
            },
        )
        self.call(
            "POST",
            "/v1/users",
            {
                "email": "addon@example.com",
                "name": "Addon",
                "password": "password-ok-1",
                "roles": ["reader", "protected_writer"],
            },
        )
        reader = self._login("ops@example.com")
        status, body = self.call("GET", "/v1/audit", authorization="Bearer " + reader)
        actions = [item["action"] for item in body["items"]]
        failed = next(item for item in body["items"] if item["action"] == "user.login_failed")
        success = next(item for item in body["items"] if item["action"] == "user.login")
        created = next(item for item in body["items"] if item["action"] == "management_token.create")
        self.assertEqual(failed_status, "401 Unauthorized")
        self.assertEqual(status, "200 OK")
        self.assertIn("user.login_failed", actions)
        self.assertEqual(failed["actor"], {"id": 0, "kind": "anonymous"})
        self.assertNotIn("nobody@example.com", json.dumps(failed["after"]))
        self.assertEqual(failed["after"]["client_address"], "203.0.113.9")
        self.assertNotIn("user_id", failed["after"])
        self.assertEqual(success["actor"]["kind"], "user")
        self.assertEqual(success["after"]["user_id"], success["actor"]["id"])
        self.assertNotIn("ops@example.com", json.dumps(success))
        self.assertNotIn(self.token, json.dumps(created))
        self.assertEqual(created["after"]["prefix"], self.token[:8])
        self.call("POST", "/v1/projects", {"key": "shop", "name": "Shop"})
        project_status, project_audit = self.call("GET", "/v1/projects/shop/audit")
        project_actions = [item["action"] for item in project_audit["items"]]
        self.assertEqual(project_status, "200 OK")
        self.assertNotIn("user.login", project_actions)
        self.assertNotIn("management_token.create", project_actions)
        page_status, page = self.call(
            "GET",
            "/v1/audit?limit=1",
            authorization="Bearer " + reader,
        )
        older_status, older = self.call(
            "GET",
            "/v1/audit?limit=1&cursor=" + str(page["next_cursor"]),
            authorization="Bearer " + reader,
        )
        bad_status, bad = self.call("GET", "/v1/audit?cursor=nope")
        self.assertEqual(page_status, "200 OK")
        self.assertEqual(older_status, "200 OK")
        self.assertGreater(page["items"][0]["id"], older["items"][0]["id"])
        self.assertEqual(bad_status, "400 Bad Request")
        self.assertEqual(bad["error"]["code"], "validation_error")
        listed_status, listed = self.call("GET", "/v1/management-tokens")
        addon = self._login("addon@example.com")
        denied_status, denied = self.call(
            "DELETE",
            f"/v1/management-tokens/{listed['items'][0]['id']}",
            authorization="Bearer " + addon,
        )
        self.assertEqual(listed_status, "200 OK")
        self.assertEqual(denied_status, "403 Forbidden")
        self.assertIn("writer", denied["error"]["message"])

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
        path_only, _, query = path.partition("?")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path_only,
            "QUERY_STRING": query,
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


def _bare_environ(remote: str, forwarded: str | None) -> dict:
    """Return a GET environ with remote and an optional X-Forwarded-For.

    Does not open a socket.
    """
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/health",
        "QUERY_STRING": "",
        "REMOTE_ADDR": remote,
        "CONTENT_LENGTH": "0",
        "wsgi.input": io.BytesIO(b""),
    }
    if forwarded is not None:
        environ["HTTP_X_FORWARDED_FOR"] = forwarded
    return environ


def _partial_connection(host: str, port: int) -> socket.socket:
    """Open a connection that sends headers and never the blank line.

    The server stays in the reader phase. Does not read a response.
    """
    sock = socket.create_connection((host, port), timeout=2)
    sock.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n")
    return sock


def _read_refusal(host: str, port: int) -> bytes:
    """Connect and return what the server writes before any request bytes.

    A full reader table answers 503 at accept. Sending a body first can
    turn that close into a TCP reset.
    """
    sock = socket.create_connection((host, port), timeout=2)
    try:
        sock.settimeout(2)
        return sock.recv(4096)
    finally:
        sock.close()


def _exchange(host: str, port: int, payload: bytes) -> bytes:
    """Send payload and return the bytes the server writes back."""
    sock = socket.create_connection((host, port), timeout=2)
    try:
        sock.sendall(payload)
        sock.settimeout(2)
        chunks: list[bytes] = []
        while True:
            try:
                piece = sock.recv(4096)
            except TimeoutError:
                break
            if piece == b"":
                break
            chunks.append(piece)
        return b"".join(chunks)
    finally:
        sock.close()


def _wait_for(ready) -> None:
    """Poll ready until it returns true, or fail after two seconds."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.01)
    raise AssertionError("the server did not reach the expected reader count")
