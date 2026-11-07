"""The pure evaluator. These tests do not open a database."""

import hashlib
import random
import sys
import time
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

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
    Prerequisite,
    Rollout,
    RolloutShare,
    Rule,
    TargetGroup,
    VariationServe,
)
from domain.services.evaluator import evaluate
from domain.validation import MAX_PREREQUISITE_DEPTH


class EvaluatorTests(unittest.TestCase):
    def test_worked_example_follows_the_domain_table(self) -> None:
        """The checkout-v2 table in the domain doc, including a disabled flag.

        Alice is targeted even when her country would also match the rule.
        A rollout rule does not match without a subject.
        """
        definitions = {"checkout-v2": self.checkout(True)}
        disabled = {"checkout-v2": self.checkout(False)}
        alice = Context("alice", {"country": "US"})
        self.assertEqual(
            evaluate("checkout-v2", disabled, Context("alice", {"country": "IT"})),
            self.decision(False, 0, REASON_OFF, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, alice),
            self.decision(True, 1, REASON_TARGET_MATCH, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("bob", {"country": "IT"})),
            self.decision(False, 0, REASON_RULE_MATCH, "r1"),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("bob", {"country": "US"})),
            self.decision(False, 0, REASON_FALLTHROUGH, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context(None, {"country": "IT"})),
            self.decision(False, 0, REASON_FALLTHROUGH, None),
        )

    def test_a_segment_pins_the_subject_and_the_first_index_wins(self) -> None:
        """A cited segment matches like an inline subject. The first index wins.

        iris is in both segments, so variation 0 wins. A context without a
        subject does not enter the target.
        """
        definition = FlagDefinition(
            key="checkout-v2",
            variations=(False, True),
            enabled=True,
            off_variation=0,
            fallthrough=VariationServe(0),
            rules=(),
            targets=(
                TargetGroup(0, (), ("testers",)),
                TargetGroup(1, ("alice",), ("developers",)),
            ),
            prerequisites=(),
            version=1,
            segments={
                "developers": ("iris", "bob"),
                "testers": ("iris",),
            },
        )
        definitions = {"checkout-v2": definition}
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("iris", {})),
            self.decision_for("checkout-v2", False, 0, REASON_TARGET_MATCH, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("bob", {})),
            self.decision_for("checkout-v2", True, 1, REASON_TARGET_MATCH, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("alice", {})),
            self.decision_for("checkout-v2", True, 1, REASON_TARGET_MATCH, None),
        )
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context(None, {})),
            self.decision_for("checkout-v2", False, 0, REASON_FALLTHROUGH, None),
        )

    def test_each_operator_and_a_missing_attribute(self) -> None:
        """Each v1 operator matches the domain table, and absence matches nothing.

        A boolean is not the number 1. A string does not satisfy gt. An
        unknown operator does not match.
        """
        self.assertTrue(self.matches("eq", ("IT",), {"country": "IT"}))
        self.assertFalse(self.matches("eq", ("IT",), {"country": "FR"}))
        self.assertFalse(self.matches("eq", ("IT",), {}))
        self.assertTrue(self.matches("eq", (True,), {"country": True}))
        self.assertFalse(self.matches("eq", (True,), {"country": 1}))
        self.assertTrue(self.matches("neq", ("IT",), {"country": "FR"}))
        self.assertFalse(self.matches("neq", ("IT",), {"country": "IT"}))
        self.assertFalse(self.matches("neq", ("IT",), {}))
        self.assertTrue(self.matches("in", ("IT", "FR"), {"country": "FR"}))
        self.assertFalse(self.matches("in", ("IT", "FR"), {"country": "US"}))
        self.assertTrue(self.matches("not_in", ("IT",), {"country": "US"}))
        self.assertFalse(self.matches("not_in", ("IT",), {"country": "IT"}))
        self.assertFalse(self.matches("not_in", ("IT",), {}))
        self.assertTrue(self.matches("starts_with", ("IT",), {"country": "ITALY"}))
        self.assertFalse(self.matches("starts_with", ("IT",), {"country": "FR"}))
        self.assertFalse(self.matches("starts_with", ("IT",), {"country": 10}))
        self.assertTrue(self.matches("ends_with", ("LY",), {"country": "ITALY"}))
        self.assertFalse(self.matches("ends_with", ("LY",), {"country": "IT"}))
        self.assertTrue(self.matches("contains", ("AL",), {"country": "ITALY"}))
        self.assertFalse(self.matches("contains", ("AL",), {"country": "IT"}))
        self.assertTrue(self.matches("gt", (18,), {"country": 30}))
        self.assertFalse(self.matches("gt", (18,), {"country": 18}))
        self.assertFalse(self.matches("gt", (18,), {"country": "30"}))
        self.assertTrue(self.matches("gte", (30,), {"country": 30}))
        self.assertFalse(self.matches("gte", (30,), {"country": 18}))
        self.assertTrue(self.matches("lt", (30,), {"country": 18}))
        self.assertFalse(self.matches("lt", (30,), {"country": 30}))
        self.assertTrue(self.matches("lte", (30,), {"country": 30}))
        self.assertFalse(self.matches("lte", (30,), {"country": 31}))
        self.assertFalse(self.matches("regex", ("IT",), {"country": "IT"}))
        self.assertFalse(self.matches("eq", ("IT",), {"Country": "IT"}))
        self.assertTrue(self.matches("eq", ("IT",), {"country": "IT"}, subject=None))

    def test_every_clause_must_match_and_the_first_rule_wins(self) -> None:
        """A rule is an AND. A later rule is not consulted after a match."""
        both = Rule(
            "both",
            (Clause("country", "eq", ("IT",)), Clause("plan", "eq", ("pro",))),
            VariationServe(1),
        )
        later = Rule("later", (Clause("country", "eq", ("IT",)),), VariationServe(0))
        definition = self.flag((both, later), VariationServe(0))
        italy_only = evaluate("flag", {"flag": definition}, Context("user", {"country": "IT"}))
        both_match = evaluate(
            "flag",
            {"flag": definition},
            Context("user", {"country": "IT", "plan": "pro"}),
        )
        self.assertEqual(italy_only.reason, REASON_RULE_MATCH)
        self.assertEqual(italy_only.rule_id, "later")
        self.assertEqual(italy_only.variation_index, 0)
        self.assertEqual(both_match.reason, REASON_RULE_MATCH)
        self.assertEqual(both_match.rule_id, "both")
        self.assertEqual(both_match.variation_index, 1)

    def test_arrays_match_only_positive_operators(self) -> None:
        """An array matches when any element matches eq, in, or a string operator.

        neq and gt do not match an array, even when no element would equal
        the value.
        """
        countries = {"country": ["IT", "FR"]}
        self.assertTrue(self.matches("eq", ("FR",), countries))
        self.assertFalse(self.matches("eq", ("US",), countries))
        self.assertTrue(self.matches("in", ("US", "FR"), countries))
        self.assertTrue(self.matches("starts_with", ("FR",), countries))
        self.assertTrue(self.matches("ends_with", ("LY",), {"country": ["IT", "ITALY"]}))
        self.assertTrue(self.matches("contains", ("AL",), {"country": ["XX", "ITALY"]}))
        self.assertFalse(self.matches("neq", ("US",), countries))
        self.assertFalse(self.matches("not_in", ("US",), countries))
        self.assertFalse(self.matches("gt", (10,), {"country": [30]}))

    def test_rollout_bucket_is_stable_and_walks_in_order(self) -> None:
        """The same subject and salt select the same variation.

        bob's bucket is 27461, so the 10000 weight does not cover it and the
        next share does. user-11 lands in the first share.
        """
        self.assertEqual(self.bucket("checkout-v2", "s1", "bob"), 27461)
        self.assertEqual(self.bucket("checkout-v2", "s1", "user-11"), 3913)
        self.assertEqual(self.bucket("checkout-v2", "s1", "user-0"), 49693)
        definitions = {"checkout-v2": self.checkout(True)}
        first = evaluate("checkout-v2", definitions, Context("bob", {"country": "IT"}))
        second = evaluate("checkout-v2", definitions, Context("bob", {"country": "IT"}))
        low = evaluate("checkout-v2", definitions, Context("user-11", {"country": "IT"}))
        high = evaluate("checkout-v2", definitions, Context("user-0", {"country": "IT"}))
        self.assertEqual(first, second)
        self.assertEqual(first, self.decision(False, 0, REASON_RULE_MATCH, "r1"))
        self.assertEqual(low, self.decision(True, 1, REASON_RULE_MATCH, "r1"))
        self.assertEqual(high, self.decision(False, 0, REASON_RULE_MATCH, "r1"))
        self.assertEqual(
            evaluate("checkout-v2", definitions, Context("", {"country": "IT"})),
            self.decision(False, 0, REASON_FALLTHROUGH, None),
        )

    def test_a_cycle_returns_prerequisite_failed(self) -> None:
        """A flag that requires itself, and a pair that require each other, both stop.

        The off variation is not the required index, so the prerequisite fails.
        """
        loop = self.plain("loop", (Prerequisite("loop", 1),), VariationServe(1))
        left = self.plain("left", (Prerequisite("right", 1),), VariationServe(1))
        right = self.plain("right", (Prerequisite("left", 1),), VariationServe(1))
        self.assertEqual(
            evaluate("loop", {"loop": loop}, Context("user", {})),
            self.decision_for("loop", False, 0, REASON_PREREQUISITE_FAILED, None),
        )
        self.assertEqual(
            evaluate("left", {"left": left, "right": right}, Context("user", {})),
            self.decision_for("left", False, 0, REASON_PREREQUISITE_FAILED, None),
        )

    def test_a_satisfied_prerequisite_continues_the_walk(self) -> None:
        """A prerequisite that returns the required index does not set the reason.

        Two flags may require the same third flag. A disabled flag does not
        walk its prerequisites.
        """
        child = self.plain("child", (), VariationServe(1))
        parent = self.plain("parent", (Prerequisite("child", 1),), VariationServe(0))
        shared = self.plain("shared", (), VariationServe(1))
        one = self.plain("one", (Prerequisite("shared", 1),), VariationServe(1))
        two = self.plain("two", (Prerequisite("shared", 1),), VariationServe(1))
        root = self.plain(
            "root",
            (Prerequisite("one", 1), Prerequisite("two", 1)),
            VariationServe(0),
        )
        disabled = FlagDefinition(
            key="parent",
            variations=(False, True),
            enabled=False,
            off_variation=0,
            fallthrough=VariationServe(1),
            rules=(),
            targets=(),
            prerequisites=(Prerequisite("missing", 1),),
            version=1,
        )
        self.assertEqual(
            evaluate("parent", {"parent": parent, "child": child}, Context("user", {})),
            self.decision_for("parent", False, 0, REASON_FALLTHROUGH, None),
        )
        self.assertEqual(
            evaluate(
                "root",
                {"root": root, "one": one, "two": two, "shared": shared},
                Context("user", {}),
            ),
            self.decision_for("root", False, 0, REASON_FALLTHROUGH, None),
        )
        self.assertEqual(
            evaluate("parent", {"parent": disabled}, Context("user", {})),
            self.decision_for("parent", False, 0, REASON_OFF, None),
        )

    def test_a_chain_past_the_depth_cap_stops(self) -> None:
        """Ten flags in a chain still evaluate. The eleventh stops the walk.

        The last flag would serve variation 1. Past the cap it serves the
        off variation, and each parent then fails its prerequisite.
        """
        allowed = evaluate("f0", self.chain(10), Context("user", {}))
        stopped = evaluate("f0", self.chain(11), Context("user", {}))
        self.assertEqual(allowed, self.decision_for("f0", True, 1, REASON_FALLTHROUGH, None))
        self.assertEqual(
            stopped,
            self.decision_for("f0", False, 0, REASON_PREREQUISITE_FAILED, None),
        )

    def test_a_wide_graph_reuses_each_flag_at_a_depth(self) -> None:
        """Ten layers, the first a single root and the rest five flags wide.

        Every flag requires every flag in the next layer. Without a memory
        that walk repeats the same flag on every path. With one, it finishes
        and the root still serves variation 1, because every leaf does.
        """
        definitions = self.fan(layers=10, width=5)
        started = time.monotonic()
        decision = evaluate("root", definitions, Context("user", {}))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(
            decision,
            self.decision_for("root", True, 1, REASON_FALLTHROUGH, None),
        )

    def test_memory_matches_a_fresh_walk_when_the_depth_cap_is_in_play(self) -> None:
        """A remembered decision matches a walk that recomputes every edge.

        The graphs are acyclic. Some chains are longer than the depth cap,
        so the same flag is required both where the cap stops it and where
        it does not. The caller's default stays off the memory: a missing
        root still returns that default.
        """
        generator = random.Random(11)
        context = Context("user", {})
        for _draw in range(30):
            definitions = self.random_dag(generator)
            for key in definitions:
                remembered = evaluate(key, definitions, context)
                fresh = _walk_without_memory(key, definitions, set(), 1)
                self.assertEqual(remembered.variation_index, fresh.variation_index)
                self.assertEqual(remembered.reason, fresh.reason)
        missing = evaluate("absent", {}, context, default="fallback")
        self.assertEqual(missing.value, "fallback")
        self.assertEqual(missing.reason, REASON_FLAG_NOT_FOUND)

    def test_a_missing_flag_returns_the_caller_default(self) -> None:
        """The default is the value only when the flag key is unknown."""
        decision = evaluate("missing", {}, Context("user", {}), default=False)
        self.assertEqual(
            decision,
            Decision(
                flag_key="missing",
                value=False,
                variation_index=None,
                reason=REASON_FLAG_NOT_FOUND,
                rule_id=None,
                version=None,
            ),
        )
        present = evaluate("flag", {"flag": self.flag((), VariationServe(1))}, Context("user", {}), default=False)
        self.assertEqual(present.value, "yes")
        self.assertEqual(present.reason, REASON_FALLTHROUGH)

    def checkout(self, enabled: bool) -> FlagDefinition:
        """Return the worked example, on or off.

        Variations are false then true. The rule rollout puts weight 10000
        on true and 90000 on false. Does not evaluate.
        """
        return FlagDefinition(
            key="checkout-v2",
            variations=(False, True),
            enabled=enabled,
            off_variation=0,
            fallthrough=VariationServe(0),
            rules=(
                Rule(
                    "r1",
                    (Clause("country", "in", ("IT", "FR")),),
                    Rollout("s1", (RolloutShare(1, 10000), RolloutShare(0, 90000))),
                ),
            ),
            targets=(TargetGroup(1, ("alice",), ()),),
            prerequisites=(),
            version=7,
        )

    def flag(self, rules: tuple[Rule, ...], fallthrough: VariationServe) -> FlagDefinition:
        """Return an enabled flag named flag with variations miss, yes.

        The off variation is 0. Does not evaluate.
        """
        return FlagDefinition(
            key="flag",
            variations=("miss", "yes"),
            enabled=True,
            off_variation=0,
            fallthrough=fallthrough,
            rules=rules,
            targets=(),
            prerequisites=(),
            version=1,
        )

    def plain(
        self,
        key: str,
        prerequisites: tuple[Prerequisite, ...],
        fallthrough: VariationServe,
    ) -> FlagDefinition:
        """Return an enabled flag with variations false, true and no rules.

        The off variation is 0. Does not evaluate.
        """
        return FlagDefinition(
            key=key,
            variations=(False, True),
            enabled=True,
            off_variation=0,
            fallthrough=fallthrough,
            rules=(),
            targets=(),
            prerequisites=prerequisites,
            version=1,
        )

    def fan(self, layers: int, width: int) -> dict[str, FlagDefinition]:
        """Return a root plus layers-1 rows of width flags.

        Each flag requires every flag in the next row, at variation 1.
        The last row has no prerequisites and serves variation 1. Does not
        evaluate.
        """
        definitions: dict[str, FlagDefinition] = {}
        previous: tuple[str, ...] = ()
        row = layers - 1
        while row >= 1:
            keys: list[str] = []
            column = 0
            while column < width:
                key = "l" + str(row) + "-" + str(column)
                keys.append(key)
                prerequisites = tuple(Prerequisite(child, 1) for child in previous)
                definitions[key] = self.plain(key, prerequisites, VariationServe(1))
                column = column + 1
            previous = tuple(keys)
            row = row - 1
        root_prerequisites = tuple(Prerequisite(child, 1) for child in previous)
        definitions["root"] = self.plain("root", root_prerequisites, VariationServe(1))
        return definitions

    def random_dag(self, generator: random.Random) -> dict[str, FlagDefinition]:
        """Return a small acyclic graph, sometimes deeper than the depth cap.

        Edges point at a higher index, so the graph has no cycle. A flag
        serves variation 1 when its prerequisites do. Does not evaluate.
        """
        count = generator.randint(4, 12)
        keys = ["n" + str(index) for index in range(count)]
        definitions: dict[str, FlagDefinition] = {}
        index = count - 1
        while index >= 0:
            later = keys[index + 1 :]
            take = generator.randint(0, min(3, len(later)))
            chosen = generator.sample(later, take) if take > 0 else []
            if index == 0:
                chosen.append("c0")
            prerequisites = tuple(Prerequisite(child, 1) for child in chosen)
            definitions[keys[index]] = self.plain(keys[index], prerequisites, VariationServe(1))
            index = index - 1
        chain_index = 11
        while chain_index >= 0:
            key = "c" + str(chain_index)
            prerequisites = ()
            if chain_index < 11:
                prerequisites = (Prerequisite("c" + str(chain_index + 1), 1),)
            definitions[key] = self.plain(key, prerequisites, VariationServe(1))
            chain_index = chain_index - 1
        definitions["shallow"] = self.plain("shallow", (Prerequisite("c8", 1),), VariationServe(1))
        return definitions

    def chain(self, length: int) -> dict[str, FlagDefinition]:
        """Return length flags where each one requires the next at variation 1.

        The last flag serves variation 1 and has no prerequisite. An empty
        chain returns an empty map. Does not evaluate.
        """
        definitions: dict[str, FlagDefinition] = {}
        index = 0
        while index < length:
            key = "f" + str(index)
            prerequisites: tuple[Prerequisite, ...] = ()
            if index + 1 < length:
                prerequisites = (Prerequisite("f" + str(index + 1), 1),)
            definitions[key] = self.plain(key, prerequisites, VariationServe(1))
            index = index + 1
        return definitions

    def matches(
        self,
        operator: str,
        values: tuple[object, ...],
        attributes: dict,
        subject: str | None = "user",
    ) -> bool:
        """Return True when one clause serves variation 1.

        A miss falls through to variation 0. Does not open a database.
        """
        definition = self.flag(
            (Rule("r", (Clause("country", operator, values),), VariationServe(1)),),
            VariationServe(0),
        )
        decision = evaluate("flag", {"flag": definition}, Context(subject, attributes))
        return decision.variation_index == 1

    def decision(self, value: object, variation_index: int, reason: str, rule_id: str | None) -> Decision:
        """Return the worked-example decision for checkout-v2 at version 7."""
        return self.decision_for("checkout-v2", value, variation_index, reason, rule_id, 7)

    def decision_for(
        self,
        flag_key: str,
        value: object,
        variation_index: int,
        reason: str,
        rule_id: str | None,
        version: int = 1,
    ) -> Decision:
        """Return a decision for a flag that exists. Does not evaluate."""
        return Decision(
            flag_key=flag_key,
            value=value,
            variation_index=variation_index,
            reason=reason,
            rule_id=rule_id,
            version=version,
        )

    def bucket(self, flag_key: str, salt: str, subject: str) -> int:
        """Return the domain bucket, computed here rather than by the evaluator.

        This is the spec's SHA-256 formula, so the test does not ask the
        function for the number it is about to trust.
        """
        material = flag_key + ":" + salt + ":" + subject
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        return int.from_bytes(digest[0:4], "big") % 100000


