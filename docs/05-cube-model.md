# 05 · The Cube data model, file by file

```
stack/cube/
├── cube.py                          maps the JWT claims to access-policy groups
└── model/
    ├── cubes/instance_snapshots.yml the fact: grain, measures, segment, rollup
    ├── cubes/instance_prices.yml    a lookup that exists nowhere else (price book)
    └── views/fleet.yml              the public contract + row-level security
```

## Cubes are private, views are the API

Both cubes say `public: false`. Consumers only see the **view** `fleet`, which picks the
members to expose and hides plumbing (`row_key`, `snapshots`). This is the same idea as
exposing a curated schema of marts in Superset and keeping staging tables out of sight:
you can refactor cubes (split them, change the SQL behind them) without breaking anyone,
as long as the view's names stay stable.

## Joins and the fan-out problem

```yaml
joins:
  - name: instance_prices
    relationship: many_to_one
    sql: "{CUBE}.instance_type = {instance_prices.instance_type}"
```

Declaring the cardinality is what lets Cube stay correct when a query mixes measures from
both sides of a join. If you `sum()` over a one-to-many join in raw SQL, rows multiply
and the sum is inflated ("fan-out"). Cube knows the primary keys (`row_key`,
`instance_type`) and the relationship, and rewrites such queries with de-duplicating
subqueries automatically. Raw SQL authors, human or model, must remember to do it themselves.

## Measures carry the rules

Look at how `estimated_cost_usd` is built:

```yaml
- name: estimated_cost_usd
  type: sum
  sql: >
    CASE WHEN {CUBE}.state = 'running'
         THEN 2 * {instance_prices.on_demand_usd_per_hour}
                * CASE WHEN {CUBE}.lifecycle = 'spot' THEN 0.35 ELSE 1.0 END
         ELSE 0 END
```

Four business rules in one place: only running time costs money, a row is 2 hours, the
price comes from the price book, spot is 35 % of on-demand. Change the discount here and
every dashboard, notebook and agent changes with it. The `description` is not decoration:
it's what the model reads to choose a measure. Write descriptions for a smart newcomer.

## Segments

`latest_snapshot` is a named filter. "How many are running now?" becomes
`running_instances` + `latest_snapshot`, which is correct by construction. Segments are a
cheap way to turn tribal knowledge ("now means the max snapshot") into a vocabulary.

## Access policies (row- and member-level security)

[fleet.yml](../stack/cube/model/views/fleet.yml):

```yaml
access_policy:
  - group: sl-admins          # sees everything
    member_level: { includes: "*" }
  - group: sl-team-members    # sees only rows of its own team
    member_level: { includes: "*" }
    row_level:
      filters:
        - member: team
          operator: equals
          values: ["{ securityContext.team }"]
```

`securityContext` is the verified JWT payload; `cube.py`'s `context_to_groups` turns its
`groups` claim into policy groups. Anyone in neither group gets nothing: policies deny by
default. `member_level` can also hide columns, e.g. exclude `estimated_cost_usd` from a
group that shouldn't see money.

## Pre-aggregations

`daily_usage` is a rollup of the additive measures by day × region × team × service ×
lifecycle × type, partitioned by month and stored in Cube Store. The query planner
**matches queries to rollups automatically**: if every requested member is in the rollup
and the granularity is day or coarser, Cube Store answers in milliseconds; otherwise the
query goes to Trino. The agent's tool result says `served_from_pre_aggregation: true` when
that happens.

Two rules to remember:
- Only **additive** measures (sum, count, min, max) can be re-aggregated from a rollup.
  Count-distinct needs `count_distinct_approx` (HLL) to be rollup-able, at a small error.
- The `refresh_key` decides staleness. Ours runs `SELECT max(snapshot_ts)` every 10 minutes
  and rebuilds a partition only when that changes, i.e. when the lakehouse loads new data.

## The SQL API: the same model for Superset

Cube also speaks the Postgres wire protocol on port 25432. Add it to Superset as a
PostgreSQL database (user `bi`, password from `bao kv get -field=sql_password secret/cube/config`)
and you can chart `fleet` with the same definitions the agent uses:

```sql
SELECT team, MEASURE(estimated_cost_usd) FROM fleet
WHERE snapshot_ts >= '2026-07-01' AND snapshot_ts < '2026-08-01' GROUP BY 1;
```

One model, many consumers: that's the payoff.

## Exercise: add a measure

Product asks: "how much GPU capacity sits idle in stopped instances?"

1. Add `stopped_gpu_hours` to `instance_snapshots.yml`: a `sum` of `2 * gpus` when
   `state = 'stopped'`, with a description.
2. `docker compose restart cube` (the model is mounted read-only, so a restart reloads it).
3. Ask the agent: "How many GPU-hours were sitting in stopped instances in August?"
4. Add a golden case to `evals/golden.yaml` with the SQL that proves the number.

Next: [06 · The agent, line by line](06-the-agent.md)
