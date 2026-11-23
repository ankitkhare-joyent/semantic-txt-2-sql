"""Ask the agent one question and see exactly what it did.

    uv run python -m app.ask "What did the ml team's GPUs cost in August?"
    uv run python -m app.ask --mode sql "..."              # raw text-to-SQL instead
    uv run python -m app.ask --persona agent-data "..."    # as the data team (row-level security)
"""
import argparse
import os
import textwrap

from .agent import Runtime, ask
from .config import Settings


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question")
    p.add_argument("--mode", choices=["semantic", "sql", "hybrid"], default="semantic")
    p.add_argument("--persona", choices=["agent-admin", "agent-data"], default=os.environ.get("SL_PERSONA", "agent-admin"))
    args = p.parse_args()

    rt = Runtime(Settings(persona=args.persona))
    try:
        r = ask(rt, args.question, args.mode)
    finally:
        rt.close()

    print(f"\n[{r.mode} | acting as {r.persona}]\n")
    for i, st in enumerate(r.steps, 1):
        flag = " ERROR" if st.is_error else ""
        print(f"step {i}: {st.tool}{flag} ({st.duration_ms} ms)")
        print(textwrap.indent(str(st.input), "    in : "))
        if st.generated_sql:
            print(textwrap.indent(st.generated_sql.strip(), "    sql| "))
        print(textwrap.indent(st.result_preview, "    out: "))
    print(f"\nanswer: {r.answer}")
    print(f"value : {r.value} {r.unit or ''}")
    print(f"cost  : {r.llm_calls} model calls, {r.input_tokens} in ({r.cache_read_tokens} cached) / "
          f"{r.output_tokens} out tokens, {r.seconds}s, stop={r.stop}")


if __name__ == "__main__":
    main()
