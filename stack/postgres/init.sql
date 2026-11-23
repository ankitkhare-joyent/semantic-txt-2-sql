-- Audit store: one row per question the agent answers (what it asked, which path it took,
-- the exact query it ran, how long it took). Runs once, on the first start of the volume.

-- OpenBao's database engine logs in as bao_admin to create and drop short-lived users.
-- This bootstrap password is replaced by OpenBao (`database/rotate-root`) on first setup.
CREATE ROLE bao_admin WITH LOGIN CREATEROLE PASSWORD 'bootstrap-only-rotated-by-openbao';

-- Group role that every dynamic user joins: it can append and read audit rows, nothing more.
CREATE ROLE audit_writer NOLOGIN;
GRANT audit_writer TO bao_admin WITH ADMIN OPTION;

CREATE DATABASE audit OWNER postgres;
\connect audit

CREATE TABLE agent_queries (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    asked_at      timestamptz NOT NULL DEFAULT now(),
    persona       text        NOT NULL,     -- whose identity the agent acted with
    mode          text        NOT NULL,     -- semantic | sql | hybrid
    question      text        NOT NULL,
    tool          text        NOT NULL,     -- query_metrics | run_sql
    query         jsonb       NOT NULL,     -- the Cube query or {"sql": ...}
    generated_sql text,                     -- the SQL that actually ran on Trino
    row_count     integer,
    error         text,
    duration_ms   integer,
    db_user       text        NOT NULL DEFAULT current_user  -- the dynamic OpenBao user that wrote the row
);

GRANT USAGE ON SCHEMA public TO audit_writer;
GRANT SELECT, INSERT ON agent_queries TO audit_writer;
