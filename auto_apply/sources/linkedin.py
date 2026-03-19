"""LinkedIn job scraper — Playwright-based with login."""

import asyncio
import json
import logging
import random
import re
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse
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

# ── Description selector chain (detail-page only) ──
# Priority-ordered for LinkedIn job detail pages. data-testid is most stable
# (LinkedIn's own test infra); semantic IDs/classes are legacy fallbacks.
# As of 2026-03, LinkedIn replaced all semantic CSS classes with obfuscated hashes.
# Add new confirmed selectors at the front when the LinkedIn layout changes.
_DESCRIPTION_SELECTORS: list[str] = [
    '[data-testid="expandable-text-box"]',             # 2026-03 DOM — stable test anchor
    "#job-details",                                    # legacy ID anchor
    ".jobs-description-content__text",                 # legacy content text class
    ".jobs-description__content",                      # legacy description content class
    "[class*='jobs-description-content__text']",       # legacy partial class match
    "[class*='jobs-description__container']",          # legacy container fallback
]

# ── Salary selector chain (card-level only) ──
# LinkedIn card DOM sometimes exposes salary in a dedicated metadata element.
# Priority-ordered; add new confirmed selectors at the front when the layout changes.
# Falls back to all salary fields = None if no element matches — job is still stored.
_SALARY_SELECTORS: list[str] = [
    "[class*='job-card-container__salary-info']",      # dedicated salary metadata item
    "[class*='job-card-list__salary-info']",           # list variant
    "[class*='salary-info']",                          # generic salary-info class
    "[class*='compensation']",                         # compensation class (rare)
]

# ── Easy Apply button selector chain (detail-page detection only) ──
# Used by _scrape_description() to validate in-platform Easy Apply support (Phase 11.5).
# Mirrors the applier's _EASY_APPLY_SELECTORS (applier/linkedin_apply.py) but is kept
# local to avoid a cross-layer import. Ordered most stable → least stable.
# Do NOT include external-apply selectors here — detection goal is presence of the
# in-platform button, not any apply affordance.
_EASY_APPLY_DETECT_SELECTORS: list[str] = [
    '[aria-label*="Easy Apply"]',         # primary aria-label anchor
    '[aria-label*="LinkedIn Apply"]',     # Apply Connect rebrand variant (2026)
    "#jobs-apply-button-id",              # dedicated HTML ID — stable across label renames
    "[data-live-test-job-apply-button]",  # test attribute — survives DOM restructuring
]

# ── Salary annualisation factors ──
_HOURS_PER_YEAR: int = 1880   # 47 weeks × 40 h — standard UK annualisation
_DAYS_PER_YEAR: int = 220     # Standard UK working days per year

# ── Pay-period detection patterns ──
_IS_HOURLY = re.compile(r'per\s+hour|/\s*(?:hr|hour)\b|hourly', re.IGNORECASE)
_IS_DAILY = re.compile(r'per\s+day|/\s*day\b|daily', re.IGNORECASE)


