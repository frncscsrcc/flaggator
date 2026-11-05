"""Controllers for environments nested under a project.

Each function checks the request shape, calls one service, and maps the
result. The response never includes an SDK secret.
"""

from api.body import read_json_object
from api.paging import page_body, page_request
from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.catalog import CreateEnvironment, Environment, PatchEnvironment
from domain.models.principal import Principal
from domain.services import environments as environment_service
from repositories.db import Connection

_PATCH_FIELDS = ("name", "protected")


def create_environment(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
) -> Response:
    """Create an environment in project_key from the JSON body.

    Returns 201 and the stored key, name, and protected. Does not return an
    SDK secret. Domain failures propagate. Does not commit; the service does.
    """
    environment = environment_service.create_environment(
        connection,
        principal,
        project_key,
        _create_command(request),
    )
    return Response(status=201, body=_environment_document(environment))


def patch_environment(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
    environment_key: str,
) -> Response:
    """Patch name and/or protected for one environment.

    Returns 200 and the stored environment. Domain failures propagate. Does
    not commit; the service does.
    """
    environment = environment_service.patch_environment(
        connection,
        principal,
        project_key,
        environment_key,
        _patch_command(request),
    )
    return Response(status=200, body=_environment_document(environment))


def issue_client_id(
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Issue or rotate the client id and return the stored environment.

    The id is in the response. There is no separate secret. Domain failures
    propagate. Does not commit; the service does.
    """
    environment = environment_service.issue_client_id(
        connection,
        principal,
        project_key,
        environment_key,
    )
    return Response(status=200, body=_environment_document(environment))


def list_environments(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
) -> Response:
    """List environments in project_key.

    Returns 200. items is empty when the project has no environments on
    this page. next_cursor is null on the last page. Does not commit.
    """
    page = environment_service.list_environments_page(
        connection,
        principal,
        project_key,
        page_request(request),
    )
    items = [_environment_document(item) for item in page.items]
    return Response(status=200, body=page_body(items, page.next_key))


def _create_command(request: Request) -> CreateEnvironment:
    """Build a create command from the JSON body.

    key and name are required strings. protected is an optional boolean and
    defaults to false. Raises ValidationError for a wrong shape. Does not
    read the database.
    """
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    for field in ("key", "name"):
        if field not in body:
            details.append({"field": field, "message": "This field is required."})
        elif not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    protected = False
    if "protected" in body:
        if not isinstance(body["protected"], bool):
            details.append({"field": "protected", "message": "This field must be a boolean."})
        else:
            protected = body["protected"]
    for field in sorted(set(body) - {"key", "name", "protected"}):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    return CreateEnvironment(key=body["key"], name=body["name"], protected=protected)


def _patch_command(request: Request) -> PatchEnvironment:
    """Build a patch command from the JSON body.

    Raises ValidationError when the body is empty, changes the key, or gives
    a field the wrong type. Omitted fields stay None. Does not read the
    database.
    """
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    if "key" in body:
        details.append({"field": "key", "message": "The environment key cannot be changed."})
    if not any(field in body for field in _PATCH_FIELDS):
        details.append(
            {
                "field": "body",
                "message": "Send a name or protected.",
            }
        )
    if "name" in body and not isinstance(body["name"], str):
        details.append({"field": "name", "message": "This field must be a string."})
    if "protected" in body and not isinstance(body["protected"], bool):
        details.append({"field": "protected", "message": "This field must be a boolean."})
    for field in sorted(set(body) - set(_PATCH_FIELDS) - {"key"}):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    name = body["name"] if "name" in body else None
    protected = body["protected"] if "protected" in body else None
    return PatchEnvironment(name=name, protected=protected)


def _environment_document(environment: Environment) -> dict:
    """Return the JSON object for one environment.

    Does not include a secret or a database id.
    """
    return {
        "key": environment.key,
        "name": environment.name,
        "protected": environment.protected,
        "client_id": environment.client_id,
    }
