# Drop built-in skills' `_vN` suffixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the platform built-in skills to suffix-free names (merging `property_maintenance_intake_v1`+`_v2` into one skill with two history versions), make seeding update the platform tier unconditionally, lock platform built-ins against in-place admin edits (customisation = copy to org tier), and add admin version browsing, pin references, and active-org dropdown filtering.

**Architecture:** One Alembic data migration rewrites every stored occurrence of the old names (skills rows, version snapshots, pipeline head/version configs, builder drafts, dependency rows) with an org-shadow skip-set; `seed_default_skills` gains a content-compare-and-append branch; two read-only admin endpoints expose `skill_versions` and `pipeline_dependencies`; the Advanced page grows a version dropdown, references list, and copy-to-org flow; org dropdowns filter to active orgs via a tiny shared helper.

**Tech Stack:** SQLAlchemy/Alembic (SQLite), FastAPI, React + TypeScript (Vite), pytest, vitest.

**Spec:** `docs/superpowers/specs/2026-09-02-skills-drop-vn-suffix-design.md`

## Global Constraints

- Run everything through `./.venv/Scripts/python.exe` (Windows venv).
- Every new/changed test file keeps a `pytestmark` (enforced by `tests/test_marker_completeness.py`).
- British English in UI copy ("organisation"); code comments in English.
- Org-tier skill rows are NEVER renamed; only the platform tier (`org_id IS NULL`).
- New Alembic revision id is `w0x1y2z3a4b5`, `down_revision = "v9w0x1y2z3a4"`.
- Migration must be idempotent and safe against the `create_all()` race (see `alembic/versions/o2p3q4r5s6t7_rename_workflow_to_pipeline.py` for the pattern and rationale).
- Snapshot ids in `skill_versions` must never change (deployed pins reference them by id).
- Surgical diffs: don't reformat or "improve" neighbouring code.

**Name mapping (used by Tasks 1, 2, 7):**

| Old (platform tier) | New |
|---|---|
| `email_triage_reply` | unchanged |
| `email_input_security_core_v1` | `email_input_security_core` |
| `property_maintenance_intake_v2` | `property_maintenance_intake` (rename first) |
| `property_maintenance_intake_v1` | `property_maintenance_intake` (then merge into it) |
| `property_maintenance_response_v1` | `property_maintenance_response` |
| `contractor_sourcing_v1` | `contractor_sourcing` |

---

### Task 1: Alembic migration `w0x1y2z3a4b5` + migration test

