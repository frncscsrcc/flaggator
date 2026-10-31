"""Authorization shared by management use cases.

The adapter applies a coarse path gate. Services call these helpers so the
role rule is visible on the use case and does not depend on HTTP. Opaque
management tokens carry every role on the principal.
"""

from domain.errors import Forbidden
from domain.models.principal import (
    ROLE_ADMIN,
    ROLE_PROTECTED_WRITER,
    ROLE_READER,
    ROLE_WRITER,
    Principal,
)


def require_reader(principal: Principal) -> None:
    """Raise Forbidden unless principal may read management resources.

    writer implies read. admin implies every management role. Does not
    read the database and does not commit.
    """
    if ROLE_READER in principal.roles or ROLE_WRITER in principal.roles or _is_admin(principal):
        return
    raise Forbidden("A reader or writer role is required.")


def require_writer(principal: Principal) -> None:
    """Raise Forbidden unless principal may mutate non-protected resources.

    admin implies every management role. Does not read the database and
    does not commit.
    """
    if ROLE_WRITER in principal.roles or _is_admin(principal):
        return
    raise Forbidden("A writer role is required.")


def require_admin(principal: Principal) -> None:
    """Raise Forbidden unless principal holds admin.

    An opaque management token holds every role, so it passes. admin
    implies every management role, including this one. Does not read the
    database and does not commit.
    """
    if ROLE_ADMIN in principal.roles:
        return
    raise Forbidden("An admin role is required.")


def require_protected_writer(principal: Principal) -> None:
    """Raise Forbidden unless principal holds protected_writer.

    An opaque management token holds every role, so it passes. admin
    implies this role. Does not read the database and does not commit.
    """
    if ROLE_PROTECTED_WRITER in principal.roles or _is_admin(principal):
        return
    raise Forbidden("A protected_writer role is required.")


def require_protected_write(principal: Principal, protected: bool) -> None:
    """Raise Forbidden unless principal may write given protected.

    Always requires writer. When protected is true, also requires
    protected_writer. admin satisfies both. Does not read the database
    and does not commit.
    """
    require_writer(principal)
    if protected and ROLE_PROTECTED_WRITER not in principal.roles and not _is_admin(principal):
        raise Forbidden("A protected_writer role is required for this environment.")


def _is_admin(principal: Principal) -> bool:
    """Return True when principal holds admin.

    Does not read the database.
    """
    return ROLE_ADMIN in principal.roles
