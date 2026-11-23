# Lab 06 · Guardrails and tools, without the model

One sitting, about 30 minutes. Background: [docs/02](../docs/02-text-to-sql.md). Code: [app/tools.py](../app/tools.py),
[app/sqlguard.py](../app/sqlguard.py), [app/semantic.py](../app/semantic.py) (`validate`),
[app/rawsql.py](../app/rawsql.py), and the helper [lab/tool.py](tool.py).

Before you hand the tools to a model, use them yourself. `mise run -q lab:tool -- TOOL 'JSON'` calls one tool exactly
the way the agent's loop does (same persona login, same Cube and Trino clients, same result formatting) and prints
what the model would see. No Claude call, nothing written to the audit table. After this lab you can explain:

- what each tool returns to the model, and why errors are returned as results instead of raised
- what `sqlguard` rejects, what it lets through, and why it isn't the security boundary
- why a row limit alone doesn't protect the model's context window

## Before you start

```bash
source lab/env.sh
vim app/tools.py          # the four tool schemas at the top are exactly what the model is given
```

## 1. The same question through both tools

**Predict (P1):** running instances per region, right now, once as SQL and once as a semantic query. Same numbers?
Which one needed you to know that "now" means `max(snapshot_ts)`?

```bash
mise run -q lab:tool -- run_sql '{"sql": "SELECT region, count(DISTINCT instance_id) AS running FROM lake.ec2.instances
  WHERE state = '"'"'running'"'"' AND snapshot_ts = (SELECT max(snapshot_ts) FROM lake.ec2.instances) GROUP BY 1 ORDER BY 2 DESC"}'
mise run -q lab:tool -- query_metrics '{"measures": ["fleet.running_instances"], "dimensions": ["fleet.region"],
  "segments": ["fleet.latest_snapshot"]}'
```

Look at the `audit` part of each: that's what Lab 08 finds in the audit table.

## 2. Errors written for a model

**Predict (P2):** one typo in each tool. What does the model get back, and which of the two helps it fix the call?

```bash
mise run -q lab:tool -- run_sql '{"sql": "SELECT instance_typ FROM lake.ec2.instances"}' | jq '{is_error, model_sees}'
mise run -q lab:tool -- query_metrics '{"measures": ["fleet.cost"]}' | jq '{is_error, model_sees}'
```

## 3. Value grounding

Users say "the checkout service"; the tag says something else.

**Predict (P3):** what will `dimension_values` find, and why might the first call take much longer than the second?

```bash
time (mise run -q lab:tool -- dimension_values '{"dimension": "fleet.service", "contains": "checkout"}' | jq -c .model_sees)
time (mise run -q lab:tool -- dimension_values '{"dimension": "fleet.service", "contains": "checkout"}' | jq -c .model_sees)
```

## 4. What sqlguard decides

`--guard` runs only the parser checks and prints what would be sent to Trino, or the message the model would get.

**Predict (P4):** which of these does it allow? Think about each before running the loop.

```bash
for q in "SELECT region, count(*) FROM ec2.instances GROUP BY 1" \
         "SELECT * FROM instances LIMIT 5000" \
         "DELETE FROM lake.ec2.instances" \
         "SELECT 1; DROP TABLE lake.ec2.instances" \
         "SELECT * FROM system.runtime.queries" \
         "WITH t AS (DELETE FROM lake.ec2.instances) SELECT 1" \
         'SELECT * FROM "lake"."EC2"."Instances"' \
         "SELECT * FROM lake.ec2.instances FOR VERSION AS OF 123"; do
  echo "== $q"; mise run -q lab:tool -- --guard "$q" | jq -c .
done
```

**Predict (P5):** run the data team's agent with SQL that tries to see every team. `sqlguard` allows it (it's one
read query on an allowed table). What comes back?

```bash
mise run -q lab:tool -- --persona agent-data run_sql '{"sql": "SELECT team, count(*) FROM lake.ec2.instances GROUP BY 1"}' \
  | jq -c '{acting_as, model_sees}'
```

## Break it, then fix it

**Break: flood the context.** A model exploring the data loves `SELECT *`.

```bash
mise run -q lab:tool -- run_sql '{"sql": "SELECT * FROM lake.ec2.instances"}' \
  | jq -c '{row_count: .model_sees.row_count, truncated: .model_sees.truncated, chars: (.model_sees | tostring | length),
            ran: .audit.generated_sql}'
```

**Predict (P6):** the 200-row cap held. Roughly how many tokens is that one tool result, and what does it do to the
next model call?

