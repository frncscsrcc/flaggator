"""Fetch and replace one flag's targeting in one environment.

The replace writes the whole document. A stale version changes no row.
Rule ids and rollout salts are assigned here, which is the one place a
targeting write draws a random value, and that value is stored.
"""

import math
import secrets
from dataclasses import dataclass

from domain.errors import Conflict, NotFound, StorageError, ValidationError
from domain.models.flag import FlagConfig
from domain.models.principal import Principal
from domain.models.targeting import (
    ClauseInput,
    ReplaceTargeting,
    RolloutInput,
    RuleInput,
    VariationServeInput,
)
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import (
    MAX_ATTRIBUTE_NAME_LENGTH,
    MAX_CLAUSES,
    MAX_CLAUSE_VALUES,
    MAX_RULES,
    MAX_TARGETS,
    ROLLOUT_WEIGHT_TOTAL,
    is_public_key,
)
from repositories.db import Connection
from repositories.environments import EnvironmentRow, find_environment
from repositories.eval_cache import EvaluationCache
from repositories.flag_configs import (
    FlagConfigRow,
    find_config,
    list_environment_prerequisites,
    update_config,
)
from repositories.flags import FlagRow, find_flag, list_variations
from repositories.json_text import dump_json_text
from repositories.projects import ProjectRow, find_project

# eq takes one scalar. The numeric operators take one number. The rest take
# one or more scalars, and the string operators take strings.
_OPERATOR_KIND = {
    "eq": "one",
    "gt": "one-number",
    "gte": "one-number",
    "lt": "one-number",
    "lte": "one-number",
    "neq": "many",
    "in": "many",
    "not_in": "many",
    "starts_with": "many-string",
    "ends_with": "many-string",
    "contains": "many-string",
}


@dataclass(frozen=True)
class PreparedConfig:
    """The column values of a targeting replace.

    enabled is 0 or 1. The JSON fields are canonical text. version is not
    here: the UPDATE increments the stored version.
    """

    enabled: int
    off_variation: int
    fallthrough_json: str
    rules_json: str
    targets_json: str
    prerequisites_json: str


def get_targeting(
    connection: Connection,
    principal: Principal,
    project_key: str,
    flag_key: str,
    environment_key: str,
) -> FlagConfig:
    """Return the stored targeting for one flag in one environment.

    Raises Forbidden for an SDK key. Raises NotFound when the project, the
    flag, or the environment is absent, including when a key cannot exist.
    Reads SQL, not a cache. Does not open a transaction.
    """
    require_management(principal)
    project = _require_project(connection, project_key)
    flag = _require_flag(connection, project.id, flag_key)
    environment = _require_environment(connection, project.id, environment_key)
    return _load_config(connection, flag.id, environment.id)


def replace_targeting(
    connection: Connection,
    principal: Principal,
    project_key: str,
    flag_key: str,
    environment_key: str,
    command: ReplaceTargeting,
    evaluation_cache: EvaluationCache,
) -> FlagConfig:
    """Replace one environment's targeting and write an audit row.

    command.version must be the version the caller read. A different stored
    version raises Conflict and leaves the row unchanged. Raises Forbidden
    for an SDK key, NotFound when the project, flag, or environment is
    absent, and ValidationError when the document breaks a targeting rule.
    The audit documents are the previous and stored configurations.
    """
    require_management(principal)
    with connection.transaction():
        project = _require_project(connection, project_key)
        flag = _require_flag(connection, project.id, flag_key)
        environment = _require_environment(connection, project.id, environment_key)
        current = _load_config(connection, flag.id, environment.id)
        if command.version != current.version:
            raise Conflict("The configuration version is stale.")
        variation_count = len(list_variations(connection, flag.id))
        prepared = _prepare(
            connection,
            project.id,
            environment.id,
            flag.key,
            variation_count,
            current,
            command,
        )
        changed = update_config(
            connection,
            flag.id,
            environment.id,
            prepared.enabled,
            prepared.off_variation,
            prepared.fallthrough_json,
            prepared.rules_json,
            prepared.targets_json,
            prepared.prerequisites_json,
            command.version,
        )
        if changed == 0:
            raise Conflict("The configuration version is stale.")
        if changed < 0:
            raise StorageError()
        updated = _load_config(connection, flag.id, environment.id)
        record_change(
            connection,
            principal,
            "targeting.replace",
            project.key,
            environment.key,
            flag.key,
            _audit_document(current),
            _audit_document(updated),
        )
        evaluation_cache.invalidate_environment(connection, project.key, environment.key)
    return updated


