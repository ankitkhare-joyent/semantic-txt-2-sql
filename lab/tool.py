"""Call one agent tool by hand, exactly as the agent's loop would, without the model.

    mise run -q lab:tool -- run_sql '{"sql": "SELECT count(*) FROM lake.ec2.instances"}'
    mise run -q lab:tool -- --persona agent-data query_metrics '{"measures": ["fleet.instances"]}'
    mise run -q lab:tool -- --guard "DELETE FROM lake.ec2.instances"      # sqlguard only, nothing runs

Prints what the model would see (the tool result and whether it is an error) and what goes to the audit log.
Nothing here calls Claude. It doesn't write to the audit table either.
"""
import argparse
import json

from app.bao import OpenBao, jwt_claims
from app.config import Settings
from app.rawsql import TrinoRunner
from app.semantic import CubeClient
from app.sqlguard import UnsafeSQL, check
from app.tools import Toolbox


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--persona", default="agent-admin", choices=["agent-admin", "agent-data"])
    p.add_argument("--guard", metavar="SQL", help="only run sqlguard.check on this SQL")
    p.add_argument("tool", nargs="?", choices=["query_metrics", "dimension_values", "run_sql"])
    p.add_argument("args", nargs="?", default="{}", help="the tool input, as JSON")
    a = p.parse_args()
    s = Settings(persona=a.persona)

    if a.guard:
        try:
            c = check(a.guard, s.allowed_tables, s.max_rows)
            print(json.dumps({"allowed": True, "tables": c.tables, "will_run": c.sql}, indent=2))
        except UnsafeSQL as e:
            print(json.dumps({"allowed": False, "message_to_model": str(e)}, indent=2))
        return

    bao = OpenBao(s.bao_addr, s.approle_dir)
    persona = jwt_claims(bao.identity_token())["persona"]
    tools = Toolbox("hybrid", CubeClient(s.cube_url, bao.identity_token),
                    TrinoRunner(s.trino_host, s.trino_port, persona, s.allowed_tables, s.max_rows), s.max_rows)
    out = tools.execute(a.tool, json.loads(a.args))
    content = out.content
    try:
        content = json.loads(content)
    except ValueError:
        pass
    print(json.dumps({"acting_as": persona, "is_error": out.is_error, "model_sees": content,
                      "audit": {k: v for k, v in out.audit.items() if k != "query"}}, indent=2, default=str))


if __name__ == "__main__":
    main()
