# Decisions

Every item in this document is accepted. A1–A9 come from the project brief. P1–P12 were the design proposals and were accepted when v1 implementation started. Their identifiers stay P1–P12 so the other documents keep their links.

## Accepted

### A1. Local, open-source feature-flag service

The service runs as a process the operator starts. It stores flag configuration and evaluates flags for a caller-supplied context.

### A2. API first

Every behavior is exposed through a versioned HTTP API. There is no behavior that exists only behind a user interface.

### A3. Python, standard library preferred

Implementation language is Python. Prefer the standard library. Avoid a web framework. Introduce a third-party library only with a written reason.

### A4. SQL through a database library, no ORM

Persistence uses a SQL database and a DB-API driver. Queries are SQL text written in repository functions. Values are bound as parameters. Result rows are mapped into dataclass DTOs. No ORM, no query builder that hides the SQL.

### A5. Thin controllers, domain services, repositories

API controllers stay small. Domain services own the use cases. Services call repository functions for database access.

Module roots:

- `src/api`
- `src/domain/models`
- `src/domain/services`
- `src/repositories`

### A6. English for code and documents

Identifiers, comments, commit messages, API fields, and documents are English.

### A7. SQLite and MySQL, chosen in configuration

The operator selects the engine in configuration. SQLite is the default. MySQL is the other supported engine. Services, controllers, and repository SQL do not change when the engine changes. A third engine is a new decision.

### A8. A cache with get, set, delete, and a TTL

The process has a cache. `set` stores bytes under a string key for a TTL. `get` returns those bytes or nothing. `delete` removes a key. The first implementation is an in-memory map for a single process. Further implementations sit behind the same three operations. The repository layer is the only caller. Detail in [Cache](cache.md).

### A9. Code is written for a human reader

Functions are explicit and commented in English. Every function has a docstring that states what it does, what it returns, and its side effects. A comment inside a function explains a constraint the code cannot show on its own. The guide is [Code style](style.md).

### P1. Two API planes

**Management** creates and edits projects, environments, flags, and targeting. **Evaluation** resolves a context to variation values and can read the configuration required to do that.

Same layering on both planes. Different credentials. See [API](api.md) and [Security](security.md).

### P2. Evaluation is a pure function

Given a flag configuration and a context, the evaluator returns a variation and a reason. It does not open a connection, read the clock for bucketing, or call a repository. Services load configuration, then call the evaluator. The algorithm is specified in [Domain](domain.md) so a future client can reproduce it.

### P3. One DML dialect, a thin database seam

Repository SQL is written once, with `?` placeholders, in the portable subset defined in [Persistence](persistence.md). A connection wrapper, chosen at startup, absorbs the driver differences: placeholder marker, last insert id, unique-violation translation, transactions, and session setup.

`CREATE TABLE` is not shared. Each migration id has a SQLite file and a MySQL file.

SQLite uses the standard-library `sqlite3` driver. MySQL uses PyMySQL, imported only by the MySQL module and installed as an optional extra. The reason is narrow: the standard library has no MySQL driver. There is still no ORM and no query builder.

### P4. A standard-library HTTP adapter we own

Controllers are plain functions from a request dataclass to a response dataclass. A small adapter in `src/api` reads HTTP, routes to those functions, and writes JSON, using the standard library (`wsgiref` or `http.server`). No Flask, Django, FastAPI, or similar.

The adapter is for a local process. The default bind address is loopback. See [Security](security.md).

### P5. Project-level identity, environment-level targeting

A flag's key, name, and variation list belong to the project. Whether the flag is on, and the rules, targets, fallthrough, and off variation, belong to an environment. Detail in [Domain](domain.md).

### P6. Normalized identity, JSON rule trees

Projects, environments, flags, variations, and per-environment config headers are tables. The ordered rule tree inside a config is stored as JSON text and mapped to dataclasses at the repository boundary. The evaluator never sees JSON.

### P7. The service method is the transaction boundary

A repository function does not commit or roll back. The service opens the transaction, calls one or more repository functions with that connection, and commits on success.

### P8. Two credential kinds

An **SDK key** is bound to one environment and may evaluate and read that environment's configuration. A **management token** may mutate. v1 has no user accounts: an operator is whoever holds a management token. Detail in [Security](security.md).

### P9. One variation model

Every flag has an ordered list of variations. Each variation value is JSON. A boolean flag is a flag with two variations, `true` and `false`. The schema does not special-case boolean flags.

### P10. v1 scope

In the first implementation:

- Projects and environments
- Flags with ordered JSON variations
- Per-environment on/off, off variation, fallthrough, ordered rules, individual targets, prerequisites
- The clause operators listed in [Domain](domain.md)
- Percentage rollouts with a stable salt
- Server-side evaluation for one flag and for every flag in the environment
- Management tokens and per-environment SDK keys, stored as hashes
- An audit record for every successful mutation
- Optimistic concurrency on targeting updates, using a config version
- SQLite by default, MySQL when configured, numbered SQL migrations per engine

Explicitly later, so the design does not pretend they exist:

- Web UI
- Streaming updates
- A client SDK that evaluates locally (the algorithm is still specified now)
- Segments
- Experiments and metrics
- Approval workflows and scheduled changes
- Accounts, SSO, and roles finer than the two credential kinds
- Regex and semantic-version clause operators
- Standard JWT authentication. v1 uses opaque bearer tokens. See [P13](#p13-standard-jwt-later).

### P11. Composition root, no container

The process has one composition root. That function reads configuration and constructs the database (`SqliteDatabase` or `MysqlDatabase`) and the cache (`MemoryCache` or `NullCache`). It is the only place that knows which implementations are live.

Service and repository functions stay plain functions. They receive a connection, and the evaluation-cache wrapper receives the cache. They do not import `sqlite3`, PyMySQL, or a cache driver, and they do not look up a global.

Those arguments are the injection. A container, a service locator, or a framework that wires constructors is not used. Tests build the same types: a temporary SQLite file by default, or MySQL when a DSN is set, and `NullCache` when the test is about SQL. The evaluator is tested with no database and no cache.

### P12. Cache-aside on evaluation reads

The cache sits in the repository layer, in a wrapper next to the SQL, not inside the SQL strings. Management reads, audit, and credential lookup go to the database. Evaluation reads use one entry per environment. The wrapper deletes that entry after the write commits. The TTL only bounds a reader that repopulates the entry with a row it loaded before the commit. Policy, keys, and the race are specified in [Cache](cache.md).

Redis and Memcached are later implementations of the same protocol. `NullCache` ships with `MemoryCache`, because "no cache" is the off switch and the SQL test double.

### P13. Standard JWT later

v1 authenticates an opaque bearer token stored as a hash. A later version also accepts a standard JWT: a signed token with the usual issuer, audience, and expiry checks.

Both methods produce the same principal. Controllers and domain services receive that principal and do not parse the `Authorization` header. Adding JWT does not add a second authorization rule. JWT is not issued or verified in v1.
