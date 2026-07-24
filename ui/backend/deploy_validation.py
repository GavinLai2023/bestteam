"""Deploy-time validation that complements the SDK's structural validation.

`validate_specification` (SDK) resolves an agent's tools/skills/KB references,
and the wizard path enforces `AgentSpec.model: str`. But the operator CRUD path
builds `Agent(**spec)` directly (`core/loader._build_workflow`), and
`Agent.model` is `ModelSpec | None` -- so a missing, `None`, empty, or
non-string model, or a real spec the platform doesn't offer, would otherwise
pass deploy and fail only at first run (the P1-11 defect). This rejects all of
those at deploy.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List


def validate_agent_models(raw_spec: Dict[str, Any], catalog_specs: Iterable[str]) -> List[str]:
    """Return a problem string for each agent whose model can't be deployed.

    A deployable model is a non-empty **string** that is either a `fake:` spec
    (deterministic, zero-cost demo/test model) or a member of `catalog_specs`.
    An agent whose model is missing, `None`, empty, non-string, or not in the
    catalog yields one problem string naming the agent and the reason, so the
    caller can reject the deploy and name every problem at once (empty list =
    all models deployable). Non-dict `agents` entries are left to the SDK's
    structural validation and skipped here.
    """
    allowed = set(catalog_specs)
    problems: List[str] = []
    for index, agent in enumerate(raw_spec.get("agents", []) or []):
        if not isinstance(agent, dict):
            continue
        name = agent.get("name") or f"#{index}"
        model = agent.get("model")
        if not isinstance(model, str) or not model:
            problems.append(f"agent '{name}' has no model set")
            continue
        if model.startswith("fake:") or model in allowed:
            continue
        problems.append(
            f"agent '{name}' uses model '{model}', which isn't available on this platform"
        )
    return problems
