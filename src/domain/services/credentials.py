"""Issue and revoke opaque management credentials.

A standard JWT is not issued here. See decisions P13.
"""

import json
import secrets

from domain.clock import utc_timestamp
from domain.errors import NotFound
from domain.models.principal import KIND_MANAGEMENT, Principal
from domain.services.access import require_protected_writer, require_reader
from domain.services.audit import record_change
from repositories.credentials import (
    ManagementTokenRow,
    insert_credential,
    list_management_tokens,
    revoke_management_token,
)
from repositories.db import Connection

_AUDIT_PROJECT = ""

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


def list_management_token_page(
    connection: Connection,
    principal: Principal,
) -> list[ManagementTokenRow]:
    """Return active management tokens.

    Raises Forbidden without reader. Secrets are not loaded. Does not commit.
    """
    require_reader(principal)
    return list_management_tokens(connection)


def revoke_management_token_now(
    connection: Connection,
    principal: Principal,
    credential_id: int,
) -> None:
    """Revoke one management token.

    Raises Forbidden without protected_writer. Raises NotFound when the id
    is missing or already revoked. The audit document has the id and the
    prefix, never the secret. Does not print the secret.
    """
    require_protected_writer(principal)
    with connection.transaction():
        rows = list_management_tokens(connection)
        prefix = ""
        found = False
        for row in rows:
            if row.id == credential_id:
                prefix = row.prefix
                found = True
        if not found:
            raise NotFound("Management token does not exist.")
        changed = revoke_management_token(connection, credential_id, utc_timestamp())
        if changed == 0:
            raise NotFound("Management token does not exist.")
        document = json.dumps(
            {"id": credential_id, "prefix": prefix},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        record_change(
            connection,
            principal,
            "management_token.revoke",
            _AUDIT_PROJECT,
            None,
            None,
            document,
            document,
        )