**Fix:** two levers. Lower the cap (it's `SL_MAX_ROWS` in [config.py](../app/config.py)), and ask for columns, not
`*`. Compare:

```bash
SL_MAX_ROWS=20 mise run -q lab:tool -- run_sql '{"sql": "SELECT * FROM lake.ec2.instances"}' \
  | jq -c '{row_count: .model_sees.row_count, chars: (.model_sees | tostring | length)}'
mise run -q lab:tool -- run_sql '{"sql": "SELECT instance_id, state, team FROM lake.ec2.instances
  WHERE snapshot_ts = (SELECT max(snapshot_ts) FROM lake.ec2.instances) LIMIT 20"}' \
  | jq -c '{row_count: .model_sees.row_count, chars: (.model_sees | tostring | length)}'
```

A production version caps bytes as well as rows, and tells the model in the result that it was truncated (this one
sets `truncated: true`, and the `run_sql` description says results are capped).

## Check your predictions

**P1.** Same numbers both ways: us-east-1 472, eu-central-1 179, ap-northeast-1 112, ap-southeast-1 82 (845 in
total, as in Labs 04 and 05). For `run_sql` you wrote the "now" rule yourself; `sqlguard` added `LIMIT 200` to what
ran. For `query_metrics` the rule lives in the `latest_snapshot` segment, and the audit shows the SQL Cube compiled:
`COUNT(DISTINCT CASE WHEN (… state = 'running') THEN … instance_id END)` with the `max(snapshot_ts)` subquery.
`pre_aggregations: []`: a distinct count can't come from the rollup (Lab 04, P3).

**P2.** Both come back with `is_error: true` instead of crashing the loop. Trino says
`TrinoUserError(… COLUMN_NOT_FOUND, message="line 1:8: Column 'instance_typ' cannot be resolved" …)`: correct, but
the model has to guess the right name. The semantic layer says
`Unknown measure 'fleet.cost'. Did you mean: fleet.instances, fleet.spot_share, fleet.estimated_cost_usd?`:
`CubeClient.validate` checked the name against the catalog before calling Cube and offered close matches. One more
step and the model is back on track. Write tool errors for the reader you have.

**P3.** `["checkout-api"]`. On a freshly started stack the first call took about 34 s and the second about 1 s.
Listing a dimension's values has no time filter, so Cube's SQL scans the `service` column in all 183 files, and
right after a start Trino is also busy with Cube's rollup builds. The second answer comes from Cube's result cache.
A production version would serve value lookups from a small dimension table or a rollup.

**P4.**

| SQL | Verdict |
|---|---|
| `SELECT region, count(*) FROM ec2.instances GROUP BY 1` | allowed, becomes `… GROUP BY 1 LIMIT 200` (`ec2.instances` resolves to `lake.ec2.instances`) |
| `SELECT * FROM instances LIMIT 5000` | allowed, the limit is lowered to `LIMIT 200` |
| `DELETE FROM lake.ec2.instances` | refused: `Only read queries (SELECT / WITH ... SELECT) are allowed.` |
| `SELECT 1; DROP TABLE …` | refused: `Send exactly one statement (got 2).` |
| `SELECT * FROM system.runtime.queries` | refused: `Table system.runtime.queries is not available. You can read: lake.ec2.instances, lake.ref.instance_types.` |
| `WITH t AS (DELETE …) SELECT 1` | refused: `DELETE is not allowed; this tool only reads.` (it walks the whole tree, CTEs included) |
| `SELECT * FROM "lake"."EC2"."Instances"` | allowed: Trino identifiers are case-insensitive, so the guard lowercases before comparing |
| `… FOR VERSION AS OF 123` | allowed: Iceberg time travel is still a read. Whether agents should read old snapshots is a policy choice the guard doesn't make |

**P5.** `{"acting_as": "agent-data-team", "model_sees": {"columns": ["team", "_col1"], "rows": [["data", 261873]] …}}`.
The guard has no idea who's asking; Trino's row filter (Lab 03) does. That's the division of labour: the guard
catches mistakes early with a helpful message, the engine enforces the rules.

**P6.** `row_count: 200`, `truncated: true`, about 110,000 characters: roughly 30,000 tokens in one tool result
(27 columns, including the `tags` map, times 200 rows). It's re-sent on every later turn of the loop, so it costs
again each step, pushes the system prompt's catalog further from the question, and can crowd out the context window.
With `SL_MAX_ROWS=20` it's about 11,400 characters; selecting three columns for 20 rows is under 1,000.

## What to remember

- Tool results are the model's only view of the data. Make them small, and make errors say how to fix the call.
- Validate names against the catalog before you call the backend; suggest near matches.
- The SQL guard is a filter for mistakes, the engine is the boundary for permissions.
- Cap rows *and* size. One `SELECT *` can cost more tokens than the whole rest of the conversation.

Next: [Lab 07 · The agent](07-agent.md).
