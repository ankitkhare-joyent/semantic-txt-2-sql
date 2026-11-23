# Lab 03 · Trino over Iceberg: what the engine reads, and who may read it

> **Changes things:** edits `stack/trino/etc/rules.json` for one minute (the break), then restores it with git.

One sitting, about 35 minutes. Background: [docs/02](../docs/02-text-to-sql.md#guardrails-the-model-is-an-untrusted-sql-author),
[docs/04](../docs/04-the-stack.md). Config: [stack/trino/etc/](../stack/trino/etc), the template
[lake.properties.ctmpl](../stack/openbao/agent-trino/lake.properties.ctmpl).

Both query paths end here: Cube compiles its queries into Trino SQL, and the raw-SQL agent sends its own. After this
lab you can explain:

- what Trino learns from the Iceberg catalog vs from the files
- why a time filter decides whether a query opens 1 file or 183
- the two layers that stop an agent from writing or reading too much: access rules and a read-only catalog

`tsql USER "SQL"` runs Trino's CLI inside its container as that user. Locally Trino believes whatever user name a
client sends; in production it authenticates the user (docs/08). The UI at http://localhost:28080/ui (user `admin`)
shows every query you run here.

## Before you start

```bash
source lab/env.sh
tsql admin "SELECT count(*) AS rows, min(snapshot_ts) AS first, max(snapshot_ts) AS last FROM lake.ec2.instances"
```

## 1. What each user can see

**Predict (P1):** which catalogs will `admin` and `agent-data-team` see?

```bash
tsql admin "SHOW CATALOGS"
tsql agent-data-team "SHOW CATALOGS"
tsql agent-data-team "SHOW SCHEMAS FROM lake"
```

The raw-SQL agent's whole picture of the data is this, column comments included (they come from the `doc=` on each
field of the Iceberg schema, written by the lakehouse's ETL):

```bash
tsql admin "SHOW COLUMNS FROM lake.ec2.instances"
```

## 2. The table's metadata, as tables

Iceberg exposes its metadata as hidden tables: `$snapshots` (one per commit), `$partitions`, `$files`.

**Predict (P2):** the data covers 183 days, partitioned by `day(snapshot_ts)`. How many data files, and roughly how
many MB of Parquet for 2 million rows?

```bash
tsql admin 'SELECT (SELECT count(*) FROM lake.ec2."instances$snapshots") AS snapshots,
                   (SELECT count(*) FROM lake.ec2."instances$partitions") AS partitions,
                   (SELECT count(*) FROM lake.ec2."instances$files") AS files,
                   (SELECT round(sum(file_size_in_bytes) / 1e6, 1) FROM lake.ec2."instances$files") AS mb'
```

## 3. What a query reads

`EXPLAIN ANALYZE` runs the query and reports, per operator, rows and bytes read and the number of splits (here, one
split per file).

**Predict (P3):** rank these four by bytes read: one snapshot, all of September (count only), the largest `vcpus`
in the table, and `count(*)` of everything.

```bash
x() { tsql admin "EXPLAIN ANALYZE $1" | grep -oE "(Input: [0-9.,]+[kKM]? rows \([0-9.]+[kKMG]?i?B\)|Physical input: [0-9.]+[kKMG]?i?B|Splits: [0-9]+)" | sort -u; echo; }
x "SELECT count(*) FROM lake.ec2.instances WHERE snapshot_ts = TIMESTAMP '2026-09-30 22:00:00 UTC'"
x "SELECT count(*) FROM lake.ec2.instances WHERE snapshot_ts >= TIMESTAMP '2026-09-01 00:00:00 UTC'"
x "SELECT max(vcpus) FROM lake.ec2.instances"
x "SELECT count(*) FROM lake.ec2.instances"
```

## 4. Row filters

**Predict (P4):** the data team's agent runs the same query as admin. What does it get, and what happens when it
asks for the payments team's rows explicitly?

```bash
Q="SELECT count(*) AS rows, count(DISTINCT team) AS teams, min(team) AS team FROM lake.ec2.instances"
tsql admin "$Q"
tsql agent-data-team "$Q"
tsql agent-data-team "SELECT count(*) FROM lake.ec2.instances WHERE team = 'payments'"
```

**Predict (P5):** which of these are refused, and by what?

```bash
tsql agent-data-team 'SELECT count(*) FROM lake.ec2."instances$files"'
tsql admin "DELETE FROM lake.ec2.instances WHERE false"
tsql mallory "SELECT 1 FROM lake.ec2.instances LIMIT 1"
tsql agent-data-team "SELECT * FROM system.runtime.queries LIMIT 1"
```

## Break it, then fix it

**Break: lose the row filter.** A refactor of `rules.json` drops the `filter` line. Trino re-reads the file every
30 seconds (`security.refresh-period`), no restart needed.

```bash
jq '(.tables[] | select(.user == "agent-data-team" and .table == "instances")) |= del(.filter)' \
  stack/trino/etc/rules.json > /tmp/rules.json && cp /tmp/rules.json stack/trino/etc/rules.json
sleep 35
tsql agent-data-team "SELECT count(*) AS rows, count(DISTINCT team) AS teams FROM lake.ec2.instances"
```

**Predict (P6):** what does the data team see now, and would anything have warned you?

**Fix:**

```bash
git checkout stack/trino/etc/rules.json && sleep 35
tsql agent-data-team "SELECT count(*) AS rows, count(DISTINCT team) AS teams FROM lake.ec2.instances"
```

## Check your predictions

**P1.** `admin`: `lake` and `system`. `agent-data-team`: only `lake` (with schemas `ec2` and `ref`). The image's
`tpch`, `tpcds`, `jmx` and `memory` catalogs exist (Lab 02, P2) but no rule grants them, so they're invisible.
8 of the 27 columns have comments (`snapshot_ts`, `state`, `platform`, `lifecycle` and the four tag columns); the rest don't. That's typical,
and it's all the context the raw-SQL agent gets.

**P2.** `27` snapshots, `183` partitions, `183` files, `18.4` MB. One file per day: 2 million rows of a narrow,
sorted, repetitive table compress to about 9 bytes a row in Parquet. The 27 snapshots are the commits the
lakehouse's ETL made while loading.

| query | rows read | bytes | splits (files) |
|---|---|---|---|
| one snapshot | 11,922 | 151 kB (108 kB physical) | 1 |
| September, `count(*)` | 340,586 | 0 B | 30 |
| `max(vcpus)` | 1,996,087 | 9.52 MB (17.5 MB physical) | 183 |
| `count(*)` of everything | 1,996,087 | 0 B | 183 |

**P3.** Counting reads **zero bytes** of data: Trino takes row counts from the Parquet footers and file stats, so
it opens files but reads no columns. The single snapshot reads one day's file (partition pruning on
`day(snapshot_ts)`), and inside it only the row groups whose min/max cover the timestamp. `max(vcpus)` has no
filter and must read the column in all 183 files. Lesson: in Iceberg the filter decides the *files*, the columns
decide the *bytes*.

