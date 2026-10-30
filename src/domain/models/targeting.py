"""The targeting document a client sends for one flag in one environment.

These are the parsed fields, not the stored row. Rule ids and rollout salts
may be omitted; the service fills those in. Variation indexes are checked
against the flag in the service, where the variation list is known.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ClauseInput:
    """One comparison inside a rule.

    values holds the JSON scalars the operator compares against. The service
    checks the operator and how many values it allows.
    """

    attribute: str
    operator: str
    values: tuple[object, ...]


@dataclass(frozen=True)
class RolloutWeightInput:
    """One bucket share inside a rollout.

    weight is a non-negative integer. The service requires the weights in
    one rollout to sum to 100000.
    """

    variation_index: int
    weight: int


@dataclass(frozen=True)
class VariationServeInput:
    """Serve one variation index to everyone who reaches this point."""

    variation_index: int


@dataclass(frozen=True)
class RolloutInput:
    """Serve by bucket.

    salt is None when the client omitted it. The service then keeps the
    stored salt, or assigns one when this rollout is new.
    """

    salt: str | None
    variations: tuple[RolloutWeightInput, ...]


@dataclass(frozen=True)
class RuleInput:
    """One ordered rule.

    rule_id is None when the client omitted id. The service assigns an id
    for a new rule and keeps a stored id when the client sends it back.
    """

    rule_id: str | None
    clauses: tuple[ClauseInput, ...]
    serve: VariationServeInput | RolloutInput


@dataclass(frozen=True)
class PrerequisiteInput:
    """Another flag that must evaluate to variation_index.

    flag_key is the public key of that flag in the same project. The service
    checks that the flag exists and that the prerequisite graph has no cycle.
    """

    flag_key: str
    variation_index: int


@dataclass(frozen=True)
class TargetInput:
    """Subjects and segments pinned to one variation index.

    segments holds public keys of segments in the same environment. An
    empty segments tuple means the variation cites subjects only.
    """

    variation_index: int
    subjects: tuple[str, ...]
    segments: tuple[str, ...]


@dataclass(frozen=True)
class ReplaceTargeting:
    """The body of a targeting replace.

    version is the version the client read. A write that sends any other
    version conflicts. targets pairs a variation index with the subjects
    and segment keys pinned to it. Inline subject lists are checked to be
    disjoint in the service.
    """

    enabled: bool
    off_variation: int
    fallthrough: VariationServeInput | RolloutInput
    rules: tuple[RuleInput, ...]
    targets: tuple[TargetInput, ...]
    prerequisites: tuple[PrerequisiteInput, ...]
    version: int
