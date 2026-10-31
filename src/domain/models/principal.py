"""The caller, after authentication has succeeded.

A principal never carries a secret or a hash. Opaque tokens and a verified
JWT both build this same object.
"""

from dataclasses import dataclass

KIND_MANAGEMENT = "management"
KIND_SDK = "sdk"
KIND_USER = "user"

ROLE_READER = "reader"
ROLE_WRITER = "writer"
ROLE_PROTECTED_WRITER = "protected_writer"

MANAGEMENT_ROLES: tuple[str, ...] = (
    ROLE_READER,
    ROLE_WRITER,
    ROLE_PROTECTED_WRITER,
)

ALLOWED_ROLES = frozenset(MANAGEMENT_ROLES)


@dataclass(frozen=True)
class Principal:
    """Who is calling.

    credential_id is the audit actor: an api_credentials id for opaque
    tokens, or a users id for a JWT. kind is management, sdk, or user.
    environment_id is set for an SDK token and None otherwise. prefix is
    the non-secret credential label, or empty for a user. roles is empty
    for an SDK token, every management role for an opaque management
    token, and the local user roles for a JWT.
    """

    credential_id: int
    kind: str
    environment_id: int | None
    prefix: str
    roles: tuple[str, ...]
