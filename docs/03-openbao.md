# 03 · Secrets with OpenBao

## Why OpenBao and not HashiCorp Vault

- **Aug 2023:** HashiCorp moved Vault (and Terraform, Consul, …) from the open-source MPL 2.0
  licence to the **Business Source License 1.1**. BSL lets you use and modify the code but
  forbids offering anything that competes with HashiCorp; each release only turns into
  MPL after four years. The Open Source Initiative doesn't count BSL as open source.
- **Dec 2023:** the community forked Vault 1.14 (the last MPL release) as **OpenBao**,
  under the Linux Foundation (an OpenSSF project), still **MPL 2.0**. It's the same
  relationship as OpenTofu to Terraform.
- **Feb 2025:** IBM completed its acquisition of HashiCorp; the BSL licensor is now IBM.
- **2026:** OpenBao 2.6–2.7 (this repo pins 2.7.1, released 2026-10-01) has diverged
  with its own features: declarative audit devices, namespaces, horizontal read
  scalability, transactional storage, external KMS keys for Transit/PKI.

**What stayed the same is the HTTP API.** Vault clients and tools work against OpenBao
unchanged; this repo uses `hvac`, the Vault Python client. The CLI is `bao` instead of
`vault`, and the env vars are `BAO_ADDR` / `BAO_TOKEN` (`VAULT_*` also work).
Everything you learn here applies to Vault too.

So the answer to "is Vault open source?" is: not any more, but OpenBao is, and it's the
same design.

## The mental model

OpenBao is a **broker of short-lived access**. Apps prove who they are, get a token, and
exchange it for secrets. Some secrets are stored (KV), but the most valuable ones are
**generated on demand** and expire on their own.

```
               ┌──────────────── OpenBao ──────────────────┐
  who are you? │ auth methods   AppRole · JWT/OIDC · K8s · AWS IAM · userpass
               │      │  ──▶ token (TTL, policies)
  what may you │ policies       path-based ACLs, deny by default
  do?          │      │
  give me X    │ secrets engines  KV v2 (stored) · database (generated) ·
               │                  identity (signed JWTs) · transit (encrypt) · PKI
               │ audit log      every request, secret values HMAC'd
               └─ storage: Raft, encrypted with a key that only exists while UNSEALED ─┘
```

### Seal and unseal

Everything OpenBao stores is encrypted. The key that decrypts it is itself encrypted by
the **root key**, which OpenBao never writes to disk. At `operator init` the root key is
split with **Shamir's secret sharing** into 5 shares, any 3 of which rebuild it. Every
restart starts **sealed**: the data is there but unreadable until 3 shares are supplied.

Here, [bootstrap.sh](../stack/openbao/bootstrap.sh) holds the shares in `.secrets/init.json`
and unseals on every `up`. That's a local shortcut. **In production** you configure
**auto-unseal** (`seal "awskms"`, GCP KMS, Azure Key Vault, an HSM, or OpenBao 2.x's
static/transit seals), so a restarted pod unseals itself and no human holds shares.
`operator init` then gives *recovery* keys for emergencies only.

### Auth: how a machine proves who it is

This repo uses **AppRole**: `role_id` (like a username, not secret) + `secret_id` (like a
password) → a 1-hour token. Its weakness is the "secret zero" problem: something has to
deliver the `secret_id`. In production you avoid that by using the platform's identity:

| Where it runs | Auth method | What proves identity |
|---|---|---|
| Kubernetes (EKS) | `kubernetes` or `jwt` | the pod's service account token |
| EC2 / Lambda / ECS | `aws` | a signed STS GetCallerIdentity request (IAM role) |
| GitHub Actions | `jwt` | the workflow's OIDC token |
| Humans | `oidc` | your IdP login (Okta, Entra, Google) |

### Policies

Deny by default; grant capabilities per path. Read the three in
[stack/openbao/policies](../stack/openbao/policies): Trino can read one secret, Cube can
read its own config, the agent can read the LLM key, mint its identity token and lease DB
credentials. Nobody but the bootstrap can read the S3 keys *and* the Anthropic key.

## The four ways this lab uses OpenBao

### 1. Static secrets in KV v2, rendered by Bao Agent

`secret/lakehouse/s3` (S3 keys) and `secret/cube/config` (JWT audience, SQL API
password, API secret). Trino and Cube never talk to OpenBao. A **Bao Agent** sidecar
next to each one logs in with that service's AppRole, renders a template into a shared
volume, and keeps its token renewed:

```
bao-agent-trino ──AppRole──▶ OpenBao ──▶ secret/lakehouse/s3
        └─ renders /etc/trino/catalog/lake.properties  (Trino reads it at start)
bao-agent-cube  ──AppRole──▶ OpenBao ──▶ secret/cube/config
        └─ renders cube.env  (Cube's entrypoint sources it)
```

