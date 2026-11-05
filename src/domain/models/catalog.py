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

    key is unique inside its project. name is the display name. protected
    is true when writes need protected_writer. client_id is the opaque
    browser id, or None until an operator issues one. The SDK secret is
    not a field here.
    """

    key: str
    name: str
    protected: bool
    client_id: str | None


@dataclass(frozen=True)
class CreateNamed:
    """The key and name from a create body.

    Used for a project. The service decides which table the pair belongs to.
    """

    key: str
    name: str


@dataclass(frozen=True)
class CreateEnvironment:
    """The body for creating an environment.

    protected defaults to false when the client omits it.
    """

    key: str
    name: str
    protected: bool


@dataclass(frozen=True)
class PatchEnvironment:
    """The body for patching an environment.

    None means the field was omitted and stays unchanged.
    """

    name: str | None
    protected: bool | None


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
