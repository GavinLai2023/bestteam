"""drop the built-in skills' _vN name suffixes and merge intake v1+v2

Revision ID: w0x1y2z3a4b5
Revises: v9w0x1y2z3a4
Create Date: 2026-09-02 00:00:00.000000

The `_vN` suffix on platform built-in skill names was a workaround for
insert-only seeding: a behaviour change could only reach seeded databases as
a brand-new name, so it shipped as `_v2` beside a frozen `_v1`.
`skill_versions` already provides real history and deploys pin immutable
snapshot ids, so the suffix is redundant versioning in the identifier. From
this release, seeding updates the platform tier in place (append a version,
move the head) and the platform tier is locked against in-place admin edits
(customisation is an org-tier copy that shadows by name). See
docs/superpowers/specs/2026-09-02-skills-drop-vn-suffix-design.md.

Renamed (platform tier only -- an org's own `*_vN` naming is the customer's
business): email_input_security_core_v1 -> email_input_security_core,
property_maintenance_response_v1 -> property_maintenance_response,
contractor_sourcing_v1 -> contractor_sourcing, and
property_maintenance_intake_v1 + _v2 MERGE into property_maintenance_intake:
_v2's row survives under the new name, _v1's snapshots are re-pointed to it
(snapshot ids untouched -- deployed pins reference them by id) and the
combined set is renumbered by (created_at, id). The head is NOT moved here:
installing new canonical content is seeding's job, not the migration's.

The rename must land consistently everywhere a skill name is stored as data,
because the runtime builds agents from the pipeline HEAD record's config
while pinned skills are keyed by pipeline_dependencies.resource_name -- the
two must agree. Rewritten: skills.name, skill_versions.config->"name"
(cosmetic; the runtime overrides it with resource_name),
pipelines.config / pipeline_versions.config / builder_sessions.
specification_json (each agent's `skills` list), and
pipeline_dependencies.resource_name.

Shadow exception: an org that owns a skill under one of the OLD built-in
names keeps that name untouched throughout its own data -- `load_skills`
lets an org skill shadow a same-named built-in, and renaming such an org's
references would silently re-point them from the org's own skill to the
platform one.

Guarded against the `create_all()` race like o2p3q4r5s6t7: a fresh database
seeded by NEW code already has the new names (this migration no-ops), and if
create_all()+seeding raced ahead and created a new-named row while the old
row still holds the real history, the merge branch absorbs the old row's
snapshots into the new one instead of failing or duplicating.

downgrade() is lossy but functional (other migrations' tests downgrade
through this revision): the three simple renames reverse cleanly;
property_maintenance_intake goes back to _v2 keeping the merged history --
the _v1 row is NOT resurrected.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "w0x1y2z3a4b5"
down_revision: Union[str, Sequence[str], None] = "v9w0x1y2z3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# old -> new, platform tier only. property_maintenance_intake_v2 must be
# processed BEFORE _v1: the _v2 rename claims the merged name, then _v1
# merges into it, its snapshot renumbered in front by created_at.
_SKILL_RENAMES = [
    ("email_input_security_core_v1", "email_input_security_core"),
    ("property_maintenance_intake_v2", "property_maintenance_intake"),
    ("property_maintenance_intake_v1", "property_maintenance_intake"),
    ("property_maintenance_response_v1", "property_maintenance_response"),
    ("contractor_sourcing_v1", "contractor_sourcing"),
]

# Big enough to clear any real version_number during the two-phase renumber,
# small enough to stay far from integer limits.
_RENUMBER_SHIFT = 1_000_000


def _tables(bind) -> set:
    return set(sa.inspect(bind).get_table_names())


def _platform_skill(bind, name: str):
    return bind.execute(
        sa.text(
            "SELECT id, current_version_id FROM skills "
            "WHERE name = :name AND org_id IS NULL"
        ),
        {"name": name},
    ).one_or_none()


def _merge_or_rename(bind, old: str, new: str) -> None:
    """Rename the platform-tier skill `old` to `new`; if `new` already exists
    (the intake merge, or create_all()+new-seeding raced ahead), absorb `old`
    into it: snapshots re-pointed (ids kept) and the combined set renumbered
    by (created_at, id); dependency resource_ids follow; `new`'s head stays."""
    old_row = _platform_skill(bind, old)
    if old_row is None:
        return  # fresh DB, or already migrated
    new_row = _platform_skill(bind, new)

    if new_row is None:
        bind.execute(
            sa.text("UPDATE skills SET name = :new WHERE id = :id"),
            {"new": new, "id": old_row.id},
        )
        return

    # Shift the incoming rows out of the way first, or the re-point itself
    # collides with (skill_id, version_number) on the surviving skill.
    bind.execute(
        sa.text(
            "UPDATE skill_versions SET version_number = version_number + :shift "
            "WHERE skill_id = :old_id"
        ),
        {"shift": _RENUMBER_SHIFT, "old_id": old_row.id},
    )
    bind.execute(
        sa.text("UPDATE skill_versions SET skill_id = :new_id WHERE skill_id = :old_id"),
        {"new_id": new_row.id, "old_id": old_row.id},
    )
    # Two phases so the renumber never collides with (skill_id, version_number).
    bind.execute(
        sa.text(
            "UPDATE skill_versions SET version_number = version_number + :shift "
            "WHERE skill_id = :sid"
        ),
        {"shift": _RENUMBER_SHIFT, "sid": new_row.id},
    )
    ordered = bind.execute(
        sa.text(
            "SELECT id FROM skill_versions WHERE skill_id = :sid "
            "ORDER BY created_at, id"
        ),
        {"sid": new_row.id},
    ).fetchall()
    for number, row in enumerate(ordered, start=1):
        bind.execute(
            sa.text("UPDATE skill_versions SET version_number = :n WHERE id = :id"),
            {"n": number, "id": row.id},
        )
    bind.execute(
        sa.text(
            "UPDATE pipeline_dependencies SET resource_id = :new_id "
            "WHERE resource_kind = 'skill' AND resource_id = :old_id"
        ),
        {"new_id": new_row.id, "old_id": old_row.id},
    )
    bind.execute(sa.text("DELETE FROM skills WHERE id = :id"), {"id": old_row.id})