**Files:**
- Create: `alembic/versions/w0x1y2z3a4b5_drop_builtin_skill_name_suffixes.py`
- Test: `tests/test_migrations.py` (append one test; note the file's existing helpers `_alembic_config`, `make_engine`)

**Interfaces:**
- Produces: a migrated DB in which the old names no longer appear in `skills` (platform tier), `skill_versions.config["name"]`, `pipelines.config`, `pipeline_versions.config`, `builder_sessions.specification_json`, or `pipeline_dependencies.resource_name` — except where an org-tier skill shadows an old name (those orgs' configs/dependency rows keep the old name on purpose).
- Consumes: nothing from other tasks. Runs before the new seeding logic ever sees an existing DB (same release).

**Migration behaviour (write it exactly like this):**

Module-level:

```python
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
_OLD_NAMES = [old for old, _ in _SKILL_RENAMES]
```

Core routine `_merge_or_rename(bind, old, new)` (platform tier, i.e. `org_id IS NULL`):

- `old` row absent → return (fresh DB, or already migrated).
- `old` present, `new` absent → `UPDATE skills SET name = :new WHERE id = :old_id`.
- Both present (the intake merge, and also the create_all()+new-seeding race) →
  1. `UPDATE skill_versions SET skill_id = :new_id WHERE skill_id = :old_id`
  2. Renumber ALL of `new_id`'s versions ordered by `(created_at, id)` as 1..n. Two phases to dodge the `(skill_id, version_number)` unique constraint: first `UPDATE skill_versions SET version_number = version_number + 1000000 WHERE skill_id = :new_id`, then assign 1..n in order.
  3. `UPDATE pipeline_dependencies SET resource_id = :new_id WHERE resource_kind = 'skill' AND resource_id = :old_id`
  4. `DELETE FROM skills WHERE id = :old_id`
  - The surviving row's `current_version_id` is an id pointer — renumbering does not touch it. Do NOT move the head: `new`'s head stays whatever it was.
- Guard the whole routine with table/column existence checks exactly like `_rename_table_safely` in `o2p3q4r5s6t7` (if `skills` or `skill_versions` is missing, return).

After the rename/merge loop:

1. **Snapshot-name tidy-up:** for every `skill_versions` row whose `skill_id` is one of the renamed platform rows, load `config` JSON; if `config.get("name")` is in `_OLD_NAMES`, set it to the row's new skill name and UPDATE. (Runtime overrides this name via `resource_name`, so this is consistency only — but do it.)
2. **Shadow skip-set:** `shadowed = {(org_id, name) for org_id, name in SELECT org_id, name FROM skills WHERE org_id IS NOT NULL AND name IN _OLD_NAMES}`. An org that has its own skill under an old built-in name keeps that name everywhere in its data — its shadow must keep working.
3. **Config rewrites** — for each of `pipelines.config` (org via own `org_id`), `pipeline_versions.config` (org via join to `pipelines`), `builder_sessions.specification_json` (own `org_id`): parse the JSON (string or dict, same tolerance as `_rewrite_config_json_key` in the o2 migration), walk `parsed.get("agents", [])`, and inside each agent's `skills` list replace `old` with `new` unless `(row_org_id, old) in shadowed`. Only write back rows that changed. Platform-owned rows (`org_id IS NULL`, the bundled demo copies if any) rewrite unconditionally.
4. **Dependency names:** `SELECT pd.id, pd.resource_name, p.org_id FROM pipeline_dependencies pd JOIN pipeline_versions pv ON pd.pipeline_version_id = pv.id JOIN pipelines p ON pv.pipeline_id = p.id WHERE pd.resource_kind = 'skill' AND pd.resource_name IN _OLD_NAMES`; update `resource_name` to the new name unless `(org_id, resource_name) in shadowed`.

`downgrade()` raises:

```python
def downgrade() -> None:
    raise NotImplementedError(
        "w0x1y2z3a4b5 merges property_maintenance_intake_v1 into _v2's row; "
        "the merge is not mechanically reversible. Restore from backup instead."
    )
```

Module docstring: explain the rename, the merge, the shadow skip-set, and why the head is not moved (seeding, not the migration, is what installs new canonical content — Task 2).

**Steps:**

- [ ] **Step 1: Write the failing migration test.** Append to `tests/test_migrations.py` (reuse `_alembic_config` and `make_engine`; model the fixture on `test_workflow_to_pipeline_rename_preserves_data_and_rewrites_config`):

```python
# Revision just before the built-in skill rename (the previous head).
_PRE_SKILL_RENAME = "v9w0x1y2z3a4"


def test_builtin_skill_suffix_rename_merges_and_rewrites(tmp_path, monkeypatch):
    """w0x1y2z3a4b5: platform skills lose their _vN suffix, intake _v1+_v2
    merge into one skill (snapshot ids untouched, renumbered by created_at),
    every stored config/dependency reference is rewritten -- except inside an
    org that shadows an old name with its own skill."""
    db_path = tmp_path / "skills_rename.db"
    cfg = _alembic_config(db_path, monkeypatch)
    command.upgrade(cfg, _PRE_SKILL_RENAME)

    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(sa.text("INSERT INTO organizations (id, name, active) VALUES (1, 'acme', 1)"))
        conn.execute(sa.text("INSERT INTO organizations (id, name, active) VALUES (2, 'shadow_co', 1)"))
        # Platform intake _v1 + _v2 with one snapshot each; _v2 also carries an
        # admin edit as its v2 snapshot (heads must survive the merge).
        conn.execute(sa.text(
            "INSERT INTO skills (id, name, org_id, config, current_version_id) VALUES "
            "(1, 'property_maintenance_intake_v1', NULL, '{\"name\": \"property_maintenance_intake_v1\", \"instructions\": \"old\"}', NULL), "
            "(2, 'property_maintenance_intake_v2', NULL, '{\"name\": \"property_maintenance_intake_v2\", \"instructions\": \"new\"}', NULL), "
            "(3, 'contractor_sourcing_v1', NULL, '{\"name\": \"contractor_sourcing_v1\", \"instructions\": \"c\"}', NULL), "
            "(4, 'property_maintenance_response_v1', 2, '{\"name\": \"property_maintenance_response_v1\", \"instructions\": \"org own\"}', NULL)"
        ))
        conn.execute(sa.text(
            "INSERT INTO skill_versions (id, skill_id, version_number, config, created_by, created_at) VALUES "
            "(11, 1, 1, '{\"name\": \"property_maintenance_intake_v1\", \"instructions\": \"old\"}', NULL, '2026-01-01'), "
            "(12, 2, 1, '{\"name\": \"property_maintenance_intake_v2\", \"instructions\": \"new\"}', NULL, '2026-01-02'), "
            "(13, 3, 1, '{\"name\": \"contractor_sourcing_v1\", \"instructions\": \"c\"}', NULL, '2026-01-01'), "
            "(14, 4, 1, '{\"name\": \"property_maintenance_response_v1\", \"instructions\": \"org own\"}', NULL, '2026-01-03')"
        ))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 11 WHERE id = 1"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 12 WHERE id = 2"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 13 WHERE id = 3"))
        conn.execute(sa.text("UPDATE skills SET current_version_id = 14 WHERE id = 4"))
        # acme: deployed team pinned to intake _v2 snapshot; config names the old skill.
        acme_cfg = ('{"name": "team", "agents": [{"name": "a", "role": "r", "goal": "g", '
                    '"model": "fake:ok", "skills": ["property_maintenance_intake_v2", "contractor_sourcing_v1"]}], '
                    '"teams": [], "pipeline": {"steps": []}}')
        conn.execute(sa.text(
            "INSERT INTO pipelines (id, name, org_id, config, status, created_at, updated_at) "
            f"VALUES (21, 'team', 1, '{acme_cfg}', 'deployed', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO pipeline_versions (id, pipeline_id, version_number, config, created_by, created_at) "
            f"VALUES (31, 21, 1, '{acme_cfg}', NULL, '2026-01-01')"
        ))
        conn.execute(sa.text("UPDATE pipelines SET current_version_id = 31 WHERE id = 21"))
        conn.execute(sa.text(
            "INSERT INTO pipeline_dependencies (id, pipeline_version_id, resource_kind, resource_name, resource_id, resource_version_id) VALUES "
            "(41, 31, 'skill', 'property_maintenance_intake_v2', 2, 12), "
            "(42, 31, 'skill', 'contractor_sourcing_v1', 3, 13)"
        ))
        # shadow_co: its OWN skill uses a built-in old name; its team must keep the old name.
        shadow_cfg = ('{"name": "steam", "agents": [{"name": "a", "role": "r", "goal": "g", '
                      '"model": "fake:ok", "skills": ["property_maintenance_response_v1"]}], '
                      '"teams": [], "pipeline": {"steps": []}}')
        conn.execute(sa.text(
            "INSERT INTO pipelines (id, name, org_id, config, status, created_at, updated_at) "
            f"VALUES (22, 'steam', 2, '{shadow_cfg}', 'deployed', '2026-01-01', '2026-01-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO pipeline_versions (id, pipeline_id, version_number, config, created_by, created_at) "
            f"VALUES (32, 22, 1, '{shadow_cfg}', NULL, '2026-01-01')"
        ))
        conn.execute(sa.text("UPDATE pipelines SET current_version_id = 32 WHERE id = 22"))
        conn.execute(sa.text(
            "INSERT INTO pipeline_dependencies (id, pipeline_version_id, resource_kind, resource_name, resource_id, resource_version_id) "
            "VALUES (43, 32, 'skill', 'property_maintenance_response_v1', 4, 14)"
        ))
        # A wizard draft referencing an old name.
        conn.execute(sa.text(
            "INSERT INTO builder_sessions (id, intent_text, as_is_text, specification_json, status, org_id, feedback_history, created_at, updated_at) "
            f"VALUES ('sess-1', 'hi', '', '{acme_cfg}', 'spec', 1, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    engine.dispose()

    command.upgrade(cfg, "head")

    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            names = {r[0] for r in conn.execute(sa.text("SELECT name FROM skills WHERE org_id IS NULL"))}
            assert "property_maintenance_intake" in names
            assert "contractor_sourcing" in names
            assert not any(n.endswith("_v1") or n.endswith("_v2") for n in names)
            # Merge: one intake row; _v1's snapshot re-pointed with id kept, renumbered 1;
            # _v2's snapshot is version 2 and remains the head.
            merged = conn.execute(sa.text(
                "SELECT id, current_version_id FROM skills WHERE name = 'property_maintenance_intake' AND org_id IS NULL"
            )).one()
            versions = conn.execute(sa.text(
                "SELECT id, version_number FROM skill_versions WHERE skill_id = :sid ORDER BY version_number",
                ), {"sid": merged.id}).fetchall()
            assert [(v.id, v.version_number) for v in versions] == [(11, 1), (12, 2)]
            assert merged.current_version_id == 12
            # The org's own same-named skill is untouched.
            assert conn.execute(sa.text(
                "SELECT COUNT(*) FROM skills WHERE org_id = 2 AND name = 'property_maintenance_response_v1'"
            )).scalar() == 1
            # acme config + dependencies rewritten; pins (resource_version_id) untouched.
            acme = conn.execute(sa.text("SELECT config FROM pipelines WHERE id = 21")).scalar()
            assert "property_maintenance_intake_v2" not in acme and "property_maintenance_intake" in acme
            assert "contractor_sourcing_v1" not in acme and "contractor_sourcing" in acme
            dep = conn.execute(sa.text(
                "SELECT resource_name, resource_id, resource_version_id FROM pipeline_dependencies WHERE id = 41"
            )).one()
            assert dep.resource_name == "property_maintenance_intake"
            assert dep.resource_id == merged.id and dep.resource_version_id == 12
            # shadow_co keeps the old name everywhere.
            shadow = conn.execute(sa.text("SELECT config FROM pipelines WHERE id = 22")).scalar()
            assert "property_maintenance_response_v1" in shadow
            assert conn.execute(sa.text(
                "SELECT resource_name FROM pipeline_dependencies WHERE id = 43"
            )).scalar() == "property_maintenance_response_v1"
            # Draft rewritten like the head config.
            draft = conn.execute(sa.text("SELECT specification_json FROM builder_sessions WHERE id = 'sess-1'")).scalar()
            assert "property_maintenance_intake_v2" not in draft
    finally:
        engine.dispose()
```

Adjust INSERT column lists to the real schemas at `_PRE_SKILL_RENAME` if a column list is wrong (run the test; sqlite will name the missing/extra column).

- [ ] **Step 2: Run it to verify it fails** (revision doesn't exist yet):
  `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py::test_builtin_skill_suffix_rename_merges_and_rewrites -v` — expect FAIL (upgrade target `head` doesn't contain the new behaviour / assertions fail on old names).
- [ ] **Step 3: Write the migration** as specified above.
- [ ] **Step 4: Run the test again** — expect PASS. Also run the whole file: `./.venv/Scripts/python.exe -m pytest tests/test_migrations.py -v` (it's marked slow; be patient).
- [ ] **Step 5: Commit** — `git add alembic/versions/w0x1y2z3a4b5_drop_builtin_skill_name_suffixes.py tests/test_migrations.py && git commit -m "feat(skills): migration -- drop built-in _vN suffixes, merge intake v1+v2"`

---

### Task 2: Rename in code + seeding updates the platform tier

**Files:**
- Modify: `ui/backend/skills.py` (DEFAULT_SKILLS names + intake merge + comment block at ~120–150 + `seed_default_skills`)
- Modify: `ui/backend/pipelines/property_maintenance_inbox_demo.yaml` (skills lists)
- Modify: `ui/backend/email_trigger.py:581` (`_PROPERTY_MAINTENANCE_RESPONSE_SKILL = "property_maintenance_response"`, plus its docstring mentions)
- Modify: `ui/backend/automation_results.py` (comment mentions only)
- Modify: `ui/backend/deploy_validation.py:76` (comment mention only)
- Test: `tests/test_skill_seeding.py`

**Interfaces:**
- Consumes: migration from Task 1 (same release; test DBs use `create_all()` + seeding and never see old names).
- Produces: `DEFAULT_SKILLS` with five entries named `email_triage_reply`, `email_input_security_core`, `property_maintenance_intake` (the former `_v2` content), `property_maintenance_response`, `contractor_sourcing`. `seed_default_skills(db)` now: inserts absent skills; appends a version + moves head when the stored head config differs from `spec.to_raw()`; no-ops when equal.

**Steps:**

- [ ] **Step 1: Write the failing tests.** In `tests/test_skill_seeding.py`:
  - Global: replace old suffixed names in existing assertions (`property_maintenance_intake_v2` appears in a comment) with new names.
  - REWRITE `test_seed_never_overwrites_admin_edits` — its premise is inverted by design. Replace with:

```python
def test_seed_updates_a_changed_head_and_keeps_history(db_session):
    # The platform tier is locked against hand edits (crud rejects them), so
    # seeding treats any drift between the stored head and DEFAULT_SKILLS as
    # a platform release: append a version, move the head, keep the old
    # content reachable as history (deployed teams stay pinned to it).
    seed_default_skills(db_session)
    record = db_session.query(SkillRecord).filter_by(name="email_triage_reply").one()
    record.config = {**record.config, "instructions": "An older platform release."}
    db_session.commit()
    old_head_id = record.current_version_id

    seed_default_skills(db_session)

    record = db_session.query(SkillRecord).filter_by(name="email_triage_reply").one()
    canonical = next(s for s in DEFAULT_SKILLS if s.name == "email_triage_reply").to_raw()
    assert record.config == canonical
    versions = (
        db_session.query(SkillVersion)
        .filter_by(skill_id=record.id)
        .order_by(SkillVersion.version_number)
        .all()
    )
    assert versions[-1].id == record.current_version_id
    assert versions[-1].created_by is None  # a seeded release, not an admin save
    assert record.current_version_id != old_head_id


def test_seed_is_a_noop_when_content_matches(db_session):
    seed_default_skills(db_session)
    before = {
        (v.skill_id, v.version_number)
        for v in db_session.query(SkillVersion).all()
    }
    seed_default_skills(db_session)
    after = {
        (v.skill_id, v.version_number)
        for v in db_session.query(SkillVersion).all()
    }
    assert before == after


def test_no_builtin_name_carries_a_version_suffix(db_session):
    assert not any(s.name.endswith(("_v1", "_v2")) for s in DEFAULT_SKILLS)
```

  Add `SkillVersion` to the imports (`from ui.backend.db import ...` — confirm it's exported there; otherwise import from `ui.backend.db.models`).
- [ ] **Step 2: Run to verify failure:** `./.venv/Scripts/python.exe -m pytest tests/test_skill_seeding.py -v` — the suffix test and update test FAIL.
- [ ] **Step 3: Implement.**
  - `DEFAULT_SKILLS`: rename the four suffixed entries; DELETE the `property_maintenance_intake_v1` entry entirely (its content survives only in migrated DBs' history); the former `_v2` entry becomes `property_maintenance_intake` (update the `name=` and any self-references inside its instruction text if present).
  - Rewrite the stale comment block (~lines 120–150) that teaches "new behaviour ships as `_v2`, never a silent edit": the new story is "built-ins are updated in place by seeding; history lives in `skill_versions`; deployed teams stay pinned; admin customisation is an org-tier copy".
  - `seed_default_skills` — replace the loop body:

```python
    for spec in DEFAULT_SKILLS:
        record = existing.get(spec.name)
        canonical = spec.to_raw()
        if record is None:
            publish_skill_version(db, org_id=None, name=spec.name, config=canonical)
            changed = True
            continue
        if record.current_version_id is None:
            ensure_skill_version(db, record)
            changed = True
        if record.config != canonical:
            # The platform tier is locked against hand edits (crud.py), so a
            # drifted head means DEFAULT_SKILLS changed: publish the new
            # release. Teams pinned to the old version are unaffected.
            publish_skill_version(db, org_id=None, name=spec.name, config=canonical)
            changed = True
```

    and update the docstring (it currently promises "Never overwrites existing rows").
  - Demo YAML: `skills: [email_input_security_core, property_maintenance_intake]` and `skills: [email_input_security_core, property_maintenance_response]`.
  - `email_trigger.py`: constant → `"property_maintenance_response"`; fix the docstring's three `_v1` mentions. The shadow-aware `_resolves_to_platform_skill` logic needs no change — an org shadowing the OLD name simply no longer resolves to the platform tier, which is the correct outcome.
  - `automation_results.py` / `deploy_validation.py`: update the comment mentions.
- [ ] **Step 4: Run:** `./.venv/Scripts/python.exe -m pytest tests/test_skill_seeding.py tests/test_skill_versions.py tests/test_dependencies.py tests/test_email_trigger.py tests/test_automation_results_api.py -v` — all PASS. If another test file asserts an old name, fix it the same way (grep first: `grep -rn "_v1\b\|_v2\b" tests/`).
- [ ] **Step 5: Commit** — `git commit -am "feat(skills): suffix-free DEFAULT_SKILLS; seeding publishes changed built-ins"`

---

### Task 3: Lock the platform tier in crud + expose `builtin`

**Files:**
- Modify: `ui/backend/crud.py` (`_make_component_router.upsert_item` + the two GET payloads)
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `_BUILTIN_SKILL_NAMES` (already defined in crud.py from `DEFAULT_SKILLS` — post-Task 2 it holds the new names).
- Produces: PUT `/api/config/skills/{name}` without `?org=` for a built-in name → 409. Skills list/get payloads gain `"builtin": bool` (true iff `org_id IS NULL` and name ∈ `_BUILTIN_SKILL_NAMES`). Frontend (Task 5) relies on the `builtin` field.

**Steps:**

- [ ] **Step 1: Failing tests** in `tests/test_crud_api.py` (follow the file's existing client/fixture conventions — read its first ~60 lines first):

```python
def test_platform_builtin_skill_put_is_locked(admin_client):
    # The platform tier stays pristine so seeding can update it; customisation
    # is a copy into an org (same-name shadowing in load_skills).
    resp = admin_client.put(
        "/api/config/skills/email_triage_reply",
        json={"instructions": "hand edit"},
    )
    assert resp.status_code == 409
    assert "org" in resp.json()["detail"].lower()


def test_platform_builtin_skill_flagged_in_listing(admin_client):
    items = admin_client.get("/api/config/skills").json()
    flagged = {i["name"]: i.get("builtin") for i in items if i["org"] is None}
    assert flagged.get("email_triage_reply") is True


def test_org_copy_of_builtin_name_is_allowed_and_not_builtin(admin_client):
    resp = admin_client.put(
        "/api/config/skills/email_triage_reply?org=acme",
        json={"instructions": "org customisation"},
    )
    assert resp.status_code == 200
    item = admin_client.get("/api/config/skills/email_triage_reply?org=acme").json()
    assert item.get("builtin") is not True
```

  (Adapt fixture names — if the file uses a differently-named admin client fixture or must create org `acme` first, mirror an existing org-scoped test in the same file.)
- [ ] **Step 2: Run to verify failure:** `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k "builtin" -v`
- [ ] **Step 3: Implement.** In `upsert_item`, immediately after `org_id = _resolve_org_id(...)`:

```python
        if name == "skills" and org_id is None and item_name in _BUILTIN_SKILL_NAMES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"'{item_name}' is a platform built-in and can't be edited in "
                    "place -- save a copy under an organisation (?org=...) to "
                    "customise it; the copy shadows the built-in on redeploy."
                ),
            )
```

  In `list_items`' skills payload dict and `get_item`'s skills branch add
  `"builtin": item.org_id is None and item.name in _BUILTIN_SKILL_NAMES`.
- [ ] **Step 4: Run:** the three new tests PASS; whole file green: `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -v`
- [ ] **Step 5: Commit** — `git commit -am "feat(skills): lock platform built-ins against in-place edits; expose builtin flag"`

---

### Task 4: Versions + references endpoints

**Files:**
- Modify: `ui/backend/crud.py` (two new GET routes on the module-level `router`, after `_make_component_router` registrations at ~line 344)
- Test: `tests/test_crud_api.py`

**Interfaces:**
- Consumes: `SkillRecord`, `SkillVersion`, `PipelineDependency`, `PipelineVersion`, `PipelineRecord`, `Organization` (all already imported in crud.py); `_resolve_org_id`.
- Produces:
  - `GET /api/config/skills/{item_name}/versions?org=` → `[{"version_number": int, "created_by": str|None, "created_at": iso, "config": dict, "is_head": bool}]`, newest first.
  - `GET /api/config/skills/{item_name}/references?org=` → `[{"org_name": str|None, "org_display_name": str|None, "org_active": bool|None, "pipeline_name": str, "pipeline_version_number": int, "pinned_version_number": int|None, "is_current_deploy": bool}]`, ordered by pipeline name then newest pipeline version.

**Steps:**

- [ ] **Step 1: Failing tests** in `tests/test_crud_api.py`:

```python
def test_skill_versions_endpoint_lists_history_newest_first(admin_client):
    admin_client.put("/api/config/skills/hist?org=acme", json={"instructions": "one"})
    admin_client.put("/api/config/skills/hist?org=acme", json={"instructions": "two"})
    versions = admin_client.get("/api/config/skills/hist/versions?org=acme").json()
    assert [v["version_number"] for v in versions] == [2, 1]
    assert versions[0]["is_head"] is True and versions[1]["is_head"] is False
    assert versions[1]["config"]["instructions"] == "one"


def test_skill_versions_404_for_unknown_skill(admin_client):
    assert admin_client.get("/api/config/skills/nope/versions?org=acme").status_code == 404


def test_skill_references_lists_pins(admin_client, deployed_team_pinning_builtin):
    # Fixture (or inline setup mirroring the file's deploy tests): an org team
    # deployed with a pinned dependency on the built-in email_triage_reply.
    refs = admin_client.get("/api/config/skills/email_triage_reply/references").json()
    assert len(refs) == 1
    ref = refs[0]
    assert ref["pipeline_name"] and ref["pinned_version_number"] >= 1
    assert ref["is_current_deploy"] is True


def test_skill_references_empty_when_unreferenced(admin_client):
    refs = admin_client.get("/api/config/skills/contractor_sourcing/references").json()
    assert refs == []
```

  For the deployed fixture, reuse however `tests/test_dependencies.py` materialises `PipelineDependency` rows (deploy through the API if the file already does that; otherwise insert `PipelineRecord`/`PipelineVersion`/`PipelineDependency` rows directly in the test DB session — the endpoint only reads).
- [ ] **Step 2: Run to verify failure** (404 route missing): `./.venv/Scripts/python.exe -m pytest tests/test_crud_api.py -k "versions_endpoint or references" -v`
- [ ] **Step 3: Implement** in crud.py:

```python
@router.get("/skills/{item_name}/versions")
def list_skill_versions(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> list[Dict[str, Any]]:
    """Immutable version history for one skill, newest first (read-only)."""
    org_id = _resolve_org_id(db, org, allow_platform=True)
    item = db.query(SkillRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill '{item_name}'")
    versions = (
        db.query(SkillVersion)
        .filter_by(skill_id=item.id)
        .order_by(SkillVersion.version_number.desc())
        .all()
    )
    return [
        {
            "version_number": v.version_number,
            "created_by": v.created_by,
            "created_at": v.created_at.isoformat(),
            "config": v.config,
            "is_head": v.id == item.current_version_id,
        }
        for v in versions
    ]


@router.get("/skills/{item_name}/references")
def list_skill_references(
    item_name: str, org: Optional[str] = Query(None), db: Session = Depends(get_db)
) -> list[Dict[str, Any]]:
    """Every deployed pipeline version pinning this skill -- current and
    superseded -- so an admin can judge whether a definition is still served.
    Matches by resolved resource_id, falling back to resource_name for legacy
    rows that predate id resolution (dependencies.py)."""
    org_id = _resolve_org_id(db, org, allow_platform=True)
    item = db.query(SkillRecord).filter_by(name=item_name, org_id=org_id).one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail=f"Unknown skill '{item_name}'")
    rows = (
        db.query(PipelineDependency, PipelineVersion, PipelineRecord, Organization, SkillVersion)
        .join(PipelineVersion, PipelineDependency.pipeline_version_id == PipelineVersion.id)
        .join(PipelineRecord, PipelineVersion.pipeline_id == PipelineRecord.id)
        .outerjoin(Organization, PipelineRecord.org_id == Organization.id)
        .outerjoin(SkillVersion, PipelineDependency.resource_version_id == SkillVersion.id)
        .filter(PipelineDependency.resource_kind == "skill")
        .filter(
            or_(
                PipelineDependency.resource_id == item.id,
                and_(
                    PipelineDependency.resource_id.is_(None),
                    PipelineDependency.resource_name == item.name,
                ),
            )
        )
        .order_by(PipelineRecord.name, PipelineVersion.version_number.desc())
        .all()
    )
    return [
        {
            "org_name": org_rec.name if org_rec else None,
            "org_display_name": (org_rec.display_name or org_rec.name) if org_rec else None,
            "org_active": org_rec.active if org_rec else None,
            "pipeline_name": pipeline.name,
            "pipeline_version_number": version.version_number,
            "pinned_version_number": skill_version.version_number if skill_version else None,
            "is_current_deploy": (
                pipeline.status == "deployed"
                and pipeline.current_version_id == version.id
            ),
        }
        for dep, version, pipeline, org_rec, skill_version in rows
    ]
```

  Add `or_`, `and_` to the existing `from sqlalchemy import ...` line if absent. Check `PipelineVersion`'s FK column is `pipeline_id` and `PipelineRecord` has `status`/`current_version_id` (they do — models.py:296/336).
- [ ] **Step 4: Run:** new tests PASS; whole file green.
- [ ] **Step 5: Commit** — `git commit -am "feat(skills): admin version-history and pin-references endpoints"`

---

### Task 5: Advanced page — read-only built-ins, copy-to-org, version dropdown, references

**Files:**
- Modify: `ui/frontend/src/lib/types.ts` (ConfigItem gains `builtin?: boolean`; add `SkillVersionInfo`, `SkillReference`)
- Modify: `ui/frontend/src/lib/api.ts` (two new calls)
- Modify: `ui/frontend/src/pages/AdvancedPage.tsx`
- Test: `ui/frontend/src/pages/AdvancedPage.test.tsx`

**Interfaces:**
- Consumes: Task 3's `builtin` flag, Task 4's endpoints.
- Produces (types.ts):

```ts
export interface SkillVersionInfo {
  version_number: number
  created_by: string | null
  created_at: string
  config: Record<string, unknown>
  is_head: boolean
}

export interface SkillReference {
  org_name: string | null
  org_display_name: string | null
  org_active: boolean | null
  pipeline_name: string
  pipeline_version_number: number
  pinned_version_number: number | null
  is_current_deploy: boolean
}
```

  (api.ts, next to the other config calls, reusing its `orgQuery` helper:)

```ts
  skillVersions: (name: string, org?: string) =>
    request<SkillVersionInfo[]>(`/api/config/skills/${encodeURIComponent(name)}/versions${orgQuery(org)}`),
  skillReferences: (name: string, org?: string) =>
    request<SkillReference[]>(`/api/config/skills/${encodeURIComponent(name)}/references${orgQuery(org)}`),
```

**Behaviour to implement in AdvancedPage (skills tab only):**

1. New state: `versions: SkillVersionInfo[]`, `references: SkillReference[]`, `viewVersion: number | null` (null = head), `copyOrg: string` (default '').
2. In `select(id)` when `activeKey === 'skills'`: reset `viewVersion` to null and fire both fetches (`api.skillVersions(id, apiOrg)`, `api.skillReferences(id, apiOrg)`), each `.catch(() => [])` — the pane must not break if a fetch fails. Clear both in `resetSelection()`.
3. Header version display: replace the static `· v{selectedItem.version}` with a `<select>` over `versions` (`v{n}`, head option labelled `v{n} (current)`), value `viewVersion ?? head`. Selecting an older version shows `JSON.stringify(thatVersion.config, null, 2)` in the textarea, `readOnly`, with hint text `Historical version — read-only. Deployed teams pinned to it keep receiving exactly this content.` Selecting the head restores the editable JSON (`jsonText`).
4. Built-in lock: when `selectedItem?.builtin` is true, the textarea is always readOnly and Save/Delete are hidden; instead render:

```tsx
<p className="hint">
  Platform built-in — updated by platform releases and locked here. To
  customise it for one organisation, copy it: the organisation&apos;s copy
  shadows the built-in the next time a team is deployed.
</p>
<div className="wizard-actions">
  <select value={copyOrg} onChange={(e) => setCopyOrg(e.target.value)}>
    <option value="">Choose organisation…</option>
    {orgs.filter((o) => o.active).map((o) => (
      <option key={o.name} value={o.name}>{o.display_name || o.name}</option>
    ))}
  </select>
  <button className="btn btn-secondary" onClick={copyToOrg} disabled={!copyOrg || saving}>
    Copy to organisation
  </button>
</div>
```

   `copyToOrg` parses the currently shown config (head, not a historical view), strips `name`, and calls `api.putConfigItem('skills', selectedId, parsed, copyOrg)`; on success `setMessage(\`Copied to ${copyOrg}. The copy shadows the built-in on that organisation's next deploy.\`)`.
5. References section under the editor (skills tab, item selected):

```tsx
<h3>Referenced by deployed teams</h3>
{references.length === 0 ? (
  <p className="hint">No deployments reference this skill.</p>
) : (
  <ul className="skill-references">
    {references.map((r, i) => (
      <li key={i}>
        {(r.org_display_name ?? 'platform')} · {r.pipeline_name} · pinned v{r.pinned_version_number ?? '?'} ·{' '}
        {r.is_current_deploy ? 'current deploy' : 'superseded version'}
      </li>
    ))}
  </ul>
)}
```

**Steps:**

- [ ] **Step 1: Read `AdvancedPage.test.tsx` first** to mirror its api-mocking pattern, then write failing tests: (a) a built-in platform skill renders readOnly textarea, no Save button, and a "Copy to organisation" button that calls `putConfigItem` with the chosen org; (b) selecting an older version from the dropdown shows that version's config readOnly; (c) the references list renders rows from `skillReferences` and the empty state.
- [ ] **Step 2: Verify failure:** `cd ui/frontend && npm test -- AdvancedPage` (use the repo's actual test script — check `package.json` `scripts`; it may be `npx vitest run src/pages/AdvancedPage.test.tsx`).
- [ ] **Step 3: Implement** types.ts → api.ts → AdvancedPage.tsx as specified.
- [ ] **Step 4: Tests pass**, plus `npm run lint` and `npm run build` in `ui/frontend`.
- [ ] **Step 5: Commit** — `git commit -am "feat(admin): skill version browser, pin references, copy-to-org for locked built-ins"`

---

### Task 6: Org dropdowns default to active organisations

**Files:**
- Create: `ui/frontend/src/lib/orgs.ts`
- Modify: `ui/frontend/src/pages/AdvancedPage.tsx` (header org `<select>`), `ui/frontend/src/pages/TracePage.tsx` (org `<select>` at ~line 238)
- Test: `ui/frontend/src/lib/orgs.test.ts`

**Interfaces:**
- Produces:

```ts
// ui/frontend/src/lib/orgs.ts
import type { AdminOrg } from './types'

// Admin org dropdowns default to active organisations; a deactivated org that
// is already selected stays listed so the selection can't silently vanish.
export function visibleOrgOptions(
  orgs: AdminOrg[],
  showInactive: boolean,
  selected?: string | null,
): AdminOrg[] {
  if (showInactive) return orgs
  return orgs.filter((o) => o.active || o.name === selected)
}
```

**Steps:**

- [ ] **Step 1: Failing unit test** `ui/frontend/src/lib/orgs.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { visibleOrgOptions } from './orgs'
import type { AdminOrg } from './types'

const orgs: AdminOrg[] = [
  { name: 'a', active: true },
  { name: 'b', active: false },
]

describe('visibleOrgOptions', () => {
  it('hides inactive orgs by default', () => {
    expect(visibleOrgOptions(orgs, false).map((o) => o.name)).toEqual(['a'])
  })
  it('shows everything when asked', () => {
    expect(visibleOrgOptions(orgs, true).map((o) => o.name)).toEqual(['a', 'b'])
  })
  it('keeps a selected inactive org visible', () => {
    expect(visibleOrgOptions(orgs, false, 'b').map((o) => o.name)).toEqual(['a', 'b'])
  })
})
```

- [ ] **Step 2: Verify failure** (module missing), **Step 3: implement** `orgs.ts`, then in both pages: add `const [showInactiveOrgs, setShowInactiveOrgs] = useState(false)`, replace `orgs.map(...)` with `visibleOrgOptions(orgs, showInactiveOrgs, org).map(...)`, and render beside each dropdown:

```tsx
<label className="advanced-org-inactive">
  <input
    type="checkbox"
    checked={showInactiveOrgs}
    onChange={(e) => setShowInactiveOrgs(e.target.checked)}
  />
  Show deactivated
</label>
```

  (TracePage's dropdown has an "all organisations" empty option — keep it first, untouched.)
- [ ] **Step 4: Run** frontend tests for orgs + both pages' test files; `npm run lint && npm run build`.
- [ ] **Step 5: Commit** — `git commit -am "feat(admin): org dropdowns default to active organisations"`

---

### Task 7: Docs sweep + full local gates

**Files:**
- Modify: `ui/backend/CLAUDE.md` (~lines 405–445: the `_vN` naming story), `docs/ADMIN_MANUAL.md` (Advanced page section: locked built-ins, copy-to-org, version dropdown, references, org filter), `docs/STATUS.md` (done entry), root `CLAUDE.md` only if it mentions suffixed names (grep).
- No code.

**Steps:**

- [ ] **Step 1: Sweep for stragglers:** `grep -rn "email_input_security_core_v1\|property_maintenance_intake_v1\|property_maintenance_intake_v2\|property_maintenance_response_v1\|contractor_sourcing_v1" --include="*.py" --include="*.md" --include="*.yaml" --include="*.ts" --include="*.tsx" src/ ui/ docs/ tests/ alembic/ CLAUDE.md README.md` — remaining hits must be only: the migration file, historical specs/plans (leave those), and docs lines being rewritten in this task.
- [ ] **Step 2: Rewrite `ui/backend/CLAUDE.md`'s skills paragraph** to the new invariants (short, per the vital-only rule): platform built-ins are suffix-free, locked in the admin UI, updated by seeding (append + move head), customised via org-tier copies that shadow by name; deployed teams pin `skill_versions` ids.
- [ ] **Step 3: Update `docs/ADMIN_MANUAL.md` + `docs/STATUS.md`.**
- [ ] **Step 4: Full gates** (feedback rule: lint + build + test + e2e before push):
  - `./.venv/Scripts/python.exe -m pytest -m "not e2e"` (serial — this is the pre-merge stand-in for `backend-full`)
  - `cd ui/frontend && npm run lint && npm run build && npm test`
  - e2e: `./.venv/Scripts/python.exe -m pytest tests/e2e -m e2e` (ports 8000/5173 free; if the known 100%-CPU import-timeout blocker recurs, record the failure mode instead of looping)
- [ ] **Step 5: Commit** — `git commit -am "docs: suffix-free built-in skills; admin manual + status"`

---

## Self-review notes

- Spec coverage: migration → Task 1; DEFAULT_SKILLS/seeding → Task 2; platform lock + copy-to-org → Tasks 3+5; versions/references endpoints + UI → Tasks 4+5; org dropdown filter → Task 6; docs/rollout notes → Task 7. Rollout itself (VPS backup → `alembic upgrade head` → restart) is operational, not in this repo's diff.
- The `email_trigger.py` result-contract constant is functional, not cosmetic — covered explicitly in Task 2.
- Fixture column lists in Task 1's test are best-effort against the current schema; the step says to correct them against sqlite's error output rather than trust the plan blindly.
