# 07 · Measuring it: evals

You can't improve an agent you don't measure, and "it answered my three test questions"
is not a measurement. [evals/](../evals) is a small, honest eval harness:

- [golden.yaml](../evals/golden.yaml): 11 questions, each with **hand-written truth SQL**
  and, where relevant, the *trap* it sets (grain, semi-additivity, a business rule, value
  grounding, security).
- [run.py](../evals/run.py): computes the truth on Trino as `admin`, asks the agent in each
  mode, grades the submitted `value` (0.5 % relative tolerance, or an absolute one), and
  writes `evals/results/<time>.md` + `.jsonl` with every step the agent took.

```bash
uv run python -m evals.run                                   # 3 modes x 11 cases
uv run python -m evals.run --modes sql --trials 5            # variance matters: repeat
uv run python -m evals.run --only data_cost_jul avg_concurrent_us_east_may
```

## Results from this lab (2026-10-04, claude-opus-5-5, effort medium, 1 trial)

| mode | accuracy | avg seconds | avg tool calls | avg input tokens | avg output tokens |
|---|---|---|---|---|---|
| semantic | 11/11 (100 %) | 7.7 | 1.3 | 6,851 | 341 |
| sql | 10/11 (91 %) | 12.5 | 2.5 | 6,052 | 858 |
| hybrid | 11/11 (100 %) | 6.0 | 1.4 | 8,208 | 382 |

| case | semantic | sql | hybrid | truth |
|---|---|---|---|---|
| running_now | 845 | 845 | 845 | 845 |
| payments_running_now | 185 | 185 | 185 | 185 |
| untagged_now | 42 | 42 | 42 | 42 |
| ml_gpu_hours_aug | 22,336 | 22,336 | 22,336 | 22,336 |
| top_region_vcpu_hours_sep | 2,303,896 | 2,303,896 | 2,303,896 | 2,303,896 |
| spot_share_jun | 10.31 % | 10.31 % | 10.31 % | 10.31 % |
| avg_concurrent_us_east_may | 338.77 | 338.77 | 338.77 | 338.77 |
| windows_distinct_apr | 23 | 23 | 23 | 23 |
| data_cost_jul | $22,562.81 | **no answer** | $22,562.81 | $22,562.81 |
| checkout_hours_q3 | 307,550 | 307,550 | 307,550 | 307,550 |
| rls_data_team_cannot_see_payments | denied | denied | denied | denied |

## How to read this

**The model is good at SQL now.** A current frontier model with a well-documented schema
handled every grain trap in raw SQL: it multiplied snapshots by 2, used one snapshot for
"now", and divided by distinct snapshots for the concurrent average. That matches dbt's
2026 benchmark: text-to-SQL on *well-modelled* data reaches the high 80s to 90s. Most of
that comes from the lakehouse doing its part: a flat, documented fact table with
`doc=` on its columns. On a raw 15-table normalised schema it would drop sharply.

**Raw SQL can't know what isn't in the data.** The one failure is the cost question. The
price book and the 35 % spot rule live only in the semantic layer. To its credit, the
model *refused* rather than inventing prices, and reported the instance-hours it could
measure. That's the good failure mode, and it comes from the system prompt telling it to
say when data can't answer. A weaker prompt would have produced a confident, made-up
number.

**The semantic path is cheaper per answer.** Half the tool calls, 60 % less output,
roughly 40 % faster, because the model picks members instead of writing, checking and
fixing SQL. The input tokens are similar; with caching the catalog is mostly cache reads.

**Hybrid is the production answer.** It never touched `run_sql` here: every question was
expressible in the catalog, so it behaved exactly like semantic mode, while keeping SQL
available for questions outside the model (try 4 below exercises that).

**Prompt caching works.** In hybrid mode about 95 % of input tokens were cache reads
(`cache_read_tokens` in the `.jsonl`): the tools and the catalog are a stable prefix, so
only the question and the tool results are billed at the full input rate.

**Security held in every mode.** The data-team persona never saw payments rows: Cube's
access policy in semantic mode, Trino's row filter in SQL mode.

## Caveats (read before quoting the numbers)

- **One trial.** LLM output varies run to run; run `--trials 5` before drawing conclusions,
  and look at the spread, not just the mean.
- **11 cases** is a teaching set, not a benchmark. Real eval sets grow from production:
  every question users ask that the agent got wrong becomes a case.
- **The truth SQL can be wrong too.** Each one was reviewed by hand; when a mode "fails",
  read the `.jsonl` trace before blaming the agent.

## What to try next

1. `SL_EFFORT=low uv run python -m evals.run`: does accuracy hold at lower cost?
2. Make SQL mode harder: remove the column docs from the prompt (`rawsql.schema_text`)
   and watch the grain traps start to fail.
3. Add the `stopped_gpu_hours` measure from lesson 05 and a golden case for it.
4. Add a question the semantic layer can't express ("which instance ran longest without
   a restart?") and check that hybrid falls back to SQL *and says so*.

Next: [08 · Production checklist](08-production.md)