This is the pattern for apps you can't change. In Kubernetes it's the same agent as an
injector sidecar, or the External Secrets / OpenBao operators syncing into K8s Secrets.

KV v2 keeps versions: `bao kv get -version=1 secret/cube/config`, `bao kv rollback`.

### 2. Dynamic database credentials

The agent's audit log in Postgres ([app/audit.py](../app/audit.py)) has **no password
anywhere**. On first use the agent asks for `database/creds/audit-writer`. OpenBao logs in
to Postgres as `bao_admin`, runs the role's `CREATE ROLE ... VALID UNTIL ...` with a random
name and password, and returns them with a **1-hour lease**. When the lease is revoked
(the agent does it on exit) or expires, OpenBao drops the Postgres user.

During bootstrap OpenBao also **rotated `bao_admin`'s password** (`database/rotate-root`),
so after setup no human knows the admin password either. Try it:

```bash
export BAO_ADDR=http://localhost:28200 BAO_TOKEN=$(cat .secrets/root-token)
bao read database/creds/audit-writer          # a new user every time
bao list sys/leases/lookup/database/creds/audit-writer
docker compose exec audit-db psql -U postgres -d audit -c '\du'   # see the v-approle-... users
```

### 3. Identity tokens: OpenBao as an OIDC provider

This is the most "production" idea in the lab. Instead of sharing Cube's JWT signing
secret with the agent (then anyone with the agent's config could mint a token claiming to
be anyone), **OpenBao signs the token**:

1. The agent logs in with its AppRole. That login maps to an **entity**
   (`agent-data-team`, metadata `team=data`, member of group `sl-team-members`).
2. It calls `identity/oidc/token/semantic-layer` and gets an RS256 JWT, valid 15 minutes,
   with claims from [token-template.json](../stack/openbao/token-template.json):
   `{"persona": "agent-data-team", "team": "data", "groups": ["sl-team-members"], "aud": ..., "iss": ...}`
3. Cube fetches OpenBao's **public** keys from `/v1/identity/oidc/.well-known/keys`
   (`CUBEJS_JWK_URL`), verifies signature, issuer and audience, and hands the claims to
   the access policy in [fleet.yml](../stack/cube/model/views/fleet.yml) as the
   security context.

The agent holds no key that can sign anything. Rotate the signing key
(`bao write -f identity/oidc/key/semantic-layer/rotate`) and nothing else changes.
Trino can verify the same token (`http-server.authentication.type=JWT` with a JWKS URL,
over TLS), which is how you'd close the "Trino trusts the user name" gap in production.

### 4. The Anthropic key

`secret/anthropic` in KV. The agent reads it at start-up
([app/agent.py](../app/agent.py), `Runtime`). Your `.env` is only the hand-over point:
bootstrap writes it into OpenBao, and nothing reads the `.env` at run time.

### Secrets across teams

The S3 keys belong to the learn-ai-agent team, in *their* OpenBao. You handed them over
once (README step 2). That's how it often works in reality, and it's the weakest link
here. Better options, in order of preference:

1. **Credential vending**: the Iceberg REST catalog (Lakekeeper, Glue, Polaris) issues
   short-lived, table-scoped S3 credentials with each table load, so consumers never hold
   storage keys. Trino supports it (`iceberg.rest-catalog.vended-credentials-enabled`).
2. A **dedicated read-only identity** per consuming team, issued by the producing team.
3. One shared OpenBao (or IdP) both teams trust, with policies per team.

## The audit log

Declared in [server.hcl](../stack/openbao/server.hcl): OpenBao 2.x refuses to create audit
devices over the API, so a stolen root token can't quietly turn auditing off.

```bash
docker compose exec openbao tail -n 3 /openbao/logs/audit.log | jq '.request.path, .auth.display_name'
```

Values are HMAC'd: you can check whether two requests read the *same* secret without the
log containing the secret.

## Production checklist for OpenBao

- TLS everywhere; 3 or 5 nodes with Raft; auto-unseal with KMS/HSM.
- Platform auth (Kubernetes/AWS/JWT) instead of AppRole where possible; response-wrapped,
  single-use `secret_id`s where AppRole is unavoidable.
- Revoke the root token after setup (`bao token revoke -self`); generate one only for
  emergencies with `operator generate-root`.
- Short TTLs, leases on everything that supports them, alerts on lease count growth.
- Ship the audit log to your SIEM; alert on `sys/` and policy changes.
- Manage mounts, policies and roles as code (OpenTofu's Vault provider works with OpenBao).

Next: [04 · Running the stack](04-the-stack.md)
