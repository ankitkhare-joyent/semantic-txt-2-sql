# 08 · From this lab to production

What's real here, what's a local shortcut, and what you'd change on AWS.

| Area | In this lab | In production |
|---|---|---|
| **Semantic model** | YAML in this repo, mounted into Cube | its own git repo with CI: `cube validate`, tests that compile key queries, review by data owners; deployed like code |
| **Cube** | one container does API + refresh | API instances behind a load balancer (stateless, autoscaled) + separate refresh workers + a Cube Store cluster (router + workers, S3 for storage) |
| **Engine** | single-node Trino, 1.2 GB heap | Trino/Starburst cluster or Athena; Cube points at it the same way |
| **Catalog/storage** | Lakekeeper + SeaweedFS (sibling lab) | Glue Iceberg REST or S3 Tables; **vended credentials** so neither Trino nor Cube holds S3 keys |
| **Agent identity** | AppRole per persona | the *end user's* identity: they sign in via your IdP, the agent acts on their behalf (token exchange / on-behalf-of), and Cube/Trino enforce *their* rows |
| **Trino auth** | trusts the user name (no TLS) | TLS + JWT/OAuth2 authentication; it can verify the same OpenBao-signed JWT via JWKS |
| **OpenBao** | 1 node, Shamir keys on disk, root token kept | 3–5 nodes with Raft, KMS auto-unseal, root revoked, platform auth (K8s/AWS IAM), audit log to SIEM, config as code (OpenTofu) |
| **Secrets in apps** | Bao Agent sidecars rendering files | the same, or the External Secrets / OpenBao operator in Kubernetes |
| **Audit** | Postgres table with dynamic creds | the same pattern, plus traces (OpenTelemetry → Langfuse/your APM) linking question → tool calls → SQL → Trino query id |
| **Evals** | 11 golden cases, run by hand | in CI on every model/prompt/semantic-model change, with a regression gate; new cases from production failures |
| **Cost/limits** | `max_steps`, row caps, 60 s query timeout | plus per-user rate limits, Trino resource groups for agent traffic, budget alerts on tokens |

## Operating the semantic layer

- **Ownership.** Every measure has an owner (the team that defines "cost"). Changes go
  through review like an API change: the view's names are a contract.
- **Version and deprecate.** Add `fleet.estimated_cost_usd_v2` and keep v1 until the
  consumers have moved; don't silently change a definition agents and dashboards rely on.
- **Watch the fallbacks.** In hybrid mode, every `run_sql` call in the audit table is a
  question the model couldn't express. Review them weekly; the frequent ones become measures.
- **Pre-aggregations are a cost tool.** Agents ask many similar questions. Rollups turn
  Trino scans into Cube Store lookups. Watch Cube's logs for queries that miss them.

## The same measure in dbt MetricFlow

If your team is on dbt, the concepts map one to one. `running_instance_hours` and the
cost measure would look like this in a dbt semantic model (MetricFlow is Apache 2.0 since
October 2025):

```yaml
semantic_models:
  - name: instance_snapshots
    model: ref('fct_instance_snapshots')
    defaults: { agg_time_dimension: snapshot_ts }
    entities:
      - { name: instance_snapshot, type: primary, expr: "instance_id || '@' || cast(snapshot_ts as varchar)" }
      - { name: instance_type, type: foreign }
    dimensions:
      - { name: snapshot_ts, type: time, type_params: { time_granularity: hour } }
      - { name: team, type: categorical }
    measures:
      - name: running_instance_hours
        agg: sum
        expr: "case when state = 'running' then 2 else 0 end"
      - name: estimated_cost_usd
        agg: sum
        expr: "case when state = 'running' then 2 * on_demand_usd_per_hour * case when lifecycle = 'spot' then 0.35 else 1 end else 0 end"

metrics:
  - { name: running_instance_hours, type: simple, type_params: { measure: running_instance_hours } }
  - name: spot_share
    type: ratio
    type_params: { numerator: spot_instance_hours, denominator: running_instance_hours }
```

Differences that matter: MetricFlow separates *measures* from *metrics* and has explicit
metric types (simple, ratio, cumulative, derived, conversion); the price would come from a
joined dbt model rather than inline SQL; and the governed runtime API, caching and access
control come from the dbt platform rather than an open-source server. Cube can also read
a dbt project directly, so the two aren't exclusive.

## A short reading list

- dbt Labs, *Semantic Layer vs. Text-to-SQL: 2026 Benchmark Update*: docs.getdbt.com/blog/semantic-layer-vs-text-to-sql-2026
- Cube docs: data modeling, access policies, pre-aggregations, deployment: docs.cube.dev
- OpenBao docs: concepts (seal, tokens, leases, identity), secrets engines: openbao.org/docs
- Apache Ossie (Open Semantic Interchange): incubator.apache.org/clutch/ossie.html
- Spider 2.0 and BIRD: text-to-SQL benchmarks on enterprise-scale schemas

Next: [09 · Architecture](09-architecture.md)
