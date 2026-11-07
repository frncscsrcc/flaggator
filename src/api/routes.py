"""Choose a controller for a request, and say who may call the path.

GET /health, POST /v1/auth/login, and POST /v1/client-evaluate do not need a
credential. This module
does not parse tokens and does not execute SQL. Management routes receive
the connection the adapter opened.
"""

from api.audit import list_audit, list_server_audit
from api.environments import create_environment, issue_client_id, list_environments, patch_environment
from api.evaluation import evaluate_all, evaluate_one, read_config
from api.flags import create_flag, get_flag, list_flags, patch_flag
from api.management_tokens import list_management_tokens, revoke_management_token
from api.health import get_health
from api.projects import create_project, get_project, list_projects
from api.request import Request, Response
from api.sdk_tokens import create_sdk_token, list_sdk_tokens, revoke_sdk_token
from api.segments import (
    create_segment,
    delete_segment,
    get_segment,
    list_segments,
    replace_segment,
)
from api.targeting import get_targeting, replace_targeting
from api.users import create_user, get_user, list_users, patch_user
from domain.errors import Forbidden, StorageError
from domain.models.principal import (
    KIND_MANAGEMENT,
    KIND_SDK,
    KIND_USER,
    ROLE_ADMIN,
    ROLE_READER,
    ROLE_WRITER,
    Principal,
)
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache

_EVALUATION_PATHS = ("/v1/evaluate", "/v1/evaluate-all")
_PUBLIC_PATHS = frozenset({"/health", "/v1/auth/login", "/v1/client-evaluate"})


def dispatch(
    request: Request,
    principal: Principal | None = None,
    connection: Connection | None = None,
    evaluation_cache: EvaluationCache | None = None,
) -> Response:
    """Return the controller response for request.

    GET /health needs no credential. Login is handled by the adapter
    before this function. Evaluation and management routes need the
    principal and the open connection. Any other path is 404. Does not
    open a transaction and does not check the token.
    """
    if request.path == "/health":
        if request.method == "GET":
            return get_health(request)
        return method_not_allowed("GET")
    if request.path in _EVALUATION_PATHS:
        return _evaluate(request, principal, connection, evaluation_cache)
    return _management_route(request, principal, connection, evaluation_cache)


def requires_credential(request: Request) -> bool:
    """Return False for health, login, and client evaluation.

    Other methods on those paths still reach the method check without a
    credential. Every other path needs a principal. Does not read the
    database.
    """
    return request.path not in _PUBLIC_PATHS


