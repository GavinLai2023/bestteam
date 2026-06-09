from __future__ import annotations

import os

from ..exceptions import ConfigurationError


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for current information and return a summary.

    Uses the Tavily API, which is optimised for LLM consumption (clean
    summaries, no HTML noise). Requires the TAVILY_API_KEY environment
    variable and the 'tavily-python' package.

    Args:
        query: The search query string.
        max_results: Maximum number of results to return (default 5).

    Returns:
        Formatted string with result titles, URLs, and content snippets.
    """
    try:
        from tavily import TavilyClient
    except ImportError as exc:
        raise ConfigurationError(
            "web_search requires the 'tavily-python' package. "
            "Install it with: pip install 'bestteam[tools-search]'"
        ) from exc

    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise ConfigurationError(
            "web_search requires the TAVILY_API_KEY environment variable. "
            "Get a free key at https://tavily.com and set it before running."
        )

    client = TavilyClient(api_key=api_key)
    response = client.search(query, max_results=max_results)

    results = response.get("results", [])
    if not results:
        return f"No results found for: {query}"

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', 'No title')}")
        lines.append(f"   URL: {r.get('url', '')}")
        snippet = r.get("content", "").strip()
        if snippet:
            lines.append(f"   {snippet}")
        lines.append("")

    return "\n".join(lines)
