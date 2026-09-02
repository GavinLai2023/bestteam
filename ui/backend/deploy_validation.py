"""Deploy-time validation that complements the SDK's structural validation.

`validate_specification` (SDK) resolves an agent's tools/skills/KB references,
and the wizard path enforces `AgentSpec.model: str`. But the operator CRUD path
builds `Agent(**spec)` directly (`core/loader._build_pipeline`), and
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
        if model.startswith("fake:") or model.startswith("fake-architect:") or model in allowed:
            continue
        problems.append(
            f"agent '{name}' uses model '{model}', which isn't available on this platform"
        )
    return problems


EMAIL_TOOL_NAMES = frozenset(
    {"email_find", "email_read", "email_read_attachment", "email_draft_reply"}
)
# Tools that can carry data to an arbitrary destination the model chooses.
EGRESS_TOOL_NAMES = frozenset({"http_get", "web_search"})


def find_email_egress_conflicts(agent_tool_sets) -> List[str]:
    """Return a problem string if this pipeline holds both an email tool and a
    general-purpose egress tool ANYWHERE -- on one agent or spread across
    several. Empty list means the combination is absent.

    Email bodies are attacker-controlled input to the model, and the toolkit's
    containment story is that the worst outcome is a bad draft a human reviews
    -- there is no send verb anywhere. That bound only holds while the mail
    cannot reach a route out. Give the same agent `http_get` and an injected
    message can direct it to put mailbox or knowledge-base content into a URL
    it fetches, which exfiltrates without ever sending an email (Phase 0,
    item 0.6).

    Splitting the two capabilities across separate agents does NOT restore the
    bound, which is what this check originally assumed. `_agent_node`
    (`adapters/langgraph_adapter.py`) feeds each agent's output into the next
    agent's context, and a pipeline's steps share state, so an injected
    instruction read by the mail agent arrives in the egress agent's prompt as
    ordinary text. The check is therefore pipeline-level and deliberately
    blunt: it does not try to reason about ordering or collaboration mode,
    because that reasoning would have to be redone -- correctly -- every time
    routing changes, and a wrong answer is an exfiltration path. No shipped
    pipeline combines the two, so nothing legitimate is refused.

    Prompt-level defences (the `email_input_security_core` skill) reduce the
    likelihood but are not a boundary, so this refuses the combination at
    deploy instead. `agent_tool_sets` is an iterable of `(agent_name, tools)`
    with each agent's tool names ALREADY resolved through its skills -- the
    caller does that, exactly as `email_tools.spec_uses_email` does.
    """
    # Materialise once: the caller may pass a generator, and this reads twice.
    pairs = [(name, set(tools)) for name, tools in agent_tool_sets]
    email_agents = [name for name, tools in pairs if tools & EMAIL_TOOL_NAMES]
    egress_agents = [name for name, tools in pairs if tools & EGRESS_TOOL_NAMES]
    if not email_agents or not egress_agents:
        return []
    egress_tools = sorted({t for _, tools in pairs for t in tools & EGRESS_TOOL_NAMES})
    tool_list = ", ".join(egress_tools)
    if email_agents == egress_agents == [email_agents[0]]:
        where = f"agent '{email_agents[0]}' combines email access with {tool_list}"
    else:
        where = (
            f"agent '{email_agents[0]}' reads email while agent "
            f"'{egress_agents[0]}' has {tool_list}, and a pipeline's agents "
            "share what they produce"
        )
    return [
        f"{where}, which would let a malicious email send mailbox content to "
        "an outside address"
    ]


def find_kb_tool_collisions(
    raw_spec: Dict[str, Any],
    standalone_kb_names: Iterable[str],
    builtin_names: Iterable[str],
) -> List[str]:
    """Return the KB names that would shadow a built-in tool.

    All tools resolve through one flat name->tool lookup, so a knowledge base
    named after a built-in silently shadows it. This returns the sorted,
    de-duplicated KB names -- inline (`raw_spec["knowledge_bases"][*].name`) plus
    the referenced standalone KB names -- that collide with `builtin_names`.
    """
    builtin = set(builtin_names)
    inline = {
        kb.get("name")
        for kb in raw_spec.get("knowledge_bases", []) or []
        if isinstance(kb, dict) and kb.get("name")
    }
    names = inline | set(standalone_kb_names)
    return sorted(n for n in names if n in builtin)
