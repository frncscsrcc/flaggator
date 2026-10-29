"""Evaluate one flag for one context.

This module has no I/O. The same definitions and context always produce the
same decision. Prerequisites call this same walk. A cycle or a walk past
the depth cap stops with prerequisite_failed.
"""

import hashlib
import math
from collections.abc import Mapping

from domain.models.evaluation import (
    REASON_FALLTHROUGH,
    REASON_FLAG_NOT_FOUND,
    REASON_OFF,
    REASON_PREREQUISITE_FAILED,
    REASON_RULE_MATCH,
    REASON_TARGET_MATCH,
    Clause,
    Context,
    Decision,
    FlagDefinition,
    Rollout,
    Rule,
    VariationServe,
)
from domain.validation import MAX_PREREQUISITE_DEPTH

_STRING_OPERATORS = ("starts_with", "ends_with", "contains")
_NUMERIC_OPERATORS = ("gt", "gte", "lt", "lte")
_ARRAY_OPERATORS = ("eq", "in", "starts_with", "ends_with", "contains")


def evaluate(
    flag_key: str,
    definitions: Mapping[str, FlagDefinition],
    context: Context,
    default: object = None,
) -> Decision:
    """Return the decision for flag_key in definitions.

    default is used only when flag_key is absent. A flag that exists does
    not use it. The walk starts at depth 1. Does not read the database and
    does not draw a random value.
    """
    return _evaluate(flag_key, definitions, context, set(), 1, default, True)


def _evaluate(
    flag_key: str,
    definitions: Mapping[str, FlagDefinition],
    context: Context,
    visiting: set[str],
    depth: int,
    default: object,
    use_default: bool,
) -> Decision:
    """Return the decision for one step of the walk.

    depth is 1 for the flag the caller asked for. visiting holds the keys
    on the current walk and is restored before this call returns. use_default
    is true only for that first flag. Does not read the database.
    """
    definition = definitions.get(flag_key)
    if flag_key in visiting:
        return _stopped(flag_key, definition, default, use_default)
    if depth > MAX_PREREQUISITE_DEPTH:
        return _stopped(flag_key, definition, default, use_default)
    if definition is None:
        return _missing(flag_key, default, use_default)
    visiting.add(flag_key)
    try:
        return _decide(definition, definitions, context, visiting, depth)
    finally:
        visiting.remove(flag_key)


def _decide(
    definition: FlagDefinition,
    definitions: Mapping[str, FlagDefinition],
    context: Context,
    visiting: set[str],
    depth: int,
) -> Decision:
    """Apply off, prerequisites, targets, rules, then fallthrough.

    Does not read the database. The caller removes definition.key from
    visiting when this returns.
    """
    if not definition.enabled:
        return _served(definition, definition.off_variation, REASON_OFF, None)
    for prerequisite in definition.prerequisites:
        decision = _evaluate(
            prerequisite.flag_key,
            definitions,
            context,
            visiting,
            depth + 1,
            None,
            False,
        )
        if decision.variation_index != prerequisite.variation_index:
            return _served(definition, definition.off_variation, REASON_PREREQUISITE_FAILED, None)
    targeted = _targeted_index(definition, context)
    if targeted is not None:
        return _served(definition, targeted, REASON_TARGET_MATCH, None)
    for rule in definition.rules:
        matched = _rule_index(definition.key, rule, context)
        if matched is not None:
            return _served(definition, matched, REASON_RULE_MATCH, rule.rule_id)
    return _fallthrough(definition, context)


def _stopped(
    flag_key: str,
    definition: FlagDefinition | None,
    default: object,
    use_default: bool,
) -> Decision:
    """Return prerequisite_failed for a cycle or a walk past the depth cap.

    A flag that is not in the map is missing instead. Does not read the
    database and does not continue the walk.
    """
    if definition is None:
        return _missing(flag_key, default, use_default)
    return _served(definition, definition.off_variation, REASON_PREREQUISITE_FAILED, None)


