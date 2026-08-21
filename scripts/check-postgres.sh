#!/usr/bin/env bash
# Verify that the local development database is reachable and is PostgreSQL 18.
set -euo pipefail

SERVICE=postgres
EXPECTED_MAJOR=18

if ! docker compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
  echo "postgres service is not running. Start it with: docker compose up -d --wait" >&2
  exit 1
fi

health=$(docker inspect --format '{{.State.Health.Status}}' agentrank-postgres)
if [ "$health" != "healthy" ]; then
  echo "postgres container health is '$health', expected 'healthy'" >&2
  exit 1
fi

version=$(docker compose exec -T "$SERVICE" \
  psql -U "${POSTGRES_USER:-agentrank}" -d "${POSTGRES_DB:-agentrank}" \
  -tAc 'SHOW server_version')
major=${version%%.*}

if [ "$major" != "$EXPECTED_MAJOR" ]; then
  echo "expected PostgreSQL $EXPECTED_MAJOR, found $version" >&2
  exit 1
fi

echo "postgres healthy, server_version $version"
