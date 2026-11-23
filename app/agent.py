"""The text-to-SQL agent: a hand-written tool loop on the Claude Messages API.

    question -> Claude picks a tool -> we run it (Cube or Trino) -> result goes back
             -> ... repeat ... -> Claude calls submit_answer -> done

The loop is written out (no framework) so every step is visible. What makes it
production-shaped rather than a demo:
  * secrets: the Anthropic key, Cube token and audit DB login all come from OpenBao
  * identity: the agent acts AS a persona; Cube and Trino enforce that persona's rows
  * bounded: max_steps, capped result rows, per-query timeouts
  * observable: every tool call goes to the audit table with the SQL that actually ran
  * prompt caching: tools + system prompt (with the catalog) are a stable cached prefix
"""
import logging
import time
from dataclasses import dataclass, field

import anthropic

from .audit import AuditLog
from .bao import OpenBao, jwt_claims
from .config import Settings
from .rawsql import TrinoRunner
from .semantic import CubeClient
from .tools import Toolbox

log = logging.getLogger("app.agent")

COMMON = """You answer questions about a company's AWS EC2 fleet for its platform team.

The data comes from `aws ec2 describe-instances` snapshots taken every 2 hours from
2026-04-01 to 2026-09-30 across 4 regions and 8 team accounts. "Now", "currently" and
"today" mean the latest snapshot. Months and dates are UTC.

Work like an analyst:
- Get every number from a tool. Never estimate or reuse numbers from memory.
- Check that the query answers the exact question (time range, filters, grain) before answering.
- If the data can't answer it, or you are not allowed to see it, say so plainly instead of guessing.
- Finish by calling submit_answer once. `value` is the single number that answers the question,
  unrounded and in the unit you state (percent as 0-100). Use null for lists or non-answers."""

SEMANTIC = """
You query a governed semantic layer through query_metrics. Use only the member names in the
catalog below; their descriptions define what each one means and when it is valid. Prefer
measures that already encode a rule (e.g. cost, shares) over rebuilding them yourself.

# Semantic layer catalog
{catalog}"""

SQL = """
You query the lakehouse with Trino SQL through run_sql. Tables and columns:

{schema}"""

HYBRID = """
You have both a governed semantic layer (query_metrics) and raw SQL (run_sql).
Use the semantic layer whenever its catalog can express the question: its measures carry
the company's definitions and access rules. Use run_sql only for what the catalog cannot
express, and say in your answer that the number came from raw SQL, not a governed metric.

# Semantic layer catalog
{catalog}

# Raw tables
{schema}"""


@dataclass
class Step:
    tool: str
    input: dict
    is_error: bool
    result_preview: str
    generated_sql: str | None
    duration_ms: int


@dataclass
class Result:
    question: str
    mode: str
    persona: str
    answer: str
    value: float | None
    unit: str | None
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    seconds: float = 0.0
    stop: str = ""


class Runtime:
    """Everything that outlives one question: logins, clients, the cached catalog text."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or Settings()
        self.bao = OpenBao(self.s.bao_addr, self.s.approle_dir)
        self.persona = jwt_claims(self.bao.identity_token())["persona"]     # e.g. agent-data-team
        self.claude = anthropic.Anthropic(api_key=self.bao.kv("anthropic", "api_key"), max_retries=4)
        self.cube = CubeClient(self.s.cube_url, self.bao.identity_token)
        self.trino = TrinoRunner(self.s.trino_host, self.s.trino_port, self.persona,
                                 self.s.allowed_tables, self.s.max_rows)
        self.audit = AuditLog(self.bao, self.s.audit_db_host, self.s.audit_db_port)
        self._prompts: dict[str, str] = {}

    def system_prompt(self, mode: str) -> str:
        if mode not in self._prompts:
            parts = {"semantic": SEMANTIC, "sql": SQL, "hybrid": HYBRID}[mode]
            catalog = self.cube.catalog_text() if mode != "sql" else ""
            schema = self.trino.schema_text() if mode != "semantic" else ""
            self._prompts[mode] = COMMON + "\n" + parts.format(catalog=catalog, schema=schema)
        return self._prompts[mode]

    def close(self) -> None:
        self.audit.close()


def ask(rt: Runtime, question: str, mode: str = "semantic") -> Result:
    s = rt.s
    tools = Toolbox(mode, rt.cube, rt.trino, s.max_rows)
    res = Result(question, mode, rt.persona, answer="", value=None, unit=None)
    messages = [{"role": "user", "content": question}]
    t0 = time.perf_counter()

    for _ in range(s.max_steps):
        resp = rt.claude.beta.messages.create(
            model=s.model,
            max_tokens=16000,
            system=[{"type": "text", "text": rt.system_prompt(mode), "cache_control": {"type": "ephemeral"}}],
            tools=tools.definitions,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": s.effort},
            # If a safety classifier declines, the API retries on a suitable model inside this call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        res.llm_calls += 1
        u = resp.usage
        res.input_tokens += u.input_tokens + (u.cache_creation_input_tokens or 0) + (u.cache_read_input_tokens or 0)
        res.cache_read_tokens += u.cache_read_input_tokens or 0
        res.output_tokens += u.output_tokens

        if resp.stop_reason == "refusal":
            res.answer, res.stop = "The model declined to answer this question.", "refusal"
            break
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            res.answer = "".join(b.text for b in resp.content if b.type == "text").strip()
            res.stop = resp.stop_reason
            break

        results, submitted = [], False
        for call in calls:
            if call.name == "submit_answer":
                res.answer = call.input.get("answer", "")
                res.value = call.input.get("value")
                res.unit = call.input.get("unit")
                submitted = True
                continue
            out = tools.execute(call.name, call.input)
            res.steps.append(Step(call.name, call.input, out.is_error, out.content[:300],
                                  out.audit.get("generated_sql"), out.audit.get("duration_ms", 0)))
            rt.audit.record(persona=rt.persona, mode=mode, question=question, tool=call.name,
                            query=out.audit.get("query", call.input), generated_sql=out.audit.get("generated_sql"),
                            row_count=out.audit.get("row_count"), error=out.audit.get("error"),
                            duration_ms=out.audit.get("duration_ms", 0))
            results.append({"type": "tool_result", "tool_use_id": call.id, "content": out.content,
                            "is_error": out.is_error})
        if submitted:
            res.stop = "submitted"
            break
        messages.append({"role": "user", "content": results})   # all results in one message
    else:
        res.answer, res.stop = "Stopped: too many steps without an answer.", "max_steps"

    res.seconds = round(time.perf_counter() - t0, 1)
    return res
