"""Shared Playwright browser session for scraping and applying.

A single BrowserContext is created per pipeline run and handed off to both
the scraper (which authenticates it) and the applier (which reuses the
authenticated session). This preserves localStorage/sessionStorage/IndexedDB
across navigations — the tokens LinkedIn requires for full authentication.
"""

import json
import logging
import random
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, BrowserContext

from auto_apply.config import CONFIG_DIR

log = logging.getLogger(__name__)

# ── Phase 11.6 — playwright-stealth integration ──
# Imported at module level so the presence/absence of the package is detected once.
# The pipeline degrades gracefully if the package is missing: stealth is skipped,
# a WARNING is emitted, and all other functionality continues unchanged.
try:
    from playwright_stealth import Stealth as _Stealth
    _STEALTH_AVAILABLE = True
except ImportError:
    _Stealth = None  # type: ignore[assignment,misc]
    _STEALTH_AVAILABLE = False

# Canonical cookie path — imported by sources/linkedin.py and applier/linkedin_apply.py.
# Both files previously defined their own COOKIES_PATH; this is the single source of truth.
COOKIES_PATH = CONFIG_DIR / "linkedin_cookies.json"


@asynccontextmanager
async def linkedin_session(visible: bool = False):
    """Yield an authenticated BrowserContext for a single pipeline run.

    Cookies are restored on entry and saved on exit for future runs.
    Login itself is performed by the scraper's _ensure_logged_in() when it
    navigates to LinkedIn and detects the session is not authenticated.

    Per-application isolation is achieved by creating a new Page per apply()
    call within the shared context. A page-level failure (navigation error,
    form exception) does not close the context — only an unrecoverable
    browser crash would propagate out and terminate the session.

    Resource cleanup (context.close, browser.close, pw.stop) happens once,
    in the finally block, at the end of the full pipeline run.

    Args:
        visible: If True, run browser in headed mode.

    Yields:
        BrowserContext — cookies pre-loaded; scraper handles login.
    """
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=not visible)
    context = await browser.new_context(
        viewport={"width": random.randint(1260, 1420), "height": random.randint(780, 900)},
        # No hardcoded user_agent — use Playwright's current Chromium default.
        # A pinned Chrome 122 UA string creates a fingerprint mismatch with the
        # actual Chromium version shipped by playwright, which is a bot signal.
    )

    # Phase 11.6 — apply stealth patches to the context so every page created
    # from it inherits the patches via context.add_init_script(). This suppresses
    # common browser fingerprinting signals: navigator.webdriver, missing plugins,
    # HeadlessChrome UA substring, etc. Fails gracefully if the package is absent.
    if _STEALTH_AVAILABLE:
        try:
            await _Stealth().apply_stealth_async(context)
            log.debug("playwright-stealth patches applied to browser context")
        except Exception as e:
            log.warning(f"playwright-stealth patch failed (non-fatal): {e}")
    else:
        log.warning(
            "playwright-stealth not installed — browser fingerprint not suppressed. "
            "Install with: pip install playwright-stealth"
        )

    try:
        if COOKIES_PATH.exists():
            try:
                cookies = json.loads(COOKIES_PATH.read_text())
                await context.add_cookies(cookies)
                log.debug(f"Restored {len(cookies)} cookies from {COOKIES_PATH}")
            except Exception as e:
                log.warning(f"Could not restore cookies from {COOKIES_PATH}: {e}")

        yield context

    finally:
        # Save cookies once at the end of the full run.
        # Replaces the per-scrape save in sources/linkedin.py and the
        # per-apply save in applier/linkedin_apply.py (standalone path retains its own).
        try:
            cookies = await context.cookies()
            COOKIES_PATH.write_text(json.dumps(cookies, indent=2))
            log.debug(f"Saved {len(cookies)} cookies to {COOKIES_PATH}")
        except Exception:
            pass
        await context.close()
        await browser.close()
        await pw.stop()


async def verify_session(context: BrowserContext) -> bool:
    """Check if the BrowserContext is authenticated on LinkedIn.

    Navigates to the LinkedIn feed in a temporary page and checks for auth
    redirects. Call this at the start of run_apply() before the apply loop —
    a failed check aborts the batch with a clear error rather than silently
    failing on every individual application.

    Failure modes detected:
    - Expired cookie (redirect to login page)
    - LinkedIn forced logout (redirect to authwall)
    - Verification challenge (redirect to checkpoint/challenge)

    Does NOT detect: partial auth where the feed loads but some features are
    gated (rare; would only surface during actual apply attempts).

    Args:
        context: An existing BrowserContext to verify.

    Returns:
        True if authenticated (feed URL confirmed), False otherwise.
    """
    page = await context.new_page()
    try:
        await page.goto(
            "https://www.linkedin.com/feed/",
            wait_until="domcontentloaded",
            timeout=15_000,
        )
        current_url = page.url
        if any(kw in current_url for kw in ("login", "authwall", "checkpoint", "challenge")):
            log.warning(f"Session health check: redirected to {current_url!r} — not authenticated")
            return False
        authenticated = "feed" in current_url
        if not authenticated:
            log.warning(f"Session health check: unexpected URL {current_url!r}")
        return authenticated
    except Exception as e:
        log.warning(f"Session health check navigation failed: {e}")
        return False
    finally:
        await page.close()
