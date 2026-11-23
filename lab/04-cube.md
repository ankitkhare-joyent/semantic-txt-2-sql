# Lab 04 · Cube: the catalog, the grain, and the rollup

> **Changes things:** edits the cost rule in the Cube model for a few minutes (the break), then restores it with
> git. Cube rebuilds its rollup both times.

One sitting, about 40 minutes. Background: [docs/01](../docs/01-semantic-layers.md),
[docs/05](../docs/05-cube-model.md). Model: [instance_snapshots.yml](../stack/cube/model/cubes/instance_snapshots.yml),
[instance_prices.yml](../stack/cube/model/cubes/instance_prices.yml), [views/fleet.yml](../stack/cube/model/views/fleet.yml).

The semantic layer turns *names* into SQL. After this lab you can explain:

- what an agent sees when it asks Cube for its catalog, and why the descriptions matter
- the difference between a semi-additive count and an additive measure, with real numbers
- when Cube answers from its rollup in Cube Store and when it goes to Trino
- what happens everywhere when one business rule changes

`cube_load PERSONA 'QUERY'` sends a Cube JSON query with that persona's OpenBao-signed token (Lab 05 takes the token
apart). Every helper is in [env.sh](env.sh).

## Before you start

```bash
source lab/env.sh
docker compose logs cube | grep -E 'listening' | tail -2
```

## 1. The catalog

**Predict (P1):** the model has two cubes and one view. What does an API client see?

```bash
cube_meta agent-admin | jq -c '.cubes[] | {name, measures: [.measures[].name], segments: [.segments[].name], dims: (.dimensions | length)}'
cube_meta agent-admin | jq -r '.cubes[0].measures[] | select(.name == "fleet.running_instances") | .description'
```

That description is what the model reads when it picks a measure (Lab 07).

## 2. "Now", and the grain trap

```bash
cube_load agent-admin '{"measures":["fleet.running_instances"],"segments":["fleet.latest_snapshot"]}' | jq -c .data
```

Now the question from docs/01: "how many instances did the platform team run in June?" Ask for three measures at
once.

**Predict (P2):** `running_instances` over a whole month, `avg_running_instances`, and `running_instance_hours`.
Which one is "how many at once", and how is one derived from another?

```bash
cube_load agent-admin '{"measures":["fleet.running_instances","fleet.avg_running_instances","fleet.running_instance_hours"],
  "filters":[{"member":"fleet.team","operator":"equals","values":["platform"]}],
  "timeDimensions":[{"dimension":"fleet.snapshot_ts","dateRange":["2026-06-01","2026-06-30"]}]}' | jq -c .data
```

## 3. The rollup

`daily_usage` keeps the additive measures by day in Cube Store. Run the July cost per team twice, timing each:

```bash
JWT=$(persona_jwt agent-admin)
C='{"measures":["fleet.estimated_cost_usd"],"dimensions":["fleet.team"],
    "timeDimensions":[{"dimension":"fleet.snapshot_ts","dateRange":["2026-07-01","2026-07-31"]}],
    "order":{"fleet.estimated_cost_usd":"desc"}}'
for i in 1 2; do
  time (JWT=$JWT cube_load '' "$C" | jq -c '{top3: [.data[:3][] | [.["fleet.team"], .["fleet.estimated_cost_usd"]]],
                                             rollup: (.usedPreAggregations // {} | keys)}')
done
```

**Predict (P3):** now count *distinct* instances per team for the same month. Will it use the rollup?

```bash
D='{"measures":["fleet.instances"],"dimensions":["fleet.team"],
    "timeDimensions":[{"dimension":"fleet.snapshot_ts","dateRange":["2026-07-01","2026-07-31"]}]}'
time (JWT=$JWT cube_load '' "$D" | jq -c '{teams: (.data | length), rollup: (.usedPreAggregations // {} | keys)}')
```

See the SQL each one becomes (compiled, not run):

```bash
cube_sql agent-admin "$C"
cube_sql agent-admin "$D"
docker compose exec -T cubestore du -sh /cube/data
```

## Break it, then fix it

**Break: change a business rule.** Finance says spot instances should be costed at the full on-demand price. Make
that change in the one place it lives, and restart Cube so it reloads the model.

