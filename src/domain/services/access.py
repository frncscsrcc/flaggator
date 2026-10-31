"""Authorization shared by management use cases.

The adapter applies a coarse path gate. Services call these helpers so the
role rule is visible on the use case and does not depend on HTTP. Opaque
management tokens carry every role on the principal.
"""

from domain.errors import Forbidden
from domain.models.principal import (
    ROLE_PROTECTED_WRITER,
    ROLE_READER,
    ROLE_WRITER,
    Principal,
)


def require_reader(principal: Principal) -> None:
    """Raise Forbidden unless principal may read management resources.

    writer implies read. Does not read the database and does not commit.
    """
    if ROLE_READER in principal.roles or ROLE_WRITER in principal.roles:
        return
    raise Forbidden("A reader or writer role is required.")


def require_writer(principal: Principal) -> None:
    """Raise Forbidden unless principal may mutate non-protected resources.

    Does not read the database and does not commit.
    """
    if ROLE_WRITER in principal.roles:
        return
    raise Forbidden("A writer role is required.")


def require_protected_writer(principal: Principal) -> None:
    """Raise Forbidden unless principal holds protected_writer.

    An opaque management token holds every role, so it passes. Does not
    read the database and does not commit.
    """
    if ROLE_PROTECTED_WRITER in principal.roles:
        return
    raise Forbidden("A protected_writer role is required.")


def require_protected_write(principal: Principal, protected: bool) -> None:
    """Raise Forbidden unless principal may write given protected.

    Always requires writer. When protected is true, also requires
    protected_writer. Does not read the database and does not commit.
    """
    require_writer(principal)
    if protected and ROLE_PROTECTED_WRITER not in principal.roles:
        raise Forbidden("A protected_writer role is required for this environment.")