def _load_json(blob):
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except ValueError:
            return None
    if isinstance(blob, dict):
        return blob
    return None


def _rewrite_agent_skill_names(parsed, org_id, rename_map, shadowed) -> bool:
    """Replace mapped names inside `agents[*].skills`, skipping names the
    owning org shadows with its own skill. Returns True when anything changed."""
    if not isinstance(parsed, dict):
        return False
    changed = False
    for agent in parsed.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        skills = agent.get("skills")
        if not isinstance(skills, list):
            continue
        for index, skill_name in enumerate(skills):
            if skill_name not in rename_map:
                continue
            if org_id is not None and (org_id, skill_name) in shadowed:
                continue
            skills[index] = rename_map[skill_name]
            changed = True
    return changed


def _rewrite_config_column(bind, table: str, column: str, org_sql: str, rename_map, shadowed) -> None:
    """Rewrite `agents[*].skills` in every row of `table`.`column`. `org_sql`
    is a SELECT yielding (id, org_id, <column>) for the table -- the org join
    differs per table and the shadow skip-set needs the owning org."""
    if table not in _tables(bind):
        return
    for row in bind.execute(sa.text(org_sql)).fetchall():
        parsed = _load_json(row[2])
        if parsed is None:
            continue
        if _rewrite_agent_skill_names(parsed, row[1], rename_map, shadowed):
            bind.execute(
                sa.text(f"UPDATE {table} SET {column} = :blob WHERE id = :id"),
                {"blob": json.dumps(parsed), "id": row[0]},
            )


