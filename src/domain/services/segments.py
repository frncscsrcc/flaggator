"""Create, list, replace, and delete segments in one environment.

A segment is a named set of subjects. Targets cite its key. Deleting a
segment that a target still cites is refused. Replacing the membership
drops the environment's evaluation bundle after commit.
"""

from domain.errors import Conflict, ConstraintViolation, NotFound, ValidationError
from domain.models.catalog import KeysetQuery
from domain.models.principal import Principal
from domain.models.segment import CreateSegment, ReplaceSegment, Segment, SegmentPage
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import (
    MAX_CONTEXT_STRING_LENGTH,
    MAX_SEGMENT_SUBJECTS,
    is_public_key,
    require_display_name,
    require_page_limit,
    require_public_key,
)
from repositories.db import Connection
from repositories.environments import EnvironmentRow, find_environment
from repositories.eval_cache import EvaluationCache
from repositories.flag_configs import list_environment_targets
from repositories.json_text import dump_json_text
from repositories.projects import ProjectRow, find_project
from repositories.segments import (
    SegmentRow,
    delete_segment,
    find_segment,
    insert_segment,
    list_segments,
    update_segment,
)


def create_segment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    command: CreateSegment,
) -> Segment:
    """Insert a segment and one audit row.

    Raises Forbidden for an SDK token, NotFound when the project or
    environment is absent, ValidationError for a bad key, name, or subject
    list, and Conflict when this environment already has the key. The audit
    document has the public fields only.
    """
    require_management(principal)
    _known_scope(project_key, environment_key)
    key = require_public_key(command.key, "key")
    name = require_display_name(command.name, "name")
    subjects = _subjects(command.subjects)
    segment = Segment(key=key, name=name, subjects=subjects)
    with connection.transaction():
        project, environment = _require_scope(connection, project_key, environment_key)
        try:
            insert_segment(connection, environment.id, key, name, subjects)
        except ConstraintViolation as exc:
            raise Conflict("Segment key already exists.") from exc
        record_change(
            connection,
            principal,
            "segment.create",
            project.key,
            environment.key,
            None,
            None,
            _document(segment),
        )
    return segment


def list_segments_page(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    query: KeysetQuery,
) -> SegmentPage:
    """Return one page of segments in the environment.

    Raises Forbidden for an SDK token, NotFound when the project or
    environment is absent, and ValidationError for a bad limit or cursor.
    next_key is None when no further page exists.
    """
    require_management(principal)
    _known_scope(project_key, environment_key)
    checked = _checked_query(query)
    with connection.transaction():
        _project, environment = _require_scope(connection, project_key, environment_key)
        rows = list_segments(connection, environment.id, checked.after_key, checked.limit + 1)
    visible = rows[: checked.limit]
    if len(rows) <= checked.limit:
        next_key = None
    else:
        next_key = visible[-1].key
    return SegmentPage(items=[_segment(row) for row in visible], next_key=next_key)


def get_segment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> Segment:
    """Return one segment.

    Raises Forbidden for an SDK token and NotFound when the project, the
    environment, or the segment is absent, including when a key cannot
    exist. Does not open a transaction.
    """
    require_management(principal)
    _known_scope(project_key, environment_key)
    if not is_public_key(segment_key):
        raise NotFound("Segment does not exist.")
    _project, environment = _require_scope(connection, project_key, environment_key)
    row = find_segment(connection, environment.id, segment_key)
    if row is None:
        raise NotFound("Segment does not exist.")
    return _segment(row)


def replace_segment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    segment_key: str,
    command: ReplaceSegment,
    evaluation_cache: EvaluationCache,
) -> Segment:
    """Replace a segment's name and subjects, then drop the evaluation bundle.

    The key does not change. Raises Forbidden for an SDK token, NotFound
    when the project, environment, or segment is absent, and ValidationError
    for a bad name or subject list. The cache delete runs after commit.
    """
    require_management(principal)
    _known_scope(project_key, environment_key)
    if not is_public_key(segment_key):
        raise NotFound("Segment does not exist.")
    name = require_display_name(command.name, "name")
    subjects = _subjects(command.subjects)
    segment = Segment(key=segment_key, name=name, subjects=subjects)
    with connection.transaction():
        project, environment = _require_scope(connection, project_key, environment_key)
        current = find_segment(connection, environment.id, segment_key)
        if current is None:
            raise NotFound("Segment does not exist.")
        changed = update_segment(connection, environment.id, segment_key, name, subjects)
        if changed == 0:
            raise NotFound("Segment does not exist.")
        record_change(
            connection,
            principal,
            "segment.update",
            project.key,
            environment.key,
            None,
            _document(_segment(current)),
            _document(segment),
        )
        evaluation_cache.invalidate_environment(connection, project.key, environment.key)
    return segment


