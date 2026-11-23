# 04 · Running the stack, and following one question through it

## What runs where

| Service | Image | Port on your Mac | Job |
|---|---|---|---|
| `openbao` | openbao/openbao:2.7.1 | 28200 (UI `/ui`) | secrets, identity tokens, dynamic DB users |
| `bao-bootstrap` | same | – (one-shot) | init, unseal, configure; re-runs on every `up` |
| `bao-agent-trino` / `bao-agent-cube` | same | – | log in with AppRole, render secrets into files |
| `trino` | trinodb/trino:483 | 28080 (UI `/ui`, user `admin`) | SQL engine over the sibling's Iceberg tables (read-only) |
| `cube` | cubejs/cube:v1.7.50 | 24000 REST, 25432 SQL API | the semantic layer |
| `cubestore` | cubejs/cubestore:v1.7.50-arm64v8 | – | stores pre-aggregations (rollups) |
| `audit-db` | postgres:18-alpine | 25433 | the agent's query audit log |
| *learn-ai-agent* | (sibling) | 8181, 9000, 8200 | Lakekeeper (Iceberg REST) + SeaweedFS (S3) + its own OpenBao |

The agent itself runs on your Mac (`uv run ...`), like an app would in its own pod.
Memory limits add up to about 3.5 GB, most of it Trino.

## Start and stop

```bash
scripts/up.sh      # starts learn-ai-agent's lakehouse too, if it isn't running; waits for healthy
scripts/down.sh    # stops this lab only; volumes are kept, OpenBao comes back sealed
```

First run only, before `up.sh` (README step 2): put `ANTHROPIC_API_KEY`,
`LAKE_S3_ACCESS_KEY` and `LAKE_S3_SECRET_KEY` in `.env`. Bootstrap moves them into OpenBao.

## Start-up order, and why

```
openbao ─healthy─▶ bao-bootstrap (init? unseal, policies, roles, secrets, identity, DB engine)
                       │ completed
          ┌────────────┴─────────────┐
   bao-agent-trino             bao-agent-cube          audit-db (needed by bootstrap)
   renders lake.properties     renders cube.env
          │ healthy                  │ healthy
        trino ──healthy───────────▶ cube ◀── cubestore
```

Compose's `depends_on: condition:` encodes it: nothing starts with a secret it doesn't have yet.

## Follow one question

`uv run python -m app.ask --persona agent-data "How much did my team's instances cost in July?"`

1. **Login.** `Runtime` reads `.secrets/approle/agent-data/{role_id,secret_id}` and logs in
   to OpenBao → a 1 h token with policy `sl-agent`.
2. **Secrets.** It reads `secret/anthropic` (the API key) and mints an identity JWT for
   entity `agent-data-team`: `team=data, groups=[sl-team-members]`.
3. **System prompt.** It calls Cube `/meta` with that JWT and renders the catalog into
   the system prompt (cached after the first call).
4. **Model turn.** Claude reads the catalog, maybe calls `dimension_values`, then
   `query_metrics({measures: [fleet.estimated_cost_usd], timeDimensions: [...July...]})`.
5. **Cube** verifies the JWT against OpenBao's JWKS, maps `groups` → access policy
   `sl-team-members`, adds `WHERE team = 'data'`, sees that the `daily_usage` rollup can
   answer, and serves it from Cube Store in milliseconds. (A count-distinct question would
   be compiled to Trino SQL instead.)
6. **Audit.** The agent asks OpenBao for `database/creds/audit-writer`, gets a fresh
   Postgres user, and inserts the question, the Cube query and the generated SQL.
7. **Answer.** Claude calls `submit_answer`. On exit the agent revokes its DB lease and
   OpenBao drops the Postgres user.

Try the same question with `--persona agent-admin` (all teams) and `--mode sql` (raw SQL,
where Trino's row filter does the restricting).

## Look inside

```bash
# OpenBao
export BAO_ADDR=http://localhost:28200 BAO_TOKEN=$(cat .secrets/root-token)
bao secrets list; bao auth list; bao policy read sl-agent
bao read identity/oidc/role/semantic-layer
curl -s $BAO_ADDR/v1/identity/oidc/.well-known/keys | jq '.keys[].kid'   # what Cube verifies with

# The audit trail
docker compose exec audit-db psql -U postgres -d audit -c \
  "select asked_at, persona, mode, tool, row_count, db_user, left(generated_sql, 60) from agent_queries order by id desc limit 10"

# Trino: what the agent's SQL did (the UI shows every query by user)
open http://localhost:28080/ui

# Cube: the SQL it generates for a query (needs a token)
TOKEN=$(BAO_TOKEN=$(bao write -field=token auth/approle/login \
   role_id=$(cat .secrets/approle/agent-admin/role_id) secret_id=$(cat .secrets/approle/agent-admin/secret_id)) \
   bao read -field=token identity/oidc/token/semantic-layer)
curl -s -H "Authorization: Bearer $TOKEN" 'http://localhost:24000/cubejs-api/v1/sql' -G \
  --data-urlencode 'query={"measures":["fleet.running_instances"],"segments":["fleet.latest_snapshot"]}' | jq -r .sql.sql[0]
```

## When something is off

| Symptom | Likely cause | Fix |
|---|---|---|
| bootstrap exits 1, "LAKE_S3_... missing" | `.env` has no S3 keys | README step 2 |
| Trino query fails with S3 403 | the lakehouse regenerated its keys | redo README step 2, then `docker compose up -d --force-recreate bao-bootstrap bao-agent-trino trino` |
| Cube 403 "Access denied" | token expired, wrong audience, or persona in no group | tokens last 15 min; the client re-mints them automatically |
| "No pre-aggregation partitions were built yet" | the rollup is still building after a start | wait a minute; check `docker compose logs cube` |
| Everything 503 from OpenBao | it restarted and is sealed | `docker compose up -d bao-bootstrap` |

Next: [05 · The Cube data model](05-cube-model.md)
