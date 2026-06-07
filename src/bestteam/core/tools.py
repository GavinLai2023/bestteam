from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, Optional


class ToolKit:
    """A named, instance-owned collection of tools that can be attached to agents.

    Customers write plain Python functions and register them by decorating —
    the framework hides whatever schema/binding format the underlying engine
    expects from a "tool".

        research_tools = ToolKit("research")

        @research_tools.register
        def web_search(query: str) -> str:
            ...

        analyst = Agent(..., tools=list(research_tools))
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._tools: Dict[str, Callable[..., Any]] = {}

    def register(
        self,
        func: Optional[Callable[..., Any]] = None,
        *,
        name: Optional[str] = None,
    ) -> Callable[..., Any]:
        def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
            self._tools[name or f.__name__] = f
            return f

        if func is not None:
            return decorator(func)
        return decorator

    def get(self, name: str) -> Callable[..., Any]:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"ToolKit '{self.name}' has no tool named '{name}'") from exc

    def __iter__(self) -> Iterator[Callable[..., Any]]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
