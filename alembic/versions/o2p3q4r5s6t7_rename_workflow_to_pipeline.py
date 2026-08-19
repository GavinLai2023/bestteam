"""rename Workflow -> Pipeline: tables, columns, and config JSON content

Revision ID: o2p3q4r5s6t7
Revises: n1o2p3q4r5s6
Create Date: 2026-08-19 00:00:00.000000

The product concept "Workflow" is renamed to "Pipeline" (see root CLAUDE.md
and docs/DECISIONS.md) so the word "Workflow" is free for a possible future
multi-step business-process concept. This is the first migration in this
project's history that RENAMES rather than only adds -- every prior migration
was additive by construction so it could never disagree with what
`db_session.py`'s `create_all()` safety net independently produces for a
brand-new table. A rename does not have that property: once `models.py`
declares `pipelines`/`pipeline_versions`/`pipeline_dependencies`, a process
that boots (and so runs `create_all()`) BEFORE this migration creates those
tables EMPTY while the real data still sits under the old names. Every
rename below is therefore guarded against that race (see
`_rename_table_safely`) rather than assumed safe.

Renamed: tables `workflows`->`pipelines`, `workflow_versions`->
`pipeline_versions`, `workflow_dependencies`->`pipeline_dependencies`;
columns `pipeline_versions.workflow_id`->`pipeline_id`,
`pipeline_dependencies.workflow_version_id`->`pipeline_version_id`,
`builder_sessions.workflow_id`->`pipeline_id`, `runs.workflow`->`pipeline`,
`runs.workflow_version_id`->`pipeline_version_id`,
`email_triggers.workflow_name`->`pipeline_name`,
`share_links.workflow_id`->`pipeline_id`; and the top-level `"workflow"` key
inside every `pipelines.config`/`pipeline_versions.config`/
`builder_sessions.specification_json` JSON blob (the `Specification.to_raw()`
shape) becomes `"pipeline"`, keeping the DB in lockstep with the SDK loader's
renamed YAML/dict schema (`core/loader.py`/`core/specification.py`). The
third table is a wizard session's in-progress draft of the same document, not
just a deployed team's -- skipping it would leave a pre-upgrade session's
`Specification.model_validate()` silently dropping its steps (the unknown
"workflow" key is ignored, `pipeline` defaults to empty) the next time that
session is opened, tested, or redeployed.

Deliberately NOT renamed: the named constraints/indexes that reference the
old names (`uq_workflows_org_id_name`, `ck_workflows_status`,
`uq_workflow_versions_workflow_id_version_number`,
`uq_workflow_dependencies_version_kind_name`) -- SQLite constraint names are
diagnostic labels only, never queried against, and a full table rebuild just
to rename them would double this migration's risk surface for zero
functional benefit. A migrated database keeps the old constraint names; a
fresh database (via `create_all()` against the renamed `models.py`) gets
`uq_pipelines_...`-style names from the start. This name drift between
"migrated" and "fresh" databases is the same class of thing this project
already tolerates (see `main.py`'s note that `create_all()` doesn't retrofit
an index onto a pre-existing table).

Also deliberately NOT touched: `email_triggers.last_error_kind`'s stored
string value `"workflow"` and `trigger_health.py`'s `"workflow_fault"`/
`"workflow_ok"`/`"workflow"` fingerprint values. Those are internal
fault-classification strings compared against data an OLDER app version may
have written (`EmailTrigger.alerted_fingerprint`); changing the stored value
would need a backfill or a dual-read compatibility branch purely for cosmetic
consistency with a renamed Python constant. See `runtime.py`/
`trigger_health.py`/`email_trigger.py` for the (renamed) constant names that
still produce these (unrenamed) values.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "o2p3q4r5s6t7"
down_revision: Union[str, Sequence[str], None] = "n1o2p3q4r5s6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old_table, new_table), parent tables first so SQLite's automatic FK-reference
# fixup on RENAME TO (default since SQLite 3.25, `legacy_alter_table` off) has
# already repointed a child table's FK definition at the new parent name by the
# time that child itself is considered.
_TABLE_RENAMES = [
    ("workflows", "pipelines"),
    ("workflow_versions", "pipeline_versions"),
    ("workflow_dependencies", "pipeline_dependencies"),
]

# (table, old_column, new_column) -- table names are the POST-rename (new) names,
# since column renames run after the table renames above.
_COLUMN_RENAMES = [
    ("pipeline_versions", "workflow_id", "pipeline_id"),
    ("pipeline_dependencies", "workflow_version_id", "pipeline_version_id"),
    ("builder_sessions", "workflow_id", "pipeline_id"),
    ("runs", "workflow", "pipeline"),
    ("runs", "workflow_version_id", "pipeline_version_id"),
    ("email_triggers", "workflow_name", "pipeline_name"),
    ("share_links", "workflow_id", "pipeline_id"),
]

# (table, json_column) -- builder_sessions.specification_json is the same
# Specification.to_raw() shape as pipelines/pipeline_versions.config (the
# wizard's in-progress draft of the same document), so a session saved before
# this rename still has a top-level "workflow" key. Left unrewritten,
# Specification.model_validate() on that stored dict would silently ignore
# the unknown "workflow" key and default `pipeline` to an empty step list --
# not a validation error, just a session that quietly lost its steps the next
# time it's opened, tested, or redeployed.
_JSON_COLUMN_TABLES = [
    ("pipelines", "config"),
    ("pipeline_versions", "config"),
    ("builder_sessions", "specification_json"),
]


def _rename_table_safely(bind, old: str, new: str) -> None:
    """Rename `old` to `new`, guarding against `create_all()` racing ahead of
    this migration (see the module docstring). Four cases:

    - only `old` exists -> the ordinary rename.
    - neither exists -> a fresh DB whose `create_all()` hasn't run yet; leave
      it alone, `create_all()` will produce `new` directly once it does.
    - only `new` exists -> already migrated (a re-run, or `create_all()` ran
      after this migration already renamed things); no-op, idempotent.
    - both exist -> `create_all()` raced ahead and created an EMPTY `new`
      before this migration ran. Safe to absorb IF `new` is genuinely empty
      (drop it, then rename the real, data-bearing `old` into its place); if
      `new` somehow already has rows, refuse rather than silently merge or
      drop real data -- that needs a human to look at both tables.
    """
    tables = set(sa.inspect(bind).get_table_names())
    old_exists, new_exists = old in tables, new in tables

    if not old_exists:
        return  # neither exists, or already renamed -- nothing to do
    if new_exists:
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {new}")).scalar()
        if count:
            raise RuntimeError(
                f"Both '{old}' and '{new}' exist and '{new}' already has "
                f"{count} row(s) -- refusing to guess which is authoritative. "
                f"This means the application booted (running create_all()) "
                f"before this migration ran AND something already wrote into "
                f"'{new}'. Inspect both tables by hand before retrying."
            )
        op.drop_table(new)

    op.rename_table(old, new)


def _rename_column_safely(bind, table: str, old: str, new: str) -> None:
    tables = set(sa.inspect(bind).get_table_names())
    if table not in tables:
        return  # table doesn't exist yet -- create_all() will make it with the new name
    columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    if new in columns:
        return  # already renamed (idempotent re-run, or a fresh create_all()'d table)
    if old not in columns:
        return  # neither name present -- nothing to rename
    with op.batch_alter_table(table) as batch:
        batch.alter_column(old, new_column_name=new)


def _rewrite_config_json_key(bind, table: str, column: str, *, from_key: str, to_key: str) -> None:
    """Rewrite the top-level `from_key` entry in every row's `column` JSON blob
    to `to_key`, matching the SDK loader's renamed schema
    (`core/specification.py::Specification.to_raw()`). Idempotent: a row
    already keyed `to_key` (or with no `column`/no `from_key` entry) is left
    untouched, so a re-run is a no-op."""
    tables = set(sa.inspect(bind).get_table_names())
    if table not in tables:
        return
    columns = {c["name"] for c in sa.inspect(bind).get_columns(table)}
    if column not in columns:
        return
    rows = bind.execute(sa.text(f"SELECT id, {column} FROM {table}")).fetchall()
    update = sa.text(f"UPDATE {table} SET {column} = :config WHERE id = :id")
    for row_id, config in rows:
        if isinstance(config, str):
            try:
                parsed = json.loads(config)
            except ValueError:
                continue
        elif isinstance(config, dict):
            parsed = config
        else:
            continue
        if not isinstance(parsed, dict) or from_key not in parsed:
            continue
        parsed[to_key] = parsed.pop(from_key)
        bind.execute(update, {"config": json.dumps(parsed), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()

    for old, new in _TABLE_RENAMES:
        _rename_table_safely(bind, old, new)

    for table, old_col, new_col in _COLUMN_RENAMES:
        _rename_column_safely(bind, table, old_col, new_col)

    for table, column in _JSON_COLUMN_TABLES:
        _rewrite_config_json_key(bind, table, column, from_key="workflow", to_key="pipeline")


def downgrade() -> None:
    bind = op.get_bind()

    for table, column in reversed(_JSON_COLUMN_TABLES):
        _rewrite_config_json_key(bind, table, column, from_key="pipeline", to_key="workflow")

    for table, old_col, new_col in reversed(_COLUMN_RENAMES):
        _rename_column_safely(bind, table, new_col, old_col)

    for old, new in reversed(_TABLE_RENAMES):
        _rename_table_safely(bind, new, old)