def _missing(flag_key: str, default: object, use_default: bool) -> Decision:
    """Return flag_not_found.

    The value is default when use_default is true, and None for a prerequisite
    the caller did not ask for by name. variation_index and version are None.
    Does not read the database.
    """
    value = None
    if use_default:
        value = default
    return Decision(
        flag_key=flag_key,
        value=value,
        variation_index=None,
        reason=REASON_FLAG_NOT_FOUND,
        rule_id=None,
        version=None,
    )


def _served(
    definition: FlagDefinition,
    variation_index: int,
    reason: str,
    rule_id: str | None,
) -> Decision:
    """Return a decision for one variation of definition.

    The value is None when variation_index is outside the variation list.
    Does not read the database.
    """
    value = None
    if 0 <= variation_index < len(definition.variations):
        value = definition.variations[variation_index]
    return Decision(
        flag_key=definition.key,
        value=value,
        variation_index=variation_index,
        reason=reason,
        rule_id=rule_id,
        version=definition.version,
    )


def _targeted_index(definition: FlagDefinition, context: Context) -> int | None:
    """Return the variation a context key is pinned to.

    None means the context has no key, or the key is not pinned. The first
    matching list wins if a key was stored twice. Does not read the database.
    """
    if not _has_context_key(context):
        return None
    for variation_index, context_keys in definition.targets:
        for context_key in context_keys:
            if context_key == context.key:
                return variation_index
    return None


def _rule_index(flag_key: str, rule: Rule, context: Context) -> int | None:
    """Return the variation this rule serves, or None when it does not match.

    A rollout with no context key does not match. Does not read the database.
    """
    if not _rule_matches(rule, context):
        return None
    if isinstance(rule.serve, VariationServe):
        return rule.serve.variation_index
    if not _has_context_key(context):
        return None
    return _rollout_index(flag_key, rule.serve, context.key)


def _rule_matches(rule: Rule, context: Context) -> bool:
    """Return True when every clause matches.

    A rule with no clauses matches. Does not read the database.
    """
    for clause in rule.clauses:
        if not _clause_matches(clause, context):
            return False
    return True


def _fallthrough(definition: FlagDefinition, context: Context) -> Decision:
    """Return the fallthrough decision.

    A rollout with no context key serves the off variation, still with
    reason fallthrough. Does not read the database.
    """
    serve = definition.fallthrough
    if isinstance(serve, VariationServe):
        return _served(definition, serve.variation_index, REASON_FALLTHROUGH, None)
    if not _has_context_key(context):
        return _served(definition, definition.off_variation, REASON_FALLTHROUGH, None)
    index = _rollout_index(definition.key, serve, context.key)
    if index is None:
        return _served(definition, definition.off_variation, REASON_FALLTHROUGH, None)
    return _served(definition, index, REASON_FALLTHROUGH, None)


def _clause_matches(clause: Clause, context: Context) -> bool:
    """Return True when the context attribute satisfies the clause.

    A missing attribute does not match, including neq and not_in. An array
    matches only the operators that look at any element. Does not read the
    database.
    """
    if clause.attribute not in context.attributes:
        return False
    value = context.attributes[clause.attribute]
    if value is None:
        return False
    if _is_array(value):
        return _array_matches(clause, value)
    return _scalar_matches(clause, value)


def _array_matches(clause: Clause, elements: list | tuple) -> bool:
    """Return True when any element satisfies a positive operator.

    neq, not_in, and the numeric operators do not match an array. Does not
    read the database.
    """
    if clause.operator not in _ARRAY_OPERATORS:
        return False
    for element in elements:
        if _is_array(element):
            continue
        if _scalar_matches(clause, element):
            return True
    return False


