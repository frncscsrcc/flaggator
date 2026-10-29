# Domain

This document is the ubiquitous language for flaggator and the specification of evaluation. Names here are the names to use in code, API fields, and schema identifiers.

Proposals [P2](decisions.md), [P5](decisions.md), [P9](decisions.md), and [P10](decisions.md) apply throughout.

## Glossary

| Term | Meaning |
| --- | --- |
| Project | A container for flags and environments. Identified by a stable `key`. |
| Environment | A deployment stage inside a project, such as `dev` or `prod`. Targeting is stored per environment. |
| Flag | A named switch inside a project. Identified by a `key` unique within the project. The key is immutable. |
| Variation | One possible value of a flag. Variations are ordered. The index in that order is the variation's identity. |
| Flag configuration | The targeting of one flag in one environment: on/off, off variation, targets, rules, fallthrough, prerequisites, version. |
| Context | The subject a caller asks about. It has a `key` and a map of attributes. |
| Target | An explicit pin of one context key to one variation index. |
| Rule | An ordered predicate. The first matching rule wins. |
| Clause | One comparison inside a rule. Every clause in the rule must match. |
| Rollout | A weighted split across variations, bucketed by a hash of the context key. |
| Fallthrough | What is served when the flag is on and no target or rule matches. A variation index, or a rollout. |
| Off variation | The variation served when the flag is off, or when a prerequisite fails. |
| Prerequisite | Another flag in the same environment that must evaluate to a given variation before this flag's own rules run. |
| Decision | The result of evaluation: variation index, JSON value, reason, config version. |
| Salt | A stable string mixed into the rollout hash so buckets do not move when unrelated rules change. |
| Principal | The authenticated caller: a management token or an SDK key. |

## Identity and scope

Two scopes exist, on purpose ([P5](decisions.md)).

**Project scope**

- Flag key, name, description
- The ordered variation list

**Environment scope**

- `enabled`
- Off variation index
- Individual targets
- Ordered rules
- Fallthrough
- Prerequisites
- Config `version`

Turning a flag on in `prod` does not turn it on in `dev`. Adding a variation changes the flag for every environment, because variation indexes are project-wide. Removing or reordering a variation is a project-level change and must be rejected while any environment config still points at the affected indexes. v1 can allow appending a variation and renaming a flag's display name. It does not allow deleting a variation that is still referenced.

## Variations

A variation is an index plus a JSON value ([P9](decisions.md)).

A boolean flag is a normal flag whose variations are `true` and `false`, conventionally index `0` = `false` and index `1` = `true`. Callers and the API may offer a shorthand to create that pair. Stored form is the same as any other flag.

Values are JSON: booleans, numbers, strings, arrays, and objects. A variation value has a maximum size, given in [Security](security.md).

## Context

```json
{
  "key": "user-123",
  "attributes": {
    "country": "IT",
    "plan": "pro",
    "age": 30
  }
}
```

- `key` is a non-empty string. Individual targets and rollouts use it.
- `attributes` is a flat map. Values are JSON scalars (string, number, boolean) or arrays of scalars. Nested objects are out of scope for v1.
- Attribute names are case-sensitive.
- A missing attribute does not match a clause, including negative operators such as `neq` and `not_in`. Absence is not a value.

A later multi-kind context (user plus organization) can be added by namespacing attribute names. v1 has one context per evaluation request.

## Configuration document

For one flag in one environment, the configuration the evaluator receives is:

| Field | Role |
| --- | --- |
| `enabled` | When false, serve the off variation immediately. |
| `off_variation` | Variation index served when the flag is off or a prerequisite fails. |
| `targets` | Map of variation index → list of context keys. |
| `rules` | Ordered list. First match wins. |
| `fallthrough` | Variation index or rollout. |
| `prerequisites` | List of `{flag_key, variation_index}`. |
| `version` | Integer incremented on every successful write of this configuration. |