def _parse_salary(text: str) -> tuple[float | None, float | None, str | None]:
    """Parse a raw salary string from a LinkedIn job card into structured fields.

    Normalises the value to an annualised GBP range. Hourly rates are multiplied
    by _HOURS_PER_YEAR (47 weeks × 40 h); daily rates by _DAYS_PER_YEAR (220 days).
    Non-GBP currencies ($ €) and vague strings ("competitive", "DOE") are stored
    as salary_text only — salary_min and salary_max are left None so the salary
    filter treats them as unknown (pass-through, not rejected).

    Args:
        text: Raw salary string from the card element (e.g. "£80k - £100k/yr").

    Returns:
        Tuple (salary_min, salary_max, salary_text):
          salary_min: Annualised lower bound (GBP float) or None.
          salary_max: Annualised upper bound (GBP float) or None.
          salary_text: Cleaned original text, or None if input was empty.
    """
    clean = text.strip()
    if not clean:
        return None, None, None

    # No digits → vague string ("Competitive", "Negotiable", "DOE", "TBD", etc.)
    if not re.search(r'\d', clean):
        return None, None, clean

    # Non-GBP currency only → store text, skip numeric parsing (can't compare to GBP threshold)
    if re.search(r'[$€]', clean) and not re.search(r'£', clean):
        return None, None, clean

    # Detect pay period; also set per-unit plausible range to avoid misidentifying
    # annual equivalents embedded in the same string (e.g. "£40/hr (£75,200/yr)").
    if _IS_HOURLY.search(clean):
        multiplier = float(_HOURS_PER_YEAR)
        lo, hi = 5.0, 500.0           # plausible hourly rates in GBP
    elif _IS_DAILY.search(clean):
        multiplier = float(_DAYS_PER_YEAR)
        lo, hi = 50.0, 5_000.0        # plausible daily rates in GBP
    else:
        multiplier = 1.0
        lo, hi = 1_000.0, 2_000_000.0  # plausible annual salaries in GBP

    # Extract numeric values — handles "80,000", "80k", "37.50"
    nums: list[float] = []
    for m in re.finditer(r'(\d[\d,]*(?:\.\d+)?)([kK])?', clean):
        try:
            val = float(m.group(1).replace(',', ''))
            if m.group(2):
                val *= 1_000.0
            if lo <= val <= hi:
                nums.append(val)
        except ValueError:
            continue

    if not nums:
        return None, None, clean

    salary_min = nums[0] * multiplier
    salary_max = nums[1] * multiplier if len(nums) >= 2 else None

    # Final sanity check on annualised values
    if salary_min > 2_000_000:
        log.debug(f"Salary parse: implausible annualised value {salary_min:.0f} from {clean!r}")
        return None, None, clean
    if salary_max is not None and salary_max > 2_000_000:
        salary_max = None

    return salary_min, salary_max, clean


