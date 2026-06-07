from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class Memory(ABC):
    """Storage abstraction so customers don't have to pick a vector store or
    persistence backend before they can write their first workflow.

    Production deployments can swap in a Redis-, Postgres-, or vector-store
    backed implementation behind this same interface.
    """

    @abstractmethod
    def remember(self, key: str, value: Any) -> None:
        """Persist a value under a key."""

    @abstractmethod
    def recall(self, key: str) -> Optional[Any]:
        """Retrieve a previously remembered value, or None if absent."""


class InMemoryStore(Memory):
    """Default in-process memory — good for development and simple deployments."""

    def __init__(self) -> None:
        self._data: Dict[str, Any] = {}

    def remember(self, key: str, value: Any) -> None:
        self._data[key] = value

    def recall(self, key: str) -> Optional[Any]:
        return self._data.get(key)
