"""Cache-aside for one environment's evaluation bundle.

SQL lives in the bundle repository. This module reads that result on a
miss and deletes the entry after a commit. It does not evaluate flags.
"""

import json

from cache.protocol import Cache
from domain.validation import MAX_CACHED_BUNDLE_BYTES
from repositories.bundles import BundleFlag, load_bundle_flags
from repositories.db import Connection
from repositories.json_text import dump_json_text


class EvaluationCache:
    """The evaluation bundle for one process.

    cache stores bytes. ttl_seconds is the backstop used on every set.
    Management reads do not use this object.
    """

    def __init__(self, cache: Cache, ttl_seconds: int) -> None:
        """Keep the byte cache and the TTL. Does not connect."""
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    def load_document(
        self,
        connection: Connection,
        project_id: int,
        environment_id: int,
        project_key: str,
        environment_key: str,
    ) -> dict:
        """Return the evaluation document for one environment.

        A hit returns the cached JSON and does not read the bundle tables.
        A miss loads SQL, then stores the document when it is at most 1 MiB.
        A larger document is returned and not stored. Does not commit.
        """
        key = cache_key(project_key, environment_key)
        cached = self._cache.get(key)
        if cached is not None:
            document = _decode(cached, project_key, environment_key)
            if document is not None:
                return document
        flags = load_bundle_flags(connection, project_id, environment_id)
        document = _document(project_key, environment_key, flags)
        encoded = dump_json_text(document).encode("utf-8")
        if len(encoded) <= MAX_CACHED_BUNDLE_BYTES:
            self._cache.set(key, encoded, self._ttl_seconds)
        return document

    def invalidate_environment(
        self,
        connection: Connection,
        project_key: str,
        environment_key: str,
    ) -> None:
        """Drop this environment's bundle after the open transaction commits.

        The delete is discarded when the transaction rolls back. Raises
        StorageError when no transaction is open. Does not delete now.
        """
        connection.on_commit(_DeleteKey(self._cache, cache_key(project_key, environment_key)))

    def invalidate_project(
        self,
        connection: Connection,
        project_key: str,
        environment_keys: list[str],
    ) -> None:
        """Drop every named environment bundle after commit.

        environment_keys is empty when the project has no environments, and
        then nothing is registered. Does not delete now.
        """
        for environment_key in environment_keys:
            self.invalidate_environment(connection, project_key, environment_key)


class _DeleteKey:
    """One cache delete registered on a transaction."""

    def __init__(self, cache: Cache, key: str) -> None:
        """Remember the key. Does not delete it yet."""
        self._cache = cache
        self._key = key

    def __call__(self) -> None:
        """Delete the key. A missing key is not an error."""
        self._cache.delete(self._key)


def cache_key(project_key: str, environment_key: str) -> str:
    """Return the cache key for one environment.

    Public keys do not contain a colon, so the parts stay unambiguous.
    Does not read the database.
    """
    return "eval:" + project_key + ":" + environment_key


def _document(project_key: str, environment_key: str, flags: list[BundleFlag]) -> dict:
    """Return the JSON document evaluation-config and the cache share.

    flags is empty when the project has no flags. Does not read the database.
    """
    items: list[dict] = []
    for flag in flags:
        items.append(
            {
                "enabled": flag.enabled,
                "fallthrough": flag.fallthrough,
                "key": flag.key,
                "off_variation": flag.off_variation,
                "prerequisites": flag.prerequisites,
                "rules": flag.rules,
                "targets": flag.targets,
                "variations": list(flag.variations),
                "version": flag.version,
            }
        )
    return {
        "environment_key": environment_key,
        "flags": items,
        "project_key": project_key,
    }


def _decode(payload: bytes, project_key: str, environment_key: str) -> dict | None:
    """Return the cached document, or None when it is not that environment.

    A damaged payload is a miss, so the caller loads SQL and replaces it.
    Does not read the database.
    """
    try:
        text = payload.decode("utf-8")
        document = json.loads(text)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    if document.get("project_key") != project_key:
        return None
    if document.get("environment_key") != environment_key:
        return None
    if not isinstance(document.get("flags"), list):
        return None
    return document
