# flaggator

flaggator is an open-source feature-flag service meant to run on your own machine. The HTTP API is the product: a local process serves it, and every behavior the service offers is available through that API.

The project is in the design phase. Until implementation starts, the documents in `docs/` are the source of truth.

The domain will feel familiar to anyone who has used a hosted feature-flag product. The HTTP contract, the schema, and the evaluation algorithm are ours. They are specified in these documents so a later client can implement the same rules.

## Design principles

1. **API first.** Behavior is defined as a versioned HTTP API. A user interface, if one is ever added, is a client of that API.
2. **Thin controllers.** A handler parses a request, calls one domain service, and writes the response. Business rules live in domain services.
3. **Explicit SQL.** Repository functions send SQL text and bind values through the DB-API. Rows are mapped into dataclass DTOs. The project does not use an ORM.
4. **Standard library first.** HTTP, JSON, hashing, and the default database driver (SQLite) come from the Python standard library. MySQL is selected in configuration and pulls in one optional driver. Any other third-party package needs a written reason.
5. **Secure by construction.** Credentials are scoped and stored as hashes. Mutations are audited. Flag rules are data, interpreted by the evaluator. SQL values are bound parameters.

## Layout

```
src/api/                  HTTP controllers
src/domain/models/        dataclass models
src/domain/services/      use cases and the evaluator
src/repositories/         SQL, parameter binding, row mapping, evaluation cache-aside
src/cache/                get / set / delete, memory and null implementations
src/composition.py        process wiring: database engine and cache
docs/                     design documents
```

Dependency direction is one way: `api` → `domain services` → `repositories`. Domain models depend on none of those layers. See [Architecture](docs/architecture.md).

## Documents

| Document | What it decides |
| --- | --- |
| [Decisions](docs/decisions.md) | Accepted constraints and proposals still open |
| [Architecture](docs/architecture.md) | Layers, request lifecycle, module layout |
| [Domain](docs/domain.md) | Flag model and the evaluation algorithm |
| [API](docs/api.md) | Resources, controllers, errors, credentials |
| [OpenAPI](docs/openapi.yaml) | Implemented routes, in a form tools can read |
| [Persistence](docs/persistence.md) | SQL style, DTO mapping, transactions, schema |
| [Cache](docs/cache.md) | Cache protocol, what is stored, invalidation |
| [Security](docs/security.md) | Tokens, authorization, injection, audit, limits |
| [Code style](docs/style.md) | How functions are named, commented, and kept explicit |

## Status

v1 implementation has started. The connection layer, schema migrations, in-memory cache, GET /health, management tokens, projects, environments, flags, and per-environment targeting are in place. `python -m main bootstrap` creates a management token. The process does not listen for requests yet. The license is not chosen yet.
