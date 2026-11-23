# Hands-on lab: the stack, one component per sitting

The lessons in [docs/](../docs) explain the design. This track has you poke each running piece until you could debug
it yourself. It follows a question through the system: secrets first (nothing starts without them), then how they
reach the services, the engine and the data, the semantic layer, identity and row-level security, the guardrails
and tools, the agent itself, its audit trail, and finally the evals that measure it.

## Fresh start (do this once, before Lab 01)

```bash
cd ~/workstuff/learn-ai-agent && git pull                   # the lakehouse this lab reads (seed fix: 31a8573)
docker ps --format '{{.Names}}' | grep -v ec2-lakehouse     # should print nothing: one lab stack at a time
curl -s localhost:8000/readyz | jq -c '{ready, rows}'      # lakehouse up and seeded? if not:
                                                            #   (cd ../learn-ai-agent && mise run up && mise run seed)
cd ~/workstuff/semantic-txt-2-sql
mise run lake-keys      # copy the lakehouse's S3 keys into ./.env (again after every lakehouse reset)
mise run reset          # DELETES this lab's volumes, OpenBao (keys, tokens) and eval results. ./.env stays
mise run setup          # Python deps
mise run up             # OpenBao (init + unseal + config), Bao Agents, Trino, Cube, Cube Store, audit Postgres. ~1 min
source lab/env.sh       # once per terminal
```

`.env` needs `ANTHROPIC_API_KEY` too (Labs 07 and 09); `mise run up` moves it into OpenBao. When you finish a
sitting, `mise run down` stops the stack and keeps its data; OpenBao comes back sealed and `mise run up` unseals it.

Every lab has the same shape:

1. **Look**: real commands against your running stack.
2. **Predict** before you run: each step asks a question (P1, P2, …). Write your guess down first. The answers, with
   the output I got running every command on a freshly reset stack, are at the bottom of each file.
3. **Break it, then fix it**: at least one failure on purpose, so you've seen it before it happens for real.

A tmux split works well: the lab on one side, a shell on the other, a third pane for `docker compose logs -f <svc>`.

| Lab | Component | You'll be able to explain | Changes anything? |
|---|---|---|---|
| [01](01-openbao.md) | OpenBao | sealed vs unsealed, Shamir shares, what a token may do, the audit log | seals and unseals OpenBao |
| [02](02-secret-delivery.md) | Bao Agent | how S3 keys reach Trino without being in any config; a bad rotation and a rollback | writes 2 secret versions, restarts Trino |
| [03](03-trino.md) | Trino over Iceberg | files vs bytes read, partition pruning, row filters, two layers of "read-only" | edits `rules.json` for a minute |
| [04](04-cube.md) | Cube | the catalog, semi-additive vs additive measures, rollups, one rule changing everywhere | edits the model for a few minutes |
| [05](05-identity-rls.md) | Identity tokens and RLS | JWT claims, JWKS and key rotation, the same query for two callers, failing closed | rotates the signing key; edits a group |
| [06](06-guardrails-and-tools.md) | Guardrails and tools | what the model sees from each tool, sqlguard, errors written for a model | no |
| [07](07-agent.md) | The agent | one question through every hop, in three modes; prompt caching; a persona that can't see the answer | calls Claude (about 10 cents) |
| [08](08-dynamic-credentials.md) | Audit trail and dynamic DB users | a Postgres user that exists for an hour, leases, revocation, rotated root | creates and drops Postgres users |
| [09](09-evals-and-mcp.md) | Evals and the MCP server | golden SQL, grading, where each mode fails; the semantic layer in Claude Code | calls Claude (about 15 cents) |

Each lab is one sitting of 30-45 minutes. Do them in order: later labs reuse things you saw earlier. All together they
spend about 25 cents of Claude API credit.

## Helpers

