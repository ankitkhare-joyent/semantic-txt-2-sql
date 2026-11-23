"""Score the agent on the golden questions, per mode, and write a report.

    uv run python -m evals.run                         # all modes, all cases, 1 trial each
    uv run python -m evals.run --modes semantic sql --trials 3
    uv run python -m evals.run --only data_cost_jul

Ground truth comes from the hand-written SQL in golden.yaml, run as Trino user `admin`.
Output: evals/results/<timestamp>.md (summary + per-case table) and .jsonl (every answer
with its tool calls), so you can see not just *that* a mode failed but *how*.
"""
import argparse
import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import trino
import yaml

from app.agent import Runtime, ask
from app.config import Settings

HERE = Path(__file__).parent


def truths(cases, s: Settings) -> dict:
    conn = trino.dbapi.connect(host=s.trino_host, port=s.trino_port, user="admin", catalog="lake", schema="ec2")
    out = {}
    for c in cases:
        if c.get("truth_sql"):
            cur = conn.cursor()
            cur.execute(c["truth_sql"])
            out[c["id"]] = float(cur.fetchone()[0])
    return out


def grade(case: dict, value, truth: float | None, defaults: dict) -> bool:
    if case.get("expect_denied"):
        return value in (None, 0, 0.0)
    if value is None or truth is None:
        return False
    if "abs_tol" in case:
        return abs(float(value) - truth) <= case["abs_tol"]
    return math.isclose(float(value), truth, rel_tol=case.get("rel_tol", defaults["rel_tol"]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--modes", nargs="+", default=["semantic", "sql", "hybrid"])
    p.add_argument("--trials", type=int, default=1, help="repeat each case; LLM answers vary run to run")
    p.add_argument("--only", nargs="*", help="case ids")
    args = p.parse_args()

    spec = yaml.safe_load((HERE / "golden.yaml").read_text())
    defaults, cases = spec["defaults"], spec["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    truth = truths(cases, Settings())

    runtimes: dict[str, Runtime] = {}
    records = []
    try:
        for case in cases:
            persona = case.get("persona", defaults["persona"])
            rt = runtimes.get(persona) or runtimes.setdefault(persona, Runtime(Settings(persona=persona)))
            for mode in args.modes:
                for trial in range(args.trials):
                    r = ask(rt, case["question"].strip(), mode)
                    passed = grade(case, r.value, truth.get(case["id"]), defaults)
                    records.append({"case": case["id"], "tags": case.get("tags", []), "mode": mode, "trial": trial,
                                    "passed": passed, "truth": truth.get(case["id"]), **asdict(r)})
                    mark = "PASS" if passed else "FAIL"
                    print(f"{mark} {mode:8} {case['id']:34} got={r.value!r:>14} truth={truth.get(case['id'])!r} "
                          f"({r.seconds}s, {len(r.steps)} tool calls)", flush=True)
    finally:
        for rt in runtimes.values():
            rt.close()

    write_report(records, args.modes)


def write_report(records: list[dict], modes: list[str]) -> None:
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with (out_dir / f"{stamp}.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec, default=str) + "\n")

    lines = [f"# Eval run {stamp}", "", "| mode | accuracy | avg seconds | avg tool calls | avg input tokens | avg output tokens |",
             "|---|---|---|---|---|---|"]
    for mode in modes:
        rs = [r for r in records if r["mode"] == mode]
        if not rs:
            continue
        n = len(rs)
        lines.append(f"| {mode} | {sum(r['passed'] for r in rs)}/{n} ({100 * sum(r['passed'] for r in rs) / n:.0f}%) "
                     f"| {sum(r['seconds'] for r in rs) / n:.1f} | {sum(len(r['steps']) for r in rs) / n:.1f} "
                     f"| {sum(r['input_tokens'] for r in rs) / n:,.0f} | {sum(r['output_tokens'] for r in rs) / n:,.0f} |")
    lines += ["", "| case | " + " | ".join(modes) + " | truth |", "|---|" + "---|" * (len(modes) + 1)]
    for case in dict.fromkeys(r["case"] for r in records):
        cells = []
        for mode in modes:
            rs = [r for r in records if r["case"] == case and r["mode"] == mode]
            cells.append(", ".join(("ok " if r["passed"] else "x ") + _fmt(r["value"]) for r in rs) or "-")
        truth = next(r["truth"] for r in records if r["case"] == case)
        lines.append(f"| {case} | " + " | ".join(cells) + f" | {_fmt(truth)} |")
    path = out_dir / f"{stamp}.md"
    path.write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nwrote {path}")


def _fmt(v) -> str:
    if v is None:
        return "null"
    return f"{v:,.2f}" if isinstance(v, float) and not v.is_integer() else f"{v:,.0f}"


if __name__ == "__main__":
    main()
