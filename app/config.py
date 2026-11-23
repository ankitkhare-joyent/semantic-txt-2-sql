"""Settings for the agent, the eval runner and the MCP server.

Nothing secret lives here. The only bootstrap input is *which AppRole to log in with*
(a persona); every credential is fetched from OpenBao at run time (see app/bao.py).
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Persona = which OpenBao AppRole the agent logs in with. The entity behind it decides
# what the agent may see (Cube access policies + Trino row filters).
PERSONAS = ("agent-admin", "agent-data")


@dataclass(frozen=True)
class Settings:
    persona: str = os.environ.get("SL_PERSONA", "agent-admin")
    bao_addr: str = os.environ.get("BAO_ADDR_HOST", "http://localhost:28200")
    approle_root: Path = Path(os.environ.get("SL_APPROLE_DIR", ROOT / ".secrets" / "approle"))
    cube_url: str = os.environ.get("CUBE_URL", "http://localhost:24000/cubejs-api/v1")
    trino_host: str = os.environ.get("TRINO_HOST", "localhost")
    trino_port: int = int(os.environ.get("TRINO_PORT", "28080"))
    audit_db_host: str = os.environ.get("AUDIT_DB_HOST", "localhost")
    audit_db_port: int = int(os.environ.get("AUDIT_DB_PORT", "25433"))
    model: str = os.environ.get("SL_MODEL", "claude-opus-5-5")
    effort: str = os.environ.get("SL_EFFORT", "medium")
    max_steps: int = int(os.environ.get("SL_MAX_STEPS", "10"))    # hard stop for runaway tool loops
    max_rows: int = int(os.environ.get("SL_MAX_ROWS", "200"))     # rows any one tool call may return
    allowed_tables: tuple = field(default=("lake.ec2.instances", "lake.ref.instance_types"))

    @property
    def approle_dir(self) -> Path:
        if self.persona not in PERSONAS:
            raise SystemExit(f"unknown persona {self.persona!r}; pick one of {PERSONAS}")
        return self.approle_root / self.persona