| | What | Where |
|---|---|---|
| `source lab/env.sh` | `$BAO_ADDR`, `$BAO_TOKEN` (the root token, local only), `$CUBE`, `$TRINO`, and the functions below | [env.sh](env.sh) |
| `bao …` | the OpenBao CLI (mise installs it for this folder) | |
| `bao_keys PATH`, `bao_field PATH FIELD` | a KV secret's field names and version; one value, for `$( … )` only | |
| `persona_token NAME`, `persona_jwt NAME` | an AppRole login token; the identity JWT that persona sends to Cube | |
| `jwt_claims TOKEN` | a token's decoded payload | |
| `cube_load PERSONA 'QUERY'`, `cube_sql …`, `cube_meta …` | Cube's REST API as that persona | |
| `tsql USER "SQL"` | Trino's CLI inside its container, as that user | |
| `audit "SQL"`, `pg_as USER PASS "SQL"` | psql on the audit database, as the superuser or as a given login | |
| `mise run -q lab:tool -- TOOL 'JSON'` | call one agent tool by hand, without the model | [tool.py](tool.py) |
| `mise run ask -- [--mode …] [--persona …] "question"` | ask the agent | [app/ask.py](../app/ask.py) |
| `mise run -q lab:mcp` | talk to the MCP server like Claude Code would | [mcp_call.py](mcp_call.py) |

None of the labs print a secret value. The ones that need a key or password read it from OpenBao into a variable.

## If something is off

- `lab/env.sh: no .secrets/root-token`: run it from the repo root, after `mise run up`.
- Everything from OpenBao says `Vault is sealed`: Docker restarted. `mise run unseal`.
- Trino: `Error processing metadata for table ec2.instances`: the lakehouse was reset and its S3 keys changed.
  `mise run lake-keys`, then `mise run up`.
- Trino won't start, `network … not found`: the lakehouse's network was recreated. `mise run up` recreates Trino.
- Cube: `No pre-aggregation partitions were built yet`: the rollup is still building after a start; wait a minute.
- Your numbers differ: the data is deterministic, so row counts and costs should match exactly. IDs, timings, token
  counts and anything an LLM wrote will differ; the shape and the reasoning should match.

## The stack, with the lab that covers each part

```
   you (terminal): a question
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 07  agent  semantic | sql | hybrid        ◄──► Claude API (claude-opus-5-5)  │
   │ 06  tools: query_metrics · dimension_values · run_sql (+ sqlguard)           │
   │ 09  evals grade it; the MCP server exposes the semantic tools to Claude Code │
   │ 08  every tool call ──► audit-db (Postgres :25433), as a 1-hour DB user      │
   └──────────────────────────────────────────────────────────────────────────────┘
        │ semantic path: JSON query + OpenBao-signed JWT     raw SQL path: SQL as the persona
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 04  Cube :24000   view fleet · measures with the business rules · rollups    │
   │ 05  verifies the JWT (JWKS) → access policy → team row filter                │
   │     Cube Store: daily_usage rollup (additive measures, by day)               │
   └──────────────────────────────────────────────────────────────────────────────┘
        │ compiled SQL as user "cube" (raw SQL path joins here)
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 03  Trino :28080   read-only Iceberg catalog · per-user row filters          │
   └──────────────────────────────────────────────────────────────────────────────┘
        │ Iceberg REST (metadata) + S3 reads (data), with keys from a rendered file
        ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ learn-ai-agent lakehouse (read-only): Lakekeeper :8181 · SeaweedFS S3 :9000  │
   │ ec2.instances: 1,996,087 rows in 183 daily Parquet files                     │
   └──────────────────────────────────────────────────────────────────────────────┘
   
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 01  OpenBao :28200   Shamir 3/5 · AppRole · policies · KV · audit log        │
   │ 02  Bao Agents render lake.properties (Trino) and cube.env (Cube)            │
   │ 05  identity tokens + JWKS (Cube)     08  database engine (audit-db users)   │
   │     feeds every box above; nothing else holds a long-lived secret            │
   └──────────────────────────────────────────────────────────────────────────────┘
```

The full architecture diagram, with every flow numbered, is in [docs/09](../docs/09-architecture.md).
