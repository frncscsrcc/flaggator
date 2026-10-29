"""Inputs and the decision of one pure evaluation.

These objects are not rows. The evaluator does not read them from SQL.
A rollout salt is already stored; evaluation never mints one.
"""

from collections.abc import Mapping
from dataclasses import dataclass

REASON_OFF = "off"
REASON_PREREQUISITE_FAILED = "prerequisite_failed"
REASON_TARGET_MATCH = "target_match"
REASON_RULE_MATCH = "rule_match"
REASON_FALLTHROUGH = "fallthrough"
REASON_FLAG_NOT_FOUND = "flag_not_found"


@dataclass(frozen=True)
class Context:
    """The subject of one evaluation.

    key is None, or empty, when the caller has no context key. Targets and
    rollouts do not match in that case. attributes is a flat map; a missing
    name is not a value.
    """

    key: str | None
    attributes: Mapping[str, object]


@dataclass(frozen=True)
class Clause:
    """One comparison. Every clause in a rule must match."""

    attribute: str
    operator: str
    values: tuple[object, ...]


@dataclass(frozen=True)
class RolloutShare:
    """One variation's weight in a rollout.

    weight is a non-negative integer. The shares in one rollout sum to 100000.
    """

    variation_index: int
    weight: int


@dataclass(frozen=True)
class Rollout:
    """A weighted choice.

    salt is the stored salt. Evaluation mixes it into the bucket and does
    not replace it.
    """

    salt: str
    shares: tuple[RolloutShare, ...]


@dataclass(frozen=True)
class VariationServe:
    """Serve one variation index."""

    variation_index: int


@dataclass(frozen=True)
class Rule:
    """One ordered rule. The first match wins.

    rule_id is the id the targeting write stored. It is copied onto a
    rule_match decision.
    """

    rule_id: str
    clauses: tuple[Clause, ...]
    serve: VariationServe | Rollout


@dataclass(frozen=True)
class Prerequisite:
    """Another flag that must evaluate to variation_index for this context."""

    flag_key: str
    variation_index: int


@dataclass(frozen=True)
class FlagDefinition:
    """One flag's variations and one environment's targeting.

    key is the public flag key mixed into rollout buckets. targets pairs a
    variation index with the context keys pinned to it. fallthrough is a
    variation or a rollout.
    """

    key: str
    variations: tuple[object, ...]
    enabled: bool
    off_variation: int
    fallthrough: VariationServe | Rollout
    rules: tuple[Rule, ...]
    targets: tuple[tuple[int, tuple[str, ...]], ...]
    prerequisites: tuple[Prerequisite, ...]
    version: int


@dataclass(frozen=True)
class Decision:
    """The result of evaluating one flag.

    variation_index and version are None when the flag is not in the
    definitions. rule_id is set only for reason rule_match. value is the
    variation's JSON value, or the caller default when the flag is absent.
    """

    flag_key: str
    value: object
    variation_index: int | None
    reason: str
    rule_id: str | None
    version: int | None
