"""Projects, environments, and the list window a service returns.

HTTP responses use the public key and the name. Database ids stay in the
repository rows and are not fields on these objects.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Project:
    """One project as the API shows it.

    key is immutable. name is the display name. This object does not carry
    the database id.
    """

    key: str
    name: str


@dataclass(frozen=True)
class Environment:
    """One environment as the API shows it.

    key is unique inside its project. name is the display name. The SDK
    secret is not a field here.
    """

    key: str
    name: str


@dataclass(frozen=True)
class CreateNamed:
    """The key and name from a create body.

    Used for both a project and an environment. The service decides which
    table the pair belongs to.
    """

    key: str
    name: str


@dataclass(frozen=True)
class KeysetQuery:
    """How far a list should read.

    limit is the page size the caller asked for, already checked against
    the cap. after_key is the last public key of the previous page, or None
    to start at the first key. The service does not treat this as a snapshot.
    """

    limit: int
    after_key: str | None


@dataclass(frozen=True)
class ProjectPage:
    """One page of projects.

    next_key is the last key on this page when another page exists, and
    None when this page is the last, including when items is empty.
    """

    items: list[Project]
    next_key: str | None


@dataclass(frozen=True)
class EnvironmentPage:
    """One page of environments.

    next_key follows the same rule as ProjectPage. items is empty when the
    project has no environments on this page.
    """

    items: list[Environment]
    next_key: str | None
