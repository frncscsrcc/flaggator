# Persistence

Repositories are the only modules that know table and column names. This document is the contract for how they talk to the database ([A4](decisions.md), [A7](decisions.md), [P3](decisions.md), [P6](decisions.md), [P7](decisions.md)).

## Engines

Configuration selects the engine ([A7](decisions.md)).

| Engine | Driver | When |
| --- | --- | --- |
| SQLite | `sqlite3` from the standard library | Default. A file path in configuration. |
| MySQL | PyMySQL, optional extra, imported only by the MySQL module | Host, database, user, and password in configuration. InnoDB, `utf8mb4`. |

Repository functions do not import either driver. They call a connection wrapper. The wrapper is the dialect seam ([P3](decisions.md)).

DML is one SQL text per query, shared by both engines. DDL is two files per migration, because `CREATE TABLE` is where the dialects diverge.

## Connection wrapper

Two modules implement the same protocol:

- `src/repositories/sqlite.py`
- `src/repositories/mysql.py`

The protocol lives in `src/repositories/db.py`. Callers see this surface:

| Operation | Behavior |
| --- | --- |
| `connect()` | Open a session. One connection per request. Never shared across requests. |
| `execute(sql, params)` | Run one statement. The cursor's `fetchone` / `fetchall` return mappings keyed by column name. |
| `executemany(sql, seq)` | Same binding rules, many parameter tuples. |
| `last_insert_id` | On the cursor, the integer id of the row just inserted. |
| `rowcount` | On the cursor, rows changed by the statement. Used by the versioned update. |
| `transaction()` | Context manager on the connection. Commit on success, roll back on any exception. |
| `on_commit(callback)` | Run the callback after this transaction commits. Drop it on rollback. The evaluation cache uses it to `delete` keys only once the new row is visible ([Cache](cache.md)). |

Session setup stays inside the module that opens the connection.

- SQLite enables foreign keys (`PRAGMA foreign_keys = ON`) and WAL (`PRAGMA journal_mode = WAL`).
- MySQL sets `utf8mb4` and uses InnoDB. It does not run a `PRAGMA`.

`last_insert_id` hides `cursor.lastrowid` on both drivers. Repository SQL does not use `INSERT ... RETURNING` and does not call `LAST_INSERT_ID()`.

A unique-constraint failure is raised as `ConstraintViolation`, a storage error with no driver type in it. The service maps that to `Conflict`. Callers do not import `sqlite3.IntegrityError` or PyMySQL's error types.

## Placeholders

Repository SQL uses `?` for every bound value. The SQLite wrapper passes the statement through. The MySQL wrapper rewrites `?` to `%s` and escapes a literal `%` as `%%`, ignoring question marks and percent signs that sit inside SQL string literals.

`LIKE` patterns are bound parameters (`WHERE name LIKE ?`), not fragments of the SQL string. That keeps the rewriter boring and keeps user input out of the statement.

## Portable DML

A repository statement stays inside this subset, so it runs on both engines without a second copy:

- Explicit column lists. No `SELECT *`.
- `?` placeholders for every value.
- Boolean literals written as `0` and `1`.
- JSON stored and loaded as text. No SQLite or MySQL JSON functions. Parsing happens in the repository, in Python.
- Timestamps stored as UTC ISO-8601 text.
- `COALESCE`, not `IFNULL`.
- `LIMIT` and `OFFSET` as bound parameters when they are variable.
- No `RETURNING`, no `ON CONFLICT`, no `ON DUPLICATE KEY`, no `PRAGMA`, no `SET`, no engine hints.

An upsert, if a use case needs one, is a wrapper method with two implementations. It is not a second SQL string inside the repository. v1 targeting writes use the versioned `UPDATE` below, which is already portable. A missed version check changes zero rows; the service raises `Conflict`.

Inserts that race on a unique key rely on `ConstraintViolation`, not on dialect-specific upsert syntax.

## What a repository function looks like

A repository function takes a connection and explicit arguments, executes SQL text, binds every value, and returns a dataclass or a list of dataclasses.

```python
@dataclass(frozen=True)
class FlagRow:
    key: str
    name: str

def get_flag(conn: Connection, project_id: int, flag_key: str) -> FlagRow | None:
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

Rules baked into that shape:

- The SQL is a literal in the function. It is readable without running the code.
- Every value from a caller, a request, or another row used as a value is a bound parameter.
- The `SELECT` lists columns. `SELECT *` is not used, so a mapping names its fields on purpose.
- The row is mapped in one place in the function, into a frozen dataclass.
- `None` from `fetchone` becomes `None`. An empty list is an empty list. Missing data is not turned into an exception here unless the query was expected to return a row that the schema guarantees (for example a row just inserted on the same connection).
- The function does not commit, roll back, or check whether a principal is allowed to see the flag.

The wrapper returns each row as a mapping by column name, so the mapper does not depend on column position or on `sqlite3.Row`.

## Parameters and identifiers

Bound parameters carry values. They do not carry table names, column names, or SQL keywords.

When a query needs a dynamic column (for example a sort field), the repository chooses the column from a fixed allow-list written in code and interpolates only that chosen literal. User input never becomes part of the SQL string. If the requested name is not in the allow-list, the function raises `ValidationError`.

This is the injection rule. [Security](security.md) refers to it.

## DTO mapping

| Kind | Where it lives | Role |
| --- | --- | --- |
| Row DTO | `src/domain/models` or the repository module if it is private to one query | Exact shape of one query result. Frozen dataclass. |
| Domain model | `src/domain/models` | The flag, configuration, clause, rollout, and decision types that services and the evaluator use. |

When a query returns an entity the domain already has a type for, the repository maps straight into that type. A second DTO that mirrors the same fields field-for-field is not required.

When a query returns a slice (a list row, an audit row, a credential record), that slice has its own dataclass. Services depend on the dataclass, not on a driver row type.

JSON columns (`variations` values, rule trees) are stored as `TEXT`. The repository serializes on write and parses on read, and returns clauses, rollouts, and values as dataclasses and Python objects. Invalid JSON in a column is a `StorageError`: the database has failed its contract with the application.

## Transactions

The service owns the transaction ([P7](decisions.md)). It uses the wrapper, not a dialect keyword:

```text
service:
    with conn.transaction():
        repository writes and reads on this connection
    on success the wrapper commits
    on any exception the wrapper rolls back, then the exception propagates
