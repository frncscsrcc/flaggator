# Cache

The cache stores bytes under a string key, with a time to live. The repository layer uses it for evaluation reads. Controllers, domain services, and the evaluator do not call it. SQL functions do not call it either. A cache-aside wrapper beside those functions does.

This splits [A8](decisions.md) into a protocol and a policy. [P12](decisions.md) is that policy.

## Protocol

```text
get(key) -> bytes | None
set(key, value, ttl_seconds)
delete(key)
```

`get` returns `None` when the key is missing or the TTL has elapsed. `delete` of a missing key does nothing. `ttl_seconds` is a positive integer on every `set`. Values are `bytes`, so a later Redis or Memcached client stores the same payload as the in-memory map. The wrapper encodes and decodes dataclasses. The cache does not know what a flag is.

| Implementation | When |
| --- | --- |
| `MemoryCache` | Default. One process, one map. Lost when the process exits. |
| `NullCache` | `get` is always `None`. `set` and `delete` do nothing. Tests of SQL, and any deploy that wants every read to hit the database. |
| Redis, Memcached | Later. Same three operations. A client library is then an optional extra, imported only by that implementation. |

`MemoryCache` uses a lock, so a threaded HTTP adapter can share it. It stores each value with a monotonic deadline. `get` drops an expired entry. The map has a maximum number of entries (default 10000). When a `set` would exceed it, expired entries are dropped first, then the entry with the soonest deadline.

`src/composition.py` builds one cache for the process and passes it to the repository wrapper. There is no container and no global lookup. Configuration selects `memory` or `null`.

## What is cached

One entry per environment, the evaluation bundle: variation lists and targeting for every flag in that environment. It is the document `GET .../evaluation-config` already returns.

```text
eval:{project_key}:{environment_key}
```

`evaluate` and `evaluate-all` read through this entry. A miss loads from SQL, encodes the bundle, and `set`s it. The TTL is a backstop (default 5 seconds, configuration). The usual way an entry disappears is `delete` after a write commits.

These reads do not use the cache:

- Management reads and lists. An operator who just saved targeting reads the database.
- Audit.
- Credential lookup. A revoked SDK key stops working on the next request. A TTL in front of the credential table would keep a revoked key alive.

Payloads larger than 1 MiB are not stored. The read still returns the SQL result.

## Invalidation

The SQL function writes. The wrapper registers the deletes on the connection. The connection runs them after a successful commit and drops them on rollback. See `on_commit` in [Persistence](persistence.md).

A targeting replace deletes `eval:{project}:{environment}`. A change to variations deletes that key for every environment of the project, because every bundle embeds the variation list. The wrapper knows those environment keys from the write it just performed.

Deleting before commit is unsafe: another request can read the old row and `set` it again while the writer is still open. Deleting after commit closes that window except for one case. A reader that has already loaded the old row can `set` it after the writer's `delete`. The entry is then stale until the TTL, or until a later write deletes it. The TTL is the bound on that hole. Five seconds is the default so a kill switch converges quickly. It is configuration because a local SQLite deploy may set it lower, and a remote MySQL deploy may set it higher.

## Modules

```text
src/cache/protocol.py       get, set, delete
src/cache/memory.py         MemoryCache
src/cache/null.py           NullCache
src/repositories/eval_cache.py
```

`eval_cache.py` calls the SQL functions and the cache. Services call `eval_cache` on the evaluation path and call the SQL functions on the management path. Services do not import `src/cache`.
