# Lab 05 · Identity tokens and row-level security

> **Changes things:** rotates OpenBao's token-signing key, and removes the data team's agent from its group for a
> minute (the break). `mise run unseal` puts the group back.

One sitting, about 35 minutes. Background: [docs/03 §3](../docs/03-openbao.md#3-identity-tokens-openbao-as-an-oidc-provider),
[docs/05](../docs/05-cube-model.md#access-policies-row--and-member-level-security). Config:
[token-template.json](../stack/openbao/token-template.json), [cube.py](../stack/cube/cube.py), the `access_policy` in
[views/fleet.yml](../stack/cube/model/views/fleet.yml), `CUBEJS_JWK_URL` in [compose.yaml](../compose.yaml).

Cube never sees a password or an API key from the agent. It sees a JWT that OpenBao signed, checks the signature
with OpenBao's *public* keys, and turns the claims into a row filter. After this lab you can explain:

- what's in the token, and where each claim comes from
- how Cube verifies a token it never issued, and why a key rotation doesn't break anyone
- how the same question returns different rows for different callers
- what a tampered token and a caller with no group look like

## Before you start

```bash
source lab/env.sh
```

## 1. Take a token apart

```bash
J=$(persona_jwt agent-data)
jwt_claims "$J"
python3 -c 'import base64, json, sys; h = sys.argv[1].split(".")[0]; print(json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4))))' "$J"
```

**Predict (P1):** which claims came from the token template, which from OpenBao itself, and what is `aud`? Compare
with what Cube was told to expect (field names only matter here; neither value is secret):

```bash
cat stack/openbao/token-template.json
bao kv get -format=json secret/cube/config | jq -c '.data.data | {jwt_audience, jwt_issuer}'
bao read -format=json identity/oidc/role/semantic-layer | jq -c '.data | {key, ttl, client_id}'
```

## 2. How Cube checks it

OpenBao publishes its public keys like any OIDC provider. Cube's `CUBEJS_JWK_URL` points here:

```bash
curl -s $BAO_ADDR/v1/identity/oidc/.well-known/openid-configuration | jq -c '{issuer, jwks_uri}'
curl -s $BAO_ADDR/v1/identity/oidc/.well-known/keys | jq -c '[.keys[] | {kid, alg}]'
```

**Predict (P2):** the token's header names one `kid`. Why are there two keys in the JWKS?

**Predict (P3):** rotate the signing key now. Does the token you minted a minute ago still work? Does a new one,
signed with a key Cube may never have downloaded?

```bash
kid() { python3 -c 'import base64, json, sys; h = sys.argv[1].split(".")[0]; print(json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4)))["kid"])' "$1"; }
OLD=$(persona_jwt agent-data); echo "old kid $(kid $OLD)"
bao write -f identity/oidc/key/semantic-layer/rotate
curl -s $BAO_ADDR/v1/identity/oidc/.well-known/keys | jq -c '[.keys[].kid]'
NEW=$(persona_jwt agent-data); echo "new kid $(kid $NEW)"
curl -s -o /dev/null -w 'old token: %{http_code}\n' "$CUBE/meta" -H "Authorization: Bearer $OLD"
curl -s -o /dev/null -w 'new token: %{http_code}\n' "$CUBE/meta" -H "Authorization: Bearer $NEW"
```

## 3. Same question, different callers

**Predict (P4):** running instances per team, right now, as the admin and as the data team. And if the data team
asks for payments by name?

```bash
Q='{"measures":["fleet.running_instances"],"dimensions":["fleet.team"],"segments":["fleet.latest_snapshot"]}'
cube_load agent-admin "$Q" | jq -c '[.data[] | [.["fleet.team"], .["fleet.running_instances"]]]'
cube_load agent-data  "$Q" | jq -c '[.data[] | [.["fleet.team"], .["fleet.running_instances"]]]'
cube_load agent-data '{"measures":["fleet.running_instances"],"segments":["fleet.latest_snapshot"],
  "filters":[{"member":"fleet.team","operator":"equals","values":["payments"]}]}' | jq -c .data
```

Where the filter goes in the SQL, and where its value comes from:

```bash
cube_sql agent-admin "$Q"
cube_sql agent-data "$Q"
curl -s -G "$CUBE/sql" -H "Authorization: Bearer $(persona_jwt agent-data)" --data-urlencode "query=$Q" | jq -c '.sql.sql[1]'
```

## Break it, then fix it

**Break 1: forge a claim.** Change `team` in a data-team token to `payments`, keep the signature.

```bash
J=$(persona_jwt agent-data); IFS=. read -r h p s <<<"$J"
P2=$(python3 -c 'import base64, json, sys; p = sys.argv[1]; d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)));
d["team"] = "payments"; print(base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("="))' "$p")
curl -s "$CUBE/meta" -H "Authorization: Bearer $h.$P2.$s" -w '  %{http_code}\n'
curl -s "$CUBE/meta" -w '  %{http_code}\n'
```

**Predict (P5):** what does Cube say to each?

**Break 2: a caller in no group.** Take the data team's entity out of `sl-team-members` (as an admin would by
mistake when cleaning up groups).

```bash
bao write identity/group/name/sl-team-members member_entity_ids=""
J=$(persona_jwt agent-data); jwt_claims "$J" | jq -c '{persona, team, groups}'
JWT=$J cube_load '' '{"measures":["fleet.running_instances"],"segments":["fleet.latest_snapshot"]}' | jq -r '.error // .data' | cut -c1-140
curl -s "$CUBE/meta" -H "Authorization: Bearer $J" | jq -c '[.cubes[].name]'
```

**Predict (P6):** the token is valid and signed. What does Cube do with a caller that matches no access policy?

**Fix:** the bootstrap is idempotent and declares the group membership, so re-running it repairs drift:

```bash
mise run unseal
jwt_claims "$(persona_jwt agent-data)" | jq -c '{groups}'
```

## Check your predictions

**P1.** From the template: `persona` (`agent-data-team`), `team` (`data`) and `groups` (`["sl-team-members"]`).
From OpenBao: `iss` (`http://openbao:8200/v1/identity/oidc`), `sub` (the entity ID), `aud`, `iat`, `exp`
(15 minutes after `iat`: the role's `ttl`) and `namespace`. `aud` is the role's `client_id`, a random string
OpenBao generated; the bootstrap copied it into `secret/cube/config` as `jwt_audience`, and Bao Agent rendered it
into Cube's `CUBEJS_JWT_AUDIENCE`. A token minted for some other role would have a different `aud` and Cube would
refuse it. The header is `{'alg': 'RS256', 'kid': '…'}`.

**P2.** The current key and the *next* one. OpenBao publishes the next signing key before it starts using it, so
every verifier that caches the JWKS already has it when the rotation happens.

**P3.** Both work: `old token: 200`, `new token: 200`. After the rotation the JWKS has three keys: the old one
(kept for `verification_ttl`, 24 h, so tokens it signed stay valid until they expire), the new current one (which was
the "next" key a moment ago, so Cube had it cached), and a new "next". The new token's `kid` is the key that was
second in the list before. Zero-downtime rotation, by design.

**P4.** Admin: `[["platform","284"],["payments","185"],["search","161"],["devex","79"],["data","68"],["ml","47"],
["logistics","21"]]` (845 in total; finance has nothing running any more). Data team: `[["data","68"]]`. Asking for
payments by name returns `[{"fleet.running_instances":"0"}]`: the access policy's filter is ANDed with yours. In the
SQL, the admin's query has only the snapshot condition; the data team's has `WHERE ("instance_snapshots".team = ?)`
added, and the parameter list is `["data"]`, taken from the token's `team` claim. A bound parameter, not string
concatenation, so a claim can't inject SQL either.

**P5.** The forged token: `{"error":"Invalid token"}  403`; the signature no longer matches the payload. No token:
`{"error":"Authorization header isn't set"}  403`.

**P6.** The new token has `"groups": null`. The query fails with `Error: You requested hidden member:
'fleet.running_instances'…`, and `/meta` lists no cubes at all: `[]`. Access policies deny by default, so a caller
in no group sees nothing, not everything. After `mise run unseal` the claim is back to `["sl-team-members"]`.

## What to remember

- A JWT is a signed claim set: anyone can read it, nobody can change it without the private key.
- JWKS lets any number of services verify tokens without holding a secret; pre-published keys make rotation painless.
- Row-level security comes from claims, as bound parameters, ANDed with the caller's own filters.
- Fail closed: a caller who matches no policy should see nothing, and here does.

Next: [Lab 06 · Guardrails and tools, without the model](06-guardrails-and-tools.md).