```

Repository functions accept the open connection and do not start their own transactions. Nested service calls share the connection they were given; they do not begin a second transaction.

Targeting writes are small. The service validates in memory, then writes. The optimistic `version` check is part of the write:

```sql
UPDATE flag_configs
   SET enabled = ?,
       version = version + 1
 WHERE id = ?
   AND version = ?;
```

If the update reports zero changed rows, the service raises `Conflict`. The version predicate is the concurrency control. The application does not use a separate lock table in v1.

## Schema outline

Logical types are what the application means. Each engine's migration renders them in its own DDL. Repository mappers turn integer `0` and `1` into `bool`. They parse JSON text and ISO-8601 timestamps in Python.

| Logical | SQLite | MySQL |
| --- | --- | --- |
| Integer id | `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY` |
| Boolean | `INTEGER NOT NULL` restricted to `0` and `1` | `TINYINT NOT NULL` |
| Short text (keys, names) | `TEXT` | `VARCHAR` of a stated length |
| JSON document | `TEXT` | `TEXT` |
| Timestamp | `TEXT` (UTC ISO-8601) | `VARCHAR(32)` (UTC ISO-8601) |

| Table | Purpose | Notable columns |
| --- | --- | --- |
| `projects` | Project identity | `key` unique, `name` |
| `environments` | Environment inside a project | `project_id`, `key` unique per project, `name` |
| `flags` | Flag identity inside a project | `project_id`, `key` unique per project, `name`, `description` |
| `flag_variations` | Ordered values | `flag_id`, `variation_index` unique per flag, `value_json` |
| `flag_configs` | One row per flag per environment | `flag_id`, `environment_id`, `enabled`, `off_variation`, `fallthrough_json`, `rules_json`, `targets_json`, `prerequisites_json`, `version` |
| `segments` | Named subject list inside an environment | `environment_id`, `key` unique per environment, `name`, `subjects_json` |
| `api_credentials` | Management tokens and SDK tokens | `kind`, `name` (operator label for SDK tokens, null for management), `secret_hash`, `prefix`, `environment_id` null for management tokens, `revoked_at` |
| `audit_events` | Mutation log | `at`, `actor_kind`, `actor_id`, `action`, `project_key`, `environment_key`, `flag_key`, `before_json`, `after_json` |
| `schema_migrations` | Applied migration ids | `id` primary key |

`rules_json`, `targets_json`, `fallthrough_json`, and `prerequisites_json` hold the tree specified in [Domain](domain.md) ([P6](decisions.md)). The whole targeting document is loaded and saved together, so v1 does not split rules or targets into child tables. `subjects_json` is the same kind of payload: the segment key is the identity, and a write replaces the subject list as a whole. Identity that other tables reference with foreign keys stays in columns: projects, environments, flags, variation indexes, segment keys, credential ids. A target cites a segment by that key inside `targets_json`; it does not use a foreign key.

Foreign keys:

- `environments.project_id` → `projects.id`
- `flags.project_id` → `projects.id`
- `flag_variations.flag_id` → `flags.id`
- `flag_configs.flag_id` → `flags.id`
- `flag_configs.environment_id` → `environments.id`
- `segments.environment_id` → `environments.id`
- `api_credentials.environment_id` → `environments.id` when present

Unique keys:

- `projects.key`
- `(environments.project_id, environments.key)`
- `(flags.project_id, flags.key)`
- `(flag_variations.flag_id, flag_variations.variation_index)`
- `(flag_configs.flag_id, flag_configs.environment_id)`
- `(segments.environment_id, segments.key)`

Database integer ids stay inside repositories and services. HTTP responses use public keys, as [API](api.md) requires. A repository may return an id to a service so the service can pass it to the next SQL call in the same use case. Controllers do not put those ids in JSON.

## Migrations

Schema changes are numbered SQL files, one pair per id:

```
db/migrations/0001_init.sqlite.sql
db/migrations/0001_init.mysql.sql
```

The id is the numeric prefix. Startup applies every id that `schema_migrations` does not yet contain, using the file for the configured engine, in order. A migration runs inside `transaction()` when the engine allows DDL in a transaction (SQLite does). MySQL DDL causes an implicit commit; the runner still records the id only after the file succeeds, and a failure aborts startup. Migrations are forward-only. The files are SQL edited by hand. No migration framework is required.

The two files of a pair create the same tables, the same unique keys, and the same foreign keys. They differ in type names, autoincrement syntax, and the MySQL table options (`ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`).

## Wiring

`src/composition.py` is the only module that chooses an engine:

```text
settings.engine == "sqlite"  →  SqliteDatabase(path)
settings.engine == "mysql"   →  MysqlDatabase(host, database, user, password)
settings.cache  == "memory"  →  MemoryCache
settings.cache  == "null"    →  NullCache
```

Repository modules import the protocol and receive a connection. They do not open files and they do not read the DSN. Tests construct `SqliteDatabase` on a temporary file, or `MysqlDatabase` when a DSN is configured, and pass that connection through the same service functions.
