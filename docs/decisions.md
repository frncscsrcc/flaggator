# Decisions

Every item in this document is accepted. A1–A9 come from the project brief. P1–P12 were accepted when v1 implementation started. P13–P14 are accepted for the users, roles, and JWT milestone. Identifiers stay stable so the other documents keep their links.

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

### P8. Credential kinds

An **SDK token** is bound to one environment and may evaluate and read that environment's configuration. An opaque **management token** may do everything on the management and evaluation planes; it is break-glass / bootstrap / automation, not the model for a person using a future UI. A **user** authenticates with a JWT and holds global roles (`reader`, `writer`, `protected_writer`, `admin`). Detail in [Security](security.md) and [P14](#p14-users-roles-and-protected-environments).

### P9. One variation model

Every flag has an ordered list of variations. Each variation value is JSON. A boolean flag is a flag with two variations, `true` and `false`. The schema does not special-case boolean flags.

### P10. v1 scope

In the first implementation:

- Projects and environments
- Flags with ordered JSON variations
- Per-environment on/off, off variation, fallthrough, ordered rules, individual targets, prerequisites
- Segments of subjects, scoped to one environment and cited from targets
- The clause operators listed in [Domain](domain.md)
- Percentage rollouts with a stable salt
- Server-side evaluation for one flag and for every flag in the environment
- Management tokens and per-environment SDK tokens, stored as hashes
- An audit record for every successful mutation
- Optimistic concurrency on targeting updates, using a config version
- SQLite by default, MySQL when configured, numbered SQL migrations per engine

Explicitly later, so the design does not pretend they exist:

- Web UI
- Streaming updates
- A client SDK that evaluates locally (the algorithm is still specified now)
- Segment clauses and a `segment_match` operator
- Experiments and metrics
- Approval workflows and scheduled changes
- Regex and semantic-version clause operators
- Project-scoped membership (roles stay global; a later local allow-list may restrict which projects a user may touch without putting that list in an external IdP token)
- Refresh tokens and browser OAuth code flows for a hosted UI

Users, global roles, protected environments, and JWT authentication are [P13](#p13-jwt-authentication) and [P14](#p14-users-roles-and-protected-environments), not deferred.

### P11. Composition root, no container

The process has one composition root. That function reads configuration and constructs the database (`SqliteDatabase` or `MysqlDatabase`) and the cache (`MemoryCache` or `NullCache`). It is the only place that knows which implementations are live.

Service and repository functions stay plain functions. They receive a connection, and the evaluation-cache wrapper receives the cache. They do not import `sqlite3`, PyMySQL, or a cache driver, and they do not look up a global.

Those arguments are the injection. A container, a service locator, or a framework that wires constructors is not used. Tests build the same types: a temporary SQLite file by default, or MySQL when a DSN is set, and `NullCache` when the test is about SQL. The evaluator is tested with no database and no cache.

### P12. Cache-aside on evaluation reads

The cache sits in the repository layer, in a wrapper next to the SQL, not inside the SQL strings. Management reads, audit, and credential lookup go to the database. Evaluation reads use one entry per environment. The wrapper deletes that entry after the write commits. The TTL only bounds a reader that repopulates the entry with a row it loaded before the commit. Policy, keys, and the race are specified in [Cache](cache.md).

Redis and Memcached are later implementations of the same protocol. `NullCache` ships with `MemoryCache`, because "no cache" is the off switch and the SQL test double.

### P13. JWT authentication

**Status: implemented** for local issue/verify and external IdP JWKS verify.

The process accepts a standard JWT on the `Authorization: Bearer` header in addition to opaque management and SDK tokens. Opaque tokens stay hash lookups in `api_credentials`. A JWT is verified (signature, `iss`, `aud`, `exp`) and then mapped to the same principal shape the rest of the stack already uses.

Two issuance / verify paths:

- **Local:** `POST /v1/auth/login` with email and password returns an access JWT signed HS256 by flaggator (`FLAGGATOR_JWT_SECRET`). No refresh token in this milestone.
- **External IdP:** `FLAGGATOR_JWT_JWKS_URL` plus issuer and audience. RS*/ES* tokens are verified against that JWKS. The IdP proves identity. **Roles come from the matching local user row** (HS256: decimal `sub`; JWKS: normalized `email`, or `sub` only when it contains `@`), not from trusting the IdP to mint `protected_writer` or `admin`. A numeric IdP `sub` does not select a local id. A JWT with no matching enabled local user is 401.

Minimum claims on a locally issued token: `sub`, `roles`, `iss`, `aud`, `exp`, `iat`. Controllers and domain services receive a principal and do not parse the header. Signing and verification use **PyJWT** (MIT) with the `crypto` extra for JWKS ([Security](security.md) dependency policy). A static verifying key (without JWKS) is not implemented yet.

### P14. Users, roles, and protected environments

There is no multi-tenant mode inside one process. Users and roles apply to every project. Parallel deployments are how separate tenants are isolated.

Roles (closed set):

| Role | Meaning |
| --- | --- |
| `reader` | Management GETs: projects, environments, flags, targeting, audit. Does not evaluate. |
| `writer` | Creates and mutates non-protected resources. Implies read. |
| `protected_writer` | Add-on: with `writer`, may mutate environments marked `protected` and may change the `protected` flag itself. Alone, it does not grant write. |
| `admin` | Every management action, including granting any role. Does not evaluate. |

An opaque management token is treated as having every role. An SDK token has no management roles.

Each environment has `protected` (default false). Promoting or clearing it requires `writer` and `protected_writer`. Writes that target a protected environment (targeting, SDK tokens for that environment, and similar) require the same pair.

Authorization is hybrid: a coarse path gate rejects SDK tokens on management routes and rejects users without `reader`, `writer`, or `admin`; fine checks (`require_reader`, `require_writer`, `require_admin`, `require_protected_write`) live in domain services next to the action, because only SQL knows whether an environment is protected. Detail and the route matrix are in [Security](security.md) and [API](api.md).
