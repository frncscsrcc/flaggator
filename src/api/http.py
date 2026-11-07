"""The standard-library WSGI adapter.

Controllers are called with a Request and return a Response. This module
turns those into HTTP. It does not contain a business rule.
"""

import ipaddress
import json
import socket
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIServer, make_server

from api.console import console_response, default_console_directory
from api.login_limit import LoginLimiter
from api.request import Request, Response
from api.routes import authorize, dispatch, requires_credential
from domain.errors import DomainError, StorageError, TooManyRequests, Unauthenticated, ValidationError
from domain.services.authentication import JwtSettings, authenticate
from cache.null import NullCache
from domain.validation import DEFAULT_EVAL_TTL_SECONDS, MAX_REQUEST_BODY_BYTES
from repositories.db import Database
from repositories.eval_cache import EvaluationCache

_MAX_IN_FLIGHT = 32
_SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
    (
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'",
    ),
    ("Cache-Control", "no-store"),
)
_MAX_READERS = 128
_MAX_READERS_PER_ADDRESS = 16
_REQUEST_DEADLINE_SECONDS = 30

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
    _MAX_READERS connections are being read, and at most
    _MAX_READERS_PER_ADDRESS of those belong to one peer, unless that peer
    is a trusted proxy. The work itself is capped later, inside the
    application, after the body has been read. Every accepted socket has
    one deadline measured from accept: a slow trickle does not renew it.
    The memory cache lock is what makes sharing the cache across these
    threads safe.
    """

    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, bind_and_activate=True):
        """Bind the port and create the reader limits.

        request_deadline_seconds is the budget from accept. Tests shorten it.
        """
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)
        self.request_deadline_seconds = _REQUEST_DEADLINE_SECONDS
        self._readers = threading.BoundedSemaphore(_MAX_READERS)
        self._readers_lock = threading.Lock()
        self._readers_by_address: dict[str, int] = {}
        self._deadlines: dict[int, threading.Timer] = {}

    def get_request(self):
        """Accept one socket and start its deadline.

        When the deadline fires the socket is closed, so a slow trickle
        cannot keep the reader. The listening socket itself keeps the
        default blocking accept.
        """
        request, client_address = super().get_request()
        timer = threading.Timer(self.request_deadline_seconds, _close_quietly, args=(request,))
        timer.daemon = True
        timer.start()
        with self._readers_lock:
            self._deadlines[id(request)] = timer
        return request, client_address

    def process_request(self, request, client_address):
        """Start a reader, or answer 503 when a reader limit is already full.

        The reader slot is released in the reader thread. A failure to start
        the thread releases it here. This does not take a work slot.
        """
        peer = _peer_host(client_address)
        if not self._acquire_reader(peer):
            self._cancel_deadline(request)
            _write_unavailable(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._cancel_deadline(request)
            self._release_reader(peer)
            raise

    def process_request_thread(self, request, client_address):
        """Read and handle one request, then free its reader slot."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._cancel_deadline(request)
            self._release_reader(_peer_host(client_address))

    def handle_error(self, request, client_address):
        """Ignore a client that disconnected or ran out its deadline.

        Those are OSError from a closed socket. Any other failure is
        reported by the base server.
        """
        if isinstance(sys.exception(), OSError):
            return
        super().handle_error(request, client_address)

    def _cancel_deadline(self, request) -> None:
        """Stop the accept-time deadline for request, if it is still waiting."""
        with self._readers_lock:
            timer = self._deadlines.pop(id(request), None)
        if timer is not None:
            timer.cancel()

    def _acquire_reader(self, peer: str) -> bool:
        """Take a global reader slot and, unless peer is trusted, a per-address slot.

        Returns False when either limit is full. A trusted proxy is exempt
        from the per-address limit because every client shares its address.
        Does not read the database.
        """
        if not self._readers.acquire(blocking=False):
            return False
        if _peer_is_trusted(peer, _trusted_networks(self)):
            return True
        with self._readers_lock:
            count = self._readers_by_address.get(peer, 0)
            if count >= _MAX_READERS_PER_ADDRESS:
                self._readers.release()
                return False
            self._readers_by_address[peer] = count + 1
        return True

    def _release_reader(self, peer: str) -> None:
        """Return the reader slots taken for peer. Does not read the database."""
        if not _peer_is_trusted(peer, _trusted_networks(self)):
            with self._readers_lock:
                count = self._readers_by_address.get(peer, 0) - 1
                if count <= 0:
                    self._readers_by_address.pop(peer, None)
                else:
                    self._readers_by_address[peer] = count
        self._readers.release()


