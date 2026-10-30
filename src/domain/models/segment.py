"""Segments: a named set of subjects inside one environment.

The public key is what a target cites. The name is the operator label.
Database ids stay in the repository.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Segment:
    """One segment as the API shows it.

    key is unique inside its environment and does not change. subjects is
    the membership, in the order the last write stored. An empty tuple
    matches nobody.
    """

    key: str
    name: str
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class CreateSegment:
    """The body of a segment create.

    The service checks the key, the name, and the subject list.
    """

    key: str
    name: str
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class ReplaceSegment:
    """The body of a segment replace.

    name and subjects replace the stored values. The key stays in the path.
    """

    name: str
    subjects: tuple[str, ...]


@dataclass(frozen=True)
class SegmentPage:
    """One page of segments.

    next_key is the last key on this page when another page exists, and
    None when this page is the last, including when items is empty.
    """

    items: list[Segment]
    next_key: str | None