def _require_project(connection: Connection, project_key: str) -> ProjectRow:
    """Return the project row for project_key.

    Raises NotFound when the key is not a public key or no row matches,
    with the same message for both. Does not commit.
    """
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    return project


def _require_flag(connection: Connection, project_id: int, flag_key: str) -> FlagRow:
    """Return the flag row for flag_key inside the project.

    Raises NotFound when the key is not a public key or no row matches,
    with the same message for both. Does not commit.
    """
    if not is_public_key(flag_key):
        raise NotFound("Flag does not exist.")
    flag = find_flag(connection, project_id, flag_key)
    if flag is None:
        raise NotFound("Flag does not exist.")
    return flag


def _require_environment(
    connection: Connection,
    project_id: int,
    environment_key: str,
) -> EnvironmentRow:
    """Return the environment row for environment_key inside the project.

    Raises NotFound when the key is not a public key or no row matches,
    with the same message for both. Does not commit.
    """
    if not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    environment = find_environment(connection, project_id, environment_key)
    if environment is None:
        raise NotFound("Environment does not exist.")
    return environment


def _load_config(connection: Connection, flag_id: int, environment_id: int) -> FlagConfig:
    """Return the configuration row as a domain document.

    Raises StorageError when the flag and environment exist and the config
    row does not. A flag and an environment are created with that row.
    Does not commit.
    """
    row = find_config(connection, flag_id, environment_id)
    if row is None:
        raise StorageError()
    return _config(row)


def _prepare(
    connection: Connection,
    project_id: int,
    environment_id: int,
    flag_key: str,
    variation_count: int,
    current: FlagConfig,
    command: ReplaceTargeting,
) -> PreparedConfig:
    """Validate command and return the column values to store.

    variation_count is the number of variations on this flag. current is
    the stored document, used to keep rule ids and salts. Raises
    ValidationError when a rule fails, and StorageError when a stored JSON
    document is not the shape this service writes. Does not commit.
    """
    _require_index(
        command.off_variation,
        variation_count,
        "off_variation",
        "Use an index from this flag's variation list.",
    )
    targets = _target_lists(command, variation_count)
    rules = _rule_documents(current, command, variation_count)
    fallthrough = _serve_document(
        command.fallthrough,
        _salt_on_serve(current.fallthrough),
        _used_tokens(rules),
        variation_count,
        "fallthrough",
    )
    prerequisites = _prerequisite_documents(
        connection,
        project_id,
        environment_id,
        flag_key,
        variation_count,
        command,
    )
    enabled = 0
    if command.enabled:
        enabled = 1
    return PreparedConfig(
        enabled=enabled,
        off_variation=command.off_variation,
        fallthrough_json=dump_json_text(fallthrough),
        rules_json=dump_json_text(rules),
        targets_json=dump_json_text(targets),
        prerequisites_json=dump_json_text(prerequisites),
    )


def _target_lists(command: ReplaceTargeting, variation_count: int) -> dict[str, list[str]]:
    """Return the target map, with variation indexes as decimal strings.

    Raises ValidationError when an index is missing, a context key is empty
    or repeated, or the total number of keys is over the cap. Does not read
    the database.
    """
    seen: set[str] = set()
    document: dict[str, list[str]] = {}
    for index, context_keys in command.targets:
        _require_index(
            index,
            variation_count,
            "targets",
            "Use an index from this flag's variation list.",
        )
        cleaned: list[str] = []
        for context_key in context_keys:
            if context_key == "":
                _reject("targets", "A target context key must not be empty.")
            if context_key in seen:
                _reject("targets", "Target lists must not share a context key.")
            if len(seen) >= MAX_TARGETS:
                _reject("targets", "A configuration can target at most 1000 context keys.")
            seen.add(context_key)
            cleaned.append(context_key)
        document[str(index)] = cleaned
    return document


