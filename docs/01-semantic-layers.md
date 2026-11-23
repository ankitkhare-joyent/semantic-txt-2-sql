# 01 · Semantic layers: what they are and why agents need them

## The problem in one example

Ask three people "how many instances did the platform team run in June?" and you can get
three different numbers from the same Iceberg table (real numbers from this lab's data):

| Query someone writes | Result | What it actually counts |
|---|---|---|
| `count(*) WHERE team='platform' AND state='running' AND <June>` | 91,511 | instance **snapshots** (each instance appears 12× a day) |
| `count(DISTINCT instance_id) ...` | 4,514 | instances that **existed at some point** (ASG churn inflates it) |
| `count(*) / count(DISTINCT snapshot_ts)` | 254 | instances running **at the same time**, on average |

None of these queries is wrong as SQL. The disagreement is about **meaning**: what grain
the table has, which aggregations are valid over time, what "run" means. You have met this
problem in Superset: two dashboards, two definitions of one metric, and a Slack thread
about which one is right.

A **semantic layer** is the place where that meaning is written down once, as code, and
then *compiled* into SQL for every consumer: BI tools, notebooks, APIs, and now agents.

## What a semantic layer contains

| Concept | In this repo | What it encodes |
|---|---|---|
| **Entity / cube** | `instance_snapshots`, `instance_prices` | a table (or SQL) and its grain, i.e. what one row is |
| **Dimension** | `fleet.team`, `fleet.region`, `fleet.snapshot_ts` | things you group and filter by |
| **Measure** | `fleet.running_instance_hours`, `fleet.estimated_cost_usd` | aggregations, with the business rule inside them |
| **Join** | snapshots → prices, `many_to_one` | how entities relate, and the cardinality, so the engine can avoid fan-out |
| **Segment** | `fleet.latest_snapshot` | a named, reusable filter ("now") |
| **View** | `fleet` | the curated public interface; cubes stay private |
| **Access policy** | `sl-admins` see everything, `sl-team-members` see their team | who may see which rows and members |
| **Pre-aggregation** | `daily_usage` rollup in Cube Store | caching/materialisation, chosen per query automatically |

Read [stack/cube/model/cubes/instance_snapshots.yml](../stack/cube/model/cubes/instance_snapshots.yml) now. The comments explain each rule.
The important part is the measure *types*:

- **Additive** (`sum` of hours, cost): safe over any time range and any grouping.
- **Semi-additive** (`running_instances`, a count at a point in time): valid for **one**
  snapshot only. Like a bank balance, you can't add Monday's to Tuesday's. This is the
  single most common analytics bug, and the one LLMs make most often.
- **Non-additive** (ratios, distinct counts): must be computed from components, never
  averaged from pre-computed values. That's why `spot_share` is defined as
  `spot_instance_hours / running_instance_hours`, and why the daily rollup can't serve
  `instances` (a distinct count) and Cube sends those queries to Trino instead.

## How a query flows

```
agent ──JSON query──▶ Cube ──compiles──▶ SQL ──▶ Trino ──▶ Iceberg (Lakekeeper + S3)
        {measures: [fleet.estimated_cost_usd],
         dimensions: [fleet.team],
         timeDimensions: [{dimension: fleet.snapshot_ts, dateRange: [2026-07-01, 2026-07-31]}]}
```

The agent picks *names*. Cube writes the join to the price table, the `CASE` for running
state, the `× 2` for hours, the spot discount, the `WHERE team = 'data'` from the caller's
token, the partition-pruning time filter, and decides whether the daily rollup in Cube
Store can answer instead of Trino. Every one of those is a place a model writing raw SQL
can go wrong.

## Why this matters more for agents than for people

1. **Agents don't have tribal knowledge.** A human analyst learns "multiply by 2, the
   snapshots are 2-hourly" once. A model rediscovers it on every question, and sometimes
   doesn't.
2. **Wrong answers look right.** Text-to-SQL fails *silently*: a plausible number with a
   confident sentence. The semantic layer fails *loudly*: "Unknown measure", which the
   agent can recover from or report.
3. **Determinism.** Same members in, same SQL out. Raw SQL varies run to run.
4. **Governance in one place.** Row-level security, approved definitions and caching live
   in the layer, so every client (agent, Superset, the MCP server) gets the same rules.

### What the benchmarks say (2026)

dbt Labs re-ran their benchmark in 2026 on 11 insurance-domain questions × 20 runs:
semantic-layer accuracy was 98–100 % for questions the model could express, and
text-to-SQL 84–90 % on the same well-modelled data (64 % on the raw normalised schema).
The text-to-SQL failures were silent wrong answers; the semantic-layer failures were
"can't answer that". Their recommendation, which this repo follows in `hybrid` mode:
**semantic layer first, raw SQL as an explicit, labelled fallback.**
([dbt blog, 2026](https://docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026))

You will measure the same effect on this data in lesson 07.

## The landscape, and why Cube here

| Tool | Licence | Where metrics live | Runtime API for agents | Notes |
|---|---|---|---|---|
| **Cube Core** (this repo) | Apache 2.0 | YAML/JS/Python model | REST, GraphQL, SQL (Postgres wire), MCP | self-hosted server with caching (Cube Store), access policies, pre-aggregations; Trino/Athena/Snowflake/… drivers |
| **dbt Semantic Layer / MetricFlow** | MetricFlow Apache 2.0 since Oct 2025 | YAML next to dbt models | JDBC/GraphQL/MCP via dbt platform (paid); OSS MetricFlow compiles SQL only | the default if your org is dbt-centric; strong job demand |
| LookML (Looker) | proprietary | LookML | Looker API | the original modern semantic layer |
| Snowflake semantic views, Databricks metric views | proprietary | in the warehouse | warehouse SQL, Cortex/Genie | great if you're all-in on one warehouse |
| Malloy, Boring Semantic Layer, Wren AI | MIT / Apache / AGPL | code | varies | smaller, interesting for experimentation |

**Why Cube for this lab:** it's open source end to end *including the server*, so you can
run the whole thing locally (MetricFlow alone gives you a SQL compiler but not the
governed API, caching or access control, which in dbt live in the paid platform). It sits
naturally on Trino, and its API-first design (REST + MCP) is what
agents call. If your next job is dbt-centric, everything conceptual here transfers:
entities, measures, dimensions, grain, the metric types. Lesson 08 shows the same measure
in MetricFlow YAML.

**Standards to watch.** *Apache Ossie* (formerly Open Semantic Interchange, in the Apache
Incubator since June 2026, backed by dbt Labs, Snowflake, Dremio, Cube and ~50 others) is
an open spec for exchanging metric definitions between tools. Today you still pick one
layer as the source of truth; Ossie aims to make definitions portable between them.

## Check your understanding

1. Why is `avg_running_instances` defined from `running_instance_hours` and `snapshots`
   instead of averaging `running_instances`?
2. The daily rollup includes `estimated_cost_usd` but not `instances`. Why?
3. A new question: "how many GPUs were idle (stopped) last week?" Which existing measure
   is closest, and what would you add to the model?

Next: [02 · How text-to-SQL agents work](02-text-to-sql.md)
