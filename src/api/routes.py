"""Choose a controller for a request, and say who may call the path.

GET /health is the only route that does not need a credential. This module
does not parse tokens and does not execute SQL. Management routes receive
the connection the adapter opened.
"""

from api.environments import create_environment, list_environments
from api.evaluation import evaluate_all, evaluate_one, read_config
from api.flags import create_flag, get_flag, list_flags, patch_flag
from api.health import get_health
from api.projects import create_project, get_project, list_projects
from api.request import Request, Response
from api.sdk_keys import create_sdk_key, list_sdk_keys, revoke_sdk_key
from api.targeting import get_targeting, replace_targeting
from domain.errors import Forbidden, StorageError
from domain.models.principal import KIND_MANAGEMENT, KIND_SDK, Principal
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache

_EVALUATION_PATHS = ("/v1/evaluate", "/v1/evaluate-all")


def dispatch(
    request: Request,
    principal: Principal | None = None,
    connection: Connection | None = None,
    evaluation_cache: EvaluationCache | None = None,
) -> Response:
    """Return the controller response for request.

    GET /health needs no credential. A different method on that path is
    405. Evaluation and management routes need the principal and the open
    connection the adapter already authenticated. Any other path is 404.
    Does not open a transaction and does not check the token.
    """
    if request.path == "/health":
        if request.method == "GET":
            return get_health(request)
        return method_not_allowed("GET")
    if request.path in _EVALUATION_PATHS:
        return _evaluate(request, principal, connection, evaluation_cache)
    return _management_route(request, principal, connection, evaluation_cache)


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
    for evaluate, evaluate-all, and evaluation-config. Does not compare
    the SDK key's environment with the request; that check belongs to the
    evaluation service. Does not commit.
    """
    if principal.kind == KIND_MANAGEMENT:
        return
    if principal.kind == KIND_SDK and _sdk_may_read(request):
        return
    raise Forbidden("The credential may not call this route.")


def _evaluate(
    request: Request,
    principal: Principal | None,
    connection: Connection | None,
    evaluation_cache: EvaluationCache | None,
) -> Response:
    """Handle POST /v1/evaluate and POST /v1/evaluate-all.

    Any other method is 405. A missing session raises StorageError. Does
    not commit.
    """
    session = _session(principal, connection)
    cache = _cache(evaluation_cache)
    if request.path == "/v1/evaluate":
        if request.method != "POST":
            return method_not_allowed("POST")
        return evaluate_one(session[0], session[1], cache, request)
    if request.method != "POST":
        return method_not_allowed("POST")
    return evaluate_all(session[0], session[1], cache, request)


def _sdk_may_read(request: Request) -> bool:
    """Return True when an SDK key is allowed to reach this path.

    The environment match is not checked here. Does not read the database.
    """
    if request.path in _EVALUATION_PATHS:
        return True
    parts = request.path.split("/")
    return (
        len(parts) == 7
        and parts[0] == ""
        and parts[1] == "v1"
        and parts[2] == "projects"
        and parts[4] == "environments"
        and parts[6] == "evaluation-config"
        and parts[3] != ""
        and parts[5] != ""
    )


def _management_route(
    request: Request,
    principal: Principal | None,
    connection: Connection | None,
    evaluation_cache: EvaluationCache | None,
) -> Response:
    """Dispatch a project, environment, flag, or targeting path, or 404.

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
    if len(parts) == 5 and parts[4] == "flags":
        return _flags_collection(request, session[0], session[1], evaluation_cache, project_key)
    if len(parts) == 6 and parts[4] == "flags":
        return _one_flag(request, session[0], session[1], evaluation_cache, project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "sdk-keys":
        return _sdk_keys(request, session[0], session[1], project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "evaluation-config":
        return _evaluation_config(
            request,
            session[0],
            session[1],
            evaluation_cache,
            project_key,
            parts[5],
        )
    if len(parts) == 8 and parts[4] == "environments" and parts[6] == "sdk-keys":
        return _one_sdk_key(request, session[0], session[1], project_key, parts[5], parts[7])
    if len(parts) == 8 and parts[4] == "flags" and parts[6] == "environments":
        return _flag_environment(
            request,
            session[0],
            session[1],
            evaluation_cache,
            project_key,
            parts[5],
            parts[7],
        )
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


def _flags_collection(
    request: Request,
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache | None,
    project_key: str,
) -> Response:
    """Handle the flag collection under project_key.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_flags(principal, connection, request, project_key)
    if request.method == "POST":
        return create_flag(principal, connection, _cache(evaluation_cache), request, project_key)
    return method_not_allowed("GET or POST")


def _one_flag(
    request: Request,
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache | None,
    project_key: str,
    flag_key: str,
) -> Response:
    """Handle one flag.

    GET fetches and PATCH updates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return get_flag(principal, connection, project_key, flag_key)
    if request.method == "PATCH":
        return patch_flag(
            principal,
            connection,
            _cache(evaluation_cache),
            request,
            project_key,
            flag_key,
        )
    return method_not_allowed("GET or PATCH")


def _sdk_keys(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Handle SDK keys for one environment.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_sdk_keys(principal, connection, project_key, environment_key)
    if request.method == "POST":
        return create_sdk_key(principal, connection, request, project_key, environment_key)
    return method_not_allowed("GET or POST")


def _one_sdk_key(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
    key_id: str,
) -> Response:
    """Revoke one SDK key.

    DELETE is the only method. Does not commit.
    """
    if request.method != "DELETE":
        return method_not_allowed("DELETE")
    return revoke_sdk_key(principal, connection, project_key, environment_key, key_id)


def _evaluation_config(
    request: Request,
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache | None,
    project_key: str,
    environment_key: str,
) -> Response:
    """Return the evaluation bundle for one environment.

    GET is the only method. Does not commit.
    """
    if request.method != "GET":
        return method_not_allowed("GET")
    return read_config(
        principal,
        connection,
        _cache(evaluation_cache),
        project_key,
        environment_key,
    )


def _flag_environment(
    request: Request,
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache | None,
    project_key: str,
    flag_key: str,
    environment_key: str,
) -> Response:
    """Handle one flag's targeting in one environment.

    GET fetches and PUT replaces the whole document. Any other method is
    405. Does not commit.
    """
    if request.method == "GET":
        return get_targeting(principal, connection, project_key, flag_key, environment_key)
    if request.method == "PUT":
        return replace_targeting(
            principal,
            connection,
            _cache(evaluation_cache),
            request,
            project_key,
            flag_key,
            environment_key,
        )
    return method_not_allowed("GET or PUT")


def _cache(evaluation_cache: EvaluationCache | None) -> EvaluationCache:
    """Return the evaluation cache the adapter built.

    Raises StorageError when a route that reads or invalidates the bundle
    was called without one. Does not connect.
    """
    if evaluation_cache is None:
        raise StorageError()
    return evaluation_cache


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