def _rule_documents(
    current: FlagConfig,
    command: ReplaceTargeting,
    variation_count: int,
) -> list[dict]:
    """Return the rules to store, with ids and salts filled in.

    A rule without an id gets a new id. A stored id is kept. An unknown id
    raises ValidationError. An omitted salt keeps the stored rollout salt,
    or a new salt is assigned when that rule has no rollout yet. Does not
    read the database.
    """
    if len(command.rules) > MAX_RULES:
        _reject("rules", "A configuration can have at most 100 rules.")
    stored = _stored_rules(current.rules)
    seen_ids: set[str] = set()
    for rule in command.rules:
        _check_rule(rule, variation_count)
        if rule.rule_id is None:
            continue
        if rule.rule_id not in stored:
            _reject("rules", "Send a stored rule id, or omit id for a new rule.")
        if rule.rule_id in seen_ids:
            _reject("rules", "Each rule id can appear only once.")
        seen_ids.add(rule.rule_id)
    used = set(stored.keys())
    documents: list[dict] = []
    for rule in command.rules:
        if rule.rule_id is None:
            rule_id = _fresh_token(used)
            stored_salt = None
        else:
            rule_id = rule.rule_id
            used.add(rule_id)
            stored_salt = _salt_on_serve(stored[rule_id].get("serve"))
        documents.append(
            {
                "clauses": _clause_documents(rule),
                "id": rule_id,
                "serve": _serve_document(
                    rule.serve,
                    stored_salt,
                    used,
                    variation_count,
                    "rules",
                ),
            }
        )
    return documents


def _check_rule(rule: RuleInput, variation_count: int) -> None:
    """Raise ValidationError when the rule's clauses or serve break a limit.

    Does not assign an id or a salt, and does not read the database.
    """
    if len(rule.clauses) > MAX_CLAUSES:
        _reject("rules", "A rule can have at most 20 clauses.")
    for clause in rule.clauses:
        _check_clause(clause)
    _check_serve(rule.serve, variation_count, "rules")


def _check_clause(clause: ClauseInput) -> None:
    """Raise ValidationError when the operator or its values are not allowed.

    Does not read the database.
    """
    if clause.attribute == "" or len(clause.attribute) > MAX_ATTRIBUTE_NAME_LENGTH:
        _reject("rules", "An attribute name must be from 1 to 64 characters.")
    kind = _OPERATOR_KIND.get(clause.operator)
    if kind is None:
        _reject(
            "rules",
            "Use eq, neq, in, not_in, starts_with, ends_with, contains, gt, gte, lt, or lte.",
        )
    value_count = len(clause.values)
    if value_count > MAX_CLAUSE_VALUES:
        _reject("rules", "A clause can have at most 50 values.")
    if kind.startswith("one") and value_count != 1:
        _reject("rules", f"{clause.operator} needs exactly one value.")
    if kind.startswith("many") and value_count < 1:
        _reject("rules", f"{clause.operator} needs at least one value.")
    for value in clause.values:
        _check_clause_value(clause.operator, kind, value)


