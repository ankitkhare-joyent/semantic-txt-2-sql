# The text-to-SQL agent (one AppRole per persona, same policy).
# Least privilege: it can read the LLM key, mint an identity token for Cube,
# and lease short-lived Postgres credentials for its audit log. It cannot read
# the object-store keys or any other app's secrets.

path "secret/data/anthropic" {
  capabilities = ["read"]
}

# Mint a signed JWT (OIDC identity token) describing *this* entity. Cube verifies it
# against OpenBao's public JWKS, so the agent never holds Cube's signing key.
path "identity/oidc/token/semantic-layer" {
  capabilities = ["read"]
}

# Dynamic database credentials: every read creates a brand-new Postgres user with a 1h lease.
path "database/creds/audit-writer" {
  capabilities = ["read"]
}

# Renew and revoke the leases it holds (renew-self / lookup-self come from the default policy).
path "sys/leases/renew" {
  capabilities = ["update"]
}
path "sys/leases/revoke" {
  capabilities = ["update"]
}
