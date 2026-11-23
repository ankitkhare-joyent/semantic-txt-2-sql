# 06 · The agent, file by file

```
app/
├── config.py       settings; the only bootstrap input is which persona (AppRole) to use
├── bao.py          OpenBao client: AppRole login + renewal, KV, identity JWT, DB leases
├── semantic.py     Cube REST client: catalog for the prompt, validation, /load polling, /sql
├── sqlguard.py     parse-and-check for model-written SQL (sqlglot, Trino dialect)
├── rawsql.py       Trino connection as the persona's user; schema + column docs for the prompt
├── tools.py        tool schemas per mode and their execution; errors become tool results
├── audit.py        audit rows written with dynamic Postgres credentials
├── agent.py        Runtime (long-lived clients) + ask() (the loop)
├── ask.py          CLI: one question, prints every step and the SQL that ran
└── mcp_server.py   the semantic layer as an MCP server for Claude Code / Desktop
```

## The loop ([agent.py](../app/agent.py), `ask`)

```python
for _ in range(max_steps):                         # hard stop: no runaway loops
    resp = claude.beta.messages.create(
        model="claude-opus-5-5",
        system=[{..., "cache_control": {"type": "ephemeral"}}],   # catalog cached after call 1
        tools=tools.definitions,
        messages=messages,
        thinking={"type": "adaptive"},             # the model decides how much to think
        output_config={"effort": "medium"},        # the main cost/quality dial
        betas=["server-side-fallback-2026-07-01"], fallbacks="default",
    )
    if resp.stop_reason == "refusal": ...          # always check before reading content
    messages.append({"role": "assistant", "content": resp.content})
    calls = [b for b in resp.content if b.type == "tool_use"]
    if not calls: break                            # plain text answer
    for call in calls:                             # parallel calls are allowed
        if call.name == "submit_answer": ...       # structured final answer -> done
        out = tools.execute(call.name, call.input) # Cube or Trino, never raises
        audit.record(...)                          # every query, with the SQL that ran
    messages.append({"role": "user", "content": results})   # ALL results in ONE message
```

Things worth noticing:

- **No framework.** LangGraph, Strands or the SDK's tool runner would hide this loop; the
  sibling project (learn-ai-agent) compares them. Here the point is to see each step.
- **Effort, not model swapping, is the first cost lever.** `SL_EFFORT=low` makes the agent
  faster and cheaper; re-run the eval to see whether accuracy holds.
- **`fallbacks="default"`.** If a safety classifier declines a request, the API retries on
  a suitable model inside the same call. You still check `stop_reason == "refusal"`.
- **Structured end.** `submit_answer(answer, value, unit)` gives a number the eval can
  grade, instead of parsing prose.

## Context engineering

The system prompt is `COMMON` + one of `SEMANTIC` / `SQL` / `HYBRID`:

- **semantic** gets the catalog from Cube `/meta`, rendered by `CubeClient.catalog_text()`:
  every member with its type and description. Those descriptions *are* the prompt. To
  improve the agent, improve the model's descriptions, not the agent's instructions.
- **sql** gets `SHOW COLUMNS` output, including the column docs the lakehouse stores in
  the Iceberg schema (`doc=` in the sibling's `load_iceberg.py`). Good table docs are the
  cheapest accuracy win for raw text-to-SQL.

This works because the catalog is small (a few thousand tokens, tools and instructions included). With hundreds of tables you
retrieve instead: embed table and column docs, fetch the top few per question (schema
linking via vector search), or give the model a `search_catalog` tool.

## Identity flows all the way down

`Runtime` logs in as a persona and reads the persona name *from the signed token's
claims*, not from config. That name becomes the Trino user, the audit row's `persona`, and
(through the token) Cube's security context. One identity, enforced in three places:

| Path | Enforced by | How |
|---|---|---|
| semantic | Cube access policy | JWT `groups` → `sl-team-members` → `WHERE team = <team claim>` |
| sql | Trino file-based access control | user `agent-data-team` → row filter `team = 'data'` |
| both | OpenBao policy `sl-agent` | which secrets the agent's token can read at all |

The eval's `rls_data_team_cannot_see_payments` case checks it: as `agent-data`, "how many
payments instances are running?" must come back as nothing, in every mode.

## The MCP server

[mcp_server.py](../app/mcp_server.py) exposes `query_metrics`, `dimension_values` and the
catalog as an MCP resource, with no raw SQL. Any MCP client gets the governed path only:

```bash
claude mcp add fleet-metrics -e SL_PERSONA=agent-data -- \
  uv --directory ~/workstuff/semantic-txt-2-sql run python -m app.mcp_server
```

Then ask Claude Code "what did my team spend on EC2 in August?" and it answers through
your semantic layer, as the data team, with your OpenBao identity.

## Run it

```bash
mise install && uv sync
uv run python -m app.ask "Which team spent the most on GPUs in Q3?"
uv run python -m app.ask --mode sql "How many instances were running at the same time in us-east-1 during May, on average?"
uv run python -m app.ask --persona agent-data --mode hybrid "How many payments instances are running now?"
uv run pytest
```

Next: [07 · Measuring it: evals](07-evals.md)