**P4.** Admin: `1996087 | 8 | data`. The data team: `261873 | 1 | data`: Trino added `team = 'data'` to every read
of the table. Asking for payments returns `0`, not an error: a row filter makes other rows *not exist* for this user,
which is the right behaviour (no "you're not allowed to know that this exists" leak).

**P5.** All four, each with an `Access Denied` from the file-based access control, before the connector is involved:

```
Access Denied: Cannot select from table lake.ec2.instances$files      (no rule for the metadata table)
Access Denied: Cannot delete from table lake.ec2.instances            (admin has SELECT only)
Access Denied: Cannot access catalog lake                             (mallory matches no rule)
Access Denied: Cannot access catalog system
```

Denying `$files` matters: metadata tables have per-file statistics, including min/max values of other teams'
columns, which would leak around the row filter. And even if a rule granted DELETE, `iceberg.security=read_only`
in the catalog would refuse it: two layers.

**P6.** `1996087 | 8`: everything, silently. Nothing errors; the query just returns more. After the fix it's back to
`261873 | 1`. That's why access rules belong in code review, and why this repo's eval has a security case
(`rls_data_team_cannot_see_payments`, Lab 09) that would turn red. Test your permissions like you test features.

## What to remember

- Partition filters choose files; columns choose bytes; counts can come from metadata alone.
- Engine-level rules are the backstop for any SQL an agent writes: row filters, catalog allowlists, read-only catalogs.
- A security misconfiguration usually returns *more* data, not an error. Only a test notices.

Next: [Lab 04 · The Cube semantic layer](04-cube.md).
