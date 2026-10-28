"""Flags, their variations, and one environment's configuration.

HTTP responses use public keys. Database ids stay in repository rows.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FlagConfig:
    """Targeting for one flag in one environment.

    key is the environment key. enabled is false until a targeting write
    turns the flag on. variations are not stored here; they belong to the
    flag. fallthrough, rules, targets, and prerequisites are the parsed
    JSON documents.
    """

    key: str
    enabled: bool
    off_variation: int
    fallthrough: object
    rules: object
    targets: object
    prerequisites: object
    version: int


@dataclass(frozen=True)
class Flag:
    """One flag as a fetch or a write response shows it.

    variations are ordered. The index is the position, starting at 0.
    environments is empty when the project has no environments.
    """

    key: str
    name: str
    description: str
    variations: tuple[object, ...]
    environments: tuple[FlagConfig, ...]


@dataclass(frozen=True)
class FlagConfigSummary:
    """The list view of one environment configuration.

    This is not the rule tree. key is the environment key.
    """

    key: str
    enabled: bool
    off_variation: int
    version: int


@dataclass(frozen=True)
class FlagSummary:
    """One flag on a list page.

    environment is None when the caller did not ask for an environment.
    variations are the full ordered list.
    """

    key: str
    name: str
    description: str
    variations: tuple[object, ...]
    environment: FlagConfigSummary | None


@dataclass(frozen=True)
class FlagPage:
    """One page of flags.

    next_key is the last key on this page when another page exists, and
    None when this page is the last, including when items is empty.
    """

    items: list[FlagSummary]
    next_key: str | None


@dataclass(frozen=True)
class CreateFlag:
    """The body of a flag create.

    variations has at least two JSON values. The service checks that rule.
    """

    key: str
    name: str
    description: str
    variations: tuple[object, ...]


@dataclass(frozen=True)
class PatchFlag:
    """The body of a flag patch.

    A None field was omitted and must be left as stored. variations, when
    set, is the full ordered list the client sent, not only the new tail.
    """

    name: str | None
    description: str | None
    variations: tuple[object, ...] | None
