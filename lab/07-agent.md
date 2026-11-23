# Lab 07 · The agent: one question through every hop

> **Calls Claude:** five questions to `claude-opus-5-5`, about 10 cents in total. Each one also writes rows to the
> audit table (Lab 08 reads them).

One sitting, about 35 minutes. Background: [docs/02](../docs/02-text-to-sql.md), [docs/06](../docs/06-the-agent.md).
Code: [app/agent.py](../app/agent.py) (`Runtime`, `ask`), [app/ask.py](../app/ask.py), the prompts at the top of
`agent.py`.

You've used every tool by hand (Lab 06). Now the model picks them. `mise run ask` prints each step: the tool, its
input, the SQL that actually ran, the result, and the token bill. After this lab you can explain:

- what the model sends, in which mode, and how many round trips a question takes
- what prompt caching saves, from the numbers
- how the agent behaves when the data can't answer, or when its persona isn't allowed to see the answer
- what a step budget protects you from, and what it costs when it's too tight

## Before you start

```bash
source lab/env.sh
bao_keys anthropic                         # the key the agent reads at start-up: {"fields":["api_key"],"version":1}
vim +/'^COMMON' app/agent.py               # the shared instructions; SEMANTIC / SQL / HYBRID follow it
```

## 1. Semantic mode

**Predict (P1):** "What was the data team's estimated EC2 compute cost in July 2026?" How many tool calls, and
will Cube answer from Trino or from the rollup?

```bash
mise run ask -- "What was the data team's estimated EC2 compute cost in July 2026, in USD?"
```

Read the last line. `in` counts every input token across all model calls; `cached` is the part read from the
prompt cache.

## 2. Raw SQL mode

**Predict (P2):** the same question with only `run_sql` and the table schema. What does a careful model do when the
data has no prices?

```bash
mise run ask -- --mode sql "What was the data team's estimated EC2 compute cost in July 2026, in USD?"
```

## 3. A persona that can't see the answer

**Predict (P3):** the data team's agent, in hybrid mode (it has `run_sql` too), asks about the payments team. What
will it answer, and will it try raw SQL to get around the semantic layer?

```bash
mise run ask -- --persona agent-data --mode hybrid "How many running instances does the payments team have right now?"
```

## Break it, then fix it

**Break: a step budget that's too tight.** `SL_MAX_STEPS` caps model calls per question (default 10) so a confused
model can't loop forever. Set it to 1:

```bash
SL_MAX_STEPS=1 mise run ask -- "Which team had the most GPU-hours in August 2026, and how many?"
```

**Predict (P4):** did the tool call find the answer? Why is there no answer anyway?

**Fix:** run it with the default budget.

```bash
mise run ask -- "Which team had the most GPU-hours in August 2026, and how many?"
audit "select id, persona, mode, tool, row_count, duration_ms, left(question, 40) as question from agent_queries order by id"
```

## Check your predictions

**P1.** One tool call, two model calls (one to call the tool, one to submit the answer):

```
step 1: query_metrics (289 ms)
    in : {'measures': ['fleet.estimated_cost_usd'], 'filters': [{'member': 'fleet.team', 'operator': 'equals', 'values': ['data']}],
          'timeDimensions': [{'dimension': 'fleet.snapshot_ts', 'dateRange': ['2026-07-01', '2026-07-31']}]}
    sql| SELECT sum("instance_snapshots__estimated_cost_usd") … FROM prod_pre_aggregations.instance_snapshots_daily_usage …
    out: {"rows":[{"fleet.estimated_cost_usd":"22562.80568"}], …, "served_from_pre_aggregation":true}
answer: The data team's estimated EC2 compute cost for July 2026 (UTC) was about $22,562.81. …spot hours priced at
        35% of on-demand. It does not include EBS, data transfer or Windows licensing.
cost  : 2 model calls, 6026 in (2784 cached) / 367 out tokens, 5.0s
```

From the rollup, in under 300 ms. The answer repeats the caveats from the measure's description: the model read the
semantic layer's documentation and passed it on. On the first question after a start the cache is cold for call 1
and warm for call 2; later questions in the same mode read almost all of it from cache.

**P2.** It refuses to invent a price:

```
step 1: run_sql: SELECT team, count(*)*2 AS running_instance_hours, sum(vcpus)*2 AS vcpu_hours … GROUP BY team
answer: I can't give a USD figure because the lakehouse has no pricing or billing data. … What I can tell you is the
        data team's usage in July 2026 …: about 75,536 running instance-hours, or 878,288 vCPU-hours.
value : None
```

The model got the grain right on its own (`count(*)*2` for hours), and said so honestly instead of guessing. That
honesty comes from the instructions in `COMMON` ("If the data can't answer it… say so plainly"); a weaker prompt
produces a confident, made-up number. The rule the question needed exists only in the semantic layer.

**P3.**

```
step 1: query_metrics … filters team = payments … out: {"rows":[{"fleet.running_instances":"0"}]}
step 2: dimension_values {'dimension': 'fleet.team'}  out: {"values":["data"]}
answer: I can't tell you how many instances payments is running. The 0 I got back doesn't mean payments has no
        instances. Your access only lets you see the data team … I didn't use raw SQL to get around that restriction.
value : None
```

The `0` is the row filter, not a fact about payments. The model noticed (only `data` is visible) and didn't treat the
0 as the answer. It didn't try `run_sql` either, and if it had, Trino's row filter would have returned the same 0
(Lab 03). Your answer text will differ; `value: None` is what the eval checks (Lab 09).

**P4.** Step 1 found it: `ml` with `22336` GPU-hours. But the budget counts *model calls*, and the answer needs a
second call to read the result and submit it. So: `Stopped: too many steps without an answer.`, `stop=max_steps`,
and you paid for the tool call anyway. With the default budget: "The ml team had the most GPU-hours in August 2026,
with 22,336 running GPU-hours", 2 model calls. A budget should be a few times the normal step count (here 2–3), not
equal to it.

The audit table now has one row per tool call: semantic, sql, the data team's two hybrid calls, and the GPU
question (once from the cut-off run, once from the full run).

## What to remember

- The loop is: model asks for a tool, you run it, the result goes back, repeat until it submits.
- Semantic mode: fewer steps, the business rules and their caveats come with the measure.
- Good instructions make "I can't answer that" a normal outcome; a 0 from a row filter isn't evidence.
- Step budgets stop runaway loops; set them with headroom.

Next: [Lab 08 · The audit trail and dynamic credentials](08-dynamic-credentials.md).