A configuration created with a new environment, before any targeting write, is disabled: `enabled` 0, `off_variation` 0, and `version` 1. The stored documents are `{"variation":0}` for fallthrough, `[]` for rules and prerequisites, and `{}` for targets. A variation serve uses that same `{"variation": <index>}` object.

A rule:

| Field | Role |
| --- | --- |
| `id` | Stable identifier. Assigned by the service. Used in reasons and as the default salt source. |
| `clauses` | All must match (logical AND). |
| `serve` | Either a variation index or a rollout. |

A clause:

| Field | Role |
| --- | --- |
| `attribute` | Attribute name to read from the context. |
| `operator` | One of the v1 operators below. |
| `values` | List of JSON scalars to compare against. |

A rollout:

| Field | Role |
| --- | --- |
| `salt` | Stable string. Set when the rollout is created. Unchanged when weights are edited. |
| `variations` | List of `{variation_index, weight}`. Weights are non-negative integers and sum to `100000`. |

Weight unit is 1/100000 of the audience, so one weight unit is 0.001%. A 50/50 split is `50000` and `50000`.

## Clause operators (v1)

| Operator | Matches when |
| --- | --- |
| `eq` | The attribute equals the single value. |
| `neq` | The attribute is present and equals none of the values. |
| `in` | The attribute equals one of the values. |
| `not_in` | The attribute is present and equals none of the values. |
| `starts_with` | The attribute is a string and starts with one of the string values. |
| `ends_with` | The attribute is a string and ends with one of the string values. |
| `contains` | The attribute is a string and contains one of the string values. |
| `gt` | The attribute is a number and is greater than the single numeric value. |
| `gte` | The attribute is a number and is greater than or equal to the single numeric value. |
| `lt` | The attribute is a number and is less than the single numeric value. |
| `lte` | The attribute is a number and is less than or equal to the single numeric value. |

Type mismatch means the clause does not match. A string compared with `gt` does not match. It is not an error.

If the attribute value is an array, `eq`, `in`, `starts_with`, `ends_with`, and `contains` match when any element matches. `neq`, `not_in`, and the numeric operators do not treat arrays as a match in v1; the clause does not match.

Regex and semantic-version operators are deferred ([P10](decisions.md)). Regex is deferred because unbounded patterns are a denial-of-service risk; see [Security](security.md).

## Evaluation algorithm

Inputs: the project variation list, the environment configuration, the context, and a way to evaluate prerequisites (the same algorithm on other flags in the same environment).

Output: a decision.

Order:

1. **Off.** If `enabled` is false, return `off_variation` with reason `off`.
2. **Prerequisites.** Evaluate each prerequisite flag for the same context. If any prerequisite's resulting variation index is not the required index, return this flag's `off_variation` with reason `prerequisite_failed`.
3. **Targets.** If the context has a key and that key is listed under a variation, return that variation with reason `target_match`. A key listed under two variations is a `ValidationError` at write time, so evaluation can treat the lists as disjoint.
4. **Rules.** Walk rules in order. A rule matches when every clause matches. On the first matching rule:
   - If the rule serves a variation, return it with reason `rule_match`.
   - If the rule serves a rollout, bucket the context. If the context has no key, the rule does not match and walking continues. If the context has a key, return the bucketed variation with reason `rule_match`.
5. **Fallthrough.** If the fallthrough is a variation, return it with reason `fallthrough`. If it is a rollout, bucket when the context has a key and return reason `fallthrough`. If the context has no key, return `off_variation` with reason `fallthrough`.

Unknown flag: the evaluate endpoint does not invent a variation. The caller supplies a default value on the request. The service returns that default with reason `flag_not_found` and no config version. A management read of a missing flag is `NotFound` instead. The default exists so an application can keep a safe value beside the call.

### Prerequisites and cycles

Writes reject a prerequisite list that contains a cycle, including a flag that requires itself, using the configs currently stored in that environment.

