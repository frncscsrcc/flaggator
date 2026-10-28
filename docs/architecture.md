# Architecture

This document describes how a request crosses the process, which module is allowed to call which, and where a rule is allowed to live.

The constraints it rests on are [A3](decisions.md), [A4](decisions.md), and [A5](decisions.md). The shapes below assume the proposals in [Decisions](decisions.md), especially [P1](decisions.md), [P2](decisions.md), [P4](decisions.md), and [P7](decisions.md).

## Planes

The process exposes two groups of endpoints.

| Plane | Callers | Allowed work |
| --- | --- | --- |
| Management | Operators holding a management token | Create and edit projects, environments, flags, variations, and per-environment targeting. Read the audit log. |
| Evaluation | SDKs and backends holding an environment SDK key | Resolve a context to variation values for that environment. Read the configuration of that environment. |

A management token may also evaluate, so an operator can debug a flag. An SDK key cannot mutate.

## Layers

```
HTTP adapter
    →  api controllers
        →  domain services
            →  repositories
                →  SQL
```

| Module | Owns | Does not own |
| --- | --- | --- |
| `src/api` | HTTP adapter, routing, JSON encoding, request and response dataclasses, controllers | Business rules, SQL |
| `src/domain/models` | Dataclasses for flags, rules, contexts, evaluation results, commands | I/O, SQL, HTTP status codes |
| `src/domain/services` | Use cases, authorization, validation of domain rules, the evaluator, transaction boundaries | HTTP parsing, SQL strings |
| `src/repositories` | SQL text, parameter binding, mapping rows to dataclasses, cache-aside for evaluation reads | Decisions about whether a rule matches or a caller is allowed to edit a flag |
| `src/cache` | `get` / `set` / `delete` with a TTL. Memory and null implementations | Flag rules, SQL, HTTP |

## Dependency rules

- Controllers import services and models. They do not import repositories.
- Services import models and call repository functions. They do not import the API layer.
- Repositories import models. The evaluation-cache wrapper may also import the cache protocol. Repositories do not import services or the API layer.
- The cache imports nothing from `api`, `services`, or `repositories`.
- Models import nothing from `api`, `services`, `repositories`, or `cache`.
- The evaluator imports models only. It does not read the cache. A cached value is a flag configuration the service already loaded.

There is no service locator and no dependency-injection container ([P11](decisions.md)). A function receives what it needs as arguments, including the database connection.

The process starts in one composition root (`src/composition.py`). That module reads configuration, builds the database and the cache, and passes them in. Nothing else imports a database driver or a cache driver. The adapter opens a connection per request and closes it when the response is sent. A connection is not shared across requests. The memory cache is one object for the process ([A8](decisions.md), [Cache](cache.md)).

## Request lifecycle

1. The HTTP adapter reads the request bytes, parses the method, path, query, headers, and body, and builds a request object.
2. Authentication runs before the controller body. An auth service hashes the bearer token and loads the credential through the credential repository. The controller receives a principal that is already resolved, or the adapter returns 401.
3. The controller checks that the JSON has the expected shape and builds a command dataclass. Shape means types and required fields. Whether a variation index exists, whether a rule is well formed, and whether the principal may touch the project are domain checks.
4. The controller calls one service function: `service(conn, principal, command)`.
5. The service authorizes the principal, validates the command against the current state, opens a transaction, calls repository functions, and commits if the work succeeds. For evaluation it loads configuration and calls the pure evaluator.
6. The service returns a domain result or raises a domain error.
7. The controller maps that result to a response dataclass and an HTTP status.
8. The adapter writes the JSON response.

A controller that needs two service calls to finish one HTTP request is a signal that the use case belongs in a single service function.

## Authentication and authorization

These are different steps.

- **Authentication** answers who is calling. It lives at the edge of `src/api` and uses an auth service plus the credential repository. Controllers do not query the credential table.
- **Authorization** answers what this principal may do to this project or environment. It lives in the domain service that performs the use case. The principal is an argument, so the rule is visible in the service and testable with the use case.

## Where data changes shape

| Boundary | From | To |
| --- | --- | --- |
| Adapter → controller | Bytes | Request dataclass |
| Controller → service | Request dataclass | Command dataclass and a principal |
| Repository → service | SQL rows | Domain dataclasses |
| Service → controller | Domain result or domain error | Response dataclass and status |
| Controller → adapter | Response dataclass | JSON bytes |

JSON parsing of stored rule trees happens inside the repository, so services receive clauses, rollouts, and variations as dataclasses.

## Transactions

A service method is the transaction boundary ([P7](decisions.md)).

- The service begins the transaction and commits or rolls back.
- Repository functions accept the connection and run SQL on it.
- Repository functions do not call `commit` or `rollback`.
- A read that must see the writes of the same use case uses the same connection.

Evaluation of a single request is a read. It still uses one connection so a flag and its prerequisites are loaded from one snapshot.

## The evaluator

The evaluator is a function:

```
evaluate(config, context) → decision
```

`config` is the flag's variation list plus one environment's targeting. `context` is the caller's key and attributes. `decision` is the variation value, its index, a reason code, and the config version.

The function is deterministic. The same inputs produce the same decision. Time is not an input. Randomness is not an input. Percentage rollouts hash a stable salt with the context key, as specified in [Domain](domain.md).

Services may call the evaluator many times in one request (bulk evaluate, prerequisites). They may not reimplement matching in the repository.

## Errors

Domain services raise typed domain errors. The set is small and stable:

| Error | Meaning | Typical HTTP status |
| --- | --- | --- |
| `ValidationError` | The command breaks a domain rule | 400 |
| `Unauthenticated` | No usable credential | 401 |
| `Forbidden` | The credential is valid and lacks this permission | 403 |
| `NotFound` | The project, environment, or flag does not exist | 404 |
| `Conflict` | The key already exists, or the config version is stale | 409 |
| `StorageError` | The database driver failed | 500 |

Repositories raise `StorageError` when the driver fails. A uniqueness violation is translated into `Conflict` by the service, because "this flag key already exists" is a domain meaning, not a driver meaning.

Controllers catch domain errors and map them. They do not invent business failures the service did not report. The JSON shape of errors is specified in [API](api.md).

## Tests, by layer

| Layer | How it is tested |
| --- | --- |
| Evaluator | Direct calls with dataclasses. No database, no HTTP. |
| Services | Database from the composition root. The default test database is a temporary SQLite file. The test asserts the use case, including authorization and version conflicts. |
| Repositories | Same database handle, with `NullCache`, so a hit cannot hide a bad query. The test asserts the SQL mapping, including empty results. The suite is written so it can run against MySQL when a DSN is configured. |
| Evaluation cache | `MemoryCache` and the wrapper. A miss loads SQL, a second read does not, a committed write removes the entry. |
| Controllers | Call the handler with a request dataclass. Assert the response dataclass and status. Persistence underneath is a temporary SQLite file, so the controller test stays focused on mapping. |

## Process model for v1

One Python process and the HTTP adapter from the standard library ([P3](decisions.md), [P4](decisions.md)). The database is a SQLite file unless configuration selects MySQL ([A7](decisions.md)). The default listen address is `127.0.0.1`. Operational limits of that choice are in [Security](security.md).

## What a serious change has to respect

A new endpoint adds a controller function, a command, a service function, and repository functions only if new SQL is required. It does not add a second path that writes SQL from the controller or evaluates a flag from a repository query.
