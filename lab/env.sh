# Source this at the start of every lab sitting (bash or zsh), from the repo root:
#
#   source lab/env.sh
#
# It only sets variables and defines shell functions. Nothing here prints a secret unless you ask for it,
# and the functions that return one (bao_field, persona_token, persona_jwt) are meant for $( ... ) only.

export BAO_ADDR="http://localhost:28200"                      # this lab's OpenBao (the lakehouse's is :8200)
export CUBE="http://localhost:24000/cubejs-api/v1"            # Cube REST API
export TRINO="http://localhost:28080"                         # Trino (UI at $TRINO/ui, user admin)
# The root token from `operator init`. A local-only shortcut: in production nobody keeps one (docs/03).
export BAO_TOKEN="$(cat .secrets/root-token 2>/dev/null)"
[ -n "$BAO_TOKEN" ] || echo "lab/env.sh: no .secrets/root-token - run this from the repo root after 'mise run up'" >&2

# OpenBao ---------------------------------------------------------------------------------------------------
# bao_keys lakehouse/s3                 -> a KV secret's field names and version, never the values
bao_keys() {
  curl -s -H "X-Vault-Token: ${TOKEN:-$BAO_TOKEN}" "$BAO_ADDR/v1/secret/data/$1" \
    | jq -c 'if .errors then {errors} else {fields: (.data.data | keys), version: .data.metadata.version} end'
}
# V=$(bao_field cube/config sql_password)   -> one value, for use inside $( ... )
bao_field() {
  curl -s -H "X-Vault-Token: ${TOKEN:-$BAO_TOKEN}" "$BAO_ADDR/v1/secret/data/$1" | jq -r --arg f "$2" '.data.data[$f] // empty'
}
# TOKEN=$(persona_token agent-data)     -> log in with that AppRole (agent-admin | agent-data | trino | cube)
persona_token() {
  local d=".secrets/approle/$1"
  curl -s "$BAO_ADDR/v1/auth/approle/login" \
    -d "{\"role_id\":\"$(cat "$d/role_id")\",\"secret_id\":\"$(cat "$d/secret_id")\"}" | jq -r '.auth.client_token // empty'
}
# JWT=$(persona_jwt agent-data)         -> the OpenBao-signed identity token that persona sends to Cube
persona_jwt() {
  curl -s -H "X-Vault-Token: $(persona_token "$1")" "$BAO_ADDR/v1/identity/oidc/token/semantic-layer" | jq -r '.data.token // empty'
}
# jwt_claims "$JWT"                     -> the token's payload (header.PAYLOAD.signature, base64url-decoded)
jwt_claims() {
  python3 -c 'import base64, json, sys
p = sys.argv[1].split(".")[1]
print(json.dumps(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))), indent=2))' "$1"
}

# Cube ------------------------------------------------------------------------------------------------------
# cube_load agent-admin '{"measures":["fleet.running_instances"],"segments":["fleet.latest_snapshot"]}'
#   -> POST /load as that persona. Cube answers "Continue wait" while a query runs; this polls until it's done.
#   JWT=... cube_load '' '{...}'  sends a token you made yourself instead.
cube_load() {
  local jwt="${JWT:-}"; [ -n "$1" ] && jwt=$(persona_jwt "$1")
  local out
  for _ in $(seq 60); do
    out=$(curl -s "$CUBE/load" -H "Authorization: Bearer $jwt" -H 'Content-Type: application/json' -d "{\"query\":$2}")
    [ "$(jq -r '.error // empty' <<<"$out" 2>/dev/null)" = "Continue wait" ] || break
    sleep 1
  done
  printf '%s\n' "$out"
}
# cube_sql agent-data '{...}'           -> the SQL Cube would run for that query (compiled, not executed)
cube_sql() {
  curl -s -G "$CUBE/sql" -H "Authorization: Bearer $(persona_jwt "$1")" --data-urlencode "query=$2" | jq -r '.sql.sql[0] // .'
}
# cube_meta agent-admin                 -> the catalog (views, members, descriptions) that persona can see
cube_meta() { curl -s "$CUBE/meta" -H "Authorization: Bearer $(persona_jwt "$1")"; }

# Trino and Postgres ----------------------------------------------------------------------------------------
# tsql admin "SELECT count(*) FROM lake.ec2.instances"     -> Trino's CLI inside its container, as that user
tsql() { docker compose exec -T trino trino --user "$1" --output-format ALIGNED --execute "$2"; }
# audit "select count(*) from agent_queries"              -> psql on the audit database, as the superuser
audit() { docker compose exec -T audit-db psql -U postgres -d audit -P pager=off -c "$1"; }
# pg_as USER PASSWORD "SQL"              -> psql over the container network, so the password is really checked
#   (inside the container, connections to 127.0.0.1 are trusted without a password: see Lab 08)
pg_as() { docker compose exec -T -e PGPASSWORD="$2" audit-db psql -h audit-db -U "$1" -d audit -P pager=off -c "$3" 2>&1; }
