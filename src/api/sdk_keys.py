"""Controllers for SDK keys nested under an environment.

Create returns the secret once. List and revoke never return it.
"""

from api.body import read_json_object
from api.request import Request, Response
from domain.errors import NotFound, ValidationError
from domain.models.principal import Principal
from domain.services import sdk_keys as sdk_key_service
from repositories.credentials import SdkKeyRow
from repositories.db import Connection


def create_sdk_key(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
    environment_key: str,
) -> Response:
    """Create an SDK key and return the secret once.

    The body is an empty object. Returns 201. The secret is not written to
    the audit log. Domain failures propagate. Does not commit; the service
    does.
    """
    _empty_body(request)
    issued = sdk_key_service.issue_sdk_key(connection, principal, project_key, environment_key)
    return Response(
        status=201,
        body={
            "id": issued.id,
            "prefix": issued.prefix,
            "created_at": issued.created_at,
            "secret": issued.secret,
        },
    )


def list_sdk_keys(
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """List active SDK keys for one environment.

    Returns 200. items is empty when the environment has no keys. Secrets
    are absent. Does not commit.
    """
    rows = sdk_key_service.list_sdk_key_page(connection, principal, project_key, environment_key)
    return Response(status=200, body={"items": [_public_key(row) for row in rows]})


def revoke_sdk_key(
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
    key_id: str,
) -> Response:
    """Revoke one SDK key.

    Returns 204 with an empty object. An unknown id, including one that is
    already revoked, is 404. Does not commit; the service does.
    """
    credential_id = _credential_id(key_id)
    sdk_key_service.revoke_sdk_key_now(
        connection,
        principal,
        project_key,
        environment_key,
        credential_id,
    )
    return Response(status=204, body={})


def _empty_body(request: Request) -> None:
    """Raise ValidationError unless the body is an empty JSON object.

    Does not read the database.
    """
    body = read_json_object(request)
    if body:
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": "body", "message": "This request does not take fields."}],
        )


def _credential_id(key_id: str) -> int:
    """Return the integer id in the path.

    Raises NotFound when key_id is not a decimal id, using the same message
    as a missing row. Does not read the database.
    """
    if key_id == "0" or not key_id.isdigit() or (len(key_id) > 1 and key_id[0] == "0"):
        raise NotFound("SDK key does not exist.")
    return int(key_id)


def _public_key(row: SdkKeyRow) -> dict:
    """Return one list item. The secret is not included."""
    return {"id": row.id, "prefix": row.prefix, "created_at": row.created_at}
