# 02 · How text-to-SQL agents work

## From one prompt to an agent

**2023-style text-to-SQL** was one model call: paste the schema and the question, get a
SQL string back, run it. It broke on anything non-trivial.

**A text-to-SQL *agent*** is a loop. The model has tools, decides what to call, sees the
result, and keeps going until it can answer:

```
question
  └─▶ model ── tool call ──▶ your code runs it (Cube or Trino) ── result ──┐
        ▲                                                                  │
        └──────────────────────────────────────────────────────────────────┘
      ... until the model calls submit_answer (or a step limit stops it)
```

That loop is [app/agent.py](../app/agent.py), written by hand on the Claude Messages API
with no framework, so you can read every step. The model is `claude-opus-5-5` with adaptive
thinking and `effort: medium`.

## The stages inside a good text-to-SQL agent

Research systems (DIN-SQL, CHESS, the Spider 2.0 / BIRD leaderboards) and production
systems converge on the same stages. Here is each one and where it lives in this repo.

| Stage | What it means | Here |
|---|---|---|
| **Schema linking** | find the tables/columns/members relevant to the question | the catalog or schema is in the system prompt (small enough to fit; large schemas need retrieval over table docs) |
| **Value grounding** | map the user's words to real values: "checkout" → `checkout-api` | `dimension_values` tool; in SQL mode the model can `SELECT DISTINCT` |
| **Generation** | write the query | `query_metrics` (members) or `run_sql` (SQL) |
| **Validation** | is it safe and well-formed *before* it runs? | `sqlguard.check` parses SQL; `CubeClient.validate` checks member names and suggests near matches |
| **Execution** | run it with limits | Trino: 60 s timeout, read-only catalog, row filters; results capped at 200 rows |
| **Self-correction** | read the error, fix, retry | errors go back as `is_error` tool results written for the model |
| **Answer + provenance** | say what the number is and where it came from | `submit_answer` with a numeric `value`; every query is in the audit table |

## Three modes, one agent

`--mode` picks the toolset ([app/tools.py](../app/tools.py)):

- **semantic**: `query_metrics` + `dimension_values`. The model never sees SQL. It can
  only ask for things the model defines, so it can't invent a definition.
- **sql**: `run_sql` only, plus the table schema and column docs from Iceberg. This is
  classic text-to-SQL, with guardrails.
- **hybrid**: both; prefer the semantic layer, fall back to SQL *and say so* in the answer.
  This is the production pattern: governed where possible, flexible where needed, and
  honest about which is which.

## Guardrails: the model is an untrusted SQL author

Treat model-written SQL like user input to a web form. It can be wrong, and a
prompt-injected question ("ignore previous instructions and DROP ...") can make it
hostile. [app/sqlguard.py](../app/sqlguard.py) parses with sqlglot (Trino dialect) and
rejects anything that isn't exactly one read query over allowlisted tables, then forces a
LIMIT. It's tested in [tests/test_sqlguard.py](../tests/test_sqlguard.py).

But a parser is not a security boundary on its own. The real boundaries are in the
engine and the identity:

1. **Trino's catalog is read-only** (`iceberg.security=read_only`), so even a query that
   slipped past the parser can't write.
2. **Trino row filters per user** ([rules.json](../stack/trino/etc/rules.json)): the
   `agent-data-team` user only ever sees `team = 'data'` rows, whatever SQL it sends.
3. **The agent acts as a persona**, an OpenBao identity, not as a shared service account.
   Cube and Trino enforce that persona's rows.

Defence in depth: the guard catches mistakes early with a helpful message; the engine
enforces the rules even when the guard is fooled.

## Tool design matters more than prompt tricks

- **One tool per intent**, with a description that says *when* to use it.
- **Errors are tool results, not exceptions.** "Unknown measure 'fleet.cost'. Did you mean:
  fleet.estimated_cost_usd?" fixes itself on the next step.
- **Cap every result.** One `SELECT *` must not flood the context window.
- **A structured final answer.** `submit_answer(value=...)` makes the agent's output
  machine-checkable, which is what makes evals (lesson 07) possible.
- **Prompt caching.** Tools, then the system prompt with the full catalog, form a stable
  prefix marked with `cache_control`. Later calls in the loop read it from cache: the
  report in lesson 07 shows `cache_read_tokens`.

## Why the semantic path is more accurate

Look at the eval cases in [evals/golden.yaml](../evals/golden.yaml). Each `trap:` is a fact
the SQL author must know and the column names don't say:

- each row is a 2-hour snapshot, so hours = rows × 2
- instance counts over a month are semi-additive
- cost uses a price book and a spot discount that aren't in any table
- "checkout service" is tagged `checkout-api`

In semantic mode those rules are inside the measures; the model only has to pick the
right one, guided by the descriptions. In SQL mode it has to *rediscover* each one, every
time, and nothing tells it when it didn't.

## When raw SQL is still the right tool

Exploration ("what states can an instance be in?"), one-off questions the model doesn't
cover, and data discovery. That's why hybrid mode keeps `run_sql`, and why the answer
must say a number came from raw SQL rather than a governed metric. If the same raw-SQL
question keeps showing up in the audit table, that's your signal to add a measure.

Next: [03 · Secrets with OpenBao](03-openbao.md)
