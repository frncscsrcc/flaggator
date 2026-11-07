"""Static files for the operator console.

The API stays on /health and /v1. Any other path is a file under ui/, or a
404 that does not open the database and does not ask for a credential.
"""

from pathlib import Path

from api.request import Response
from api.routes import route_not_found

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
}


def default_console_directory() -> Path:
    """Return the ui directory that ships next to the sources.

    Walks parents of this file so `python -m main` and an editable install
    find the same tree. Returns that path even when the directory is
    missing. Does not create it.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "ui"
        if candidate.is_dir():
            return candidate
    return here.parents[1] / "ui"


def console_response(
    method: str,
    path: str,
    root: Path | None,
    headless: bool,
) -> Response | None:
    """Serve one console file, or None when path belongs to the API.

    /health and /v1 are left to the API. When headless is true, any other
    path is 404 and root is not read. A GET whose path would leave root, or
    whose suffix is not a console type, is 404. Does not open the database.
    """
    if _is_api_path(path):
        return None
    if headless or root is None or method != "GET":
        return route_not_found()
    file_path = _file_inside(root, path)
    if file_path is None:
        return route_not_found()
    content_type = _CONTENT_TYPES.get(file_path.suffix.lower())
    if content_type is None:
        return route_not_found()
    return Response(status=200, raw=file_path.read_bytes(), content_type=content_type)


def _is_api_path(path: str) -> bool:
    """Return True for the health check and the versioned API.

    Those paths are never read from ui/. Does not look at the filesystem.
    """
    return path == "/health" or path == "/v1" or path.startswith("/v1/")


def _file_inside(root: Path, path: str) -> Path | None:
    """Return the file for path when it stays inside root.

    Rejects an empty path, a directory, a null byte, and any segment that
    is empty, `.`, or `..`. resolve() is then checked with relative_to, so
    a symlink that points out of root is also rejected. Returns None when
    the path is not a file.
    """
    if path == "" or not path.startswith("/") or path.startswith("//"):
        return None
    if "\x00" in path or "\\" in path:
        return None
    relative = path[1:]
    if relative == "" or relative.endswith("/"):
        return None
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    root_resolved = root.resolve()
    candidate = (root_resolved / relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate
