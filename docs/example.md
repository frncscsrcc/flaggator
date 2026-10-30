# Example: roll out a new checkout

A shop wants a new checkout page for 10% of customers in Italy and France. One tester, Alice, always sees it. Everyone else keeps the old page. This walkthrough does that with the local process, from the management token to the call an application makes.

The commands assume the virtual environment is already active and the shell is at the repository root. The process listens on `127.0.0.1:8080`.

## 1. Create the management token

```bash
python -m main bootstrap
```

The process creates `flaggator.db` if it is missing, applies migrations, and prints one token on stdout. A line on stderr says to save it. That line does not contain the token. Copy the stdout value. It is shown once.

```bash
export TOKEN=the-printed-token
```

This token is an operator credential. It can create projects, flags, targeting, and SDK tokens. It is not the credential the application should ship with.

## 2. Start the process

In another terminal, from the same directory and with the same virtual environment:

```bash
python -m main
```

Leave it running. It applies migrations again (a second run changes nothing) and serves HTTP. A quick check, which does not need a token:

```bash
curl -s http://127.0.0.1:8080/health
```

The body is `{"status": "ok"}`. Health does not check the database.

## 3. Create the project and the production environment

```bash
curl -s -X POST http://127.0.0.1:8080/v1/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"shop","name":"Shop"}'

curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/environments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"prod","name":"Production"}'
```

Each response is `201` with the key and the name. The environment response does not contain a secret. SDK tokens are a separate step, so listing environments cannot leak one.

## 4. Create the flag, still off

Variations are an ordered list. Index `0` is `false` (the old checkout). Index `1` is `true` (the new one).

```bash
curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/flags \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"checkout-v2","name":"Checkout v2","description":"New checkout page","variations":[false,true]}'
```

The `201` body includes one environment entry for `prod`. A new flag starts disabled there: `enabled` is `false`, `off_variation` is `0`, and `version` is `1`. Creating the flag does not turn the feature on.

Confirm that with the operator token. `default` is required and is used only when the flag does not exist, so here it is ignored:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"subject":"alice","attributes":{"country":"US"}},"default":false}'
```

```json
{
  "flag_key": "checkout-v2",
  "value": false,
  "variation_index": 0,
  "reason": "off",
  "rule_id": null,
  "version": 1
}
```

`reason` `off` means the flag is disabled, so the off variation wins before targets and rules. Alice is not in the new checkout yet.

## 5. Turn on the rollout

Targeting is one document per environment. The `version` in the body is the version just read, `1`. The service stores `2` and rejects a later write that still says `1`.

The rule has no `id`. The service assigns one and returns it. The salt is `s1` on purpose: it is the salt in the [worked example](domain.md#worked-example), so the same person always lands in the same bucket. Omitting `salt` would assign a random one and reshuffle who is in the 10%.

Weights sum to `100000`. The first share is variation `1` with weight `10000` (10%). The rest is variation `0`.

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/projects/shop/flags/checkout-v2/environments/prod \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "off_variation": 0,
    "fallthrough": {"variation": 0},
    "rules": [
      {
        "clauses": [
          {"attribute": "country", "operator": "in", "values": ["IT", "FR"]}
        ],
        "serve": {
          "rollout": {
            "salt": "s1",
            "variations": [
              {"variation_index": 1, "weight": 10000},
              {"variation_index": 0, "weight": 90000}
            ]
          }
        }
      }
    ],
    "targets": {"1": ["alice"]},
    "prerequisites": [],
    "version": 1
  }'
```

The `200` body is the stored document. `version` is `2`. `rules[0].id` is the id to expect as `rule_id` below. `targets` uses the variation index as a decimal string: `"1"` means the `true` variation. Alice is listed there, so she is served that variation even when her country would not match the rule.

## 6. See who gets the new checkout

Same request shape as before. Only the context changes.

Alice is targeted. Country is irrelevant:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"subject":"alice","attributes":{"country":"US"}},"default":false}'
```

`value` is `true`, `variation_index` is `1`, `reason` is `target_match`, `rule_id` is `null`, `version` is `2`.

`user-11` lives in Italy, so the rule matches. The bucket of `checkout-v2:s1:user-11` is `3913`, which falls inside the first `10000` of weight, so this customer is in the 10%:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"subject":"user-11","attributes":{"country":"IT"}},"default":false}'
```

`value` is `true`, `variation_index` is `1`, `reason` is `rule_match`, and `rule_id` is the id from the targeting response. Calling again with the same subject and salt returns the same variation.

Bob is also in Italy, but the bucket of `checkout-v2:s1:bob` is `27461`, past the first share, so he stays on the old checkout:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"subject":"bob","attributes":{"country":"IT"}},"default":false}'
```

`value` is `false`, `variation_index` is `0`, `reason` is `rule_match`.

Bob in the US does not match the country rule, and he is not targeted. Fallthrough is variation `0`:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"subject":"bob","attributes":{"country":"US"}},"default":false}'
```

`value` is `false`, `reason` is `fallthrough`, `rule_id` is `null`.

A context with a country but no subject does not enter the rollout. A rollout needs a subject. Omit `subject`; an empty string is rejected.

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"attributes":{"country":"IT"}},"default":false}'
```

`value` is `false`, `reason` is `fallthrough`.

## 7. Give the application its own token

The shop backend should not hold the management token. Create an SDK token bound to `prod`. The body is an empty object. `secret` is returned once.

```bash
curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/environments/prod/sdk-tokens \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"checkout-api"}'
```

The response looks like this (values differ every time):

```json
{
  "id": 2,
  "name": "checkout-api",
  "prefix": "hzyQaoQw",
  "created_at": "2026-10-29T17:51:11Z",
  "secret": "hzyQaoQw...."
}
```

`name` is the label you chose so the list stays readable. `id` is a decimal number and the only value the revoke path accepts. `prefix` is the first eight characters of `secret`; it is a label for logs, not the path id. Putting the prefix in the path returns `404` and leaves the token active.

```bash
export SDK_TOKEN='paste-the-secret-field-here'
export SDK_TOKEN_ID=2
```

Replace `2` with the `id` from your create response. List active tokens with `GET /v1/projects/shop/environments/prod/sdk-tokens` if you lost the number; that list shows `id`, `name`, and `prefix`, never `secret`.

The application sends the SDK token and omits `project_key` and `environment_key`. The token already names `shop` / `prod`:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $SDK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"flag_key":"checkout-v2","context":{"subject":"user-11","attributes":{"country":"IT"}},"default":false}'
```

The decision matches the operator call for `user-11`: `true`, `rule_match`. If the body names a different project or environment, including one that does not exist, the response is `403`.

A page that needs every flag for one customer uses one call. Decisions are ordered by flag key. There is no default: only flags that exist are returned.

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate-all \
  -H "Authorization: Bearer $SDK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"context":{"subject":"user-11","attributes":{"country":"IT"}}}'
```

The body is `{ "decisions": [ ... ] }`. With only `checkout-v2` in the project, that is the one decision.

## 8. Retire the application token

When the secret is no longer wanted, revoke it with the management token. The path must end with the numeric `id` from create (for example `.../sdk-tokens/2`). A path that ends with the prefix returns `404` and does not revoke.

```bash
curl -s -w "\n%{http_code}\n" -X DELETE \
  http://127.0.0.1:8080/v1/projects/shop/environments/prod/sdk-tokens/$SDK_TOKEN_ID \
  -H "Authorization: Bearer $TOKEN"
```

That must print `{}` and then `204`. If it prints `404`, `$SDK_TOKEN_ID` was not the numeric id and the secret still works. The next evaluation with that secret is `401`, the same response as an unknown token:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $SDK_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"flag_key":"checkout-v2","context":{"subject":"user-11","attributes":{"country":"IT"}},"default":false}'
```

That prints `401`.

## 9. Ask for a flag that was never created

The application can still ask. A missing flag is `200`, not `404`, and `value` is the caller's `default`:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"not-a-flag","context":{"subject":"user-11","attributes":{}},"default":false}'
```

`value` is `false`, `variation_index` is `null`, `reason` is `flag_not_found`, `version` is `null`. Management routes stay strict: a missing project or flag on `GET` is `404`.
