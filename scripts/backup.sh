#!/usr/bin/env bash
# Take one consistent backup of the AgentRank database.
#
# PostgreSQL holds every immutable artifact this product produces: source snapshots, compiler
# runs, reviews, published representations, benchmark runs, mission traces and launch history.
# None of it is reconstructible from anywhere else. There is no backup service and there is not
# going to be one; what exists is `pg_dump`, run by an operator, on a schedule they choose.
#
# The custom format is used rather than plain SQL because it is what `pg_restore` reads, it
# compresses, and it can be restored into a database that already exists without editing the file.
#
# Connection details come from the environment, exactly as every process in this deployment reads
# them, so a backup cannot be taken against a database the application is not using. The password
# is passed through PGPASSWORD rather than on the command line, because an argument vector is
# readable by every process on the host.
set -euo pipefail

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/backup.sh [output-file]" >&2
  exit 64
fi

host=${POSTGRES_HOST:-localhost}
port=${POSTGRES_PORT:-5432}
database=${POSTGRES_DB:-agentrank}
user=${POSTGRES_USER:-agentrank}

target=${1:-agentrank-$(date -u +%Y%m%dT%H%M%SZ).dump}
if [ -e "$target" ]; then
  echo "refusing to overwrite $target" >&2
  exit 1
fi

PGPASSWORD=${POSTGRES_PASSWORD:-} pg_dump \
  --host="$host" \
  --port="$port" \
  --username="$user" \
  --dbname="$database" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$target"

echo "wrote $target from $database at $host:$port"
