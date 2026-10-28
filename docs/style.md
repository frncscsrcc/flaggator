# Code style

Code in this project is written for a human reader. An AI assistant will read it too. The structure, the names, and the comments are for the person who opens the file six months later and has to see what a function does, what it changes, and why.

[A6](decisions.md) still holds: identifiers, comments, and docstrings are English. [A9](decisions.md) is this document.

## Shape of a function

A function does one job. Its name says that job (`replace_targeting`, `get_flag`). A reader should be able to read it top to bottom without jumping through several modules to learn what the next line is for.

Every function has type hints on its parameters and its return value, and a docstring directly under the `def` line. The docstring says:

- what the function does
- what it returns, including the empty case (`None`, an empty list)
- the side effects: a SQL write, a cache `delete`, a commit, a rollback, a raised domain error

An argument is described in the docstring when its name does not already carry the constraint. `flag_key` needs no gloss. `version` does, because the caller must send the version it read.

```python
def get_flag(conn: Connection, project_id: int, flag_key: str) -> FlagRow | None:
    """Load one flag by its public key inside a project.

    Returns None when no row matches. Does not commit. Whether the caller
    may see the flag is checked by the service before this runs.
    """
    row = conn.execute(
        """
        SELECT key, name
          FROM flags
         WHERE project_id = ? AND key = ?
        """,
        (project_id, flag_key),
    ).fetchone()
    if row is None:
        return None
    return FlagRow(key=row["key"], name=row["name"])
```

A comment inside the body is for a reason the next lines cannot state by themselves: a security rule, a dialect limit, the order of two operations. The comment sits on the line above that code and is a complete sentence.

```python
# The cache is dropped only after commit, so a rollback cannot leave
# readers loading a value the database never stored.
conn.on_commit(lambda: cache.delete(cache_key))
```

A comment that repeats the next statement does not belong. `i = i + 1  # increment i` fails this guide.

## Explicit

Prefer a few plain lines to a compact expression that a reader has to unpack.

- Names are full words: `project_key`, `ttl_seconds`, `environment_id`.
- A known set of fields is named parameters, not `**kwargs`.
- Branches are `if` statements, not nested conditional expressions.
- Imports name the module they come from. No star-imports.
- No metaprogramming, no decorator stack, and no registry that hides which function runs.
- Constants that bound a behavior (a TTL, a maximum number of rules) are named and live next to the code that enforces them.

SQL follows [Persistence](persistence.md): a literal in the repository function, parameters bound, columns listed. The docstring does not replace the SQL. The SQL is the readable part.

## Where a comment must not invent a second behavior

Controllers stay thin, services own the use case, repositories own SQL and the evaluation cache. A docstring describes the function that contains it. It does not document a rule that actually lives in another layer. If the authorization rule is in the service, the repository docstring says that the repository does not check it, as the example above does.
