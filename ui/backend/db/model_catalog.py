"""CRUD for `ModelCatalogEntry` (Phase 3).

Maps model spec strings to customer-friendly names, complexity tiers, and
per-1K-token pricing -- see `docs/team_builder_methodology.md`, Phase 3.
`to_prompt_text()` renders the catalog for the Solution Architect's prompt
(`ui/backend/builder.py`); `record_usage` (`db/usage.py`) looks entries up by
`spec` to convert token counts into a `cost_estimate`.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy.orm import Session

from .models import ModelCatalogEntry

# Seeded into a fresh database so the wizard has something to choose from out
# of the box. `fake:` entries are $0 and exist for demos/dry runs; the
# provider entries are illustrative starting points an operator should adjust
# to their actual negotiated pricing.
DEFAULT_MODEL_CATALOG: List[Dict[str, Any]] = [
    {
        "spec": "fake:ok",
        "display_name": "Demo Assistant (free, for testing)",
        "description": "A $0 scripted assistant for dry runs and demos. No API key required.",
        "tier": "fast",
        "input_price_per_1k": 0.0,
        "output_price_per_1k": 0.0,
    },
    {
        "spec": "openai:gpt-4o-mini",
        "display_name": "Quick Assistant (fast & affordable)",
        "description": "Best for simple, high-volume tasks like drafting replies or summarizing.",
        "tier": "fast",
        "input_price_per_1k": 0.00015,
        "output_price_per_1k": 0.0006,
    },
    {
        "spec": "openai:gpt-4o",
        "display_name": "Senior Assistant (smarter but slower)",
        "description": "Best for complex reasoning, coordination, or high-stakes responses.",
        "tier": "advanced",
        "input_price_per_1k": 0.0025,
        "output_price_per_1k": 0.01,
    },
]


# `tier` value marking an entry as an embedding model rather than a chat
# model. Embedding entries share this table so `db/usage.py::record_usage` can
# price a knowledge base's embedding spend from one catalog -- they are
# deliberately NOT in `DEFAULT_MODEL_CATALOG`, since their prices depend on the
# provider an operator actually uses.
EMBEDDING_TIER = "embedding"


def list_entries(db: Session) -> List[ModelCatalogEntry]:
    return db.query(ModelCatalogEntry).order_by(ModelCatalogEntry.spec).all()


def list_chat_entries(db: Session) -> List[ModelCatalogEntry]:
    """Every entry except the embedding models.

    Anything that offers a model as an agent's `model` -- the wizard's
    catalog endpoint, the Solution Architect's prompt, smart search's default
    chat model -- wants this, not `list_entries`: an embedding model handed to
    an agent produces a team that cannot answer anything. Admin CRUD still
    uses `list_entries`, because somebody has to maintain those prices.

    Deliberately does NOT filter fake:/fake-architect: entries: neither is
    ever in `DEFAULT_MODEL_CATALOG`, so a real deployment's catalog never
    holds one unless an admin adds it themselves -- and this same
    customer-facing endpoint is the one channel the E2E harness has to make
    `pickDefaultModel()`/`ModelPicker` resolve to `fake-architect:e2e` (see
    `docs/superpowers/specs/2026-08-13-e2e-and-ci-test-tiering-design.md`,
    "Fake-architect mechanism"). A prior attempt to filter both prefixes out
    here (2026-08-20) broke that harness: the E2E fixture reshapes a catalog
    down to just `fake:ok` + `fake-architect:e2e`, and filtering both left
    this endpoint empty, permanently disabling the wizard's "Start building
    my team" button (Codex review finding, 2026-08-21).
    """
    return (
        db.query(ModelCatalogEntry)
        .filter(ModelCatalogEntry.tier != EMBEDDING_TIER)
        .order_by(ModelCatalogEntry.spec)
        .all()
    )


def get_entry(db: Session, spec: str) -> Optional[ModelCatalogEntry]:
    return db.query(ModelCatalogEntry).filter_by(spec=spec).one_or_none()


def upsert_entry(db: Session, spec: str, **fields: Any) -> ModelCatalogEntry:
    if fields.get("is_default"):
        # At most one entry is ever the default -- clear every other row's
        # flag in the same call rather than requiring the caller (the CRUD
        # route, or any other upsert_entry caller) to do it themselves.
        db.query(ModelCatalogEntry).filter(ModelCatalogEntry.spec != spec).update(
            {"is_default": False}
        )
    entry = get_entry(db, spec)
    if entry is None:
        entry = ModelCatalogEntry(spec=spec, **fields)
        db.add(entry)
    else:
        for key, value in fields.items():
            setattr(entry, key, value)
    db.commit()
    db.refresh(entry)
    return entry


def delete_entry(db: Session, spec: str) -> bool:
    entry = get_entry(db, spec)
    if entry is None:
        return False
    db.delete(entry)
    db.commit()
    return True


def seed_default_catalog(db: Session) -> None:
    """Populate `model_catalog` with `DEFAULT_MODEL_CATALOG` if it's empty."""
    if db.query(ModelCatalogEntry).first() is not None:
        return
    for entry in DEFAULT_MODEL_CATALOG:
        db.add(ModelCatalogEntry(**entry))
    db.commit()


def to_prompt_text(entries: Sequence[ModelCatalogEntry]) -> str:
    """Render the catalog as plain text for the Solution Architect's prompt.

    Each line gives the architect everything it needs to pick a model by
    role complexity and reuse the exact `spec` string in `AgentSpec.model`.
    """
    if not entries:
        return ""
    lines = ["Available models (use the spec string exactly as shown for an agent's `model`):"]
    for entry in entries:
        lines.append(
            f'- spec: "{entry.spec}" -- "{entry.display_name}" (tier: {entry.tier}). '
            f"{entry.description} "
            f"Price: ${entry.input_price_per_1k:.5f}/1K input tokens, "
            f"${entry.output_price_per_1k:.5f}/1K output tokens."
        )
    return "\n".join(lines)
