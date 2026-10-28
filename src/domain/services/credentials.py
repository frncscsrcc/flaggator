"""Issue opaque credentials.

A standard JWT is not issued here. See decisions P13.
"""

import secrets

from domain.clock import utc_timestamp
from domain.models.principal import KIND_MANAGEMENT
from repositories.credentials import insert_credential
from repositories.db import Connection

_TOKEN_BYTES = 32


def issue_management_token(connection: Connection) -> str:
    """Create a management token and return the plaintext secret once.

    Stores only the hash and a prefix. Does not commit. Does not write an
    audit row, because that row would have to omit the only copy of the
    secret the operator has not seen yet. The caller prints the return
    value and does not log it.
    """
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    insert_credential(
        connection,
        kind=KIND_MANAGEMENT,
        token=token,
        environment_id=None,
        created_at=utc_timestamp(),
    )
    return token
