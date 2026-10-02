#!/bin/sh
# Read-only: prints "held=<n>" for the production run lock on Postgres Core.
# Uses the same railway ssh + read-only psql path as every other forensic query.
set -eu
PROJECT=898f2e3a-1c1e-4b00-b9a6-686cf0432282
ENVIRONMENT=bae427bd-64a6-4f4e-8f56-fbd406985434
DB=f3e843e3-16f9-495e-a63b-574a653f3016
SQL="SELECT 'held=' || count(*) FROM pg_locks WHERE locktype='advisory' AND objid=1952937059 AND granted;"
Q=$(printf '%s' "$SQL" | gzip -9c | base64 -w0)
MSYS_NO_PATHCONV=1 railway ssh -p "$PROJECT" -e "$ENVIRONMENT" -s "$DB" \
  "echo $Q | base64 -d | gunzip | PGOPTIONS='-c default_transaction_read_only=on' psql -U postgres -d railway -X -t -A -f -" \
  2>/dev/null | tr -d ' \r' | grep '^held=' | head -1
