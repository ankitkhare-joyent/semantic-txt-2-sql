#!/bin/sh
# Initialise, unseal and configure OpenBao. Safe to run on every `docker compose up`:
# every step checks the current state first.
#
# Read docs/03-openbao.md alongside this file; each block below is one concept.
set -eu

export BAO_ADDR="${BAO_ADDR:-http://openbao:8200}"
S=/secrets                      # bind-mounted ./.secrets (git-ignored)
P=/bootstrap/policies
mkdir -p "$S/approle"
umask 077                        # every file this script writes is owner-only

log() { echo "[bao-bootstrap] $*"; }

# ---------------------------------------------------------------- 0. wait for the API
# `bao status` exits 0 = unsealed, 2 = sealed, 1 = unreachable.
for _ in $(seq 60); do
  rc=0; bao status >/dev/null 2>&1 || rc=$?
  [ "$rc" -ne 1 ] && break
  sleep 1
done

# ---------------------------------------------------------------- 1. initialise (first run only)
# `operator init` creates the root key and splits it into 5 Shamir shares, any 3 of which
# rebuild it. In production each share is PGP-encrypted to a different person
# (-pgp-keys=...) or, with auto-unseal, there are no shares at all, only recovery keys.
if bao status -format=json | grep -q '"initialized": false'; then
  log "initialising (5 key shares, threshold 3)"
  bao operator init -key-shares=5 -key-threshold=3 -format=json > "$S/init.json"
fi

# The root token is for this bootstrap and for you to explore with `bao`. Production:
# revoke it after setup (`bao token revoke -self`) and generate one only for emergencies.
awk -F'"' '/"root_token"/ {print $4}' "$S/init.json" > "$S/root-token"

# ---------------------------------------------------------------- 2. unseal
# A sealed OpenBao has its data encrypted at rest and knows nothing. Every restart starts
# sealed. Feed it 3 of the 5 shares and it reconstructs the key in memory.
if bao status -format=json | grep -q '"sealed": true'; then
  log "unsealing"
  awk '/"unseal_keys_b64"/ {on=1; next} on && /\]/ {on=0} on {gsub(/[",[:space:]]/, ""); print}' "$S/init.json" \
    | head -3 | while read -r key; do bao operator unseal "$key" >/dev/null; done
fi

export BAO_TOKEN="$(cat "$S/root-token")"
# Raft elects itself leader a moment after unseal.
for _ in $(seq 30); do bao secrets list >/dev/null 2>&1 && break; sleep 1; done

# ---------------------------------------------------------------- 3. audit log
# Declared in server.hcl (OpenBao refuses API-created audit devices), so it is already on
# and this setup itself is audited. Read it: docker compose exec openbao tail /openbao/logs/audit.log

# ---------------------------------------------------------------- 4. KV v2: static secrets
bao secrets list | grep -q '^secret/' || bao secrets enable -path=secret -version=2 kv

# Object-store keys of the sibling lakehouse. That team keeps them in *its* OpenBao; you
# hand them over once via .env and they live in ours from then on. Trino's Bao Agent
# renders them into Trino's catalog file; no app reads them directly.
# (Production: the producing team issues each consumer its own read-only identity, or the
# catalog vends short-lived storage credentials per table and nobody copies keys at all.)
if [ -n "${LAKE_S3_ACCESS_KEY:-}" ] && [ -n "${LAKE_S3_SECRET_KEY:-}" ]; then
  bao kv put -mount=secret lakehouse/s3 access_key="$LAKE_S3_ACCESS_KEY" secret_key="$LAKE_S3_SECRET_KEY" >/dev/null
elif ! bao kv get -mount=secret lakehouse/s3 >/dev/null 2>&1; then
  log "LAKE_S3_ACCESS_KEY / LAKE_S3_SECRET_KEY missing from .env (see README step 2)"; exit 1
fi

# The Anthropic key: written only if you supplied one in .env, never echoed.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  bao kv put -mount=secret anthropic api_key="$ANTHROPIC_API_KEY" >/dev/null
  log "anthropic key stored at secret/anthropic"
else
  bao kv get -mount=secret anthropic >/dev/null 2>&1 || log "no ANTHROPIC_API_KEY in .env yet; agent calls will fail until you add it and re-run"
fi