def _walk_without_memory(
    flag_key: str,
    definitions: dict[str, FlagDefinition],
    visiting: set[str],
    depth: int,
) -> Decision:
    """Decide flag_key the way the walk did before it remembered decisions.

    Only the flags these tests build: enabled, no rules, a variation
    fallthrough. A cycle or a depth past the cap serves the off variation.
    Does not read the database.
    """
    definition = definitions.get(flag_key)
    if flag_key in visiting or depth > MAX_PREREQUISITE_DEPTH:
        if definition is None:
            return Decision(flag_key, None, None, REASON_FLAG_NOT_FOUND, None, None)
        index = definition.off_variation
        return Decision(
            flag_key,
            definition.variations[index],
            index,
            REASON_PREREQUISITE_FAILED,
            None,
            definition.version,
        )
    if definition is None:
        return Decision(flag_key, None, None, REASON_FLAG_NOT_FOUND, None, None)
    visiting.add(flag_key)
    try:
        for prerequisite in definition.prerequisites:
            child = _walk_without_memory(
                prerequisite.flag_key,
                definitions,
                visiting,
                depth + 1,
            )
            if child.variation_index != prerequisite.variation_index:
                index = definition.off_variation
                return Decision(
                    flag_key,
                    definition.variations[index],
                    index,
                    REASON_PREREQUISITE_FAILED,
                    None,
                    definition.version,
                )
        if not isinstance(definition.fallthrough, VariationServe):
            raise AssertionError("the reference walk only serves a variation")
        index = definition.fallthrough.variation_index
        return Decision(
            flag_key,
            definition.variations[index],
            index,
            REASON_FALLTHROUGH,
            None,
            definition.version,
        )
    finally:
        visiting.remove(flag_key)
