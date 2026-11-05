"""The standard-library WSGI adapter.

Controllers are called with a Request and return a Response. This module
turns those into HTTP. It does not contain a business rule.
"""

import json
import threading
import traceback
from collections.abc import Callable
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from api.login_limit import LoginLimiter
from api.request import Request, Response
from api.routes import authorize, dispatch, requires_credential
from domain.errors import DomainError, StorageError, TooManyRequests, ValidationError
from domain.services.authentication import JwtSettings, authenticate
from cache.null import NullCache
from domain.validation import DEFAULT_EVAL_TTL_SECONDS, MAX_REQUEST_BODY_BYTES
from repositories.db import Database
from repositories.eval_cache import EvaluationCache

_MAX_IN_FLIGHT = 32
_SOCKET_TIMEOUT_SECONDS = 30

_DEFAULT_JWT = JwtSettings(
    secret="",
    issuer="flaggator",
    audience="flaggator",
    ttl_seconds=3600,
    jwks_url="",
)

StartResponse = Callable[[str, list[tuple[str, str]]], object]

_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """A WSGI server that runs each request on its own thread.

    daemon_threads lets the process exit when the main thread does. At most
    _MAX_IN_FLIGHT requests run at once. Each accepted socket has a read
    timeout. The memory cache lock is what makes sharing the cache across
    these threads safe.
    """

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        """Bind the port and create the in-flight semaphore."""
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)
        self._in_flight = threading.BoundedSemaphore(_MAX_IN_FLIGHT)

    def get_request(self):
        """Accept one socket and set its timeout.

        The listening socket itself keeps the default blocking accept.
        """
        request, client_address = super().get_request()
        request.settimeout(_SOCKET_TIMEOUT_SECONDS)
        return request, client_address

    def process_request(self, request, client_address):
        """Start a worker, or answer 503 when every slot is taken.

        The slot is released in the worker thread. A failure to start the
        thread releases it here.
        """
        if not self._in_flight.acquire(blocking=False):
            _write_unavailable(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._in_flight.release()
            raise

    def process_request_thread(self, request, client_address):
        """Handle one request and then free its slot."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._in_flight.release()


class Application:
    """The WSGI callable.

    GET /health does not use the database. Login and client evaluation do
    not use a bearer token. Any other path authenticates a bearer token
    against the database before the controller runs. Calling this object
    does not listen. serve() does that.
    """

    def __init__(
        self,
        database: Database | None = None,
        evaluation_cache: EvaluationCache | None = None,
        jwt_settings: JwtSettings | None = None,
        client_eval_limit: int = 60,
        client_eval_window_seconds: float = 60,
    ) -> None:
        """Keep database, the evaluation cache, and JWT verify settings.

        database may be None only when the caller will request GET /health.
        When evaluation_cache is omitted, a cache that stores nothing is
        used so a write still registers a harmless delete. Composition is
        the place that selects the process cache and JWT settings. Does not
        open a connection.
        """
        self._database = database
        if evaluation_cache is None:
            self._evaluation_cache = EvaluationCache(NullCache(), DEFAULT_EVAL_TTL_SECONDS)
        else:
            self._evaluation_cache = evaluation_cache
        self._jwt_settings = _DEFAULT_JWT if jwt_settings is None else jwt_settings
        self._login_limiter = LoginLimiter()
        self._client_eval_limiter = LoginLimiter(
            limit=client_eval_limit,
            window_seconds=client_eval_window_seconds,
        )

    def __call__(self, environ: dict, start_response: StartResponse) -> list[bytes]:
        """Handle one request and return the body as a list of byte strings.

        A DomainError becomes the JSON error document. Any other exception
        is printed to stderr and answered with a generic 500, so driver text
        does not reach the client.
        """
        try:
            request = request_from_environ(environ)
            if request.path == "/v1/auth/login":
                response = self._login(request)
            elif request.path == "/v1/client-evaluate":
                response = self._client_evaluate(request)
            elif requires_credential(request):
                response = self._authenticated(request)
            else:
                response = dispatch(request)
        except DomainError as error:
            response = response_for_domain_error(error)
        except Exception:
            traceback.print_exc()
            response = response_for_domain_error(StorageError())
        return encode(start_response, response)

    def _login(self, request: Request) -> Response:
        """Handle local login without a bearer token.

        Opens one connection and closes it before returning. A missing
        database raises StorageError. Does not authenticate.
        """
        from api.users import login
        from api.routes import method_not_allowed

        if request.method != "POST":
            return method_not_allowed("POST")
        if not self._login_limiter.allow(request.client_address):
            raise TooManyRequests()
        if self._database is None:
            raise StorageError()
        connection = self._database.connect()
        try:
            return login(connection, request, self._jwt_settings)
        finally:
            connection.close()

    def _client_evaluate(self, request: Request) -> Response:
        """Evaluate public flags without a bearer token.

        Opens one connection and closes it before returning. Over the
        address limit raises TooManyRequests before the database is used.
        A missing database raises StorageError.
        """
        from api.evaluation import evaluate_public
        from api.routes import method_not_allowed

        if request.method != "POST":
            return method_not_allowed("POST")
        if not self._client_eval_limiter.allow(request.client_address):
            raise TooManyRequests()
        if self._database is None:
            raise StorageError()
        connection = self._database.connect()
        try:
            return evaluate_public(connection, self._evaluation_cache, request)
        finally:
            connection.close()

    def _authenticated(self, request: Request) -> Response:
        """Authenticate, authorize, and dispatch.

        Opens one connection and closes it before returning. A missing
        database raises StorageError. Does not commit.
        """
        if self._database is None:
            raise StorageError()
        connection = self._database.connect()
        try:
            principal = authenticate(
                connection,
                request.headers.get("authorization"),
                self._jwt_settings,
            )
            authorize(request, principal)
            return dispatch(request, principal, connection, self._evaluation_cache)
        finally:
            connection.close()


def request_from_environ(environ: dict) -> Request:
    """Copy the WSGI environ into a Request.

    Body is the raw bytes, limited to Content-Length. An unreadable length
    raises ValidationError. Does not parse JSON and does not query the
    database.
    """
    method = str(environ.get("REQUEST_METHOD", ""))
    path = str(environ.get("PATH_INFO", ""))
    query = _first_query_values(str(environ.get("QUERY_STRING", "")))
    headers = _headers_from_environ(environ)
    raw_length = str(environ.get("CONTENT_LENGTH", "") or "0")
    if not raw_length.isdigit():
        raise ValidationError(
            "Content-Length is not an integer.",
            details=[{"field": "content-length", "message": "Content-Length must be a number."}],
        )
    length = int(raw_length)
    if length > MAX_REQUEST_BODY_BYTES:
        raise ValidationError(
            "The request body is too large.",
            details=[{"field": "body", "message": "The body must be at most 256 KiB."}],
        )
    body = environ["wsgi.input"].read(length)
    client_address = str(environ.get("REMOTE_ADDR", "") or "")
    return Request(
        method=method,
        path=path,
        query=query,
        headers=headers,
        body=body,
        client_address=client_address,
    )


def response_for_domain_error(error: DomainError) -> Response:
    """Build the JSON error document for error.

    The message and details are the ones on the error. This does not add
    driver text.
    """
    return Response(
        status=error.status,
        body={
            "error": {
                "code": error.code,
                "message": error.message,
                "details": error.details,
            }
        },
    )


def encode(start_response: StartResponse, response: Response) -> list[bytes]:
    """Send the status line and return the JSON body.

    Content-Type is application/json. Does not include a stack trace.
    """
    payload = json.dumps(response.body).encode("utf-8")
    reason = _REASONS.get(response.status, "Error")
    start_response(
        f"{response.status} {reason}",
        [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
        ],
    )
    return [payload]


def serve(application: Application, host: str, port: int) -> None:
    """Listen for HTTP requests until the process is killed.

    Binds host and port. Each request runs on its own thread. Does not
    return. python -m main calls this. Tests of a single route call the
    application directly and do not bind a port.
    """
    server = make_server(host, port, application, server_class=ThreadingWSGIServer)
    server.serve_forever()


def _write_unavailable(request) -> None:
    """Send 503 on request and close it.

    Used when the server has no free worker slot. Does not read a body.
    """
    payload = json.dumps(
        {
            "error": {
                "code": "unavailable",
                "message": "The server is busy.",
                "details": [],
            }
        }
    ).encode("utf-8")
    head = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n\r\n"
    )
    try:
        request.sendall(head + payload)
    except OSError:
        pass
    finally:
        request.close()


def _first_query_values(query_string: str) -> dict[str, str]:
    """Return the first value for each query key.

    A repeated key keeps the first value. An empty query string returns an
    empty dict. Does not read the body.
    """
    parsed = parse_qs(query_string, keep_blank_values=True)
    values: dict[str, str] = {}
    for key, items in parsed.items():
        if items:
            values[key] = items[0]
    return values


def _headers_from_environ(environ: dict) -> dict[str, str]:
    """Collect HTTP headers as lower-case names.

    CONTENT_TYPE is included as content-type. The authorization header is
    kept so a later phase can read the bearer token. Does not log the values.
    """
    headers: dict[str, str] = {}
    for key, value in environ.items():
        if not isinstance(key, str) or not key.startswith("HTTP_"):
            continue
        name = key[len("HTTP_") :].lower().replace("_", "-")
        headers[name] = str(value)
    content_type = environ.get("CONTENT_TYPE")
    if content_type:
        headers["content-type"] = str(content_type)
    return headers
