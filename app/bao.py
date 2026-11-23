"""OpenBao client for the agent: one login, then everything else is short-lived.

What the agent gets from OpenBao, and why each is shaped the way it is:

  token          AppRole login -> a 1h service token, renewed while the process runs.
  Anthropic key  KV v2 read. Static secret: it rotates only when a human rotates it.
  identity JWT   Signed by OpenBao, 15 min TTL, claims = who this entity is (team,
                 groups). Cube verifies it against OpenBao's public keys, so the agent
                 never holds a key that could mint tokens for someone else.
  DB login       Dynamic secret: OpenBao creates a fresh Postgres user per request and
                 drops it when the lease ends. Leaked creds die on their own.

hvac is the Vault Python client; OpenBao kept Vault's HTTP API, so it works unchanged.
"""
import base64
import json
import logging
import threading
import time
from dataclasses import dataclass

import hvac

log = logging.getLogger("app.bao")


@dataclass
class DbLease:
    username: str
    password: str
    lease_id: str
    expires_at: float

    def expiring(self, margin: float = 300) -> bool:
        return time.time() > self.expires_at - margin


def jwt_claims(token: str) -> dict:
    """Decode a JWT's payload WITHOUT verifying it - for display and routing only.
    Cube does the real verification against OpenBao's JWKS."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


class OpenBao:
    def __init__(self, addr: str, approle_dir):
        self.client = hvac.Client(url=addr, timeout=10)
        self._role_id = (approle_dir / "role_id").read_text().strip()
        self._secret_id = (approle_dir / "secret_id").read_text().strip()
        self._lock = threading.Lock()
        self._token_expires = 0.0
        self._jwt: str | None = None
        self._jwt_expires = 0.0
        self._login()

    # ------------------------------------------------------------------ auth
    def _login(self) -> None:
        auth = self.client.auth.approle.login(role_id=self._role_id, secret_id=self._secret_id)["auth"]
        self._token_expires = time.time() + auth["lease_duration"]
        log.info("logged in to OpenBao", extra={"policies": auth["policies"]})

    def _ensure_token(self) -> None:
        """Renew the token before it expires; log in again once it hits its max TTL."""
        with self._lock:
            if time.time() < self._token_expires - 120:
                return
            try:
                auth = self.client.auth.token.renew_self()["auth"]
                self._token_expires = time.time() + auth["lease_duration"]
                if auth["lease_duration"] < 300:   # near token_max_ttl: renewals stop helping
                    self._login()
            except hvac.exceptions.Forbidden:
                self._login()

    # ------------------------------------------------------------------ secrets
    def kv(self, path: str, key: str) -> str:
        self._ensure_token()
        data = self.client.secrets.kv.v2.read_secret_version(path=path, mount_point="secret",
                                                             raise_on_deleted_version=True)
        return data["data"]["data"][key]

    def identity_token(self, role: str = "semantic-layer") -> str:
        """A signed JWT for this entity, cached until a minute before it expires."""
        self._ensure_token()
        with self._lock:
            if self._jwt is None or time.time() > self._jwt_expires - 60:
                self._jwt = self.client.secrets.identity.generate_signed_id_token(name=role)["data"]["token"]
                self._jwt_expires = jwt_claims(self._jwt)["exp"]
            return self._jwt

    def database_creds(self, role: str) -> DbLease:
        self._ensure_token()
        resp = self.client.secrets.database.generate_credentials(name=role)
        return DbLease(resp["data"]["username"], resp["data"]["password"], resp["lease_id"],
                       time.time() + resp["lease_duration"])

    def revoke(self, lease_id: str) -> None:
        """Give a dynamic credential back early: OpenBao drops the Postgres user now."""
        self._ensure_token()
        self.client.sys.revoke_lease(lease_id=lease_id)
