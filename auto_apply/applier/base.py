"""Base class for auto-apply engines."""

from abc import ABC, abstractmethod


class BaseApplier(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def apply(self, job: dict) -> tuple[bool, str]:
        """Attempt to apply. Returns (success, message)."""
        ...