def authorize(request: Request, principal: Principal) -> None:
    """Raise Forbidden when principal may not reach this path.

    A management token may continue. An SDK token may continue only for
    evaluate, evaluate-all, and evaluation-config. A user JWT may continue
    on management paths when it has reader, writer, or admin; it cannot
    evaluate.
    Fine role checks stay in the services. Does not commit.
    """
    if principal.kind == KIND_MANAGEMENT:
        return
    if principal.kind == KIND_SDK:
        if _sdk_may_read(request):
            return
        raise Forbidden("The credential may not call this route.")
    if principal.kind == KIND_USER:
        if _sdk_may_read(request) or request.path in _EVALUATION_PATHS:
            raise Forbidden("The credential may not call this route.")
        if (
            ROLE_READER in principal.roles
            or ROLE_WRITER in principal.roles
            or ROLE_ADMIN in principal.roles
        ):
            return
        raise Forbidden("The credential may not call this route.")
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
    """Return True when an SDK token is allowed to reach this path.

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
    if len(parts) < 3 or parts[0] != "" or parts[1] != "v1":
        return route_not_found()
    # The leading empty segment is the root slash. A later empty segment is
    # a trailing or doubled slash, which is not a route.
    if any(part == "" for part in parts[1:]):
        return route_not_found()
    session = _session(principal, connection)
    if parts[2] == "management-tokens":
        if len(parts) == 3:
            return _management_tokens_collection(request, session[0], session[1])
        if len(parts) == 4:
            return _one_management_token(request, session[0], session[1], parts[3])
        return route_not_found()
    if parts[2] == "audit" and len(parts) == 3:
        if request.method != "GET":
            return method_not_allowed("GET")
        return list_server_audit(principal, connection, request)
    if parts[2] == "users":
        if len(parts) == 3:
            return _users_collection(request, session[0], session[1])
        if len(parts) == 4:
            return _one_user(request, session[0], session[1], parts[3])
        return route_not_found()
    if parts[2] != "projects":
        return route_not_found()
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
    if len(parts) == 5 and parts[4] == "audit":
        if request.method != "GET":
            return method_not_allowed("GET")
        return list_audit(session[0], session[1], request, project_key)
    if len(parts) == 6 and parts[4] == "environments":
        return _one_environment(request, session[0], session[1], project_key, parts[5])
    if len(parts) == 6 and parts[4] == "flags":
        return _one_flag(request, session[0], session[1], evaluation_cache, project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "client-id":
        return _client_id(request, session[0], session[1], project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "segments":
        return _segments(request, session[0], session[1], project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "sdk-tokens":
        return _sdk_tokens(request, session[0], session[1], project_key, parts[5])
    if len(parts) == 7 and parts[4] == "environments" and parts[6] == "evaluation-config":
        return _evaluation_config(
            request,
            session[0],
            session[1],
            evaluation_cache,
            project_key,
            parts[5],
        )
    if len(parts) == 8 and parts[4] == "environments" and parts[6] == "segments":
        return _one_segment(
            request,
            session[0],
            session[1],
            evaluation_cache,
            project_key,
            parts[5],
            parts[7],
        )
    if len(parts) == 8 and parts[4] == "environments" and parts[6] == "sdk-tokens":
        return _one_sdk_token(request, session[0], session[1], project_key, parts[5], parts[7])
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


def _management_tokens_collection(
    request: Request,
    principal: Principal,
    connection: Connection,
) -> Response:
    """List management tokens.

    GET is the only method. Does not commit.
    """
    if request.method != "GET":
        return method_not_allowed("GET")
    return list_management_tokens(principal, connection)


def _one_management_token(
    request: Request,
    principal: Principal,
    connection: Connection,
    token_id: str,
) -> Response:
    """Revoke one management token.

    DELETE is the only method. Does not commit.
    """
    if request.method != "DELETE":
        return method_not_allowed("DELETE")
    return revoke_management_token(principal, connection, token_id)


def _users_collection(
    request: Request,
    principal: Principal,
    connection: Connection,
) -> Response:
    """Handle the users collection.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_users(principal, connection, request)
    if request.method == "POST":
        return create_user(principal, connection, request)
    return method_not_allowed("GET or POST")


def _one_user(
    request: Request,
    principal: Principal,
    connection: Connection,
    user_id: str,
) -> Response:
    """Handle one user by id.

    GET fetches and PATCH updates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return get_user(principal, connection, user_id)
    if request.method == "PATCH":
        return patch_user(principal, connection, request, user_id)
    return method_not_allowed("GET or PATCH")


def _client_id(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Issue or rotate the client id for one environment.

    POST is the only method. Does not commit.
    """
    if request.method != "POST":
        return method_not_allowed("POST")
    return issue_client_id(principal, connection, project_key, environment_key)


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


def _one_environment(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Handle one environment under project_key.

    PATCH updates name and/or protected. Any other method is 405. Does not
    commit.
    """
    if request.method == "PATCH":
        return patch_environment(
            principal,
            connection,
            request,
            project_key,
            environment_key,
        )
    return method_not_allowed("PATCH")


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


def _segments(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Handle segments for one environment.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_segments(principal, connection, request, project_key, environment_key)
    if request.method == "POST":
        return create_segment(principal, connection, request, project_key, environment_key)
    return method_not_allowed("GET or POST")


def _one_segment(
    request: Request,
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache | None,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> Response:
    """Handle one segment.

    GET fetches, PUT replaces the name and subjects, and DELETE removes the
    segment when no target cites it. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return get_segment(principal, connection, project_key, environment_key, segment_key)
    if request.method == "PUT":
        return replace_segment(
            principal,
            connection,
            _cache(evaluation_cache),
            request,
            project_key,
            environment_key,
            segment_key,
        )
    if request.method == "DELETE":
        return delete_segment(principal, connection, project_key, environment_key, segment_key)
    return method_not_allowed("GET, PUT, or DELETE")


def _sdk_tokens(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> Response:
    """Handle SDK tokens for one environment.

    GET lists and POST creates. Any other method is 405. Does not commit.
    """
    if request.method == "GET":
        return list_sdk_tokens(principal, connection, project_key, environment_key)
    if request.method == "POST":
        return create_sdk_token(principal, connection, request, project_key, environment_key)
    return method_not_allowed("GET or POST")


def _one_sdk_token(
    request: Request,
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
    token_id: str,
) -> Response:
    """Revoke one SDK token.

    DELETE is the only method. Does not commit.
    """
    if request.method != "DELETE":
        return method_not_allowed("DELETE")
    return revoke_sdk_token(principal, connection, project_key, environment_key, token_id)


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
