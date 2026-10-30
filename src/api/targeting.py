"""Controllers for one flag's targeting in one environment.

Each function checks the request shape, calls one service, and maps the
result. The environment key stays in the path. The body does not repeat it.
"""

from typing import NoReturn

from api.body import read_json_object
from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.flag import FlagConfig
from domain.models.principal import Principal
from domain.models.targeting import (
    ClauseInput,
    PrerequisiteInput,
    ReplaceTargeting,
    RolloutInput,
    RolloutWeightInput,
    RuleInput,
    TargetInput,
    VariationServeInput,
)
from domain.services import targeting as targeting_service
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache

_FIELDS = (
    "enabled",
    "off_variation",
    "fallthrough",
    "rules",
    "targets",
    "prerequisites",
    "version",
)


def get_targeting(
    principal: Principal,
    connection: Connection,
    project_key: str,
    flag_key: str,
    environment_key: str,
) -> Response:
    """Fetch one flag's targeting in one environment.

    Returns 200 and the stored document, including version. NotFound
    propagates when the project, flag, or environment is absent. Does not
    commit.
    """
    config = targeting_service.get_targeting(
        connection,
        principal,
        project_key,
        flag_key,
        environment_key,
    )
    return Response(status=200, body=_document(config))


def replace_targeting(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
    project_key: str,
    flag_key: str,
    environment_key: str,
) -> Response:
    """Replace one environment's targeting from the JSON body.

    Returns 200 and the stored document. The version in the body is the
    version the client read. Domain failures propagate. Does not commit;
    the service does.
    """
    config = targeting_service.replace_targeting(
        connection,
        principal,
        project_key,
        flag_key,
        environment_key,
        _replace_command(request),
        evaluation_cache,
    )
    return Response(status=200, body=_document(config))


def _replace_command(request: Request) -> ReplaceTargeting:
    """Build a replace command from the JSON body.

    Raises ValidationError when a field is missing, has the wrong type, or
    is not part of the targeting document. Does not read the database.
    Variation indexes and rollout weights are checked by the service.
    """
    body = read_json_object(request)
    details = _shape_details(body)
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    return ReplaceTargeting(
        enabled=body["enabled"],
        off_variation=body["off_variation"],
        fallthrough=_parse_serve(body["fallthrough"], "fallthrough"),
        rules=_parse_rules(body["rules"]),
        targets=_parse_targets(body["targets"]),
        prerequisites=_parse_prerequisites(body["prerequisites"]),
        version=body["version"],
    )


def _shape_details(body: dict) -> list[dict[str, str]]:
    """Return shape errors for the top-level targeting fields.

    An empty list means each field is present and has its JSON type. Does
    not read the database and does not inspect nested documents.
    """
    details: list[dict[str, str]] = []
    if "enabled" not in body:
        details.append({"field": "enabled", "message": "This field is required."})
    elif not isinstance(body["enabled"], bool):
        details.append({"field": "enabled", "message": "This field must be a boolean."})
    if "off_variation" not in body:
        details.append({"field": "off_variation", "message": "This field is required."})
    elif not _is_whole(body["off_variation"]):
        details.append({"field": "off_variation", "message": "This field must be an integer."})
    if "fallthrough" not in body:
        details.append({"field": "fallthrough", "message": "This field is required."})
    elif not isinstance(body["fallthrough"], dict):
        details.append({"field": "fallthrough", "message": "This field must be an object."})
    if "rules" not in body:
        details.append({"field": "rules", "message": "This field is required."})
    elif not isinstance(body["rules"], list):
        details.append({"field": "rules", "message": "This field must be a list."})
    if "targets" not in body:
        details.append({"field": "targets", "message": "This field is required."})
    elif not isinstance(body["targets"], dict):
        details.append({"field": "targets", "message": "This field must be an object."})
    if "prerequisites" not in body:
        details.append({"field": "prerequisites", "message": "This field is required."})
    elif not isinstance(body["prerequisites"], list):
        details.append({"field": "prerequisites", "message": "This field must be a list."})
    if "version" not in body:
        details.append({"field": "version", "message": "This field is required."})
    elif not _is_whole(body["version"]):
        details.append({"field": "version", "message": "This field must be an integer."})
    details.extend(_unknown_details(body, set(_FIELDS)))
    return details


