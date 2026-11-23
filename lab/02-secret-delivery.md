# Lab 02 · Secret delivery: Bao Agent, rendered files, rotation

> **Changes things:** writes two new versions of `secret/lakehouse/s3` (a wrong key, then a rollback) and restarts
> `bao-agent-trino` and `trino`.

One sitting, about 30 minutes. Background: [docs/03 §1](../docs/03-openbao.md#1-static-secrets-in-kv-v2-rendered-by-bao-agent).
Code: [stack/openbao/agent-trino/](../stack/openbao/agent-trino), [agent-cube/](../stack/openbao/agent-cube),
the `bao-agent-*`, `trino` and `cube` services in [compose.yaml](../compose.yaml).

Trino and Cube never talk to OpenBao. A Bao Agent sidecar logs in for each of them, renders a template into a
shared volume, and keeps its token renewed. After this lab you can explain:

- where a secret is (and isn't) once a container is running
- why changing a secret in OpenBao doesn't change what Trino uses until something restarts
- what a wrong storage key looks like from the engine's side, and how to roll a secret back

## Before you start

```bash
source lab/env.sh
docker compose ps --format '{{.Service}}\t{{.Status}}'      # bao-agent-trino and bao-agent-cube: Up (healthy)
```

## 1. What Trino is given

**Predict (P1):** Trino reads S3 with an access key. Will you find it in Trino's compose environment?

```bash
docker compose config --format json trino | jq -c '.services.trino | {environment,
  volumes: [.volumes[] | "\(.source | split("/") | last) -> \(.target)"]}'
docker inspect semantic-lab-trino-1 --format '{{json .Config.Env}}' | jq -r '.[]' | cut -d= -f1
```

The keys arrive as a file. Look at it from both sides of the shared volume (key names only):

```bash
docker compose exec -T bao-agent-trino ls -l /rendered
docker compose exec -T trino sh -c 'ls -l /etc/trino/catalog; cut -d= -f1 /etc/trino/catalog/lake.properties | grep -v "^#"'
vim stack/openbao/agent-trino/lake.properties.ctmpl     # the template the agent filled in
```

**Predict (P2):** the catalog folder has more files than the agent's template. Where do `tpch.properties` and the
others come from, and can the agents query them? (Hint: `stack/trino/etc/rules.json`.)

## 2. How the agent got there

```bash
vim stack/openbao/agent-trino/agent.hcl
docker compose logs --no-log-prefix bao-agent-trino | grep -E 'authentication successful|rendered|renewed' | head -4
```

**Predict (P3):** Cube gets its secrets as environment variables instead (`cube.env`, sourced by Cube's entrypoint).
If you `exec` into Cube and run `env`, will you see `CUBEJS_SQL_PASSWORD`?

```bash
docker compose exec -T cube sh -c 'env | cut -d= -f1 | grep -E "JWT|SQL_PASS|API_SECRET"'
docker compose exec -T cube sh -c "tr '\0' '\n' < /proc/1/environ | cut -d= -f1 | grep -E 'JWT|SQL_PASS|API_SECRET'"
docker compose exec -T bao-agent-cube sh -c "sed 's/=.*/=…/' /rendered/cube.env"
```

## 3. Versions

KV v2 keeps every version of a secret:

```bash
bao kv metadata get -format=json secret/lakehouse/s3 | jq -c '.data | {current_version, versions: (.versions | keys)}'
```

## Break it, then fix it

**Break: rotate the key to a wrong value.** This is what a bad rotation looks like: someone writes a new key to
OpenBao before (or instead of) changing it in the object store.

```bash
bao kv patch -format=json secret/lakehouse/s3 secret_key=not-the-real-key | jq -c '.data | {version}'
docker compose restart bao-agent-trino          # re-render now instead of waiting for the 5-minute refresh
docker compose exec -T trino stat -c '%y' /etc/trino/catalog/lake.properties
```

**Predict (P4):** the file now holds the wrong key. Does this query still work?

```bash
tsql admin "SELECT count(*) FROM lake.ec2.instances"
```

Now restart Trino, the way a deploy or a crash would:

```bash
docker compose restart trino
until curl -s $TRINO/v1/info | grep -q '"starting":false'; do sleep 3; done
tsql admin "SELECT count(*) FROM lake.ec2.instances"
```

**Predict (P5):** the error says *metadata*. Is the problem the Iceberg catalog (Lakekeeper) or the object store?
Get the full cause chain:

```bash
docker compose exec -T trino trino --user admin --debug --execute "SELECT count(*) FROM lake.ec2.instances" 2>&1 \
  | grep -E '^Caused by' | cut -c1-160
```

**Fix: roll back.** `rollback` writes the old data as a *new* version; history is never rewritten.

```bash
bao kv rollback -format=json -version=1 secret/lakehouse/s3 | jq -c '.data | {version}'
bao kv metadata get -format=json secret/lakehouse/s3 | jq -c '.data | {current_version,
  versions: [.versions | to_entries[] | {v: .key, created: .value.created_time[11:19]}]}'
docker compose restart bao-agent-trino && sleep 3 && docker compose restart trino
until curl -s $TRINO/v1/info | grep -q '"starting":false'; do sleep 3; done
tsql admin "SELECT count(*) FROM lake.ec2.instances"
```

## Check your predictions

**P1.** No. `environment` is `null`, and the image's env is just `PATH`, `JAVA_HOME`, `LANG`, `CATALOG_MANAGEMENT`.
The volumes are config files plus `trino-catalog -> /etc/trino/catalog`, the volume the agent writes into.

**P2.** `lake.properties` (550 bytes, `root:trino`, mode `-rw-r-----`, written by the agent running as uid 0 / gid
1000) sits next to `jmx`, `memory`, `tpcds` and `tpch` properties owned by `trino`. Docker seeded the empty named
volume with the image's own `/etc/trino/catalog` the first time a container mounted it there. The agents can't use
them: `rules.json` gives agent users only the `lake` catalog (`"catalog": ".*", "allow": "none"` catches the rest).
The key names are the eleven in the template: connector, catalog type, REST URI, warehouse, `iceberg.security`,
the S3 endpoint settings, `s3.aws-access-key` and `s3.aws-secret-key`.

**P3.** Not in `exec`'s environment (only the non-secret `CUBEJS_JWT_ALGS`), but PID 1 has `CUBEJS_JWT_ISSUER`,
`CUBEJS_JWT_AUDIENCE`, `CUBEJS_SQL_PASSWORD` and `CUBEJS_API_SECRET`: the entrypoint `sh -c '. /secrets/cube.env &&
exec … cubejs server'` sourced the file, then `exec` replaced the shell with Cube, keeping the environment. The
agent logged `authentication successful`, `renewed auth token`, then `rendered "/templates/…" => "/rendered/…"`.
`metadata get` shows `current_version: 1` on a fresh stack (each run of the bootstrap writes one more version).

**P4.** Yes, `1996087`. Trino read the catalog file at start-up and keeps the old key in memory; the new file is
ignored until the process restarts. That cuts both ways: a bad rotation doesn't hurt until the next restart, which
is exactly when you're least expecting it.

**P5.** The object store. After the restart: `Query … failed: io.trino.spi.TrinoException: Error processing
metadata for table ec2.instances`. The cause chain ends:

```
Caused by: java.io.UncheckedIOException: Failed to open input stream for file: s3://lakehouse/warehouse/…/metadata/snap-…avro
Caused by: java.io.IOException: S3 HEAD request failed for file: s3://lakehouse/warehouse/…/metadata/snap-…avro
Caused by: software.amazon.awssdk.services.s3.model.S3Exception: Forbidden (Service: S3, Status Code: 403, …)
```

Trino loaded the table from Lakekeeper fine (that's a REST call, no S3 key), then failed reading the snapshot's
manifest list (an Avro file *in S3*) with a 403. "Metadata" in Iceberg means files in object storage too. The fix
writes version 3, a copy of version 1; after the restarts the count is `1996087` again.

Two follow-ups worth knowing. `mise run up` re-runs the bootstrap, which writes the keys from `.env` again, so it
would also have repaired this. And in production you avoid the whole class of failure with credentials the catalog
*vends* per table load, so no service holds a long-lived storage key ([docs/03](../docs/03-openbao.md#secrets-across-teams)).

## What to remember

- Bao Agent turns "app can't talk to OpenBao" into "app reads a file": no secret in compose, images or `inspect`.
- A rendered secret takes effect when the consumer reloads it. Know which of your services reload, and when.
- Rollback is a new version. KV history shows who changed a secret and when.

Next: [Lab 03 · Trino over Iceberg](03-trino.md).
