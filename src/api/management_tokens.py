"""Controllers for opaque management tokens.

List and revoke never return a secret. Bootstrap is the only way to
create one.
"""

from api.request import Response
from domain.errors import NotFound
from domain.models.principal import Principal
from domain.services import credentials as credential_service
from repositories.credentials import ManagementTokenRow
from repositories.db import Connection


def list_management_tokens(
    principal: Principal,
    connection: Connection,
) -> Response:
    """List active management tokens.

    Returns 200. items is empty when none are active. Secrets are absent.
    Does not commit.
    """
    rows = credential_service.list_management_token_page(connection, principal)
    return Response(status=200, body={"items": [_public_token(row) for row in rows]})


def revoke_management_token(
    principal: Principal,
    connection: Connection,
    token_id: str,
) -> Response:
    """Revoke one management token.

    Returns 204 with an empty object. An unknown id, including one that is
    already revoked, is 404. Does not commit; the service does.
    """
    credential_id = _credential_id(token_id)
    credential_service.revoke_management_token_now(connection, principal, credential_id)
    return Response(status=204, body={})


def _credential_id(raw: str) -> int:
    """Return a positive decimal credential id.

    A non-decimal value is the same 404 as a missing token.
    """
    if not raw.isdigit() or (len(raw) > 1 and raw[0] == "0"):
        raise NotFound("Management token does not exist.")
    return int(raw)


def _public_token(row: ManagementTokenRow) -> dict:
    """Return one list item. The secret is not included."""
    return {"id": row.id, "prefix": row.prefix, "created_at": row.created_at}