def _parse_rules(rules: list) -> tuple[RuleInput, ...]:
    """Return the rules in body order.

    Raises ValidationError when an item is not a rule object. Does not read
    the database.
    """
    parsed: list[RuleInput] = []
    for item in rules:
        parsed.append(_parse_rule(item))
    return tuple(parsed)


def _parse_rule(item: object) -> RuleInput:
    """Return one rule.

    id may be omitted. Raises ValidationError when the object has the wrong
    fields. Does not read the database.
    """
    if not isinstance(item, dict):
        _reject("rules", "Each rule must be an object.")
    allowed = {"id", "clauses", "serve"}
    _reject_unknown(item, allowed, "rules")
    if "clauses" not in item or "serve" not in item:
        _reject("rules", "Each rule needs clauses and a serve.")
    if not isinstance(item["clauses"], list) or not isinstance(item["serve"], dict):
        _reject("rules", "Each rule needs a list of clauses and a serve object.")
    rule_id = None
    if "id" in item:
        if not isinstance(item["id"], str):
            _reject("rules", "Rule id must be a string when it is sent.")
        rule_id = item["id"]
    clauses: list[ClauseInput] = []
    for clause in item["clauses"]:
        clauses.append(_parse_clause(clause))
    return RuleInput(
        rule_id=rule_id,
        clauses=tuple(clauses),
        serve=_parse_serve(item["serve"], "rules"),
    )


def _parse_clause(item: object) -> ClauseInput:
    """Return one clause.

    Raises ValidationError when the object is not an attribute, an operator,
    and a list of values. Does not read the database.
    """
    if not isinstance(item, dict):
        _reject("rules", "Each clause must be an object.")
    _reject_unknown(item, {"attribute", "operator", "values"}, "rules")
    if "attribute" not in item or "operator" not in item or "values" not in item:
        _reject("rules", "Each clause needs an attribute, an operator, and values.")
    if not isinstance(item["attribute"], str) or not isinstance(item["operator"], str):
        _reject("rules", "The attribute and the operator must be strings.")
    if not isinstance(item["values"], list):
        _reject("rules", "Clause values must be a list.")
    return ClauseInput(
        attribute=item["attribute"],
        operator=item["operator"],
        values=tuple(item["values"]),
    )


def _parse_serve(item: dict, field: str) -> VariationServeInput | RolloutInput:
    """Return a variation serve or a rollout.

    field is the parent field name used in an error. Exactly one of
    variation and rollout is accepted. Does not read the database.
    """
    if set(item.keys()) == {"variation"}:
        if not _is_whole(item["variation"]):
            _reject(field, "A variation serve needs an integer index.")
        return VariationServeInput(variation_index=item["variation"])
    if set(item.keys()) == {"rollout"}:
        return _parse_rollout(item["rollout"], field)
    _reject(field, "Send a variation or a rollout.")


def _parse_rollout(item: object, field: str) -> RolloutInput:
    """Return one rollout.

    salt may be omitted. Raises ValidationError when the weights are not
    objects with an integer index and an integer weight. Does not read the
    database.
    """
    if not isinstance(item, dict):
        _reject(field, "A rollout must be an object.")
    _reject_unknown(item, {"salt", "variations"}, field)
    if "variations" not in item or not isinstance(item["variations"], list):
        _reject(field, "A rollout needs a list of variations.")
    salt = None
    if "salt" in item:
        if not isinstance(item["salt"], str):
            _reject(field, "Salt must be a string when it is sent.")
        salt = item["salt"]
    weights: list[RolloutWeightInput] = []
    for weight in item["variations"]:
        weights.append(_parse_weight(weight, field))
    return RolloutInput(salt=salt, variations=tuple(weights))


def _parse_weight(item: object, field: str) -> RolloutWeightInput:
    """Return one rollout weight.

    Raises ValidationError when the object is not an integer index and an
    integer weight. Does not read the database.
    """
    if not isinstance(item, dict):
        _reject(field, "Each rollout variation must be an object.")
    _reject_unknown(item, {"variation_index", "weight"}, field)
    if "variation_index" not in item or "weight" not in item:
        _reject(field, "Each rollout variation needs a variation index and a weight.")
    if not _is_whole(item["variation_index"]) or not _is_whole(item["weight"]):
        _reject(field, "A variation index and a weight must be integers.")
    return RolloutWeightInput(variation_index=item["variation_index"], weight=item["weight"])


