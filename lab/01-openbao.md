# Lab 01 · OpenBao: sealed storage, policies and the audit log

One sitting, about 30 minutes. Background: [docs/03](../docs/03-openbao.md). Code:
[stack/openbao/bootstrap.sh](../stack/openbao/bootstrap.sh), [server.hcl](../stack/openbao/server.hcl),
[policies/](../stack/openbao/policies).

OpenBao comes first because everything else boots from it: Trino's S3 keys, Cube's settings, the agent's Anthropic
key, its identity token and its database login. After this lab you can explain:

- what "sealed" means, and what the 5 key shares in `.secrets/init.json` are for
- how to find out what a token may do without reading code
- what the audit log records about a secret it never writes down
- which parts of the stack stop working when OpenBao is sealed, and which carry on

Answers to every **Predict** are at the bottom. Write your guess down before you run the command.

## Before you start

```bash
cd ~/workstuff/semantic-txt-2-sql
source lab/env.sh
bao version        # mise puts the bao CLI on PATH in this folder; otherwise: mise exec -- bao version
```

`lab/env.sh` sets `BAO_ADDR` to this lab's OpenBao (port 28200) and `BAO_TOKEN` to the root token from `operator
init`. That's a local shortcut so you can look at everything; in production nobody keeps a root token.

## 1. Is it open?

**Predict (P1):** the bootstrap initialised OpenBao on first start. How many key shares did it make, and how many
does it take to unseal?

```bash
curl -s $BAO_ADDR/v1/sys/health | jq '{initialized, sealed, version}'
curl -s $BAO_ADDR/v1/sys/seal-status | jq '{type, t, n, storage_type}'
```

These two endpoints need no token: health checks and load balancers call them.

## 2. Who am I, and what is mounted?

**Predict (P2):** what does the root token's lookup say about its policies and expiry?

```bash
bao token lookup -format=json | jq '.data | {display_name, policies, ttl, expire_time, orphan}'
```

**Predict (P3):** which secrets engines and auth methods did `bootstrap.sh` enable? Name them before you look.

```bash
bao secrets list
bao auth list
bao audit list -detailed
```

## 3. Least privilege, seen from outside

Log in as the data team's agent (an AppRole) and try to read three secrets. `bao_keys` shows field names and the
version, never values.

**Predict (P4):** which of the three can `agent-data` read?

```bash
T=$(persona_token agent-data)
for s in anthropic cube/config lakehouse/s3; do printf '%-14s' $s; TOKEN=$T bao_keys $s; done
```

You don't have to read the policy files to know why. Ask OpenBao what the token may do on a path:

```bash
BAO_TOKEN=$T bao token capabilities secret/data/lakehouse/s3
BAO_TOKEN=$T bao token capabilities database/creds/audit-writer
BAO_TOKEN=$T bao token lookup -format=json | jq '.data | {display_name, policies, ttl, entity_id, meta}'
bao policy read trino
```

**Predict (P5):** the agent logged in with an AppRole, but Cube later sees it as "the data team". Where does
"data team" come from?

```bash
bao read -format=json identity/entity/name/agent-data-team \
  | jq '.data | {name, metadata, group_ids, aliases: [.aliases[] | {name, mount_type}]}'
cat .secrets/approle/agent-data/role_id; echo
```

## 4. The audit log

Every request is logged, including the read you just did. The log is declared in `server.hcl`, not created over the
API (OpenBao 2 refuses that, so a stolen root token can't switch auditing off).

**Predict (P6):** you read `secret/anthropic` as `agent-data` in step 3. Will the audit log contain your Anthropic
key?

```bash
docker compose exec -T openbao cat /openbao/logs/audit.log | jq -c 'select(.type=="response" and
  .request.path=="secret/data/anthropic" and .auth.display_name=="approle") |
  {time, who: .auth.display_name, policies: .auth.policies, api_key: .response.data.data.api_key}' | tail -1
docker compose exec -T openbao cat /openbao/logs/audit.log | jq -r .request.path | sort | uniq -c | sort -rn | head -5
```

## Break it, then fix it

**Break: seal it.** Sealing throws away the key in memory; the data on disk is unreadable until 3 shares are
supplied again. Mint a Cube token first, so you can see what an already-issued token does.

**Predict (P7):** with OpenBao sealed, which of these still work: (a) a new AppRole login, (b) a new identity token,
(c) Cube accepting the token you minted a minute ago, (d) a Trino query?

```bash
JWT=$(persona_jwt agent-admin)
bao operator seal
curl -s $BAO_ADDR/v1/sys/health | jq -c '{sealed}'
curl -s "$BAO_ADDR/v1/auth/approle/login" -d '{}' | jq -c .                                    # (a)
[ -z "$(persona_jwt agent-admin)" ] && echo "(b) no token"                                     # (b)
curl -s -o /dev/null -w '(c) %{http_code}\n' "$CUBE/meta" -H "Authorization: Bearer $JWT"       # (c)
tsql admin "SELECT 1" | tail -1                                                                 # (d)
```

**Fix: unseal by hand, one share at a time.** In production you'd configure KMS auto-unseal and never do this;
doing it once shows you what auto-unseal automates.

```bash
for i in 0 1 2; do
  bao operator unseal -format=json "$(jq -r ".unseal_keys_b64[$i]" .secrets/init.json)" | jq -c '{sealed, progress, t}'
