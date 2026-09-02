#!/usr/bin/env bash
# Upgrade a running deployment to the current `main`, in the order
# docs/deployment.md ("Updating an existing deployment") prescribes -- as one
# command, so the backup cannot be forgotten, the .env check cannot be run
# before the pull (when it says nothing), and nothing is rebuilt if that check
# fails.
#
# Usage:
#   ./scripts/deploy.sh [backup-dir]        # default /var/backups/bestteam
#
# What it does, in order:
#   1. backs the database up to <backup-dir>/pre-upgrade-<timestamp>.db
#      (scripts/backup-db.sh, SQLite's online backup API);
#   2. pulls: a clean tree is a plain `git pull`; host-local edits (a port
#      binding, say) are stashed around it and put back -- if they no longer
#      apply, the script stops there, with the old containers still serving;
#   3. shows what .env.example gained since the commit you were on and waits
#      for you to add it to .env by hand -- never by re-copying the example;
#   4. runs `admin check-env` against the new code and STOPS on any FAIL.
#      Nothing has been rebuilt yet, so the running deployment is untouched;
#   5. builds, starts (the entrypoint runs `alembic upgrade head`), waits for
#      /api/health, runs check-env once more -- its `schema: at head` line is
#      the proof the migration ran -- and prints the live release tag.
#
# Run it from the checkout on the host: `docker compose` finds the project by
# the current directory. If step 5 fails, the rollback is printed.
set -euo pipefail

BACKUP_DIR="${1:-/var/backups/bestteam}"
cd "$(dirname "$0")/.."

BACKUP="$BACKUP_DIR/pre-upgrade-$(date +%F-%H%M%S).db"
echo "1/5 Backing up the database to $BACKUP..."
./scripts/backup-db.sh "$BACKUP"

echo "2/5 Pulling..."
BEFORE=$(git rev-parse HEAD)
if [ -n "$(git status --porcelain)" ]; then
  echo "    Host-local edits found -- stashing around the pull. (Edits that must"
  echo "    survive every pull belong in docker-compose.override.yml instead.)"
  git stash
  git pull
  git stash pop    # a conflict stops the script here, before anything is rebuilt
else
  git pull
fi
if [ "$(git rev-parse HEAD)" = "$BEFORE" ]; then
  echo "    Already at $(git rev-parse --short HEAD); nothing to upgrade."
  echo "    (For a .env-only change, 'docker compose up -d' restarts with the new values.)"
  exit 0
fi

echo "3/5 Variables .env.example gained since $(git rev-parse --short "$BEFORE"):"
if git diff --quiet "$BEFORE" HEAD -- .env.example; then
  echo "    none"
else
  git --no-pager diff "$BEFORE" HEAD -- .env.example
  echo "    Add anything new to .env by hand (never by re-copying .env.example)."
  read -r -p "    Done -- continue? [y/N] " answer
  if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
    echo "Stopped before the .env check; the old containers are still serving. Re-run when .env is ready."
    exit 1
  fi
fi

echo "4/5 Checking .env against the new code (a FAIL stops here; nothing is rebuilt)..."
docker compose run --rm --no-deps backend python -m ui.backend.admin check-env

echo "5/5 Building and starting..."
docker compose build
docker compose up -d

echo "    Waiting for /api/health..."
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/api/health > /dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 2
done
if [ "$healthy" -ne 1 ]; then
  echo "The backend has not answered /api/health within 120s -- check 'docker compose logs backend'." >&2
  echo "To roll back: git checkout $BEFORE && docker compose build && ./scripts/restore.sh $BACKUP" >&2
  exit 1
fi

# The container serving traffic, not a fresh one: only `exec` proves the live
# backend's schema is at head and that it picked up the .env it was started with.
docker compose exec -T backend python -m ui.backend.admin check-env
echo "Upgraded $(git rev-parse --short "$BEFORE") -> $(git rev-parse --short HEAD); live release: $(docker compose exec -T backend printenv BESTTEAM_RELEASE | tr -d '\r')"
