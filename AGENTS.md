# bestteam — agent instructions

The project's agent instructions live in **`CLAUDE.md`** files, one per
directory that needs one. This file exists only so that tools which look for
`AGENTS.md` (Codex and others) find the same instructions Claude Code does.

Read these, in this order, before working in the corresponding directory:

- `CLAUDE.md` (repo root) — architecture, common commands, known limitations,
  testing notes. Always read it first.
- `src/bestteam/CLAUDE.md` — SDK core (`Agent`/`Team`/`Pipeline`, the
  `EngineAdapter` seam).
- `src/bestteam/core/CLAUDE.md` — structured outputs, knowledge bases, memory.
- `src/bestteam/tools/CLAUDE.md` — built-in tools and their trust boundaries.
- `ui/backend/CLAUDE.md` — FastAPI backend.
- `ui/backend/db/CLAUDE.md` — SQLAlchemy persistence schema.
- `ui/frontend/CLAUDE.md` — React/Vite dashboard and Team Builder wizard.

Do not duplicate content here. An earlier copy of this file was a fork of
`CLAUDE.md` that drifted for weeks and described limitations the code no
longer had; keeping one source of truth is the fix.
