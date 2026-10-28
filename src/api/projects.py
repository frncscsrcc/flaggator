"""Controllers for the project collection and one project.

Each function checks the request shape, calls one service, and maps the
result to a response. SQL and authorization stay in the service.
"""

from api.body import read_json_object, require_string_fields
from api.paging import page_body, page_request
from api.request import Request, Response
from domain.models.catalog import CreateNamed
from domain.models.principal import Principal
from domain.services import projects as project_service
from repositories.db import Connection


def create_project(principal: Principal, connection: Connection, request: Request) -> Response:
    """Create a project from the JSON body.

    Returns 201 and the stored key and name. Raises ValidationError when
    the body shape is wrong. Domain failures propagate. Does not commit;
    the service does.
    """
    fields = require_string_fields(read_json_object(request), ("key", "name"))
    project = project_service.create_project(
        connection,
        principal,
        CreateNamed(key=fields["key"], name=fields["name"]),
    )
    return Response(status=201, body={"key": project.key, "name": project.name})


def list_projects(principal: Principal, connection: Connection, request: Request) -> Response:
    """List projects for the query limit and cursor.

    Returns 200. items is empty when no project matches. next_cursor is
    null on the last page. Does not commit.
    """
    page = project_service.list_projects_page(connection, principal, page_request(request))
    items = [{"key": item.key, "name": item.name} for item in page.items]
    return Response(status=200, body=page_body(items, page.next_key))


def get_project(principal: Principal, connection: Connection, project_key: str) -> Response:
    """Fetch one project by its path key.

    Returns 200 and the key and name. NotFound propagates when the project
    is absent. Does not commit.
    """
    project = project_service.get_project(connection, principal, project_key)
    return Response(status=200, body={"key": project.key, "name": project.name})
