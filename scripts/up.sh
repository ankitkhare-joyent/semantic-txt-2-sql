#!/usr/bin/env bash
# Start this lab (and the learn-ai-agent lakehouse it reads, if that isn't running).
#   scripts/up.sh          start, wait until every service is healthy
#   scripts/down.sh        stop this lab only (keeps data; the lakehouse keeps running)
set -euo pipefail
cd "$(dirname "$0")/.."
LAKE=../learn-ai-agent

if ! docker ps --format '{{.Names}}' | grep -q '^ec2-lakehouse-lakekeeper-1$'; then
  echo "lakehouse is not running: starting $LAKE (seed it once with its README if ec2.instances is empty)"
  (cd "$LAKE" && docker compose up -d --wait)
fi

for v in ANTHROPIC_API_KEY LAKE_S3_ACCESS_KEY LAKE_S3_SECRET_KEY; do
  grep -q "^$v=." .env 2>/dev/null || echo "warning: $v is not in .env (see README, step 2)"
done

mkdir -p .secrets && chmod 700 .secrets
# Trino joins the lakehouse's network. If the lakehouse was recreated since Trino last ran, the stopped container
# still points at the old network ID and won't start, so always recreate Trino (it keeps no state). Recreate the
# Bao Agents too, so they render the secrets the bootstrap just wrote (e.g. new lakehouse keys from .env).
docker compose rm -sf trino bao-agent-trino bao-agent-cube >/dev/null 2>&1 || true
docker compose up -d --wait
echo
echo "OpenBao  http://localhost:28200/ui   (token: .secrets/root-token)"
echo "Trino    http://localhost:28080/ui   (user: admin)"
echo "Cube     http://localhost:24000/cubejs-api/v1   (needs an OpenBao-signed JWT)"
