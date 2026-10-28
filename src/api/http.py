"""The standard-library WSGI adapter.

Controllers are called with a Request and return a Response. This module
turns those into HTTP. It does not contain a business rule.
"""

import json
import traceback
from collections.abc import Callable
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from api.request import Request, Response
from api.routes import authorize, dispatch, requires_credential
from domain.errors import DomainError, StorageError, ValidationError
from domain.services.authentication import authenticate
from repositories.db import Database

StartResponse = Callable[[str, list[tuple[str, str]]], object]

_REASONS = {
    200: "OK",
    201: "Created",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    500: "Internal Server Error",
}


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    """A WSGI server that runs each request on its own thread.

    daemon_threads lets the process exit when the main thread does. The
    memory cache lock is what makes sharing the cache across these threads
    safe.
    """

    daemon_threads = True


class Application:
    """The WSGI callable.

    GET /health does not use the database. Any other path authenticates a
    bearer token against database before the controller runs. Calling this
    object does not listen. serve() does that.
    """

    def __init__(self, database: Database | None = None) -> None:
        """Keep database for credential lookup.

        database may be None only when the caller will request GET /health.
        An authenticated path without a database becomes a generic storage
        error. Does not open a connection.
        """
        self._database = database

    def __call__(self, environ: dict, start_response: StartResponse) -> list[bytes]:
        """Handle one request and return the body as a list of byte strings.

        A DomainError becomes the JSON error document. Any other exception
        is printed to stderr and answered with a generic 500, so driver text
        does not reach the client.
        """
        try:
            request = request_from_environ(environ)
            if requires_credential(request):
                response = self._authenticated(request)
            else:
                response = dispatch(request)
        except DomainError as error:
            response = response_for_domain_error(error)
        except Exception:
            traceback.print_exc()
            response = response_for_domain_error(StorageError())
        return encode(start_response, response)

    def _authenticated(self, request: Request) -> Response:
        """Authenticate, authorize, and dispatch.

        Opens one connection and closes it before returning. A missing
        database raises StorageError. Does not commit.
        """
        if self._database is None:
            raise StorageError()
        connection = self._database.connect()
        try:
            principal = authenticate(connection, request.headers.get("authorization"))
            authorize(request, principal)
            return dispatch(request, principal, connection)
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
    body = environ["wsgi.input"].read(length)
    return Request(method=method, path=path, query=query, headers=headers, body=body)


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
    return. The process entry calls this in a later phase; tests call the
    application directly and do not bind a port.
    """
    server = make_server(host, port, application, server_class=ThreadingWSGIServer)
    server.serve_forever()


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