```bash
Q='{"measures":["fleet.estimated_cost_usd","fleet.spot_share"],
    "filters":[{"member":"fleet.team","operator":"equals","values":["data"]}],
    "timeDimensions":[{"dimension":"fleet.snapshot_ts","dateRange":["2026-07-01","2026-07-31"]}]}'
cube_load agent-admin "$Q" | jq -c .data
sed -i '' "s/THEN 0.35 ELSE 1.0 END/THEN 1.0 ELSE 1.0 END/" stack/cube/model/cubes/instance_snapshots.yml
git diff --stat
docker compose restart cube
until docker inspect -f '{{.State.Health.Status}}' semantic-lab-cube-1 | grep -q healthy; do sleep 3; done
```

**Predict (P4):** the July cost lives in a rollup that was built with the old rule. After the restart, does the
query return the old number, an error, or the new number? By how much does it change, given the data team's
`spot_share`?

```bash
cube_load agent-admin "$Q" | jq -c '{data, rollup: (.usedPreAggregations // {} | keys)}'
```

**Fix:**

```bash
git checkout stack/cube/model/cubes/instance_snapshots.yml
docker compose restart cube
until docker inspect -f '{{.State.Health.Status}}' semantic-lab-cube-1 | grep -q healthy; do sleep 3; done
cube_load agent-admin "$Q" | jq -c .data
```

## Check your predictions

**P1.** One entry, `fleet`, with 9 measures, 20 dimensions and the segment `fleet.latest_snapshot`. The cubes are
`public: false`, so clients never see `instance_snapshots` or `instance_prices`, and `row_key` and `snapshots` are
excluded from the view. The description of `running_instances` spells out that it's semi-additive and what to use
instead. The latest-snapshot count is `845`.

**P2.** `4514`, `254.197…` and `183022`:

- `running_instances` over June counts **distinct instances that ran at some point** in the month (4,514). The
  platform team's autoscaling groups churn, so that's 18 times the fleet size. It's the number a naive query gives.
- `avg_running_instances` (254.2) is "how many at once": running instance-hours / (2 × snapshots).
- `running_instance_hours` (183,022) is additive: 254.2 × 720 hours in June = 183,022.

The same numbers are in the table at the top of docs/01; now you've produced them.

**P3.** The cost query uses `prod_pre_aggregations.instance_snapshots_daily_usage`: about 0.3 s the first time and
0.1 s the second (Cube's in-memory result cache). Top three: platform $94,908.87, search $81,761.19, ml $48,404.55.
The distinct count has `rollup: []` and took about 4 s: a distinct count can't be re-aggregated from daily rows (an
instance running on two days would count twice), so Cube compiles it to Trino SQL. `cube_sql` shows the difference:
the cost query becomes `SELECT sum("instance_snapshots__estimated_cost_usd") FROM
prod_pre_aggregations.instance_snapshots_daily_usage …` (Cube Store SQL), the distinct count becomes
`COUNT(DISTINCT …) FROM lake.ec2.instances …` with a `snapshot_ts` range (Trino SQL). Cube Store holds about 700 KB
of Parquet chunks for six months of daily rollups.

**P4.** The new number, straight away: `37429.5568`, up from `22562.80568`, and still served from the rollup.
Cube names each rollup with a hash of its definition, so a changed measure means a new rollup, which the refresh
worker built while the API was coming up. Nothing stale is served. The size of the change follows from the rule:
44 % of the data team's instance-hours are spot, and those went from 35 % to 100 % of the price. After the fix the
query returns `22562.80568` again.

That's the point of a semantic layer: the rule changed in one line, and every consumer (this query, the agent, the
MCP server, a Superset chart on the SQL API) got the new number together. In SQL mode, the agent never knew the rule
in the first place.

## What to remember

- The view is the API. Its names and descriptions are the documentation an agent reads.
- Counts over time are semi-additive; hours and money are additive. Pick the measure that matches the question.
- Rollups serve additive measures in milliseconds; distinct counts go to the engine. Cube decides per query.
- One rule, one place, every consumer.

Next: [Lab 05 · Identity tokens and row-level security](05-identity-rls.md).