def delete_segment_now(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> None:
    """Delete a segment that no target in this environment cites.

    Raises Forbidden for an SDK token, NotFound when the project,
    environment, or segment is absent, and Conflict when a target still
    cites the key. Does not drop the evaluation bundle: a cited segment is
    not deleted, and an uncited one is not part of a decision.
    """
    require_management(principal)
    _known_scope(project_key, environment_key)
    if not is_public_key(segment_key):
        raise NotFound("Segment does not exist.")
    with connection.transaction():
        project, environment = _require_scope(connection, project_key, environment_key)
        current = find_segment(connection, environment.id, segment_key)
        if current is None:
            raise NotFound("Segment does not exist.")
        if _is_referenced(connection, environment.id, segment_key):
            raise Conflict("Segment is still referenced.")
        changed = delete_segment(connection, environment.id, segment_key)
        if changed == 0:
            raise NotFound("Segment does not exist.")
        record_change(
            connection,
            principal,
            "segment.delete",
            project.key,
            environment.key,
            None,
            _document(_segment(current)),
            None,
        )


def _known_scope(project_key: str, environment_key: str) -> None:
    """Raise NotFound when a path key cannot exist.

    A key that is not a public key uses the same message as a missing row.
    Does not read the database.
    """
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    if not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")


def _require_scope(
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> tuple[ProjectRow, EnvironmentRow]:
    """Return the project and environment rows.

    Raises NotFound when either row is absent. Does not commit.
    """
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    environment = find_environment(connection, project.id, environment_key)
    if environment is None:
        raise NotFound("Environment does not exist.")
    return project, environment


def _subjects(subjects: tuple[str, ...]) -> tuple[str, ...]:
    """Return subjects when the list can be stored.

    Raises ValidationError when a subject is empty, longer than 1024
    characters, repeated, or when the list is over the cap. Does not read
    the database.
    """
    if len(subjects) > MAX_SEGMENT_SUBJECTS:
        raise ValidationError(
            "The subject list is not valid.",
            details=[
                {
                    "field": "subjects",
                    "message": "A segment can hold at most 1000 subjects.",
                }
            ],
        )
    seen: set[str] = set()
    for subject in subjects:
        if subject == "" or len(subject) > MAX_CONTEXT_STRING_LENGTH:
            raise ValidationError(
                "The subject list is not valid.",
                details=[
                    {
                        "field": "subjects",
                        "message": "Use a subject of 1 to 1024 characters.",
                    }
                ],
            )
        if subject in seen:
            raise ValidationError(
                "The subject list is not valid.",
                details=[
                    {
                        "field": "subjects",
                        "message": "A segment must not repeat a subject.",
                    }
                ],
            )
        seen.add(subject)
    return subjects


def _checked_query(query: KeysetQuery) -> KeysetQuery:
    """Return query after checking the limit and the cursor key.

    Raises ValidationError when the cursor key is not a public key. Does
    not read the database.
    """
    limit = require_page_limit(query.limit)
    if query.after_key is not None and not is_public_key(query.after_key):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": "Send the cursor from a previous page."}],
        )
    return KeysetQuery(limit=limit, after_key=query.after_key)


def _is_referenced(connection: Connection, environment_id: int, segment_key: str) -> bool:
    """Return True when a target in this environment cites segment_key.

    A list of subjects is not a citation. Raises StorageError when a stored
    targets document is not an object. Does not commit.
    """
    for targets in list_environment_targets(connection, environment_id):
        if _document_cites(targets, segment_key):
            return True
    return False


def _document_cites(targets: object, segment_key: str) -> bool:
    """Return True when targets cites segment_key.

    An array value is subjects only. An object cites the keys in its
    segments list. Raises StorageError when the document is not that shape.
    Does not read the database.
    """
    if not isinstance(targets, dict):
        raise StorageError()
    for value in targets.values():
        if isinstance(value, list):
            continue
        if not isinstance(value, dict):
            raise StorageError()
        segments = value.get("segments")
        if not isinstance(segments, list):
            raise StorageError()
        if segment_key in segments:
            return True
    return False


def _segment(row: SegmentRow) -> Segment:
    """Copy a repository row into the API document.

    Does not read the database.
    """
    return Segment(key=row.key, name=row.name, subjects=row.subjects)


def _document(segment: Segment) -> str:
    """Return the audit JSON for one segment.

    The document has the public fields only. Does not include a secret.
    """
    return dump_json_text(
        {
            "key": segment.key,
            "name": segment.name,
            "subjects": list(segment.subjects),
        }
    )
