"""LinkedIn job scraper — Playwright-based with login."""

import asyncio
import json
import logging
import re
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext
from auto_apply.config import (
    HEADLESS, BROWSER_TIMEOUT, RATE_LIMIT_LINKEDIN, CONFIG_DIR,
)
from auto_apply.models import Job, JobSource
from auto_apply.sources.base import BaseJobSource

log = logging.getLogger(__name__)

COOKIES_PATH = CONFIG_DIR / "linkedin_cookies.json"
MAX_PAGES = 3  # 25 jobs/page

# ── Runtime config (set via configure()) ──
_linkedin_email: str = ""
_linkedin_password: str = ""
_visible: bool = False


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

    async def scrape(self, search_terms: list[str], location: str, min_salary: int, visible: bool = False) -> list[Job]:
        if not _linkedin_email or not _linkedin_password:
            raise RuntimeError(
                "LinkedInSource credentials not set — call configure(email, password) before scrape()"
            )

        all_jobs: list[Job] = []
        seen_ids: set[str] = set()
        _headless = False if (visible or _visible) else HEADLESS

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=_headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            )

            # Restore or create session
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)
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
                await asyncio.sleep(RATE_LIMIT_LINKEDIN)

            # Save cookies for next run
            cookies = await context.cookies()
            COOKIES_PATH.write_text(json.dumps(cookies))
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
                f"?keywords={keywords.replace(' ', '%20')}"
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

            await asyncio.sleep(RATE_LIMIT_LINKEDIN)

        return jobs

    async def _parse_card(self, card) -> Job | None:
        # Title
        title_el = await card.query_selector('.job-card-list__title, a[class*="job-card-list__title"]')
        if not title_el:
            return None
        title = (await title_el.inner_text()).strip()

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

        # Company
        company_el = await card.query_selector('.job-card-container__primary-description, [class*="company"]')
        company = (await company_el.inner_text()).strip() if company_el else "Unknown"

        # Location
        loc_el = await card.query_selector('.job-card-container__metadata-item, [class*="location"]')
        location = (await loc_el.inner_text()).strip() if loc_el else "London"

        # Easy Apply badge — check multiple selectors and also page text
        easy_apply = False
        for sel in [
            '[class*="easy-apply"]',
            '.job-card-container__apply-method',
            'li-icon[type="jobs-easy-apply-icon"]',
            'span.job-card-container__footer-item',
        ]:
            easy_el = await card.query_selector(sel)
            if easy_el:
                txt = (await easy_el.inner_text()).strip().lower()
                if "easy apply" in txt or "easyapply" in txt:
                    easy_apply = True
                    break
        # If we filtered by f_AL=true, all results are Easy Apply
        if not easy_apply:
            easy_apply = True  # f_AL=true filter guarantees Easy Apply

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
