# flaggator

flaggator is an open-source feature-flag service meant to run on your own machine. The HTTP API is the product: a local process serves it, and every behavior the service offers is available through that API.

The documents in `docs/` specify the HTTP contract, the schema, and the evaluation algorithm. The run section starts a local process that serves the implemented routes.

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
| [Example](docs/example.md) | A checkout rollout, from the management token to an SDK call |

## Run

From the repository, with the virtual environment already activated:

```bash
source .venv/bin/activate
python -m main bootstrap
```

Bootstrap creates `flaggator.db` when it is missing, applies migrations, and prints a management token once. The notice on stderr does not contain the token. Save the token, then start the process in another terminal:

```bash
python -m main
```

The process listens on `127.0.0.1:8080`. `FLAGGATOR_BIND_HOST`, `FLAGGATOR_BIND_PORT`, and `FLAGGATOR_SQLITE_PATH` change the address and the database file. Put the printed token in `TOKEN` before the examples:

```bash
export TOKEN=the-printed-token
```

Create a project, an environment, and a flag:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"shop","name":"Shop"}'

curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/environments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"prod","name":"Prod"}'

curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/flags \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"checkout-v2","name":"Checkout","description":"","variations":[false,true]}'
```

Turn the flag on. `version` is the version returned when the flag was created, which is `1` for a new flag. Fallthrough variation `1` is `true`:

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/projects/shop/flags/checkout-v2/environments/prod \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"off_variation":0,"fallthrough":{"variation":1},"rules":[],"targets":{},"prerequisites":[],"version":1}'
```

Evaluate it. The flag exists, so `default` is ignored:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"key":"user-1","attributes":{}},"default":false}'
```

The decision is `value` `true`, `variation_index` `1`, `reason` `fallthrough`, and `version` `2`.

The same path, with the country rollout and an SDK key, is written out in [Example](docs/example.md).
