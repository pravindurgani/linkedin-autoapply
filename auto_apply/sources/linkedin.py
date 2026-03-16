"""LinkedIn job scraper — Playwright-based with login."""

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from urllib.parse import quote
from playwright.async_api import async_playwright, Page, BrowserContext
from auto_apply.config import HEADLESS, BROWSER_TIMEOUT, RATE_LIMIT_LINKEDIN
from auto_apply.browser import COOKIES_PATH
from auto_apply.models import Job, JobSource
from auto_apply.sources.base import BaseJobSource

log = logging.getLogger(__name__)
MAX_PAGES = 3  # 25 jobs/page

# ── Runtime config (set via configure()) ──
_linkedin_email: str = ""
_linkedin_password: str = ""
_visible: bool = False

# ── Company name selector chain ──
# Priority-ordered: most specific / most stable anchor first, broadest last.
# LinkedIn's card DOM class names change with product updates — add new confirmed
# selectors at the front and keep old ones as fallbacks for accounts still on
# the previous layout. "Unknown" is only the result if every selector fails.
_COMPANY_SELECTORS: list[str] = [
    ".job-card-container__primary-description",        # legacy (pre-2025)
    ".artdeco-entity-lockup__subtitle span",           # 2024–2025 artdeco layout
    "[class*='job-card-list__company-name']",          # BEM variant
    "[class*='job-card-container__company-name']",     # alternative container class
    "[class*='company-name']",                         # generic company-name class
    "[data-tracking-control-name*='company']",         # tracking attribute anchor
]


def configure(email: str, password: str, visible: bool = False) -> None:
    """Set LinkedIn credentials and browser visibility for the scraper at runtime.

    Args:
        email: LinkedIn account email.
        password: LinkedIn account password.
        visible: If True, override HEADLESS and run browser in visible mode.
    """
    global _linkedin_email, _linkedin_password, _visible
    _linkedin_email = email
    _linkedin_password = password
    _visible = visible


