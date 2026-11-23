#!/usr/bin/env bash
# Stop this lab, keeping its volumes. OpenBao comes back sealed; scripts/up.sh unseals it.
# Add -v to delete everything (OpenBao data, audit log, Cube Store) and start fresh:
#   docker compose down -v && rm -rf .secrets
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose stop