# ---------------------------------------------------------------- 5. policies (least privilege)
for f in "$P"/*.hcl; do bao policy write "$(basename "$f" .hcl)" "$f" >/dev/null; done

# ---------------------------------------------------------------- 6. AppRole: machine login
# role_id ~ username (not secret), secret_id ~ password. A Bao Agent or app trades both for
# a short-lived token. Production: deliver secret_id response-wrapped and single-use from
# your CI/orchestrator, or skip AppRole and use the platform's identity (Kubernetes,
# AWS IAM, JWT/OIDC auth) so there is no "secret zero" at all.
bao auth list | grep -q '^approle/' || bao auth enable approle
# secret_ids are leases too, capped by the mount's max TTL (24h server-wide in server.hcl). Let this mount's last
# 30 days so a secret_id_ttl of 720h means what it says; tokens stay capped at 24h by each role's token_max_ttl.
bao auth tune -max-lease-ttl=720h approle/ >/dev/null

approle() {  # name policies
  bao write "auth/approle/role/$1" token_policies="$2" token_ttl=1h token_max_ttl=24h \
    secret_id_ttl=720h token_type=service >/dev/null
  mkdir -p "$S/approle/$1"
  bao read -field=role_id "auth/approle/role/$1/role-id" > "$S/approle/$1/role_id"
  # Mint a secret_id when there is none, or the one on disk no longer works (expired after secret_id_ttl, or
  # OpenBao was reset under an old .secrets/ folder). Lookup prints nothing for an unknown secret_id.
  if [ ! -s "$S/approle/$1/secret_id" ] || ! bao write -format=json "auth/approle/role/$1/secret-id/lookup" \
       secret_id="$(cat "$S/approle/$1/secret_id")" 2>/dev/null | grep -q '"creation_time"'; then
    bao write -f -field=secret_id "auth/approle/role/$1/secret-id" > "$S/approle/$1/secret_id"
  fi
}
approle trino trino
approle cube cube
approle agent-admin sl-agent
approle agent-data sl-agent

# ---------------------------------------------------------------- 7. identity: who is calling
# An entity is "one caller" across auth methods. The two agent personas get entities with
# metadata and group membership; the identity token below copies those into JWT claims,
# and Cube turns the claims into row-level security.
accessor=$(bao read -field=accessor sys/auth/approle)

persona() {  # approle-name entity-name team group
  bao write identity/entity/name/"$2" metadata=team="$3" >/dev/null
  eid=$(bao read -field=id identity/entity/name/"$2")
  rid=$(cat "$S/approle/$1/role_id")
  # An AppRole login is identified by its role_id; bind that alias to the entity.
  bao write identity/entity-alias name="$rid" canonical_id="$eid" mount_accessor="$accessor" >/dev/null 2>&1 || true
  echo "$eid"
}
admin_id=$(persona agent-admin agent-platform-admin platform)
data_id=$(persona agent-data agent-data-team data)
bao write identity/group/name/sl-admins member_entity_ids="$admin_id" >/dev/null
bao write identity/group/name/sl-team-members member_entity_ids="$data_id" >/dev/null

# ---------------------------------------------------------------- 8. identity tokens (OIDC)
# OpenBao signs JWTs with a rotating RSA key and publishes the public half at
# /v1/identity/oidc/.well-known/keys. Cube fetches that JWKS to verify tokens. The issuer
# defaults to api_addr + /v1/identity/oidc, i.e. http://openbao:8200/v1/identity/oidc.
bao write identity/oidc/key/semantic-layer allowed_client_ids="*" algorithm=RS256 \
  rotation_period=24h verification_ttl=24h >/dev/null
bao write identity/oidc/role/semantic-layer key=semantic-layer ttl=15m \
  template=@/bootstrap/token-template.json >/dev/null
audience=$(bao read -field=client_id identity/oidc/role/semantic-layer)

# Cube's settings. Its SQL API password and API secret are generated once, here, and never
# typed by a human. (The API secret is unused while JWKS auth is on, but Cube requires one.)
rand() { head -c 32 /dev/urandom | base64 | tr -d '/+=\n'; }
if ! bao kv get -mount=secret cube/config >/dev/null 2>&1; then
  bao kv put -mount=secret cube/config sql_password="$(rand)" api_secret="$(rand)" >/dev/null
fi
bao kv patch -mount=secret cube/config jwt_audience="$audience" \
  jwt_issuer="http://openbao:8200/v1/identity/oidc" >/dev/null

# ---------------------------------------------------------------- 9. dynamic database credentials
# The database engine holds an admin login to Postgres and creates a *new* Postgres user
# for every credential request, dropping it when the lease expires or is revoked.
bao secrets list | grep -q '^database/' || bao secrets enable database
if ! bao read database/config/audit >/dev/null 2>&1; then
  bao write database/config/audit plugin_name=postgresql-database-plugin \
    connection_url="postgresql://{{username}}:{{password}}@audit-db:5432/audit?sslmode=disable" \
    username=bao_admin password="$AUDIT_DB_BOOTSTRAP_PASSWORD" allowed_roles=audit-writer >/dev/null
  # Rotate the admin password so that from now on only OpenBao knows it.
  bao write -f database/rotate-root/audit >/dev/null
  log "database engine configured; bao_admin password rotated (only OpenBao knows it now)"
fi
bao write database/roles/audit-writer db_name=audit default_ttl=1h max_ttl=24h \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE audit_writer;" \
  revocation_statements="DROP ROLE IF EXISTS \"{{name}}\";" >/dev/null   # dynamic users own nothing, so a plain drop works

log "done. UI: http://localhost:28200/ui (token in .secrets/root-token)"
