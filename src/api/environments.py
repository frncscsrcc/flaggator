"""Controllers for environments nested under a project.

Each function checks the request shape, calls one service, and maps the
result. The response never includes an SDK secret.
"""

from api.body import read_json_object, require_string_fields
from api.paging import page_body, page_request
from api.request import Request, Response
from domain.models.catalog import CreateNamed
from domain.models.principal import Principal
from domain.services import environments as environment_service
from repositories.db import Connection


def create_environment(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
) -> Response:
    """Create an environment in project_key from the JSON body.

    Returns 201 and the stored key and name. Does not return an SDK secret.
    Domain failures propagate. Does not commit; the service does.
    """
    fields = require_string_fields(read_json_object(request), ("key", "name"))
    environment = environment_service.create_environment(
        connection,
        principal,
        project_key,
        CreateNamed(key=fields["key"], name=fields["name"]),
    )
    return Response(status=201, body={"key": environment.key, "name": environment.name})


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
    items = [{"key": item.key, "name": item.name} for item in page.items]
    return Response(status=200, body=page_body(items, page.next_key))
