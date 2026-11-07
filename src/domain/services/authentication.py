"""Turn an Authorization header into a principal.

Accepts an opaque bearer token (hash lookup), a local HS256 JWT, or an
IdP JWT verified via JWKS. A JWT must name an enabled local user; roles
come from that row. Controllers do not parse the header.
"""

from dataclasses import dataclass

from domain.errors import Unauthenticated
from domain.jwt import looks_like_jwt, verify_access_token
from domain.models.principal import KIND_USER, Principal
from repositories.db import Connection
from repositories.credentials import find_active_principal
from repositories.users import UserRow, find_enabled_user_by_email, find_enabled_user_by_id

_MESSAGE = "A valid credential is required."


@dataclass(frozen=True, repr=False)
class JwtSettings:
    """How this process issues and verifies JWTs.

    secret is omitted from repr so a log of Settings does not record it.
    ttl_seconds is the access-token lifetime used at local login.
    jwks_url enables external IdP verification when non-empty.
    """

    secret: str
    issuer: str
    audience: str
    ttl_seconds: int
    jwks_url: str = ""

    def __repr__(self) -> str:
        """Describe JWT settings without the signing secret."""
        return (
            "JwtSettings("
            f"issuer={self.issuer!r}, audience={self.audience!r}, "
            f"ttl_seconds={self.ttl_seconds!r}, jwks_url={self.jwks_url!r})"
        )


def authenticate(
    connection: Connection,
    authorization_header: str | None,
    jwt_settings: JwtSettings | None = None,
) -> Principal:
    """Resolve a bearer token to a principal.

    Tries an opaque credential first. When that misses and the token looks
    like a JWT, verifies HS256 or JWKS and loads the local user. A missing
    header, a bad token, a revoked credential, a bad JWT, and a missing or
    disabled user all raise Unauthenticated with the same message. Does not
    commit and does not log the header.
    """
    token = _bearer_token(authorization_header)
    if token is None:
        raise Unauthenticated(_MESSAGE)
    principal = find_active_principal(connection, token)
    if principal is not None:
        return principal
    if jwt_settings is not None and looks_like_jwt(token):
        return _authenticate_jwt(connection, token, jwt_settings)
    raise Unauthenticated(_MESSAGE)


def _authenticate_jwt(
    connection: Connection,
    token: str,
    jwt_settings: JwtSettings,
) -> Principal:
    """Verify token and return a user principal from the local user row.

    Roles come from the database, not from the JWT claims. Raises
    Unauthenticated when verification fails or no enabled user matches.
    """
    payload, algorithm = verify_access_token(
        token,
        jwt_settings.secret,
        jwt_settings.issuer,
        jwt_settings.audience,
        jwt_settings.jwks_url,
    )
    user = _load_user(connection, payload, algorithm)
    if user is None:
        raise Unauthenticated(_MESSAGE)
    return Principal(
        credential_id=user.id,
        kind=KIND_USER,
        environment_id=None,
        prefix="",
        roles=user.roles,
    )


def _load_user(connection: Connection, payload: dict, algorithm: str) -> UserRow | None:
    """Return the enabled user named by this verified token.

    An HS256 token is one this process signed, so a decimal sub is the
    local user id. Its gen claim must equal that row's token_generation.
    A missing or different gen is the same miss as an unknown token. A
    JWKS token matches the normalized email claim only when email_verified
    is the boolean true. A string, false, or a missing claim does not match,
    and sub is not an email. The token is not checked for gen. Returns None
    when nothing matches.
    """
    if algorithm == "HS256":
        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub.isdigit():
            return None
        user = find_enabled_user_by_id(connection, int(sub))
        if user is None or not _generation_matches(payload.get("gen"), user.token_generation):
            return None
        return user
    if payload.get("email_verified") is not True:
        return None
    email = payload.get("email")
    if isinstance(email, str) and email.strip() != "":
        return find_enabled_user_by_email(connection, email.strip().lower())
    return None


def _generation_matches(claim: object, generation: int) -> bool:
    """Return True when claim is the integer generation.

    A bool is not a generation. Does not read the database.
    """
    if isinstance(claim, bool) or not isinstance(claim, int):
        return False
    return claim == generation


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
