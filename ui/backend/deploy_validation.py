"""Deploy-time validation that complements the SDK's structural validation.

`validate_specification` (SDK) resolves an agent's tools/skills/KB references,
but not that its model is one the platform actually offers. A bad model spec
would otherwise pass deploy and fail only at first run. This checks agent model
specs against the model catalog so the failure surfaces at deploy.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


def validate_agent_models(raw_spec: Dict[str, Any], catalog_specs: Iterable[str]) -> List[str]:
    """Return the agent model specs in `raw_spec` that the platform doesn't offer.

    A spec is offered if it is in `catalog_specs`. `fake:` specs (deterministic,
    zero-cost demo/test models) are always allowed and never reported. The result
    keeps first-seen order and is de-duplicated so the caller can name every
    rejected model at once.
    """
    allowed = set(catalog_specs)
    unknown: List[str] = []
    seen = set()
    for agent in raw_spec.get("agents", []) or []:
        if not isinstance(agent, dict):
            continue
        model = agent.get("model")
        if not model or model.startswith("fake:"):
            continue
        if model not in allowed and model not in seen:
            unknown.append(model)
        seen.add(model)
    return unknown
