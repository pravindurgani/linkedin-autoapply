"""Base class for auto-apply engines."""

from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext


class BaseApplier(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    async def apply(
        self, job: dict, context: BrowserContext | None = None
    ) -> tuple[bool, str, str | None, str | None]:
        """Attempt to apply. Returns (success, message, screenshot_path, failure_url).

        Args:
            job: Job dict with at least 'url', 'title', and 'easy_apply' keys.
            context: Optional shared BrowserContext from linkedin_session(). If
                provided, a new Page is created within it and closed on return —
                the context itself is not touched. If None, a standalone browser
                lifecycle is used (scrape subcommand path).

        Returns:
            (success, message, screenshot_path, failure_url).
            screenshot_path and failure_url are None on success or permanent skips
            (external-apply, connect-button). They are populated on FAILED outcomes
            where a page was rendered but the apply could not proceed.
        """
        ...
