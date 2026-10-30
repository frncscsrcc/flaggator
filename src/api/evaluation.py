"""Controllers for evaluate, evaluate-all, and evaluation-config.

The body is parsed here. The service checks the credential's environment
and reads the bundle through the cache.
"""

from api.body import read_json_object
from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.evaluation import Context, Decision
from domain.models.principal import Principal
from domain.services import evaluation as evaluation_service
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache

_EVALUATE_FIELDS = ("project_key", "environment_key", "flag_key", "context", "default")
_EVALUATE_ALL_FIELDS = ("project_key", "environment_key", "context")


def evaluate_one(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
) -> Response:
    """Evaluate one flag and return the decision.

    A missing flag is still 200, with the caller default. Returns 200.
    Does not commit.
    """
    project_key, environment_key, flag_key, context, default = _evaluate_command(request)
    decision = evaluation_service.evaluate_one(
        connection,
        evaluation_cache,
        principal,
        project_key,
        environment_key,
        flag_key,
        context,
        default,
    )
    return Response(status=200, body=_decision_document(decision))


def evaluate_all(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
) -> Response:
    """Evaluate every flag in one environment.

    Returns 200. decisions is empty when the environment has no flags.
    Does not commit.
    """
    project_key, environment_key, context = _evaluate_all_command(request)
    decisions = evaluation_service.evaluate_all(
        connection,
        evaluation_cache,
        principal,
        project_key,
        environment_key,
        context,
    )
    return Response(status=200, body={"decisions": [_decision_document(item) for item in decisions]})


def read_config(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    project_key: str,
    environment_key: str,
) -> Response:
    """Return the evaluation bundle for one environment.

    Salts are included. Secrets are absent. Returns 200. Does not commit.
    """
    document = evaluation_service.read_config(
        connection,
        evaluation_cache,
        principal,
        project_key,
        environment_key,
    )
    return Response(status=200, body=document)


def _evaluate_command(
    request: Request,
) -> tuple[str | None, str | None, str, Context, object]:
    """Return project, environment, flag, context, and default.

    project and environment are None when omitted. flag_key, context, and
    default are required. Raises ValidationError for a bad shape. Does not
    read the database.
    """
    body = read_json_object(request)
    details = _unknown(body, set(_EVALUATE_FIELDS))
    flag_key = body.get("flag_key")
    if "flag_key" not in body:
        details.append({"field": "flag_key", "message": "This field is required."})
    elif not isinstance(flag_key, str):
        details.append({"field": "flag_key", "message": "This field must be a string."})
    if "context" not in body:
        details.append({"field": "context", "message": "This field is required."})
    if "default" not in body:
        details.append({"field": "default", "message": "This field is required."})
    project_key = _optional_key(body, "project_key", details)
    environment_key = _optional_key(body, "environment_key", details)
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    if not isinstance(flag_key, str):
        raise ValidationError("The request body has the wrong fields.", details=details)
    return project_key, environment_key, flag_key, _context(body["context"]), body["default"]


def _evaluate_all_command(request: Request) -> tuple[str | None, str | None, Context]:
    """Return project, environment, and context for evaluate-all.

    project and environment are None when omitted. context is required.
    Raises ValidationError for a bad shape. Does not read the database.
    """
    body = read_json_object(request)
    details = _unknown(body, set(_EVALUATE_ALL_FIELDS))
    if "context" not in body:
        details.append({"field": "context", "message": "This field is required."})
    project_key = _optional_key(body, "project_key", details)
    environment_key = _optional_key(body, "environment_key", details)
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    return project_key, environment_key, _context(body["context"])


def _optional_key(body: dict, field: str, details: list[dict[str, str]]) -> str | None:
    """Return a string key, or None when field was omitted.

    Appends a detail when the field is present and not a string. Does not
    read the database.
    """
    if field not in body:
        return None
    value = body[field]
    if not isinstance(value, str):
        details.append({"field": field, "message": "This field must be a string."})
        return None
    return value


def _context(value: object) -> Context:
    """Return a context from the JSON object.

    subject may be omitted. attributes defaults to an empty map. Raises
    ValidationError when the shape is wrong. Does not check lengths; the
    service does.
    """
    if not isinstance(value, dict):
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": "context", "message": "Context must be an object."}],
        )
    unknown = sorted(set(value) - {"subject", "attributes"})
    if unknown:
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": "context", "message": "This field is not accepted."}],
        )
    subject = None
    if "subject" in value:
        if not isinstance(value["subject"], str):
            raise ValidationError(
                "The request body has the wrong fields.",
                details=[{"field": "context.subject", "message": "This field must be a string."}],
            )
        subject = value["subject"]
    attributes: dict[str, object] = {}
    if "attributes" in value:
        raw = value["attributes"]
        if not isinstance(raw, dict):
            raise ValidationError(
                "The request body has the wrong fields.",
                details=[{"field": "context.attributes", "message": "Attributes must be an object."}],
            )
        attributes = raw
    return Context(subject=subject, attributes=attributes)


def _unknown(body: dict, allowed: set[str]) -> list[dict[str, str]]:
    """Return one error for each field that is not allowed.

    An empty list means body has no extra fields. Does not read the database.
    """
    details: list[dict[str, str]] = []
    for field in sorted(set(body) - allowed):
        details.append({"field": field, "message": "This field is not accepted."})
    return details


def _decision_document(decision: Decision) -> dict:
    """Return the JSON object for one decision.

    variation_index, rule_id, and version are null when the decision has
    none. Does not read the database.
    """
    return {
        "flag_key": decision.flag_key,
        "value": decision.value,
        "variation_index": decision.variation_index,
        "reason": decision.reason,
        "rule_id": decision.rule_id,
        "version": decision.version,
    }
