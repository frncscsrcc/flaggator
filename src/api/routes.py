"""Choose a controller for a request, and say who may call the path.

GET /health is the only route that does not need a credential. This module
does not parse tokens and does not execute SQL. Management routes receive
the connection the adapter opened.
"""

from api.environments import create_environment, list_environments
from api.health import get_health
from api.projects import create_project, get_project, list_projects
from api.request import Request, Response
from domain.errors import Forbidden, StorageError
from domain.models.principal import KIND_MANAGEMENT, KIND_SDK, Principal
from repositories.db import Connection

_EVALUATION_PATHS = ("/v1/evaluate", "/v1/evaluate-all")


def dispatch(
    request: Request,
    principal: Principal | None = None,
    connection: Connection | None = None,
) -> Response:
    """Return the controller response for request.

    GET /health needs no credential. A different method on that path is
    405. Project and environment routes need the principal and the open
    connection the adapter already authenticated. Any other path is 404.
    Does not open a transaction and does not check the token.
    """
    if request.path == "/health":
        if request.method == "GET":
            return get_health(request)
        return method_not_allowed("GET")
    return _management_route(request, principal, connection)


def requires_credential(request: Request) -> bool:
    """Return False only for the health path.

    POST /health is still the health path: the method check answers it
    without a credential. Every other path needs a principal. Does not
    read the database.
    """
    return request.path != "/health"


def authorize(request: Request, principal: Principal) -> None:
    """Raise Forbidden when principal may not reach this path.

    A management token may continue, and the dispatcher then returns 404
    for a path that has no controller yet. An SDK key may continue only
    for an evaluation path. Does not compare the SDK key's environment
    with the request; that check belongs to the evaluation service. Does
    not commit.
    """
    if principal.kind == KIND_MANAGEMENT:
        return
    if principal.kind == KIND_SDK and request.path in _EVALUATION_PATHS:
        return
    raise Forbidden("The credential may not call this route.")


def _management_route(
    request: Request,
    principal: Principal | None,
    connection: Connection | None,
) -> Response:
    """Dispatch a project or environment path, or 404.

    A path this module knows requires a session. A missing session raises
    StorageError. Does not authenticate and does not commit.
    """
    parts = request.path.split("/")
    if len(parts) < 3 or parts[0] != "" or parts[1] != "v1" or parts[2] != "projects":
        return route_not_found()
    # The leading empty segment is the root slash. A later empty segment is
    # a trailing or doubled slash, which is not a route.
    if any(part == "" for part in parts[1:]):
        return route_not_found()
    session = _session(principal, connection)
    if len(parts) == 3:
        return _projects_collection(request, session[0], session[1])
    project_key = parts[3]
    if len(parts) == 4:
        if request.method != "GET":
            return method_not_allowed("GET")
        return get_project(session[0], session[1], project_key)
    if len(parts) == 5 and parts[4] == "environments":
        return _environments_collection(request, session[0], session[1], project_key)
    return route_not_found()


def _projects_collection(
    request: Request,
    principal: Principal,
    connection: Connection,
) -> Response:
    """Handle the project collection.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_projects(principal, connection, request)
    if request.method == "POST":
        return create_project(principal, connection, request)
    return method_not_allowed("GET or POST")


def _environments_collection(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
) -> Response:
    """Handle the environment collection under project_key.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_environments(principal, connection, request, project_key)
    if request.method == "POST":
        return create_environment(principal, connection, request, project_key)
    return method_not_allowed("GET or POST")


def _session(
    principal: Principal | None,
    connection: Connection | None,
) -> tuple[Principal, Connection]:
    """Return the authenticated session.

    Raises StorageError when the adapter called a management route without
    one. Does not open a connection.
    """
    if principal is None or connection is None:
        raise StorageError()
    return principal, connection


def method_not_allowed(allowed_method: str) -> Response:
    """Reject a method this route does not implement.

    allowed_method is a verb this module chose, not caller input, and it is
    included in the message. Returns 405. Does not read the database.
    """
    return Response(
        status=405,
        body={
            "error": {
                "code": "validation_error",
                "message": f"This route accepts {allowed_method}.",
                "details": [],
            }
        },
    )


def route_not_found() -> Response:
    """Answer an unknown path.

    Returns 404 and the standard error document. Does not read the database.
    """
    return Response(
        status=404,
        body={
            "error": {
                "code": "not_found",
                "message": "The route does not exist.",
                "details": [],
            }
        },
    )
