"""Controllers for flags nested under a project.

Each function checks the request shape, calls one service, and maps the
result. Variation indexes are positions in the variations array.
"""

from api.body import read_json_object
from api.paging import page_body, page_request
from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.flag import CreateFlag, Flag, FlagConfig, FlagSummary, PatchFlag
from domain.models.principal import Principal
from domain.services import flags as flag_service
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache

_CREATE_FIELDS = ("key", "name", "description", "variations")
_PATCH_FIELDS = ("name", "description", "variations")


def create_flag(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
    project_key: str,
) -> Response:
    """Create a flag from the JSON body.

    Returns 201 and the stored flag, including a disabled configuration for
    each environment. Domain failures propagate. Does not commit; the
    service does.
    """
    flag = flag_service.create_flag(
        connection,
        principal,
        project_key,
        _create_command(request),
        evaluation_cache,
    )
    return Response(status=201, body=_flag_document(flag))


def list_flags(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
) -> Response:
    """List flags for the query limit, cursor, and optional environment.

    Returns 200. items is empty when the project has no flags on this page.
    next_cursor is null on the last page. Does not commit.
    """
    page = flag_service.list_flags_page(
        connection,
        principal,
        project_key,
        page_request(request),
        request.query.get("environment"),
    )
    items = [_summary_document(item) for item in page.items]
    return Response(status=200, body=page_body(items, page.next_key))


def get_flag(
    principal: Principal,
    connection: Connection,
    project_key: str,
    flag_key: str,
) -> Response:
    """Fetch one flag by its path key.

    Returns 200, including every environment configuration. NotFound
    propagates when the flag is absent. Does not commit.
    """
    flag = flag_service.get_flag(connection, principal, project_key, flag_key)
    return Response(status=200, body=_flag_document(flag))


def patch_flag(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
    project_key: str,
    flag_key: str,
) -> Response:
    """Patch the name, the description, or the variation list.

    Returns 200 and the stored flag. A key in the body is rejected here.
    Domain failures propagate. Does not commit; the service does.
    """
    flag = flag_service.patch_flag(
        connection,
        principal,
        project_key,
        flag_key,
        _patch_command(request),
        evaluation_cache,
    )
    return Response(status=200, body=_flag_document(flag))


def _create_command(request: Request) -> CreateFlag:
    """Build a create command from the JSON body.

    Raises ValidationError when a required field is missing, has the wrong
    type, or when the body contains another field. Does not read the database.
    """
    body = read_json_object(request)
    details = _string_details(body, ("key", "name", "description"))
    if "variations" not in body:
        details.append({"field": "variations", "message": "This field is required."})
    elif not isinstance(body["variations"], list):
        details.append({"field": "variations", "message": "This field must be a list."})
    details.extend(_unknown_details(body, set(_CREATE_FIELDS)))
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    raw_variations = body["variations"]
    if not isinstance(raw_variations, list):
        raise ValidationError("The request body has the wrong fields.", details=details)
    return CreateFlag(
        key=body["key"],
        name=body["name"],
        description=body["description"],
        variations=tuple(raw_variations),
    )


def _patch_command(request: Request) -> PatchFlag:
    """Build a patch command from the JSON body.

    Raises ValidationError when the body is empty, changes the key, or gives
    a field the wrong type. Omitted fields stay None. Does not read the
    database.
    """
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    if "key" in body:
        details.append({"field": "key", "message": "The flag key cannot be changed."})
    if not any(field in body for field in _PATCH_FIELDS):
        details.append(
            {
                "field": "body",
                "message": "Send a name, a description, or variations.",
            }
        )
    for field in ("name", "description"):
        if field in body and not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    if "variations" in body and not isinstance(body["variations"], list):
        details.append({"field": "variations", "message": "This field must be a list."})
    details.extend(_unknown_details(body, set(_PATCH_FIELDS) | {"key"}))
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    name = body["name"] if "name" in body else None
    description = body["description"] if "description" in body else None
    if "variations" in body:
        variations = tuple(body["variations"])
    else:
        variations = None
    return PatchFlag(name=name, description=description, variations=variations)


def _string_details(body: dict, fields: tuple[str, ...]) -> list[dict[str, str]]:
    """Return shape errors for required string fields.

    An empty list means each field is present and is a string. Does not
    read the database.
    """
    details: list[dict[str, str]] = []
    for field in fields:
        if field not in body:
            details.append({"field": field, "message": "This field is required."})
        elif not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    return details


def _unknown_details(body: dict, allowed: set[str]) -> list[dict[str, str]]:
    """Return one error for each field that is not allowed.

    An empty list means body has no extra fields. Does not read the database.
    """
    details: list[dict[str, str]] = []
    for field in sorted(set(body) - allowed):
        details.append({"field": field, "message": "This field is not accepted."})
    return details


def _flag_document(flag: Flag) -> dict:
    """Return the JSON object for one flag and its environment configurations.

    variations stay in stored order. Does not include a database id.
    """
    return {
        "key": flag.key,
        "name": flag.name,
        "description": flag.description,
        "variations": list(flag.variations),
        "environments": [_config_document(item) for item in flag.environments],
    }


def _config_document(config: FlagConfig) -> dict:
    """Return the JSON object for one environment configuration.

    Does not read the database.
    """
    return {
        "key": config.key,
        "enabled": config.enabled,
        "off_variation": config.off_variation,
        "fallthrough": config.fallthrough,
        "rules": config.rules,
        "targets": config.targets,
        "prerequisites": config.prerequisites,
        "version": config.version,
    }


def _summary_document(item: FlagSummary) -> dict:
    """Return the JSON object for one list item.

    environment is omitted when the caller did not ask for one. Does not
    read the database.
    """
    body: dict = {
        "key": item.key,
        "name": item.name,
        "description": item.description,
        "variations": list(item.variations),
    }
    if item.environment is not None:
        body["environment"] = {
            "key": item.environment.key,
            "enabled": item.environment.enabled,
            "off_variation": item.environment.off_variation,
            "version": item.environment.version,
        }
    return body
