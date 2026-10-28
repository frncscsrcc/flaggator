"""Request and response values the WSGI adapter and the controllers share.

A controller returns a Response. The adapter encodes it as JSON bytes.
These types do not read the socket.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Request:
    """The parts of an HTTP request a controller is allowed to see.

    body is the raw bytes. Headers use lower-case names. query keeps the
    first value when a key is repeated. This object does not parse JSON.
    """

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class Response:
    """An HTTP response before it is written to the socket.

    status is the numeric code. body is the JSON document. Building a
    Response does not send it.
    """

    status: int
    body: dict
