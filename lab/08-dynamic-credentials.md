# Lab 08 · The audit trail and dynamic database credentials

> **Changes things:** creates and drops short-lived Postgres users, and inserts one test row into `agent_queries`.

One sitting, about 30 minutes. Background: [docs/03 §2](../docs/03-openbao.md#2-dynamic-database-credentials).
Code: [app/audit.py](../app/audit.py), [app/bao.py](../app/bao.py) (`database_creds`, `revoke`),
[stack/postgres/init.sql](../stack/postgres/init.sql), section 9 of [bootstrap.sh](../stack/openbao/bootstrap.sh).

Every tool call the agent made in Lab 07 was written to Postgres by a user that didn't exist a few seconds earlier
and was dropped when the agent exited. After this lab you can explain:

- how OpenBao makes a database user on demand, and what limits it
- what a lease is, and the difference between it expiring and being revoked
- why nobody, not even you with the root token, knows the admin password any more
- how to read the audit trail to answer "what did the agent actually run?"

## Before you start

```bash
source lab/env.sh
audit "select count(*) as rows, count(distinct db_user) as writers from agent_queries"   # rows from Lab 07
```

## 1. Read the audit trail

**Predict (P1):** for each question you asked in Lab 07, the table holds one row per *tool call*, not per question.
Which column tells you which mode it ran in, and which one holds the SQL that Trino really executed?

```bash
audit "select id, persona, mode, tool, row_count, duration_ms, left(question, 40) as question
       from agent_queries order by id"
audit "select tool, left(generated_sql, 120) as sql from agent_queries where generated_sql is not null order by id limit 3"
audit "select db_user, count(*), min(asked_at), max(asked_at) from agent_queries group by 1"
```

## 2. How the user is made

```bash
bao read -format=json database/config/audit | jq -c '.data | {plugin_name, allowed_roles,
  connection_url: .connection_details.connection_url, username: .connection_details.username}'
bao read -format=json database/roles/audit-writer | jq -c '.data | {default_ttl, max_ttl, creation_statements}'
```

**Predict (P2):** ask for credentials as the data team's agent. How long will they last, and what will the Postgres
user be called?

```bash
C=$(BAO_TOKEN=$(persona_token agent-data) bao read -format=json database/creds/audit-writer)
jq -c '{lease_id, lease_duration, renewable, username: .data.username}' <<<"$C"
U=$(jq -r .data.username <<<"$C"); P=$(jq -r .data.password <<<"$C"); L=$(jq -r .lease_id <<<"$C")
audit "select rolname, rolvaliduntil, rolcanlogin from pg_roles where rolname like 'v-%'"
bao list sys/leases/lookup/database/creds/audit-writer
```

## 3. What the user may do

**Predict (P3):** the role joins `audit_writer` (see `init.sql`). Which of these succeed: reading the table,
inserting a row, deleting rows, creating a table?

```bash
pg_as "$U" "$P" "select current_user, count(*) from agent_queries"
pg_as "$U" "$P" "insert into agent_queries (persona, mode, question, tool, query)
                 values ('lab', 'semantic', 'hello from lab 08', 'none', '{}') returning id, db_user"
pg_as "$U" "$P" "delete from agent_queries"
pg_as "$U" "$P" "create table x (a int)"
```

## 4. Revoke it

**Predict (P4):** you revoke the lease. What happens to the Postgres user, and to the row it inserted?

```bash
bao lease revoke "$L"
pg_as "$U" "$P" "select 1"
audit "select count(*) as dynamic_users from pg_roles where rolname like 'v-%'"
audit "select id, db_user from agent_queries where persona = 'lab'"
```

`app/audit.py` does exactly this in `close()`: it revokes its lease when the agent exits, instead of leaving the
user alive for the rest of the hour.

## Break it, then fix it

**Break: log in as the admin with the password from the repo.** `compose.yaml` and `init.sql` contain
`bootstrap-only-rotated-by-openbao`, the password `bao_admin` was created with.

**Predict (P5):** does it still work? Try it twice: once the way you'd naively test it inside the container, once
over the network the way OpenBao connects.

```bash
docker compose exec -T -e PGPASSWORD=bootstrap-only-rotated-by-openbao audit-db \
  psql -h 127.0.0.1 -U bao_admin -d audit -Atc 'select 1'
pg_as bao_admin bootstrap-only-rotated-by-openbao 'select 1' | tail -1
docker compose exec -T audit-db sh -c 'grep -v "^#" /var/lib/postgresql/*/docker/pg_hba.conf | grep .'
```

**Fix (and the lesson):** nothing to repair. The admin password was rotated by `database/rotate-root/audit` in the
bootstrap; only OpenBao knows the new one. If you ever need admin access, you don't "find the password": you use the
superuser through `docker compose exec` (local only) or, in production, a break-glass procedure. And when you test
whether a password works, test it the way the real client connects.

## Check your predictions

**P1.** `mode` is `semantic`, `sql` or `hybrid`; `generated_sql` is the SQL that ran on Trino (for semantic calls
it's the SQL Cube compiled, fetched from Cube's `/sql`; for raw SQL it's the query after `sqlguard` added the
`LIMIT`). `query` holds what the model sent (the Cube JSON or the original SQL). Each `ask` run has its own
`db_user`: every run got a fresh Postgres user.

**P2.** `lease_duration: 3600` (the role's `default_ttl`, 1 hour; renewable up to `max_ttl` 24 hours). The name
looks like `v-approle-audit-wr-lpIH6VFAl3jPGcRvK91I-1791723882`: `v-`, the auth method that asked (`approle`), a
truncated role name, random characters and a Unix timestamp. `pg_roles` shows it with `rolvaliduntil` an hour
ahead, so even if OpenBao vanished, Postgres itself would refuse the login after that. The lease list shows one ID,
the last part of `lease_id`.

**P3.** Select (`count` 0 on a fresh stack, or the Lab 07 rows) and insert succeed; the insert's `db_user` default is
`current_user`, so the row records who wrote it. Delete fails with `permission denied for table agent_queries`;
create fails with `permission denied for schema public`. An audit writer that can't delete is the point.

**P4.** `All revocation operations queued successfully!`, then the login fails with
`FATAL: role "v-approle-audit-wr-…" does not exist`: OpenBao ran the role's `revocation_statements`
(`DROP ROLE`). The count of `v-%` users is back to 0. The row stays, still naming its writer: audit history outlives
the credential that wrote it.

**P5.** Inside the container it prints `1`, which looks like the rotation failed. Over the network it fails with
`password authentication failed for user "bao_admin"`. The `pg_hba.conf` lines explain it: the official Postgres
image trusts every connection from `127.0.0.1` and the local socket (`trust`, no password asked at all), and only
`host all all all` uses `scram-sha-256`. The rotation worked.

## What to remember

- A dynamic secret is created when asked for and destroyed when its lease ends: leaks expire on their own.
- Revoke early (on exit) rather than waiting for the TTL; `VALID UNTIL` is the backstop if OpenBao is gone.
- Rotate the root credential the engine uses, so it exists in exactly one place.
- Test credentials the way the real client connects; local trust rules lie.

Next: [Lab 09 · Evals and the MCP server](09-evals-and-mcp.md).