class Application:
    """The WSGI callable.

    GET /health does not use the database. Login and client evaluation do
    not use a bearer token. A path outside /health and /v1 is a console
    file, or a 404, and does not authenticate. Any other path authenticates
    a bearer token against the database before the controller runs. Calling
    this object does not listen. serve() does that.
    """

    def __init__(
        self,
        database: Database | None = None,
        evaluation_cache: EvaluationCache | None = None,
        jwt_settings: JwtSettings | None = None,
        client_eval_limit: int = 60,
        client_eval_window_seconds: float = 60,
        console_directory: Path | None = None,
        headless: bool = False,
        trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
    ) -> None:
        """Keep database, the evaluation cache, and JWT verify settings.

        database may be None only when the caller will request GET /health.
        When evaluation_cache is omitted, a cache that stores nothing is
        used so a write still registers a harmless delete. headless skips
        the console directory. When console_directory is omitted and
        headless is false, the ui/ tree next to the sources is used.
        Composition is the place that selects the process cache and JWT
        settings. Does not open a connection.
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
        self._in_flight = threading.BoundedSemaphore(_MAX_IN_FLIGHT)
        self._login_account_limiter = LoginLimiter()
        self.trusted_proxies = tuple(trusted_proxies)
        self._headless = headless
        if headless:
            self._console_directory = None
        elif console_directory is None:
            self._console_directory = default_console_directory()
        else:
            self._console_directory = console_directory

    def __call__(self, environ: dict, start_response: StartResponse) -> list[bytes]:
        """Handle one request and return the body as a list of byte strings.

        A DomainError becomes the JSON error document. Any other exception
        is printed to stderr and answered with a generic 500, so driver text
        does not reach the client.
        """
        try:
            request = request_from_environ(environ, self.trusted_proxies)
        except DomainError as error:
            return encode(start_response, response_for_domain_error(error))
        except Exception:
            traceback.print_exc()
            return encode(start_response, response_for_domain_error(StorageError()))
        if not self._in_flight.acquire(blocking=False):
            return encode(start_response, _unavailable_response())
        try:
            try:
                response = self._response_for(request)
            except DomainError as error:
                response = response_for_domain_error(error)
            except Exception:
                traceback.print_exc()
                response = response_for_domain_error(StorageError())
            return encode(start_response, response)
        finally:
            self._in_flight.release()

    def _response_for(self, request: Request) -> Response:
        """Choose the handler for a request whose body is already read.

        Does not take the work slot. The caller does that, after
        request_from_environ has returned. Does not catch errors.
        """
        static = console_response(
            request.method,
            request.path,
            self._console_directory,
            self._headless,
        )
        if static is not None:
            return static
        if request.path == "/v1/auth/login":
            return self._login(request)
        if request.path == "/v1/client-evaluate":
            return self._client_evaluate(request)
        if requires_credential(request):
            return self._authenticated(request)
        return dispatch(request)

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
        email = _normalized_login_email(request)
        if email is not None and self._login_account_limiter.over_limit(email):
            raise TooManyRequests()
        if self._database is None:
            raise StorageError()
        connection = self._database.connect()
        try:
            return login(connection, request, self._jwt_settings)
        except Unauthenticated:
            if email is not None:
                self._login_account_limiter.record(email)
            raise
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


def request_from_environ(
    environ: dict,
    trusted_proxies: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (),
) -> Request:
    """Copy the WSGI environ into a Request.

    Body is the raw bytes, limited to Content-Length. An unreadable length
    raises ValidationError. client_address is the address the rate limits
    count: the peer, unless that peer is a trusted proxy, in which case it
    is taken from X-Forwarded-For. An IPv6 address is folded to its /64.
    Does not parse JSON and does not query the database.
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
    client_address = _client_address(environ, headers, trusted_proxies)
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
    """Send the status line and return the body.

    raw is sent with the content type on the response. Otherwise the body
    is JSON. Does not include a stack trace.
    """
    if response.raw is not None:
        payload = response.raw
        content_type = response.content_type
    else:
        payload = json.dumps(response.body).encode("utf-8")
        content_type = "application/json; charset=utf-8"
    reason = _REASONS.get(response.status, "Error")
    start_response(
        f"{response.status} {reason}",
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(payload))),
            *_SECURITY_HEADERS,
        ],
    )
    return [payload]


