#!/usr/bin/env bash
# Restore one AgentRank backup into an empty database.
#
# Deliberately refuses a database that is not empty. A restore over live data is not a recovery,
# it is two histories merged, and the failure mode is a merchant's evidence silently interleaved
# with somebody else's: `pg_restore` would fail on the first duplicate key and leave whatever it
# had already written behind. The supported procedure is to create an empty database and restore
# into that, then point the deployment at it.
#
# After restoring, run `uv run alembic upgrade head` if the backup predates the running build.
# The dump carries the `alembic_version` row it was taken at, so an older backup restores as an
# older schema and `/ready` will report it as incompatible until it is migrated.
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "usage: scripts/restore.sh <dump-file> [database]" >&2
  exit 64
fi

dump=$1
host=${POSTGRES_HOST:-localhost}
port=${POSTGRES_PORT:-5432}
database=${2:-${POSTGRES_DB:-agentrank}}
user=${POSTGRES_USER:-agentrank}

if [ ! -r "$dump" ]; then
  echo "cannot read $dump" >&2
  exit 1
fi

export PGPASSWORD=${POSTGRES_PASSWORD:-}

tables=$(psql --host="$host" --port="$port" --username="$user" --dbname="$database" -tAc \
  "select count(*) from information_schema.tables where table_schema = 'public'")
if [ "$tables" != "0" ]; then
  echo "$database already holds $tables table(s). Restore into an empty database." >&2
  exit 1
fi

pg_restore \
  --host="$host" \
  --port="$port" \
  --username="$user" \
  --dbname="$database" \
  --no-owner \
  --no-privileges \
  --exit-on-error \
  "$dump"

revision=$(psql --host="$host" --port="$port" --username="$user" --dbname="$database" -tAc \
  "select version_num from alembic_version" || true)
echo "restored $dump into $database at $host:$port, schema revision ${revision:-none}"
