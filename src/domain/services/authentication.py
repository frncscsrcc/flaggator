"""Turn an Authorization header into a principal.

v1 accepts an opaque bearer token. A standard JWT is a later input that
must produce the same principal and is rejected here (decisions P13).
"""

from domain.errors import Unauthenticated
from domain.models.principal import Principal
from repositories.credentials import find_active_principal
from repositories.db import Connection

_MESSAGE = "A valid credential is required."


def authenticate(connection: Connection, authorization_header: str | None) -> Principal:
    """Resolve a bearer token to a principal.

    A missing header, a header that is not Bearer, an unknown token, and a
    revoked token all raise Unauthenticated with the same message. Does not
    commit and does not log the header. Does not accept a JWT.
    """
    token = _bearer_token(authorization_header)
    if token is None:
        raise Unauthenticated(_MESSAGE)
    principal = find_active_principal(connection, token)
    if principal is None:
        raise Unauthenticated(_MESSAGE)
    return principal


def _bearer_token(authorization_header: str | None) -> str | None:
    """Return the token after Bearer, or None when the header is not usable.

    The scheme match is case-insensitive. A token containing a space is
    rejected. Does not log the header.
    """
    if authorization_header is None:
        return None
    scheme, separator, token = authorization_header.partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return None
    if token == "" or " " in token:
        return None
    return token
