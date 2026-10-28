"""UTC timestamps stored in text columns.

The value is second precision and ends in Z so it fits VARCHAR(32).
"""

from datetime import datetime, timezone


def utc_timestamp() -> str:
    """Return the current UTC time as an ISO-8601 string.

    Microseconds are dropped. Does not read the database.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return now.isoformat().replace("+00:00", "Z")
