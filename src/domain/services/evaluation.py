"""Evaluate flags and read the evaluation bundle.

Management reads of targeting do not come here. These reads use the
evaluation cache. A missing flag on evaluate-one returns the caller default.
"""

import math

from domain.errors import Forbidden, NotFound, StorageError, ValidationError
from domain.models.evaluation import (
    Clause,
    Context,
    Decision,
    FlagDefinition,
    Prerequisite,
    Rollout,
    RolloutShare,
    Rule,
    VariationServe,
)
from domain.models.principal import KIND_MANAGEMENT, KIND_SDK, Principal
from domain.services.evaluator import evaluate
from domain.validation import (
    MAX_ATTRIBUTE_NAME_LENGTH,
    MAX_CONTEXT_ATTRIBUTES,
    MAX_CONTEXT_STRING_LENGTH,
    is_public_key,
)
from repositories.db import Connection
from repositories.environments import EnvironmentScope, find_environment, find_environment_scope
from repositories.eval_cache import EvaluationCache
from repositories.projects import find_project


def evaluate_one(
    connection: Connection,
    evaluation_cache: EvaluationCache,
    principal: Principal,
    project_key: str | None,
    environment_key: str | None,
    flag_key: str,
    context: Context,
    default: object,
) -> Decision:
    """Return the decision for one flag.

    default is used only when the flag is not in the environment. An SDK
    key may omit the project and environment keys; a sent key that does
    not match the credential is Forbidden. A management token must send
    both keys. Does not store the context.
    """
    checked = _checked_context(context)
    scope = _scope(connection, principal, project_key, environment_key)
    document = evaluation_cache.load_document(
        connection,
        scope.project_id,
        scope.environment_id,
        scope.project_key,
        scope.environment_key,
    )
    definitions = _definitions(document)
    return evaluate(flag_key, definitions, checked, default)


def evaluate_all(
    connection: Connection,
    evaluation_cache: EvaluationCache,
    principal: Principal,
    project_key: str | None,
    environment_key: str | None,
    context: Context,
) -> list[Decision]:
    """Return one decision for every flag in the environment.

    Flags are ordered by key. An environment with no flags returns an
    empty list. There is no caller default: every flag has a configuration.
    Credential rules match evaluate_one. Does not store the context.
    """
    checked = _checked_context(context)
    scope = _scope(connection, principal, project_key, environment_key)
    document = evaluation_cache.load_document(
        connection,
        scope.project_id,
        scope.environment_id,
        scope.project_key,
        scope.environment_key,
    )
    definitions = _definitions(document)
    decisions: list[Decision] = []
    for flag in document["flags"]:
        if not isinstance(flag, dict):
            raise StorageError()
        flag_key = flag.get("key")
        if not isinstance(flag_key, str):
            raise StorageError()
        decisions.append(evaluate(flag_key, definitions, checked))
    return decisions


def read_config(
    connection: Connection,
    evaluation_cache: EvaluationCache,
    principal: Principal,
    project_key: str,
    environment_key: str,
) -> dict:
    """Return the evaluation bundle for one environment.

    The document includes salts and does not include secrets. An SDK key
    may read only its own environment. A management token may read any
    environment that exists. Does not evaluate.
    """
    scope = _scope(connection, principal, project_key, environment_key)
    return evaluation_cache.load_document(
        connection,
        scope.project_id,
        scope.environment_id,
        scope.project_key,
        scope.environment_key,
    )


def _scope(
    connection: Connection,
    principal: Principal,
    project_key: str | None,
    environment_key: str | None,
) -> EnvironmentScope:
    """Return the environment this call may evaluate.

    A management token must name the project and the environment. An SDK
    key uses its own environment, and a sent key that differs is Forbidden.
    Does not commit.
    """
    if principal.kind == KIND_SDK:
        return _sdk_scope(connection, principal, project_key, environment_key)
    if principal.kind != KIND_MANAGEMENT:
        raise Forbidden("The credential may not call this route.")
    if project_key is None or environment_key is None:
        raise ValidationError(
            "The project and environment are required.",
            details=[
                {
                    "field": "environment_key",
                    "message": "Send the project key and the environment key.",
                }
            ],
        )
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    if not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    environment = find_environment(connection, project.id, environment_key)
    if environment is None:
        raise NotFound("Environment does not exist.")
    return EnvironmentScope(
        project_id=project.id,
        project_key=project.key,
        environment_id=environment.id,
        environment_key=environment.key,
    )


def _sdk_scope(
    connection: Connection,
    principal: Principal,
    project_key: str | None,
    environment_key: str | None,
) -> EnvironmentScope:
    """Return the environment bound to this SDK key.

    A project or environment key in the request must match that binding.
    A mismatch is Forbidden, including when the other key does not exist,
    so the response does not say which projects are real. Does not commit.
    """
    if principal.environment_id is None:
        raise Forbidden("The credential is not allowed for this environment.")
    scope = find_environment_scope(connection, principal.environment_id)
    if scope is None:
        raise Forbidden("The credential is not allowed for this environment.")
    if project_key is not None and project_key != scope.project_key:
        raise Forbidden("The credential is not allowed for this environment.")
    if environment_key is not None and environment_key != scope.environment_key:
        raise Forbidden("The credential is not allowed for this environment.")
    return scope


def _checked_context(context: Context) -> Context:
    """Return context when it is inside the context caps.

    Raises ValidationError when the key, an attribute name, a string, or
    the number of attributes is over the cap, or when a value is not a
    scalar or an array of scalars. Does not read the database.
    """
    if context.key is not None and (
        context.key == "" or len(context.key) > MAX_CONTEXT_STRING_LENGTH
    ):
        raise ValidationError(
            "The context is not valid.",
            details=[{"field": "context.key", "message": "Use a key of 1 to 1024 characters."}],
        )
    if len(context.attributes) > MAX_CONTEXT_ATTRIBUTES:
        raise ValidationError(
            "The context is not valid.",
            details=[{"field": "context.attributes", "message": "Send at most 50 attributes."}],
        )
    for name, value in context.attributes.items():
        if name == "" or len(name) > MAX_ATTRIBUTE_NAME_LENGTH:
            raise ValidationError(
                "The context is not valid.",
                details=[
                    {
                        "field": "context.attributes",
                        "message": "An attribute name must be from 1 to 64 characters.",
                    }
                ],
            )
        _require_attribute_value(value)
    return context


def _require_attribute_value(value: object) -> None:
    """Raise ValidationError when value is not a scalar or an array of scalars.

    Does not read the database.
    """
    if _is_scalar(value):
        return
    if isinstance(value, list):
        for item in value:
            if not _is_scalar(item):
                raise ValidationError(
                    "The context is not valid.",
                    details=[
                        {
                            "field": "context.attributes",
                            "message": "An attribute value must be a scalar or an array of scalars.",
                        }
                    ],
                )
        return
    raise ValidationError(
        "The context is not valid.",
        details=[
            {
                "field": "context.attributes",
                "message": "An attribute value must be a scalar or an array of scalars.",
            }
        ],
    )


def _is_scalar(value: object) -> bool:
    """Return True for a boolean, a finite number, or a string up to 1024 characters.

    None, objects, and arrays are not scalars. A longer string is rejected
    by the caller with its own message. Does not read the database.
    """
    if isinstance(value, str):
        if len(value) > MAX_CONTEXT_STRING_LENGTH:
            raise ValidationError(
                "The context is not valid.",
                details=[
                    {
                        "field": "context.attributes",
                        "message": "A string attribute must be at most 1024 characters.",
                    }
                ],
            )
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _definitions(document: dict) -> dict[str, FlagDefinition]:
    """Build the evaluator inputs from one bundle document.

    Raises StorageError when a stored flag is not the shape a write saves.
    Does not read the database.
    """
    flags = document.get("flags")
    if not isinstance(flags, list):
        raise StorageError()
    definitions: dict[str, FlagDefinition] = {}
    for flag in flags:
        definition = _definition(flag)
        definitions[definition.key] = definition
    return definitions


def _definition(flag: object) -> FlagDefinition:
    """Return one flag definition from a bundle item.

    Raises StorageError when flag is not an object with the stored fields.
    Does not read the database.
    """
    if not isinstance(flag, dict):
        raise StorageError()
    key = flag.get("key")
    variations = flag.get("variations")
    if not isinstance(key, str) or not isinstance(variations, list):
        raise StorageError()
    enabled = flag.get("enabled")
    off_variation = flag.get("off_variation")
    version = flag.get("version")
    if not isinstance(enabled, bool) or not _whole(off_variation) or not _whole(version):
        raise StorageError()
    rules = _rules(flag.get("rules"))
    targets = _targets(flag.get("targets"))
    prerequisites = _prerequisites(flag.get("prerequisites"))
    return FlagDefinition(
        key=key,
        variations=tuple(variations),
        enabled=enabled,
        off_variation=off_variation,
        fallthrough=_serve(flag.get("fallthrough")),
        rules=rules,
        targets=targets,
        prerequisites=prerequisites,
        version=version,
    )


