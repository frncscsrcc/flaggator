"""External IdP JWT verification via JWKS."""

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from api.http import Application
from composition import load_settings
from domain.clock import utc_timestamp
from domain.services.authentication import JwtSettings, authenticate
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase

_ISSUER = "https://idp.example.test/"
_AUDIENCE = "flaggator"
_JWT_SECRET = "test-jwt-signing-secret-32b-min!!"


class IdpTests(unittest.TestCase):
    def setUp(self) -> None:
        """Start a JWKS HTTP stub and migrate a temporary database."""
        self._temporary = tempfile.TemporaryDirectory()
        db_path = str(Path(self._temporary.name) / "test.db")
        self.database = SqliteDatabase(db_path)
        apply_migrations(self.database, default_migrations_directory())
        self._private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self._kid = "test-key"
        self._jwks_server = _JwksServer(self._jwks_document())
        self._jwks_url = self._jwks_server.start()
        load_settings(
            {
                "FLAGGATOR_SQLITE_PATH": db_path,
                "FLAGGATOR_CACHE": "null",
                "FLAGGATOR_JWT_SECRET": _JWT_SECRET,
                "FLAGGATOR_JWT_ISSUER": _ISSUER,
                "FLAGGATOR_JWT_AUDIENCE": _AUDIENCE,
                "FLAGGATOR_JWT_JWKS_URL": self._jwks_url,
            }
        )
        self.jwt_settings = JwtSettings(
            secret=_JWT_SECRET,
            issuer=_ISSUER,
            audience=_AUDIENCE,
            ttl_seconds=3600,
            jwks_url=self._jwks_url,
        )
        self.application = Application(self.database, jwt_settings=self.jwt_settings)

    def tearDown(self) -> None:
        """Stop the JWKS stub and delete the temporary database."""
        self._jwks_server.stop()
        self._temporary.cleanup()

    def test_idp_jwt_loads_local_user_by_email(self) -> None:
        """A JWKS-signed token matches the pre-registered local user roles."""
        user_id = self._insert_user("ops@example.com", '["reader","writer"]')
        token = self._idp_token(email="Ops@Example.com", sub="idp-subject-1")
        connection = self.database.connect()
        try:
            principal = authenticate(connection, f"Bearer {token}", self.jwt_settings)
        finally:
            connection.close()
        status, body = self._call("GET", "/v1/projects", token)
        self.assertEqual(principal.kind, "user")
        self.assertEqual(principal.credential_id, user_id)
        self.assertEqual(principal.roles, ("reader", "writer"))
        self.assertEqual(status, "200 OK")
        self.assertEqual(body["items"], [])

    def test_idp_numeric_sub_does_not_select_a_local_id(self) -> None:
        """A JWKS token with sub equal to a local id still matches by email."""
        self._insert_user("first@example.com", '["reader"]')
        second = self._insert_user("second@example.com", '["writer"]')
        token = self._idp_token(email="second@example.com", sub="1")
        connection = self.database.connect()
        try:
            principal = authenticate(connection, f"Bearer {token}", self.jwt_settings)
        finally:
            connection.close()
        self.assertEqual(principal.credential_id, second)
        self.assertEqual(principal.roles, ("writer",))

    def test_idp_jwt_without_local_user_is_401(self) -> None:
        """A valid IdP signature with no local user is the same 401."""
        token = self._idp_token(email="missing@example.com", sub="nobody")
        status, body = self._call("GET", "/v1/projects", token)
        self.assertEqual(status, "401 Unauthorized")
        self.assertEqual(body["error"]["code"], "unauthenticated")

    def _idp_token(self, email: str, sub: str) -> str:
        """Return an RS256 JWT signed by the stub IdP key."""
        return jwt.encode(
            {
                "sub": sub,
                "email": email,
                "iss": _ISSUER,
                "aud": _AUDIENCE,
                "iat": 1_700_000_000,
                "exp": 2_000_000_000,
            },
            self._private_key,
            algorithm="RS256",
            headers={"kid": self._kid},
        )

    def _insert_user(self, email: str, roles_json: str) -> int:
        """Insert one enabled local user and return its id."""
        connection = self.database.connect()
        try:
            with connection.transaction():
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        email, name, password_hash, roles_json, created_at, disabled_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (email, "Operator", None, roles_json, utc_timestamp(), None),
                )
                return cursor.last_insert_id
        finally:
            connection.close()

    def _jwks_document(self) -> dict:
        """Return a JWKS document for the stub private key."""
        public_numbers = self._private_key.public_key().public_numbers()

        def _b64url_int(value: int) -> str:
            """Encode an integer as base64url without padding."""
            length = (value.bit_length() + 7) // 8
            return jwt.utils.base64url_encode(value.to_bytes(length, "big")).decode("ascii")

        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self._kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64url_int(public_numbers.n),
                    "e": _b64url_int(public_numbers.e),
                }
            ]
        }

    def _call(self, method: str, path: str, token: str) -> tuple[str, dict]:
        """Invoke the WSGI app with a bearer token."""
        captured: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            """Record the status line."""
            captured["status"] = status

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
            "HTTP_AUTHORIZATION": f"Bearer {token}",
        }
        payload = b"".join(self.application(environ, start_response))
        return str(captured["status"]), json.loads(payload.decode("utf-8"))


class _JwksServer:
    """Serve one JWKS document on a random loopback port."""

    def __init__(self, document: dict) -> None:
        """Keep the JWKS body to return on GET /."""
        self._body = json.dumps(document).encode("utf-8")
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        """Bind loopback and return the JWKS URL."""
        body = self._body

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                """Return the JWKS document."""
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                """Suppress request logs during tests."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        host, port = self._server.server_address[:2]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://{host}:{port}/"

    def stop(self) -> None:
        """Shut down the stub server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
