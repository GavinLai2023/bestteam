#!/bin/sh
# Backend container entrypoint (beta gate G3).
#
# When the container is starting the server, apply pending migrations first
# so an upgrade can never be served on a stale schema. Any other command --
# `docker compose run backend python -m ui.backend.admin ...`, or an explicit
# `alembic ...` -- runs as given: the operator CLI is the documented recovery
# path when a migration *refuses* (docs/deployment.md, "Recovering a legacy
# multi-member org"), so it must not be gated on that same migration.
set -e
if [ "$1" = "uvicorn" ]; then
    echo "[entrypoint] alembic upgrade head"
    alembic upgrade head
fi
exec "$@"