def _rules(value: object) -> tuple[Rule, ...]:
    """Return the stored rules.

    An empty list returns an empty tuple. Raises StorageError otherwise
    when the shape is wrong. Does not read the database.
    """
    if not isinstance(value, list):
        raise StorageError()
    rules: list[Rule] = []
    for item in value:
        if not isinstance(item, dict):
            raise StorageError()
        rule_id = item.get("id")
        clauses = item.get("clauses")
        if not isinstance(rule_id, str) or not isinstance(clauses, list):
            raise StorageError()
        parsed: list[Clause] = []
        for clause in clauses:
            if not isinstance(clause, dict):
                raise StorageError()
            attribute = clause.get("attribute")
            operator = clause.get("operator")
            values = clause.get("values")
            if not isinstance(attribute, str) or not isinstance(operator, str) or not isinstance(values, list):
                raise StorageError()
            parsed.append(Clause(attribute, operator, tuple(values)))
        rules.append(Rule(rule_id, tuple(parsed), _serve(item.get("serve"))))
    return tuple(rules)


def _targets(value: object) -> tuple[tuple[int, tuple[str, ...]], ...]:
    """Return target lists keyed by variation index.

    An empty object returns an empty tuple. Raises StorageError when a key
    is not a decimal index or a value is not a list of strings. Does not
    read the database.
    """
    if not isinstance(value, dict):
        raise StorageError()
    parsed: list[tuple[int, tuple[str, ...]]] = []
    for key, context_keys in value.items():
        if not isinstance(context_keys, list) or not _decimal(key):
            raise StorageError()
        keys: list[str] = []
        for context_key in context_keys:
            if not isinstance(context_key, str):
                raise StorageError()
            keys.append(context_key)
        parsed.append((int(key), tuple(keys)))
    return tuple(parsed)


def _prerequisites(value: object) -> tuple[Prerequisite, ...]:
    """Return the stored prerequisites.

    An empty list returns an empty tuple. Raises StorageError when an item
    is not a flag key and an integer index. Does not read the database.
    """
    if not isinstance(value, list):
        raise StorageError()
    parsed: list[Prerequisite] = []
    for item in value:
        if not isinstance(item, dict):
            raise StorageError()
        flag_key = item.get("flag_key")
        variation_index = item.get("variation_index")
        if not isinstance(flag_key, str) or not _whole(variation_index):
            raise StorageError()
        parsed.append(Prerequisite(flag_key, variation_index))
    return tuple(parsed)


def _serve(value: object) -> VariationServe | Rollout:
    """Return a variation serve or a rollout.

    Raises StorageError when value is neither. Does not read the database.
    """
    if not isinstance(value, dict):
        raise StorageError()
    if set(value.keys()) == {"variation"}:
        index = value["variation"]
        if not _whole(index):
            raise StorageError()
        return VariationServe(index)
    if set(value.keys()) != {"rollout"}:
        raise StorageError()
    rollout = value["rollout"]
    if not isinstance(rollout, dict):
        raise StorageError()
    salt = rollout.get("salt")
    shares = rollout.get("variations")
    if not isinstance(salt, str) or salt == "" or not isinstance(shares, list):
        raise StorageError()
    parsed: list[RolloutShare] = []
    for share in shares:
        if not isinstance(share, dict):
            raise StorageError()
        variation_index = share.get("variation_index")
        weight = share.get("weight")
        if not _whole(variation_index) or not _whole(weight):
            raise StorageError()
        parsed.append(RolloutShare(variation_index, weight))
    return Rollout(salt, tuple(parsed))


def _whole(value: object) -> bool:
    """Return True when value is a JSON integer.

    Booleans are not integers. Does not read the database.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _decimal(value: object) -> bool:
    """Return True when value is a decimal variation index.

    "0" and "12" are indexes. "01" is not. Does not read the database.
    """
    if not isinstance(value, str):
        return False
    if value == "0":
        return True
    return value.isdigit() and value[0] != "0"