class LinkedInSource(BaseJobSource):
    name = "linkedin"

    async def scrape(
        self,
        search_terms: list[str],
        location: str,
        min_salary: int,
        visible: bool = False,
        context: BrowserContext | None = None,
    ) -> list[Job]:
        """Scrape LinkedIn jobs for the given search terms.

        Args:
            search_terms: List of job title keywords to search.
            location: LinkedIn location string (e.g. "Manchester, England").
            min_salary: Minimum salary filter (used for post-scrape filtering).
            visible: If True, override HEADLESS for this session.
            context: Shared BrowserContext from linkedin_session(). If provided,
                the scraper creates one Page within it and closes that page on
                return — the context is not closed. If None, a standalone browser
                lifecycle is used (scrape subcommand path).

        Returns:
            List of Job objects scraped from LinkedIn.
        """
        if not _linkedin_email or not _linkedin_password:
            raise RuntimeError(
                "LinkedInSource credentials not set — call configure(email, password) before scrape()"
            )

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        _headless = False if (visible or _visible) else HEADLESS

        if context is not None:
            # Shared-session path: use the provided context; do NOT close it.
            # Cookie restore is already done by linkedin_session() before yield.
            # Cookie save is done by linkedin_session() finally block — not here.
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)
            try:
                await self._ensure_logged_in(page, context, headless=_headless)
                for term in search_terms:
                    try:
                        jobs = await self._search_term(page, term, location, min_salary)
                        for j in jobs:
                            if j.external_id not in seen_ids:
                                seen_ids.add(j.external_id)
                                all_jobs.append(j)
                    except Exception as e:
                        log.error(f"LinkedIn search failed for '{term}': {e}")
                    await asyncio.sleep(random.uniform(8, 15))
            finally:
                await page.close()  # page only; context is owned by the caller
        else:
            # Standalone path: own the full Playwright lifecycle.
            # Used by the `scrape` subcommand which calls run_scrape() without a context.
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=_headless)
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    # No hardcoded user_agent — use Playwright's current Chromium default.
                )
                page = await ctx.new_page()
                page.set_default_timeout(BROWSER_TIMEOUT)
                try:
                    await self._ensure_logged_in(page, ctx, headless=_headless)
                    for term in search_terms:
                        try:
                            jobs = await self._search_term(page, term, location, min_salary)
                            for j in jobs:
                                if j.external_id not in seen_ids:
                                    seen_ids.add(j.external_id)
                                    all_jobs.append(j)
                        except Exception as e:
                            log.error(f"LinkedIn search failed for '{term}': {e}")
                        await asyncio.sleep(random.uniform(8, 15))
                    cookies = await ctx.cookies()
                    COOKIES_PATH.write_text(json.dumps(cookies))
                finally:
                    await browser.close()

        log.info(f"LinkedIn: {len(all_jobs)} jobs found")
        return all_jobs

    async def _ensure_logged_in(self, page: Page, context: BrowserContext, headless: bool = True):
        # Try loading saved cookies first
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text())
                await context.add_cookies(cookies)
                await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if "feed" in page.url:
                    log.info("LinkedIn: restored session from cookies")
                    return
            except Exception:
                pass

        # Fresh login
        log.info("LinkedIn: logging in...")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(1)

        await page.fill('#username', _linkedin_email)
        await page.fill('#password', _linkedin_password)
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)

        # Check for verification challenge
        if "checkpoint" in page.url or "challenge" in page.url:
            log.warning("LinkedIn: verification challenge detected. Run with --visible flag to solve manually.")
            if not headless:
                await page.wait_for_url("**/feed/**", timeout=120_000)
            else:
                raise RuntimeError("LinkedIn requires manual verification. Run with --visible flag.")

        log.info("LinkedIn: logged in successfully")

    async def _search_term(self, page: Page, keywords: str, location: str,
                           min_salary: int) -> list[Job]:
        jobs = []

        # LinkedIn salary filter: f_SB2 parameter (6 = £80k+, 7 = £100k+)
        # We use 6 (£80k+) to cast a slightly wider net, then filter in post
        salary_filter = "6"  # £80,000+

        for page_num in range(MAX_PAGES):
            start = page_num * 25
            url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={quote(keywords)}"
                f"&location={location}"
                f"&f_SB2={salary_filter}"
                f"&f_WT=2"  # On-site and hybrid
                f"&f_JT=F"  # Full-time
                f"&f_AL=true"  # Easy Apply only
                f"&sortBy=DD"  # Date posted
                f"&start={start}"
            )

            await page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(3)

            # Parse job cards from the list
            cards = await page.query_selector_all('.job-card-container, .jobs-search-results__list-item')
            if not cards:
                log.debug(f"LinkedIn: no cards on page {page_num + 1} for '{keywords}'")
                break

            for card in cards:
                try:
                    job = await self._parse_card(card)
                    if job:
                        jobs.append(job)
                except Exception as e:
                    log.debug(f"LinkedIn card parse error: {e}")

            # Single summary warning per page — replaces per-card WARNING spam.
            # Only fires when the company selector chain fails for some or all cards.
            unknown_count = sum(1 for j in jobs if j.company == "Unknown")
            if unknown_count:
                log.warning(
                    f"{unknown_count}/{len(jobs)} jobs on page {page_num + 1} "
                    f"for '{keywords}' had unknown company "
                    f"— company selector chain may need updating (see Step 10.3)"
                )

            await asyncio.sleep(RATE_LIMIT_LINKEDIN)

        return jobs

    async def _parse_card(self, card) -> Job | None:
        # Title
        title_el = await card.query_selector('.job-card-list__title, a[class*="job-card-list__title"]')
        if not title_el:
            return None
        title = (await title_el.inner_text()).strip().split("\n")[0].strip()

        # Link and ID
        link_el = await card.query_selector('a[href*="/jobs/view/"]')
        url = ""
        external_id = ""
        if link_el:
            href = await link_el.get_attribute("href") or ""
            url = href if href.startswith("http") else f"https://www.linkedin.com{href}"
            id_match = re.search(r'/jobs/view/(\d+)', href)
            if id_match:
                external_id = id_match.group(1)

        if not external_id:
            # Try data attribute
            data_id = await card.get_attribute("data-job-id") or ""
            external_id = data_id or title[:50]

        if not url and external_id and external_id.isdigit():
            url = f"https://www.linkedin.com/jobs/view/{external_id}/"

        if not url or not external_id:
            log.debug(f"Skipping card with no URL and no external_id (title='{title[:50]}')")
            return None

        # Company — try each selector in priority order; "Unknown" only if all fail.
        company = "Unknown"
        for _sel in _COMPANY_SELECTORS:
            _el = await card.query_selector(_sel)
            if _el:
                _text = (await _el.inner_text()).strip().split("\n")[0].strip()
                if _text:
                    company = _text
                    break
        if company == "Unknown":
            # Demoted to DEBUG — per-card warnings cause 25+ log lines per page per search
            # term when the selector chain is stale. A single summary WARNING is emitted
            # at the end of each page loop in _search_term() instead.
            log.debug(f"Could not extract company for '{title[:50]}' — selector chain may need updating")

        # Location
        loc_el = await card.query_selector('.job-card-container__metadata-item, [class*="location"]')
        location = (await loc_el.inner_text()).strip() if loc_el else "London"

        # NOTE: Job description is not extracted at card-scrape time.
        # LinkedIn list pages do not expose full descriptions in card DOM.
        # Fetching descriptions requires a separate page.goto() per job (detail-page scrape).
        # See Step 9.4 architectural note in IMPLEMENTATION_PLAN.md for the session-reuse
        # prerequisite that makes detail-page scraping practical.
        # Scores are currently based on title + company only (description="", salary_text=None).

        easy_apply = True  # f_AL=true URL filter guarantees all results are Easy Apply

        if not title:
            return None

        return Job(
            title=title,
            company=company,
            location=location,
            url=url,
            source=JobSource.LINKEDIN,
            external_id=external_id,
            easy_apply=easy_apply,
        )
