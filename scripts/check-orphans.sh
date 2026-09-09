#!/usr/bin/env bash
# Report rows whose foreign key points at nothing, in the running backend's
# database.
#
# Usage:
#   ./scripts/check-orphans.sh
#
# This is the first step of the SQLite -> Postgres cutover (spec
# docs/superpowers/specs/2026-09-07-database-engine-portability-design.md,
# §11 and its "the live file's orphan state is unknown" risk). `admin
# migrate-db` refuses to copy a database that has orphans, so this count is
# what decides whether the cutover needs --fix-orphans -- and it calls the
# very same orphan_report() that migrate-db's pre-flight calls, so the two
# cannot drift apart.
#
# Read-only and safe against a live deployment: the database is opened
# through readonly_engine() (a SQLite file gets a `mode=ro` URI, so it can be
# neither created nor written) and only SELECTs run. Foreign keys are not
# enforced on the production file, which is exactly why the check is needed.
#
# Exit status: 0 when every foreign key resolves, 1 when any orphan is found.
set -euo pipefail

docker compose exec -T backend python -c "
import os, sys
from ui.backend.db.database import describe_database_url, readonly_engine, resolve_database_url
from ui.backend.db.migrate import orphan_report

url = resolve_database_url(os.environ)
print('database: ' + describe_database_url(url))
engine = readonly_engine(url)
try:
    orphans = orphan_report(engine)
finally:
    engine.dispose()

if not orphans:
    print('clean: every foreign key points at a row that exists')
    sys.exit(0)

print('orphan rows found:')
for o in orphans:
    action = 'written as NULL' if o.nullable else 'SKIPPED -- the whole row is dropped'
    print('  %s.%s -> %s: %d row(s); with migrate-db --fix-orphans they would be %s'
          % (o.table, o.column, o.parent, o.rows, action))
sys.exit(1)
"
