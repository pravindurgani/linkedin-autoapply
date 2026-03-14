"""Base class for all job sources."""

from abc import ABC, abstractmethod
from auto_apply.models import Job


class BaseJobSource(ABC):
    """Abstract base for job scrapers."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def scrape(self, search_terms: list[str], location: str, min_salary: int) -> list[Job]:
        """Scrape jobs matching criteria. Returns list of Job objects."""
        ...
