"""workflow_dependencies table + backfill from current versions

Revision ID: d4e6b2c9f1a7
Revises: c3f5a1b8e2d4
Create Date: 2026-07-25 00:00:00.000000

Typed skill/KB dependency records (P1-04). Guarded/idempotent: db_session runs
create_all at import before upgrade, so create the table only when absent. The
Python backfill materializes dep rows for each workflow's CURRENT version by
parsing its config and resolving skill/KB names to record ids exactly as the
runtime does (an org skill shadows a same-named platform built-in; KBs are
org-scoped). It skips any version that already has rows, so a re-run is a no-op.
Resolving the id (not just the name) keeps the rewired delete guard non-regressing
for pre-migration deployed workflows.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e6b2c9f1a7"
down_revision: Union[str, Sequence[str], None] = "c3f5a1b8e2d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _resolve_skill_id(bind, name, org_id):
    if org_id is not None:
        row = bind.execute(
            sa.text("SELECT id FROM skills WHERE name = :n AND org_id = :o"),
            {"n": name, "o": org_id},
        ).first()
        if row is not None:
            return row[0]
    row = bind.execute(
        sa.text("SELECT id FROM skills WHERE name = :n AND org_id IS NULL"),
        {"n": name},
    ).first()
    return row[0] if row is not None else None


def _resolve_kb_id(bind, name, org_id):
    if org_id is None:
        row = bind.execute(
            sa.text("SELECT id FROM knowledge_bases WHERE name = :n AND org_id IS NULL"),
            {"n": name},
        ).first()
    else:
        row = bind.execute(
            sa.text("SELECT id FROM knowledge_bases WHERE name = :n AND org_id = :o"),
            {"n": name, "o": org_id},
        ).first()
    return row[0] if row is not None else None


def _names(config):
    """(skill names, tool names) from a config dict, defensively. Only string
    references are kept -- a legacy config with a non-string entry (e.g.
    ``skills: ["ok", 1]``) must not poison the set and abort the whole upgrade
    when `sorted()` later compares a str against an int."""
    skills, tools = set(), set()
    if not isinstance(config, dict):
        return skills, tools
    agents = config.get("agents")
    if not isinstance(agents, list):
        return skills, tools
    for agent in agents:
        if not isinstance(agent, dict):
            continue
        for field, sink in (("skills", skills), ("tools", tools)):
            try:
                sink.update(x for x in (agent.get(field) or []) if isinstance(x, str))
            except TypeError:
                continue
    return skills, tools


def _inline_kb_names(config):
    """Names of knowledge bases defined inline in the config. An inline KB
    shadows a same-named standalone KB at runtime, so a tool name satisfied by
    an inline KB is not a standalone-KB dependency."""
    if not isinstance(config, dict):
        return set()
    kbs = config.get("knowledge_bases")
    if not isinstance(kbs, list):
        return set()
    return {
        kb["name"]
        for kb in kbs
        if isinstance(kb, dict) and isinstance(kb.get("name"), str)
    }


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    if "workflow_dependencies" not in tables:
        op.create_table(
            "workflow_dependencies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("workflow_version_id", sa.Integer(),
                      sa.ForeignKey("workflow_versions.id"), nullable=False),
            sa.Column("resource_kind", sa.String(), nullable=False),
            sa.Column("resource_name", sa.String(), nullable=False),
            sa.Column("resource_id", sa.Integer(), nullable=True),
            sa.UniqueConstraint(
                "workflow_version_id", "resource_kind", "resource_name",
                name="uq_workflow_dependencies_version_kind_name",
            ),
        )

    # Backfill dep rows for each workflow's current version. Idempotent: skip any
    # version that already has rows.
    have = {
        r[0] for r in bind.execute(
            sa.text("SELECT DISTINCT workflow_version_id FROM workflow_dependencies")
        )
    }
    rows = bind.execute(sa.text(
        "SELECT id, org_id, config, current_version_id FROM workflows "
        "WHERE current_version_id IS NOT NULL"
    )).fetchall()
    insert = sa.text(
        "INSERT INTO workflow_dependencies "
        "(workflow_version_id, resource_kind, resource_name, resource_id) "
        "VALUES (:v, :k, :n, :rid)"
    )
    for _wf_id, org_id, config, ver_id in rows:
        if ver_id in have:
            continue
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except ValueError:
                continue
        skills, tools = _names(config)
        kb_tools = tools - _inline_kb_names(config)
        for name in sorted(skills):
            bind.execute(insert, {"v": ver_id, "k": "skill", "n": name,
                                  "rid": _resolve_skill_id(bind, name, org_id)})
        for name in sorted(kb_tools):
            kid = _resolve_kb_id(bind, name, org_id)
            if kid is not None:
                bind.execute(insert, {"v": ver_id, "k": "knowledge_base", "n": name,
                                      "rid": kid})


def downgrade() -> None:
    bind = op.get_bind()
    if "workflow_dependencies" in set(sa.inspect(bind).get_table_names()):
        op.drop_table("workflow_dependencies")