def _scalar_matches(clause: Clause, value: object) -> bool:
    """Return True when one scalar satisfies the operator.

    A type mismatch does not match and is not an error. An unknown operator
    does not match. Does not read the database.
    """
    operator = clause.operator
    if operator == "eq":
        if len(clause.values) != 1:
            return False
        return _same(value, clause.values[0])
    if operator == "neq" or operator == "not_in":
        if len(clause.values) == 0:
            return False
        return _equals_none(value, clause.values)
    if operator == "in":
        return _equals_any(value, clause.values)
    if operator in _STRING_OPERATORS:
        return _string_matches(value, clause.values, operator)
    if operator in _NUMERIC_OPERATORS:
        return _number_matches(value, clause.values, operator)
    return False


def _string_matches(value: object, expected: tuple[object, ...], operator: str) -> bool:
    """Return True when value is a string and one expected string matches.

    operator is starts_with, ends_with, or contains. A non-string value or
    expected item does not match. Does not read the database.
    """
    if not isinstance(value, str):
        return False
    for item in expected:
        if not isinstance(item, str):
            continue
        if operator == "starts_with" and value.startswith(item):
            return True
        if operator == "ends_with" and value.endswith(item):
            return True
        if operator == "contains" and item in value:
            return True
    return False


def _number_matches(value: object, expected: tuple[object, ...], operator: str) -> bool:
    """Return True when both sides are finite numbers and the comparison holds.

    operator is gt, gte, lt, or lte. Booleans are not numbers. The operator
    needs one expected value. Does not read the database.
    """
    if len(expected) != 1 or not _is_number(value) or not _is_number(expected[0]):
        return False
    target = expected[0]
    if operator == "gt":
        return value > target
    if operator == "gte":
        return value >= target
    if operator == "lt":
        return value < target
    if operator == "lte":
        return value <= target
    return False


def _equals_any(value: object, expected: tuple[object, ...]) -> bool:
    """Return True when value is equal to one expected scalar.

    An empty list returns False. Does not read the database.
    """
    for item in expected:
        if _same(value, item):
            return True
    return False


def _equals_none(value: object, expected: tuple[object, ...]) -> bool:
    """Return True when value equals no expected scalar.

    Does not read the database. The caller has already rejected a missing
    attribute.
    """
    for item in expected:
        if _same(value, item):
            return False
    return True


def _same(left: object, right: object) -> bool:
    """Return True when two JSON scalars are the same value.

    A boolean equals only a boolean, so 1 does not equal true. Two numbers
    use numeric equality. A string equals only the same string. Does not
    read the database.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


def _is_number(value: object) -> bool:
    """Return True for a finite int or float.

    Booleans are not numbers. Does not read the database.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _is_array(value: object) -> bool:
    """Return True for a list or a tuple.

    A string is not an array. Does not read the database.
    """
    return isinstance(value, (list, tuple))


def _has_context_key(context: Context) -> bool:
    """Return True when context.key can be targeted or bucketed.

    None and an empty string are not keys. Does not read the database.
    """
    return isinstance(context.key, str) and context.key != ""


def _rollout_index(flag_key: str, rollout: Rollout, context_key: str | None) -> int | None:
    """Return the variation the bucket lands on.

    None means the weights do not cover the bucket. context_key is the
    non-empty key the caller already checked. Does not read the database.
    """
    if context_key is None:
        return None
    bucket = _bucket(flag_key, rollout.salt, context_key)
    covered = 0
    for share in rollout.shares:
        covered = covered + share.weight
        if covered > bucket:
            return share.variation_index
    return None


def _bucket(flag_key: str, salt: str, context_key: str) -> int:
    """Return the bucket in 0..99999 for this flag, salt, and context key.

    The material is flag_key, salt, and context_key joined by colons, then
    UTF-8 encoded. The bucket is the first four SHA-256 bytes as an
    unsigned big-endian integer, modulo 100000. Does not read the database.
    """
    material = flag_key + ":" + salt + ":" + context_key
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[0:4], "big") % 100000