def _check_clause_value(operator: str, kind: str, value: object) -> None:
    """Raise ValidationError when value is the wrong JSON type for kind.

    kind comes from _OPERATOR_KIND. Does not read the database.
    """
    if kind.endswith("string"):
        if not isinstance(value, str):
            _reject("rules", f"{operator} values must be strings.")
        return
    if kind.endswith("number"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not _finite(value):
            _reject("rules", f"{operator} values must be finite numbers.")
        return
    if not _is_scalar(value):
        _reject("rules", "Clause values must be JSON scalars.")


def _check_serve(
    serve: VariationServeInput | RolloutInput,
    variation_count: int,
    field: str,
) -> None:
    """Raise ValidationError when a serve points off the flag or the weights are wrong.

    field is "rules" or "fallthrough", and it is copied into the error.
    Does not read the database.
    """
    if isinstance(serve, VariationServeInput):
        _require_index(
            serve.variation_index,
            variation_count,
            field,
            "Use an index from this flag's variation list.",
        )
        return
    if serve.salt is not None and serve.salt == "":
        _reject(field, "Salt must not be empty.")
    if len(serve.variations) == 0:
        _reject(field, "A rollout needs at least one variation.")
    total = 0
    for item in serve.variations:
        _require_index(
            item.variation_index,
            variation_count,
            field,
            "Use an index from this flag's variation list.",
        )
        if item.weight < 0:
            _reject(field, "Rollout weights must be non-negative and sum to 100000.")
        total = total + item.weight
    if total != ROLLOUT_WEIGHT_TOTAL:
        _reject(field, "Rollout weights must be non-negative and sum to 100000.")


def _serve_document(
    serve: VariationServeInput | RolloutInput,
    stored_salt: str | None,
    used: set[str],
    variation_count: int,
    field: str,
) -> dict:
    """Return the JSON object for one serve, assigning a salt when needed.

    stored_salt is the salt already saved for this rule or fallthrough, or
    None when there is no rollout yet. An omitted salt keeps stored_salt.
    used collects ids and salts already chosen for this document so a new
    token does not repeat one. Does not read the database.
    """
    _check_serve(serve, variation_count, field)
    if isinstance(serve, VariationServeInput):
        return {"variation": serve.variation_index}
    salt = serve.salt
    if salt is None:
        if stored_salt is None:
            salt = _fresh_token(used)
        else:
            salt = stored_salt
    used.add(salt)
    weights: list[dict[str, int]] = []
    for item in serve.variations:
        weights.append({"variation_index": item.variation_index, "weight": item.weight})
    return {"rollout": {"salt": salt, "variations": weights}}


def _clause_documents(rule: RuleInput) -> list[dict]:
    """Return the JSON objects for a rule's clauses, in stored order.

    Does not read the database.
    """
    documents: list[dict] = []
    for clause in rule.clauses:
        documents.append(
            {
                "attribute": clause.attribute,
                "operator": clause.operator,
                "values": list(clause.values),
            }
        )
    return documents


def _prerequisite_documents(
    connection: Connection,
    project_id: int,
    environment_id: int,
    flag_key: str,
    variation_count: int,
    command: ReplaceTargeting,
) -> list[dict]:
    """Return the prerequisite list after the existence and cycle checks.

    Raises ValidationError when a prerequisite flag is absent, a variation
    index is off that flag, or the list would form a cycle in this
    environment. Does not commit.
    """
    seen: set[str] = set()
    for item in command.prerequisites:
        if not is_public_key(item.flag_key):
            _reject("prerequisites", "Use a flag key in this project.")
        if item.flag_key in seen:
            _reject("prerequisites", "List each prerequisite flag once.")
        seen.add(item.flag_key)
        if item.flag_key == flag_key:
            available = variation_count
        else:
            other = find_flag(connection, project_id, item.flag_key)
            if other is None:
                _reject("prerequisites", "Prerequisite flag does not exist.")
            available = len(list_variations(connection, other.id))
        _require_index(
            item.variation_index,
            available,
            "prerequisites",
            "Use a variation index from that flag.",
        )
    _reject_cycles(connection, environment_id, flag_key, command)
    documents: list[dict] = []
    for item in command.prerequisites:
        documents.append(
            {"flag_key": item.flag_key, "variation_index": item.variation_index}
        )
    return documents


def _reject_cycles(
    connection: Connection,
    environment_id: int,
    flag_key: str,
    command: ReplaceTargeting,
) -> None:
    """Raise ValidationError when the proposed prerequisites contain a cycle.

    The graph is this environment's stored lists, with flag_key's list
    replaced by the one in command. A flag that requires itself is a cycle.
    Does not commit. Two writes can each see the other's old list and both
    commit; evaluation still stops on a cycle.
    """
    graph: dict[str, tuple[str, ...]] = {}
    for row in list_environment_prerequisites(connection, environment_id):
        graph[row.flag_key] = _stored_prerequisite_keys(row.prerequisites)
    proposed: list[str] = []
    for item in command.prerequisites:
        proposed.append(item.flag_key)
    graph[flag_key] = tuple(proposed)
    if _has_cycle(graph, flag_key):
        _reject("prerequisites", "Prerequisites must not contain a cycle.")


def _stored_rules(rules: object) -> dict[str, dict]:
    """Index the stored rules by id.

    An empty list returns an empty dict. Raises StorageError when the stored
    document is not a list of rules with string ids. Does not read the
    database.
    """
    if not isinstance(rules, list):
        raise StorageError()
    stored: dict[str, dict] = {}
    for item in rules:
        if not isinstance(item, dict):
            raise StorageError()
        rule_id = item.get("id")
        if not isinstance(rule_id, str) or rule_id == "":
            raise StorageError()
        stored[rule_id] = item
    return stored


def _stored_prerequisite_keys(prerequisites: object) -> tuple[str, ...]:
    """Return the flag keys named by a stored prerequisite list.

    An empty list returns an empty tuple. Raises StorageError when the
    stored document is not that list. Does not read the database.
    """
    if not isinstance(prerequisites, list):
        raise StorageError()
    keys: list[str] = []
    for item in prerequisites:
        if not isinstance(item, dict):
            raise StorageError()
        required = item.get("flag_key")
        if not isinstance(required, str):
            raise StorageError()
        keys.append(required)
    return tuple(keys)


def _salt_on_serve(serve: object) -> str | None:
    """Return the rollout salt on a stored serve, or None for a variation.

    Raises StorageError when serve is not a variation object or a rollout
    object with a salt. Does not read the database.
    """
    if not isinstance(serve, dict):
        raise StorageError()
    if set(serve.keys()) == {"variation"}:
        return None
    if set(serve.keys()) != {"rollout"}:
        raise StorageError()
    rollout = serve["rollout"]
    if not isinstance(rollout, dict):
        raise StorageError()
    salt = rollout.get("salt")
    if not isinstance(salt, str) or salt == "":
        raise StorageError()
    return salt


def _has_cycle(graph: dict[str, tuple[str, ...]], start: str) -> bool:
    """Return True when start can reach itself through graph.

    A missing key requires nothing. Does not read the database.
    """
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[tuple[str, int]] = [(start, 0)]
    while len(stack) > 0:
        flag_key, edge_index = stack[-1]
        if flag_key in visited:
            stack.pop()
            continue
        if flag_key not in visiting:
            visiting.add(flag_key)
        edges = graph.get(flag_key, ())
        if edge_index >= len(edges):
            visiting.remove(flag_key)
            visited.add(flag_key)
            stack.pop()
            continue
        required = edges[edge_index]
        stack[-1] = (flag_key, edge_index + 1)
        if required in visiting:
            return True
        if required not in visited:
            stack.append((required, 0))
    return False


def _used_tokens(rules: list[dict]) -> set[str]:
    """Return the rule ids and rollout salts already chosen for rules.

    Fallthrough salt assignment uses this set so its new token does not copy
    a rule's id or salt. An empty rule list returns an empty set. Does not
    read the database.
    """
    used: set[str] = set()
    for rule in rules:
        rule_id = rule.get("id")
        if isinstance(rule_id, str):
            used.add(rule_id)
        serve = rule.get("serve")
        if isinstance(serve, dict):
            salt = _salt_on_serve(serve)
            if salt is not None:
                used.add(salt)
    return used


def _fresh_token(used: set[str]) -> str:
    """Return a new url-safe token that is not already in used.

    Adds the token to used. Raises StorageError when five draws all collide.
    Does not read the database.
    """
    draws = 0
    while draws < 5:
        draws = draws + 1
        token = secrets.token_urlsafe(16)
        if token not in used:
            used.add(token)
            return token
    raise StorageError()


def _require_index(index: int, count: int, field: str, message: str) -> None:
    """Raise ValidationError when index is outside the variation list.

    count is the number of variations. Does not read the database.
    """
    if index < 0 or index >= count:
        _reject(field, message)


def _finite(value: object) -> bool:
    """Return True when value is a finite number.

    Booleans are not finite numbers here. Does not read the database.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _is_scalar(value: object) -> bool:
    """Return True when value is a JSON scalar this clause list may store.

    Objects, arrays, and null are not scalars. A non-finite float is not a
    scalar. Does not read the database.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return True
    return _finite(value)


def _reject(field: str, message: str) -> None:
    """Raise ValidationError naming field.

    Does not read the database.
    """
    raise ValidationError(
        "The targeting document is not valid.",
        details=[{"field": field, "message": message}],
    )


def _config(row: FlagConfigRow) -> FlagConfig:
    """Copy a repository config into the domain document.

    Does not read the database.
    """
    return FlagConfig(
        key=row.environment_key,
        enabled=row.enabled,
        off_variation=row.off_variation,
        fallthrough=row.fallthrough,
        rules=row.rules,
        targets=row.targets,
        prerequisites=row.prerequisites,
        version=row.version,
    )


def _audit_document(config: FlagConfig) -> str:
    """Return the audit JSON for one targeting document.

    The document has no credential secret. It matches the fields a fetch
    returns.
    """
    return dump_json_text(
        {
            "enabled": config.enabled,
            "fallthrough": config.fallthrough,
            "off_variation": config.off_variation,
            "prerequisites": config.prerequisites,
            "rules": config.rules,
            "targets": config.targets,
            "version": config.version,
        }
    )