def _parse_targets(targets: dict) -> tuple[TargetInput, ...]:
    """Return target lists in object order.

    A value that is a list is subjects only. A value that is an object has
    subjects and segments. Names are variation indexes written as decimal
    strings. Raises ValidationError when a name or a value is not that
    form. Does not read the database.
    """
    parsed: list[TargetInput] = []
    for index, value in targets.items():
        subjects, segments = _parse_target_value(value)
        parsed.append(
            TargetInput(
                variation_index=_target_index(index),
                subjects=subjects,
                segments=segments,
            )
        )
    return tuple(parsed)


def _parse_target_value(value: object) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the subjects and segment keys in one target value.

    A list is subjects and no segments. An object must name both lists.
    Raises ValidationError otherwise. Does not read the database.
    """
    if isinstance(value, list):
        return _string_list(value, "A target subject must be a string."), ()
    if isinstance(value, dict):
        _reject_unknown(value, {"subjects", "segments"}, "targets")
        if "subjects" not in value or "segments" not in value:
            _reject("targets", "A target object needs subjects and segments.")
        if not isinstance(value["subjects"], list) or not isinstance(value["segments"], list):
            _reject("targets", "A target object needs a list of subjects and a list of segments.")
        return (
            _string_list(value["subjects"], "A target subject must be a string."),
            _string_list(value["segments"], "A segment key must be a string."),
        )
    _reject("targets", "Each target list must be a list of subjects or an object with subjects and segments.")


def _string_list(values: list, message: str) -> tuple[str, ...]:
    """Return values when every item is a string.

    Raises ValidationError with message otherwise. Does not read the database.
    """
    cleaned: list[str] = []
    for item in values:
        if not isinstance(item, str):
            _reject("targets", message)
        cleaned.append(item)
    return tuple(cleaned)


def _parse_prerequisites(prerequisites: list) -> tuple[PrerequisiteInput, ...]:
    """Return the prerequisites in list order.

    Raises ValidationError when an item is not a flag key and an integer
    variation index. Does not read the database.
    """
    parsed: list[PrerequisiteInput] = []
    for item in prerequisites:
        if not isinstance(item, dict):
            _reject("prerequisites", "Each prerequisite must be an object.")
        _reject_unknown(item, {"flag_key", "variation_index"}, "prerequisites")
        if "flag_key" not in item or "variation_index" not in item:
            _reject("prerequisites", "Each prerequisite needs a flag key and a variation index.")
        if not isinstance(item["flag_key"], str) or not _is_whole(item["variation_index"]):
            _reject("prerequisites", "The flag key must be a string and the index an integer.")
        parsed.append(
            PrerequisiteInput(
                flag_key=item["flag_key"],
                variation_index=item["variation_index"],
            )
        )
    return tuple(parsed)


def _target_index(index: str) -> int:
    """Return the variation index encoded as a decimal string.

    "0" and "12" are indexes. "01" and "1.0" are not. Raises ValidationError
    otherwise. Does not read the database.
    """
    if index == "0" or (index.isdigit() and index[0] != "0"):
        return int(index)
    _reject("targets", "Each name in targets is a variation index, written as a decimal string.")


def _reject_unknown(body: dict, allowed: set[str], field: str) -> None:
    """Raise ValidationError when body contains a field that is not allowed.

    Does not read the database.
    """
    unknown = sorted(set(body) - allowed)
    if unknown:
        _reject(field, "This field is not accepted.")


def _unknown_details(body: dict, allowed: set[str]) -> list[dict[str, str]]:
    """Return one error for each field that is not allowed.

    An empty list means body has no extra fields. Does not read the database.
    """
    details: list[dict[str, str]] = []
    for field in sorted(set(body) - allowed):
        details.append({"field": field, "message": "This field is not accepted."})
    return details


def _is_whole(value: object) -> bool:
    """Return True when value is a JSON integer.

    Booleans are not integers here. Does not read the database.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _reject(field: str, message: str) -> NoReturn:
    """Raise ValidationError naming field.

    Does not read the database.
    """
    raise ValidationError(
        "The request body has the wrong fields.",
        details=[{"field": field, "message": message}],
    )


def _document(config: FlagConfig) -> dict:
    """Return the JSON object for one targeting document.

    The environment key is not included; it is the path. Does not read the
    database.
    """
    return {
        "enabled": config.enabled,
        "off_variation": config.off_variation,
        "fallthrough": config.fallthrough,
        "rules": config.rules,
        "targets": config.targets,
        "prerequisites": config.prerequisites,
        "version": config.version,
    }
