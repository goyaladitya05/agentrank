#!/usr/bin/env bash
# Restore one AgentRank backup into an empty database.
#
# Deliberately refuses a database that is not empty. A restore over live data is not a recovery,
# it is two histories merged, and the failure mode is a merchant's evidence silently interleaved
# with somebody else's. The supported procedure is to create an empty database and restore into
# that, then point the deployment at it.
#
# One transaction, so a restore that is interrupted leaves nothing behind and can simply be run
# again. Without it, an interrupted restore leaves the enum types and trigger functions it had
# already created, which the emptiness check below does not count, and the retry then fails on
# `type already exists` with no way forward but dropping the database.
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

# libpq reads a `dbname` containing `=` or `://` as a whole connection string, which would
# override the host, the port and the user this script resolved, and offer PGPASSWORD to whatever
# host it named. A database name is a name.
case "$database" in
  *=* | *://*)
    echo "refusing a database name that is a connection string: $database" >&2
    exit 64
    ;;
esac

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
  --single-transaction \
  --exit-on-error \
  "$dump"

revision=$(psql --host="$host" --port="$port" --username="$user" --dbname="$database" -tAc \
  "select version_num from alembic_version" || true)
echo "restored $dump into $database at $host:$port, schema revision ${revision:-none}"