def _apply_renames(bind, renames) -> None:
    """Rename the platform rows per `renames` [(from, to)] and rewrite every
    stored reference, honouring org-tier shadows of the FROM names."""
    rename_map = dict(renames)
    from_names = [source for source, _ in renames]

    for source, target in renames:
        _merge_or_rename(bind, source, target)

    # Tidy the internal "name" inside snapshots of the (now renamed) platform
    # rows. The runtime overrides it with resource_name, so consistency only.
    target_names = sorted(set(rename_map.values()))
    placeholders = ", ".join(f":n{i}" for i in range(len(target_names)))
    params = {f"n{i}": n for i, n in enumerate(target_names)}
    rows = bind.execute(
        sa.text(
            "SELECT sv.id, sv.config, s.name FROM skill_versions sv "
            "JOIN skills s ON sv.skill_id = s.id "
            f"WHERE s.org_id IS NULL AND s.name IN ({placeholders})"
        ),
        params,
    ).fetchall()
    for version_id, blob, skill_name in rows:
        parsed = _load_json(blob)
        if parsed is None or parsed.get("name") not in rename_map:
            continue
        parsed["name"] = skill_name
        bind.execute(
            sa.text("UPDATE skill_versions SET config = :blob WHERE id = :id"),
            {"blob": json.dumps(parsed), "id": version_id},
        )

    # Orgs whose OWN skill sits under a FROM name: that name keeps resolving to
    # the org's skill and must not be rewritten in that org's data.
    from_placeholders = ", ".join(f":o{i}" for i in range(len(from_names)))
    from_params = {f"o{i}": n for i, n in enumerate(from_names)}
    shadowed = {
        (row.org_id, row.name)
        for row in bind.execute(
            sa.text(
                "SELECT org_id, name FROM skills "
                f"WHERE org_id IS NOT NULL AND name IN ({from_placeholders})"
            ),
            from_params,
        ).fetchall()
    }

    _rewrite_config_column(
        bind, "pipelines", "config",
        "SELECT id, org_id, config FROM pipelines", rename_map, shadowed,
    )
    _rewrite_config_column(
        bind, "pipeline_versions", "config",
        "SELECT pv.id, p.org_id, pv.config FROM pipeline_versions pv "
        "JOIN pipelines p ON pv.pipeline_id = p.id",
        rename_map, shadowed,
    )
    _rewrite_config_column(
        bind, "builder_sessions", "specification_json",
        "SELECT id, org_id, specification_json FROM builder_sessions "
        "WHERE specification_json IS NOT NULL",
        rename_map, shadowed,
    )

    if "pipeline_dependencies" in _tables(bind):
        deps = bind.execute(
            sa.text(
                "SELECT pd.id, pd.resource_name, p.org_id "
                "FROM pipeline_dependencies pd "
                "JOIN pipeline_versions pv ON pd.pipeline_version_id = pv.id "
                "JOIN pipelines p ON pv.pipeline_id = p.id "
                f"WHERE pd.resource_kind = 'skill' AND pd.resource_name IN ({from_placeholders})"
            ),
            from_params,
        ).fetchall()
        for dep_id, resource_name, org_id in deps:
            if org_id is not None and (org_id, resource_name) in shadowed:
                continue
            bind.execute(
                sa.text("UPDATE pipeline_dependencies SET resource_name = :new WHERE id = :id"),
                {"new": rename_map[resource_name], "id": dep_id},
            )


def upgrade() -> None:
    bind = op.get_bind()
    if not {"skills", "skill_versions"} <= _tables(bind):
        return  # brand-new DB: create_all() + seeding will produce the new names
    _apply_renames(bind, _SKILL_RENAMES)


def downgrade() -> None:
    """Lossy reverse: the intake merge stays merged (its history goes back
    under the _v2 name; _v1 is not resurrected), everything else reverses."""
    bind = op.get_bind()
    if not {"skills", "skill_versions"} <= _tables(bind):
        return
    _apply_renames(bind, [
        ("email_input_security_core", "email_input_security_core_v1"),
        ("property_maintenance_intake", "property_maintenance_intake_v2"),
        ("property_maintenance_response", "property_maintenance_response_v1"),
        ("contractor_sourcing", "contractor_sourcing_v1"),
    ])