def _canonical_url(url: str) -> str:
    """Strip query parameters and fragment from a LinkedIn job URL.

    LinkedIn card hrefs carry tracking parameters (refId, trackingId, trk, midToken,
    etc.) that differ across sessions and search contexts. Removing them produces a
    stable URL for storage and reports. Deduplication itself uses the (source,
    external_id) unique constraint and is unaffected by this normalisation.

    Args:
        url: Raw LinkedIn URL, possibly with query string and fragment.

    Returns:
        URL with scheme, host, and path only. Returns url unchanged on parse error.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return url


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

        # Dynamic LinkedIn salary filter (Step 8.6 / Phase 11.3).
        # f_SB2 maps min_salary to LinkedIn's server-side filter: 7 = £100k+, 6 = £80k+.
        # Omitted entirely below £80k — Python-side _salary_passes_filter() handles
        # post-scrape filtering once salary_min/salary_max are populated by _parse_card().
        if min_salary >= 100_000:
            _sb2_param = "&f_SB2=7"
        elif min_salary >= 80_000:
            _sb2_param = "&f_SB2=6"
        else:
            _sb2_param = ""

        for page_num in range(MAX_PAGES):
            start = page_num * 25
            url = (
                f"https://www.linkedin.com/jobs/search/"
                f"?keywords={quote(keywords)}"
                f"&location={location}"
                f"{_sb2_param}"
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

            page_start = len(jobs)  # Phase 11.1: track start index for this page's jobs

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

            # Phase 11.1 — Detail-page description scraping.
            # Card element handles are no longer needed at this point.
            # Failures are soft: a job without a description is still stored
            # and scored on title + company only.
            page_jobs = jobs[page_start:]
            if page_jobs:
                log.info(
                    f"Fetching descriptions: {len(page_jobs)} jobs "
                    f"('{keywords}' page {page_num + 1})"
                )
                desc_count = 0
                for job in page_jobs:
                    job.description, job.easy_apply = await self._scrape_description(page, job.url)
                    if job.description:
                        desc_count += 1
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                log.info(f"  Descriptions fetched: {desc_count}/{len(page_jobs)}")

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

        # Salary — try each selector; all fields remain None if no element matches.
        # None is the correct fallback: jobs without card-level salary still flow
        # through _salary_passes_filter() as pass-through (not rejected).
        salary_min: float | None = None
        salary_max: float | None = None
        salary_text: str | None = None
        for _sel in _SALARY_SELECTORS:
            _el = await card.query_selector(_sel)
            if _el:
                _raw = (await _el.inner_text()).strip().split("\n")[0].strip()
                if _raw:
                    salary_min, salary_max, salary_text = _parse_salary(_raw)
                    log.debug(
                        f"Salary '{title[:40]}': {salary_text!r} "
                        f"→ min={salary_min}, max={salary_max}"
                    )
                    break

        # description and easy_apply are populated by _scrape_description() in
        # _search_term() after all cards on a page are parsed — not here.
        # easy_apply=True is the initial default (f_AL=true in the search URL ensures
        # all listed results are Easy Apply); _scrape_description() will override it
        # with the confirmed button-presence result from the detail page (Phase 11.5).

        easy_apply = True

        if not title:
            return None

        # Phase 11.4 — strip tracking/UTM parameters from the URL before storage.
        url = _canonical_url(url)

        return Job(
            title=title,
            company=company,
            location=location,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_text=salary_text,
            url=url,
            source=JobSource.LINKEDIN,
            external_id=external_id,
            easy_apply=easy_apply,
        )

    async def _scrape_description(self, page: Page, url: str) -> tuple[str, bool]:
        """Fetch the full description and Easy Apply status from a LinkedIn job detail page.

        Navigates to url and extracts both the description text and the presence of the
        Easy Apply button (Phase 11.5). easy_apply is set to False only on a confirmed
        successful page load where no Easy Apply button is found — any failure preserves
        the default (True) so jobs are never incorrectly blocked by a transient error.
        Rate limiting between calls is the caller's responsibility.

        Args:
            page: Playwright Page to navigate — shared with card scraping.
            url: LinkedIn job detail URL (https://www.linkedin.com/jobs/view/<id>/).

        Returns:
            Tuple (description, easy_apply):
              description: Extracted description text, or "" if unavailable.
              easy_apply: True if the Easy Apply button is present, or True if the
                          page could not be loaded (preserves scrape default — only
                          False on a confirmed successful load with no button found).
        """
        if not url:
            return "", True
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT)
            await asyncio.sleep(random.uniform(1, 2))

            # Auth redirect — session expired or listing gated behind login.
            # Can't determine Easy Apply status; preserve default.
            current_url = page.url
            if any(kw in current_url for kw in ("login", "authwall", "checkpoint", "challenge")):
                log.warning(
                    f"Description scrape: auth redirect to {current_url!r} "
                    f"for {url[:80]} — session may have expired"
                )
                return "", True

            # Phase 11.5 — Easy Apply button detection.
            # Run before description extraction so we check the same loaded page.
            # easy_apply=False only when we are confident: page loaded, no button found.
            easy_apply = False
            for sel in _EASY_APPLY_DETECT_SELECTORS:
                if await page.query_selector(sel):
                    easy_apply = True
                    break
            if not easy_apply:
                log.debug(f"Easy Apply button absent for {url[:80]} — easy_apply=False")

            # Description extraction
            description = ""
            for sel in _DESCRIPTION_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        log.debug(f"Description: {len(text)} chars via '{sel}' for {url[:80]}")
                        description = text
                        break

            if not description:
                log.debug(f"Description: no element matched for {url[:80]}")

            return description, easy_apply

        except Exception as e:
            log.warning(f"Description scrape failed for {url[:80]}: {e}")
            return "", True
