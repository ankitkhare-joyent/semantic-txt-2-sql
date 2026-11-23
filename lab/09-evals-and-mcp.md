# Lab 09 · Evals and the MCP server

> **Calls Claude:** 11 agent runs (9 in the eval subset, 2 in the break), about 15 cents. Edits the Cube model for a
> few minutes (the break) and restores it with git.

One sitting, about 35 minutes. Background: [docs/07](../docs/07-evals.md), [docs/06 §MCP](../docs/06-the-agent.md#the-mcp-server).
Code: [evals/golden.yaml](../evals/golden.yaml), [evals/run.py](../evals/run.py), [app/mcp_server.py](../app/mcp_server.py),
the helper [lab/mcp_call.py](mcp_call.py).

The last two pieces: a way to *measure* the agent, and a way to give the same governed metrics to any other MCP
client. After this lab you can explain:

- how a golden case is built, graded, and why its truth is SQL rather than an expected sentence
- where each mode fails, from the trace rather than the score
- how an eval catches a business-rule change nobody announced
- what an MCP client sees from this repo, and why raw SQL isn't on that list

## Before you start

```bash
source lab/env.sh
vim evals/golden.yaml        # read three cases: avg_concurrent_us_east_may, data_cost_jul, rls_data_team_cannot_see_payments
```

## 1. Run a subset

**Predict (P1):** three cases × three modes. Which of the nine runs fail, and why?

```bash
uv run python -m evals.run --only data_cost_jul avg_concurrent_us_east_may rls_data_team_cannot_see_payments
```

The truth for each case comes from its `truth_sql`, run on Trino as `admin` at the start (`truths()` in `run.py`).
The agent's `submit_answer` value is compared within 0.5 %. The security case has no truth number: it passes when the
value is null or 0.

## 2. Read the failure, not the score

```bash
ls evals/results/
jq -c 'select(.passed == false) | {case, mode, value, truth, steps: [.steps[] | .tool], answer: .answer[:120]}' \
  "$(ls -t evals/results/*.jsonl | head -1)"
```

**Predict (P2):** is that failure a bug in the agent? What would you change to make SQL mode pass it, and should you?

## 3. The same metrics in any MCP client

`app/mcp_server.py` exposes `query_metrics`, `dimension_values` and the catalog as an MCP resource. `lab:mcp` starts it
over stdio and talks to it the way Claude Code would:

```bash
mise run -q lab:mcp
SL_PERSONA=agent-data mise run -q lab:mcp | grep '^query_metrics'
```

**Predict (P3):** which tools does an MCP client get? Does the data-team persona see other teams through MCP?

To use it from Claude Code for real (optional):

```bash
claude mcp add fleet-metrics -e SL_PERSONA=agent-data -- uv --directory ~/workstuff/semantic-txt-2-sql run python -m app.mcp_server
```

## Break it, then fix it

**Break: a silent business-rule change.** Someone "fixes" the spot discount in the model (the same edit as Lab 04)
and doesn't tell anyone. Run the cost case in semantic mode only:

```bash
sed -i '' "s/THEN 0.35 ELSE 1.0 END/THEN 1.0 ELSE 1.0 END/" stack/cube/model/cubes/instance_snapshots.yml
docker compose restart cube
until docker inspect -f '{{.State.Health.Status}}' semantic-lab-cube-1 | grep -q healthy; do sleep 3; done
uv run python -m evals.run --modes semantic --only data_cost_jul
```

**Predict (P4):** the agent did nothing wrong. Does the eval pass?

**Fix:**

```bash
git checkout stack/cube/model/cubes/instance_snapshots.yml && docker compose restart cube
until docker inspect -f '{{.State.Health.Status}}' semantic-lab-cube-1 | grep -q healthy; do sleep 3; done
uv run python -m evals.run --modes semantic --only data_cost_jul
```

## Check your predictions

**P1.** One failure out of nine: SQL mode on `data_cost_jul`.

```
PASS hybrid   avg_concurrent_us_east_may   got=338.76612903225805 truth=338.76612903225805
PASS semantic data_cost_jul                got=   22562.80568 truth=22562.80568
FAIL sql      data_cost_jul                got=          None truth=22562.80568 (12.7s, 2 tool calls)
…
| semantic | 3/3 (100%) | 8.1 | 1.3 | 7,043 | 371 |
| sql      | 2/3 (67%)  | 12.7 | 2.7 | 7,320 | 954 |
| hybrid   | 3/3 (100%) | 5.3 | 1.3 | 8,412 | 397 |
```

All three modes get the concurrency average right: the model handles the semi-additive trap in SQL too, given good
column docs. All three refuse the security case. Raw SQL takes twice the tool calls and two and a half times the
output tokens. LLM runs vary: on another run SQL mode could pass or fail differently. That's why `--trials` exists.

**P2.** Not a bug: `"steps": ["run_sql", "run_sql"]`, answer "I can't give a dollar figure because I don't have access
to any pricing or billing data…". The model looked, found no prices, and declined. You *could* make it pass by
pasting the price book and the spot rule into the SQL prompt. Then you'd have the business rule in two places (the
prompt and the Cube model), and they would drift. The semantic layer is where that rule belongs; hybrid mode is how
the agent gets it.

**P3.** `tools: ['query_metrics', 'dimension_values']`, `resources: ['semantic://catalog']`, no `run_sql`: external
clients get governed metrics only. As admin the query returns all seven teams (845 running in total); as
`agent-data` it returns `[{'fleet.team': 'data', 'fleet.running_instances': '68'}]`. The MCP server logs in to
OpenBao as `SL_PERSONA` and sends that persona's JWT, so Cube's access policy applies to Claude Code exactly as to
the agent. A bad measure name comes back as a tool error (`isError = True`) with the same "Did you mean" hint.

**P4.** It fails: `FAIL semantic data_cost_jul got=37429.5568 truth=22562.80568`. The agent answered correctly *for the
model it was given*, but the number no longer matches the independently written truth SQL, which still says 35 %.
That's the eval doing its job: a definition changed, and something noticed. In a real team this suite runs in CI on
every change to the model, the prompts or the model version, and a failing case forces the conversation ("did we
mean to change cost?"). After the fix: `PASS … got=22562.80568`.

## What to remember

- Grade numbers against independently written SQL, not text against expected text.
- Read the trace of each failure; many "failures" are correct refusals or a truth that needs updating.
- Evals guard the semantic layer as much as the agent: a changed rule shows up as a failed case.
- MCP is the distribution channel: one governed server, any client, the caller's own row-level security.

That's the whole track. The architecture, with every flow numbered, is in [docs/09](../docs/09-architecture.md).
When you're done: `mise run down`.
