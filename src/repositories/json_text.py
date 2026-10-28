"""Parse JSON text that was stored in a column.

Invalid JSON means the database broke its contract with the application.
"""

import json

from domain.errors import StorageError


def dump_json_text(value: object) -> str:
    """Return canonical JSON text for a document the application is storing.

    Object keys are sorted so two equal documents store the same text. Does
    not read the database. The caller passes a document it just built.
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def load_json_text(value: object) -> object:
    """Return the Python value stored in a JSON text column.

    Raises StorageError when value is not valid JSON text. Does not commit
    and does not log the text.
    """
    if not isinstance(value, str):
        raise StorageError()
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise StorageError() from exc
