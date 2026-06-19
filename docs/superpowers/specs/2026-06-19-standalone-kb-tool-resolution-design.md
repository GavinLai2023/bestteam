# Wire standalone knowledge bases into workflow tool resolution

## Context

`/api/config/knowledge_bases` (manual JSON, and now file upload) lets a user
create a standalone `KnowledgeBaseRecord` in the database. Today nothing
ever reads it back into an actual `KnowledgeBase` instance — a workflow can
only use a knowledge base it embeds inline in its own `knowledge_bases:`
list (`core/loader.py::_build_workflow`, which only loops over
`raw.get("knowledge_bases", [])`). This is `crud.py`'s own documented
behavior: "agents/teams/knowledge_bases are validated as standalone
components ... they aren't cross-referenced into a workflows config
automatically."

This means every standalone KB created via the Advanced page — including
ones created through the new upload feature — sits unused unless someone
also copy-pastes its path/config into a workflow's own `knowledge_bases:`
list by hand. This spec wires standalone KBs into the same tool-resolution
path the loader already uses for inline ones, so creating a KB via Advanced
(JSON or upload) is enough to make it usable by name in any workflow's
`tools:` list — no duplication required.

## Scope

In scope: `ui/backend/main.py::_get_workflow` (powers `POST /api/runs` and
`GET /api/workflows/{name}/graph`) and `ui/backend/crud.py`'s workflow `PUT`
validation route. Both currently hardcode `extra_tools={}` when calling
`_build_workflow`.

Out of scope: the Team Builder wizard (`ui/backend/builder.py`'s 6 call
sites into `validate_specification`/`generate_specification`, which also
never pass `extra_tools` today) — a separate, larger follow-up since it also
touches what the LLM-driven Solution Architect needs to know about.

## Design

### New helper: `ui/backend/knowledge_bases.py::load_knowledge_base_tools`

Mirrors the existing `skills.py::load_skills(db)` pattern, but builds only
the standalone KBs a given workflow's raw config actually references —
not every standalone KB in the database. Building a knowledge base means
re-reading and re-chunking every file (and, for `type: vector`, calling an
embedding model), so unconditionally rebuilding every standalone KB on
every `/api/runs` call would pay that cost for KBs that have nothing to do
with the workflow being run.

```python
def load_knowledge_base_tools(db: Session, raw: Dict[str, Any], source: Path) -> Dict[str, Any]:
    """Build a name -> tool mapping for only the standalone knowledge bases
    `raw`'s agents actually reference by name in their `tools:` lists."""
    referenced = {
        tool_name
        for agent in raw.get("agents", [])
        for tool_name in agent.get("tools", [])
    }
    if not referenced:
        return {}

    records = db.query(KnowledgeBaseRecord).filter(KnowledgeBaseRecord.name.in_(referenced)).all()
    tools: Dict[str, Any] = {}
    for record in records:
        kb = _build_knowledge_base(record.config, source)
        tools[kb.name] = make_knowledge_base_tool(kb)
    return tools
```

This reuses `core/loader.py::_build_knowledge_base` (the same private
function the loader already uses to build *inline* KBs — same path
resolution, same type dispatch, same error type) and
`core/knowledge_base.py::make_knowledge_base_tool`, rather than
duplicating either.

### Call-site changes

`main.py::_get_workflow` and `crud.py`'s workflow `PUT` route both already
have `raw`/`record.config` and a `source` path in scope at the point they
call `_build_workflow`. Each changes its `extra_tools={}` to
`extra_tools=load_knowledge_base_tools(db, raw, source)`.

### Resolution priority

No new code needed for this — it falls out of the existing merge order in
`core/loader.py:68-72`:

```python
tool_lookup = {**_TOOL_REGISTRY, **extra_tools}   # built-ins, then standalone KBs
...
for spec in raw.get("knowledge_bases", []):        # then inline KBs, last — wins on name collision
    tool_lookup[kb.name] = make_knowledge_base_tool(kb)
```

So a workflow's own inline `knowledge_bases:` entry always wins over a
standalone KB of the same name — the workflow's own config stays
authoritative, matching today's behavior for everything else in
`_build_workflow`.

### Failure mode

A referenced standalone KB that fails to build (bad/missing path, missing
embedding-model API key, etc.) raises `ConfigurationError` immediately,
exactly like an inline KB does today — no special-casing, no partial tool
lookups. A workflow that references a broken standalone KB fails to load;
workflows that don't reference it are unaffected, since only referenced
KBs are built at all.

### Relative paths

A standalone KB's `path` is resolved against the same `source` anchor
already passed to `_build_workflow` at each call site (`main.py` uses
`WORKFLOWS_DIR / f"{name}.yaml"`; `crud.py` uses the same pattern for
validation) — no new path-resolution concept. Uploaded KBs already store an
absolute path, so this only matters for a manually-JSON-created KB with a
relative `path`.

## Testing

- A standalone KB (created via the existing PUT route or the upload
  endpoint) referenced by name in a workflow's `tools:` list is queryable
  in a real run (extends the pattern already used in
  `test_uploaded_kb_is_queryable_by_a_workflow`, but this time *without*
  embedding the KB inline — proving the new wiring, not the fallback).
- A workflow referencing an unknown KB name (no standalone record, no
  inline entry) still gets today's existing `"Unknown tool '<name>'..."`
  error unchanged.
- A workflow with an inline `knowledge_bases:` entry whose name collides
  with a standalone KB uses the inline one's content (proves resolution
  priority).
- A workflow referencing a standalone KB whose path no longer exists fails
  to load with the existing `ConfigurationError` message (proves failure
  mode), while a second, unrelated workflow that doesn't reference it
  loads fine in the same test (proves only-referenced-KBs-are-built keeps
  blast radius contained to workflows that actually use it).
