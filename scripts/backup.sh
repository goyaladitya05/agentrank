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
#
# What comes out is the most sensitive file this project produces: every tenant's evidence, plus
# every credential and session digest. It is created under `umask 077` so no other account on the
# host can read it, and through `noclobber` so the check that it does not already exist is the
# kernel's rather than this script's, which closes both the symlink and the race an `-e` test
# leaves open. A dump that fails part way is removed, because a truncated file that looks like a
# backup is worse than no file at all: it is discovered at the moment somebody needs it.
set -euo pipefail
umask 077

if [ "$#" -gt 1 ]; then
  echo "usage: scripts/backup.sh [output-file]" >&2
  exit 64
fi

host=${POSTGRES_HOST:-localhost}
port=${POSTGRES_PORT:-5432}
database=${POSTGRES_DB:-agentrank}
user=${POSTGRES_USER:-agentrank}

target=${1:-agentrank-$(date -u +%Y%m%dT%H%M%SZ).dump}

# `noclobber` makes this an O_EXCL create: it fails if anything is already at that path, including
# a symlink, and nothing can appear between the check and the write because they are one call.
if ! (set -o noclobber; : > "$target") 2>/dev/null; then
  echo "refusing to overwrite $target" >&2
  exit 1
fi
# From here the file exists and is ours. Anything that does not finish takes it with it.
trap 'rm -f -- "$target"' EXIT

PGPASSWORD=${POSTGRES_PASSWORD:-} pg_dump \
  --host="$host" \
  --port="$port" \
  --username="$user" \
  --dbname="$database" \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$target"

trap - EXIT
echo "wrote $target from $database at $host:$port"