Evaluation also keeps the set of flag keys already entered on the current walk. A key is removed when that flag's decision has been produced, so two flags may require the same third flag. Re-entering a key that is still on the walk yields `prerequisite_failed` for the flag that closed the cycle. The requested flag is depth 1. A flag entered deeper than the cap returns `prerequisite_failed` and its prerequisites are not evaluated. The cap is in [Security](security.md). Both checks exist so a bad write and a racing pair of writes cannot make evaluation recurse without bound.

### Bucketing

Bucketing is deterministic. It does not use a random source.

```
material = flag_key + ":" + salt + ":" + context_key
digest   = SHA-256(material)          # bytes, hashlib
bucket   = uint32_be(digest[0:4]) % 100000
```

`uint32_be` is the first four digest bytes as an unsigned big-endian integer.

Walk the rollout variations in stored order, accumulating weight. The first variation whose cumulative weight is greater than `bucket` is chosen. Because weights sum to `100000`, every bucket hits one variation.

The salt is created by the service when the rollout is first saved (a random id is appropriate here; this is the one place randomness enters, and it is stored). Editing weights keeps the salt, so people stay in the same bucket unless the weight boundaries move over them. Replacing the salt is a deliberate reshuffle, represented as a new salt on write, and it is audited like any other targeting change.

SHA-256 is the standard-library hash. Bucket assignments are **not** compatible with hosted feature-flag products. They are compatible with any client that implements this document.

### Reasons

| Reason | When |
| --- | --- |
| `off` | Flag is disabled. |
| `prerequisite_failed` | A prerequisite did not evaluate to the required variation, or a cycle/depth guard tripped. |
| `target_match` | Context key is individually targeted. |
| `rule_match` | The first matching rule served this variation. The decision includes the rule id. |
| `fallthrough` | No target or rule selected a variation. |
| `flag_not_found` | Evaluate was asked for a key that is not in the project. The value is the caller's default. |

Each decision also includes the config `version` when a flag existed, so a caller can tell which targeting produced the result.

## Write rules worth stating now

- Flag keys and project keys are immutable.
- Keys match `^[a-z0-9]+(?:-[a-z0-9]+)*$` (lowercase, digits, single hyphens, no leading or trailing hyphen).
- Creating an existing key is a `Conflict`.
- A targeting write sends the `version` it read. The service updates only if the stored version equals it, then increments the version. A mismatch is a `Conflict`.
- Clause lists, value lists, and rule lists are bounded by the limits in [Security](security.md).
- The service checks that every variation index in targets, rules, fallthrough, off variation, and prerequisites exists on the flag.
- Individual target lists in one configuration are disjoint.
- Rollout weights are non-negative and sum to `100000`.
- `eq`, `gt`, `gte`, `lt`, and `lte` require `values` to contain exactly one element. The list operators `in`, `not_in`, `neq`, `starts_with`, `ends_with`, and `contains` require at least one element.

## Worked example

Flag `checkout-v2` in environment `prod`, variations `false` (0) and `true` (1), enabled.

- Off variation: `0`
- Target: context key `alice` → variation `1`
- Rule `r1`: `country in ["IT", "FR"]` serves a rollout salt `s1`, weights `1 → 10000`, `0 → 90000`
- Fallthrough: variation `0`

| Context | Decision | Reason |
| --- | --- | --- |
| flag disabled, any context | `false` | `off` |
| `{key: alice, country: US}` | `true` | `target_match` |
| `{key: bob, country: IT}` | bucket of `checkout-v2:s1:bob` | `rule_match` |
| `{key: bob, country: US}` | `false` | `fallthrough` |
| `{country: IT}` and no key | `false` | `fallthrough` (the rollout rule does not match without a key; fallthrough is variation `0`) |

## Segments

A segment is a named set of context keys or clauses, reusable across flags. v1 rules do not reference segments ([P10](decisions.md)). Clause operators stay a closed list so a later `segment_match` operator can be added without changing the evaluation order.