def serve(
    application: Application,
    host: str,
    port: int,
    allow_cleartext: bool = False,
) -> None:
    """Listen for HTTP requests until the process is killed.

    A host that is not loopback is refused unless allow_cleartext is true,
    because this listener speaks cleartext HTTP. Binds host and port. Each
    request runs on its own thread. Does not return. python -m main calls
    this. Tests of a single route call the application directly and do not
    bind a port.
    """
    if not allow_cleartext and not loopback_host(host):
        raise ValidationError(
            "The bind address is not loopback.",
            details=[
                {
                    "field": "bind_host",
                    "message": (
                        "Bind a loopback address, or set "
                        "FLAGGATOR_ALLOW_CLEARTEXT_BIND=true to serve cleartext HTTP."
                    ),
                }
            ],
        )
    server = make_server(host, port, application, server_class=ThreadingWSGIServer)
    server.serve_forever()


def loopback_host(host: str) -> bool:
    """Return True when host is a loopback address or the name localhost.

    Does not resolve DNS. An unparsable name other than localhost is not
    loopback.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _close_quietly(request: socket.socket) -> None:
    """Shut the socket down so a blocked read ends.

    Used when the accept-time deadline fires. A socket that is already
    closed stays closed.
    """
    try:
        request.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        request.close()
    except OSError:
        pass


def _peer_host(client_address: object) -> str:
    """Return the host part of a socket address.

    A missing or empty address is "". Does not read the database.
    """
    if isinstance(client_address, tuple) and len(client_address) > 0:
        return str(client_address[0])
    if client_address is None:
        return ""
    return str(client_address)


def _trusted_networks(server: ThreadingWSGIServer) -> tuple:
    """Return the trusted proxy networks stored on the server's application.

    An application without the attribute contributes none. Does not read
    the database.
    """
    application = getattr(server, "application", None)
    networks = getattr(application, "trusted_proxies", ())
    return tuple(networks)


def _client_address(
    environ: dict,
    headers: dict[str, str],
    trusted_proxies: tuple,
) -> str:
    """Return the address a rate limit counts for this request.

    A peer that is not a trusted proxy is counted itself, so a client
    cannot choose the bucket by sending X-Forwarded-For. A trusted peer
    contributes the nearest untrusted hop in that header. A missing header,
    a hop that is not an IP, or a chain of only trusted hops is the bucket
    unknown. An IPv6 address counts as its /64. Does not read the database.
    """
    remote = str(environ.get("REMOTE_ADDR", "") or "")
    if not _peer_is_trusted(remote, trusted_proxies):
        return _limit_key(remote)
    return _forwarded_client(headers.get("x-forwarded-for", ""), trusted_proxies)


def _forwarded_client(header: str, trusted_proxies: tuple) -> str:
    """Return the client hop in an X-Forwarded-For list.

    Hops are separated by commas. Walking from the right skips trusted
    proxies. The first untrusted IP is the client. Anything else is unknown.
    Does not read the database.
    """
    if header.strip() == "":
        return "unknown"
    parts = [part.strip() for part in header.split(",")]
    index = len(parts) - 1
    while index >= 0:
        hop = parts[index]
        if hop == "":
            index = index - 1
            continue
        try:
            address = ipaddress.ip_address(hop)
        except ValueError:
            return "unknown"
        if not _address_is_trusted(address, trusted_proxies):
            return _limit_key(str(address))
        index = index - 1
    return "unknown"


def _limit_key(address: str) -> str:
    """Return the rate-limit bucket for address.

    Empty is unknown. An IPv4-mapped IPv6 is counted as its IPv4 address.
    Any other IPv6 is counted as the network address of its /64. An IPv4
    is counted as itself. A value that is not an IP is kept, so a test
    double still has a bucket. Does not read the database.
    """
    if address == "":
        return "unknown"
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return address
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        return str(mapped)
    if isinstance(parsed, ipaddress.IPv6Address):
        network = ipaddress.ip_network(str(parsed) + "/64", strict=False)
        return str(network.network_address)
    return str(parsed)


def _normalized_login_email(request: Request) -> str | None:
    """Return the login email, lowercased, or None when it is not usable.

    A body that is not a JSON object, a missing email, and an email the
    domain rejects are None, so they do not enter the per-email limit.
    Does not read the database and does not check the password.
    """
    from api.body import read_json_object
    from domain.services.users import require_email

    try:
        body = read_json_object(request)
    except ValidationError:
        return None
    email = body.get("email")
    if not isinstance(email, str):
        return None
    try:
        return require_email(email)
    except ValidationError:
        return None


def _address_is_trusted(address: ipaddress.IPv4Address | ipaddress.IPv6Address, networks: tuple) -> bool:
    """Return True when address sits in one of networks. Does not read the database."""
    for network in networks:
        if address in network:
            return True
    return False


def _peer_is_trusted(peer: str, networks: tuple) -> bool:
    """Return True when peer is inside one of networks.

    A peer that is not an IP is not trusted. An empty list trusts nobody.
    Does not read the database.
    """
    if peer == "" or len(networks) == 0:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for network in networks:
        if address in network:
            return True
    return False


def _unavailable_response() -> Response:
    """Return the 503 document used when the work slots are full.

    Does not read the database.
    """
    return Response(
        status=503,
        body={
            "error": {
                "code": "unavailable",
                "message": "The server is busy.",
                "details": [],
            }
        },
    )


def _write_unavailable(request) -> None:
    """Send 503 on request and close it.

    Used when the server has no free reader slot. Pending bytes are
    discarded first: closing with unread data makes the kernel answer RST
    and the client never sees the 503.
    """
    _drain(request)
    payload = json.dumps(
        {
            "error": {
                "code": "unavailable",
                "message": "The server is busy.",
                "details": [],
            }
        }
    ).encode("utf-8")
    extra = "".join(f"{name}: {value}\r\n" for name, value in _SECURITY_HEADERS)
    head = (
        b"HTTP/1.1 503 Service Unavailable\r\n"
        b"Content-Type: application/json; charset=utf-8\r\n"
        b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n"
        + extra.encode("ascii")
        + b"Connection: close\r\n\r\n"
    )
    try:
        request.sendall(head + payload)
    except OSError:
        pass
    finally:
        request.close()


def _drain(request) -> None:
    """Read and discard whatever has already arrived on request.

    Does not wait for more bytes. A socket that cannot be read is left as it is.
    """
    try:
        request.setblocking(False)
    except OSError:
        return
    try:
        while request.recv(4096):
            pass
    except OSError:
        pass


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