done
[ -n "$(persona_jwt agent-admin)" ] && echo "identity tokens work again"
```

(`mise run unseal` does the same thing by re-running the idempotent bootstrap.)

## Check your predictions

**P1.** `{"initialized": true, "sealed": false, "version": "2.7.1"}` and
`{"type": "shamir", "t": 3, "n": 5, "storage_type": "raft"}`: five shares, any three rebuild the root key. They all
sit in `.secrets/init.json` here, which defeats the point of splitting them. In production each share goes to a
different person (PGP-encrypted with `-pgp-keys`), or you use KMS auto-unseal and get recovery keys instead.

**P2.** `display_name: "root"`, `policies: ["root"]`, `ttl: 0`, `expire_time: null`, `orphan: true`. A root token
can do anything and never expires. That's why production revokes it after setup and generates a new one
(`bao operator generate-root`, which needs the key holders) only for emergencies.

**P3.** Secrets engines: `secret/` (KV v2), `database/`, plus the built-ins `identity/`, `cubbyhole/` and `sys/`.
Auth methods: `approle/` plus the built-in `token/`. One audit device: `file/`, writing to
`/openbao/logs/audit.log`, with the description from `server.hcl`.

**P4.** Only `anthropic`:

```
anthropic     {"fields":["api_key"],"version":1}
cube/config   {"errors":["1 error occurred:\n\t* permission denied\n\n"]}
lakehouse/s3  {"errors":["1 error occurred:\n\t* permission denied\n\n"]}
```

`token capabilities` answers `deny` for the S3 keys and `read` for `database/creds/audit-writer`. The token lookup
shows `policies: ["default", "sl-agent"]`, a TTL just under 3600 s, an `entity_id`, and
`meta: {"role_name": "agent-data"}`. The `trino` policy is a single `read` on `secret/data/lakehouse/s3`.

**P5.** From the identity store, not from the AppRole. The entity `agent-data-team` has `metadata: {"team": "data"}`
and one group (`sl-team-members`); its alias is an `approle` alias whose `name` is exactly the role ID you printed.
Logging in with that role ID resolves to that entity, and the identity token template
([token-template.json](../stack/openbao/token-template.json)) copies the entity's team and groups into the JWT.
Swap auth methods (Kubernetes, OIDC) and the entity, and so the claims, stay the same.

**P6.** No. The value is HMAC'd:

```
{"time":"…","who":"approle","policies":["default","sl-agent"],"api_key":"hmac-sha256:6041358a…"}
```

The same key always gives the same HMAC (per audit device), so you can tell *that* two requests read the same
value, or check a suspected leaked key with `bao write sys/audit-hash/file input=…`, without the log holding the key.
The busiest paths on a fresh stack are bootstrap and Bao Agent traffic: `secret/data/cube/config`,
`sys/internal/ui/mounts/secret`, `sys/mounts`, `secret/data/lakehouse/s3`, `auth/approle/login`.

**P7.** (a) fails: `{"errors":["Vault is sealed"]}` (OpenBao kept Vault's error strings). (b) fails: no token.
(c) still works: `200`, because Cube downloaded OpenBao's public keys (JWKS) earlier and caches them, and a JWT is
verified offline. (d) still works: Trino read its rendered catalog file at start-up and never talks to OpenBao.
So a sealed OpenBao doesn't take the data path down at once: it stops *new* logins and tokens. Agents fail when
their 15-minute JWT runs out; Bao Agents fail at their next renewal. Alert on `sealed: true`, not on query errors.

Unsealing prints `progress` 1, then 2, then `sealed: false` on the third share.

## What to remember

- Sealed means the data is there but the key isn't. Every restart starts sealed; auto-unseal fixes that in production.
- `bao token capabilities` and `bao token lookup` answer "what can this token do" from the outside.
- Identity (entity, groups, metadata) is separate from authentication (AppRole), and it's what the claims come from.
- The audit log proves who read what without containing the secret.

Next: [Lab 02 · Secret delivery](02-secret-delivery.md).
