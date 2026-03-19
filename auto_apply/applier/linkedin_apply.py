"""LinkedIn Easy Apply — Playwright automation with smart screening question handling."""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import anthropic
from playwright.async_api import async_playwright, BrowserContext, Page

from auto_apply.config import CONFIG_DIR, HEADLESS, BROWSER_TIMEOUT, CLAUDE_MODEL_SCORING
from auto_apply.browser import COOKIES_PATH
from auto_apply.cv_parser import extract_cv_text
from auto_apply.applier.base import BaseApplier

log = logging.getLogger(__name__)

# ── Button selector constants ──
# Single source of truth for all apply-related CTA detection in _apply_on_page().
# Both wait_for_selector (CSS) and page.evaluate() (JS) are derived from these lists,
# ensuring wait and click targets are always in sync — a change in one place applies
# everywhere without a second edit.
#
# LinkedIn rebranding context (2025–2026):
#   - Old label:   "Easy Apply" (aria-label / button text)
#   - New labels:  "LinkedIn Apply", "Apply Connect", or "Apply to {job title}"
#   - Stable anchors that survive label renames:
#       #jobs-apply-button-id             (dedicated HTML ID — most stable)
#       data-live-test-job-apply-button   (test attribute — survives any label change)
#       class*="jobs-apply-button"        (BEM class — changes less often than labels)
#
# To adapt after future renames: add new aria-label variants to _EASY_APPLY_SELECTORS.
# Do NOT remove existing entries — they serve as backwards-compat fallbacks for
# accounts still on the old "Easy Apply" branding.

# In-platform Easy Apply / Apply Connect button.
# Ordered: most specific/stable → least specific (generic aria-label last).
#
# 2026-03-16: LinkedIn switched from <button> to <a> tags with hashed CSS-module
# class names.  All selectors are now element-agnostic (no "button" prefix) so they
# match regardless of tag name.  Legacy button-prefixed entries are kept at the end
# for accounts that still render the old DOM.
_EASY_APPLY_SELECTORS: list[str] = [
    # ── element-agnostic (current LinkedIn DOM: <a> tags, hashed classes) ──
    '[aria-label*="Easy Apply"]',             # primary — matches <a aria-label="Easy Apply to this job">
    '[aria-label*="LinkedIn Apply"]',         # Apply Connect rebrand variant
    '[aria-label*="Apply to"]',              # generic last-resort
    # ── legacy anchors (pre-2026 <button> DOM) — kept as backwards-compat ──
    "#jobs-apply-button-id",                  # dedicated HTML ID
    "[data-live-test-job-apply-button]",      # test attribute
    '[class*="jobs-apply-button"]',           # BEM class
]

# External-apply button: redirects user off LinkedIn to the company's own ATS.
# Structural selectors only — broad aria-label matches (e.g., "Apply now") are
# intentionally excluded because they risk collision with Apply Connect labels.
_EXTERNAL_APPLY_SELECTORS: list[str] = [
    'a[href*="externalApply"]',                              # anchor with external href (element-agnostic class)
    'a[class*="jobs-apply-button"][href*="externalApply"]',  # legacy: BEM class + external href
    '[aria-label*="Apply on company"]',                      # explicit external-origin label
]

# LinkedIn Connect button appearing on a job page when Apply is absent.
# Indicates: job is closed, recruiter disabled Apply, or posting has no Apply
# integration. Without this check the code waits a full 10s timeout and returns
# "Easy Apply button not visible" — consuming a failed-application slot and giving
# zero diagnostic context. These jobs should be classified as skipped.
_CONNECT_BUTTON_SELECTORS: list[str] = [
    "[data-live-test-connect-button]",    # dedicated test attribute for the Connect CTA
    '[aria-label*="Connect with"]',       # person-to-person connection label pattern (element-agnostic)
]

# ── Runtime config (set via configure()) ──
_api_key: str = ""
_visible: bool = False
_linkedin_email: str = ""
_linkedin_password: str = ""
_cv_path: str = ""
_QUICK_ANSWERS: dict = {}

# ── Visa sponsorship runtime state ──
# Set once per run via prompt_visa_status(). Persists across all apply
# attempts in the same run. Reset on configure().
#   None  = not yet asked / skipped (defer to Claude)
#   False = does not require sponsorship (auto-answer)
#   True  = requires sponsorship (defer to Claude with context)
_visa_needs_sponsorship: bool | None = None
_visa_prompted: bool = False
_work_authorisation: str = ""  # Populated from config.json via configure()

# Visa question detection — two categories with opposite answer polarities.
# "Right to work" questions ask "do you have permission?" → Yes when no sponsorship.
# "Sponsorship" questions ask "do you need help?" → No when no sponsorship.
_VISA_RIGHT_TO_WORK_RE = re.compile(
    r"right.to.work|"
    r"(?:authoriz|authoris)\w*.+work|work.+(?:authoriz|authoris)|"
    r"eligible.+work|"
    r"work.+permit|"
    r"legally.+work|"
    r"legal.+right.+work|"
    r"permission.+work",
    re.IGNORECASE,
)
_VISA_SPONSORSHIP_RE = re.compile(
    r"require.+sponsor|need.+sponsor|"
    r"sponsor.+require|sponsor.+need|"
    r"visa.+sponsor|sponsor.+visa|"
    r"immigration.+sponsor|"
    r"will.+you.+require.+visa|future.+sponsor|"
    r"now.+or.+(?:in.+the.+)?future.+require",
    re.IGNORECASE,
)

# ── Claude client singleton ──
_claude_client: anthropic.Anthropic | None = None


def _get_claude_client(api_key: str) -> anthropic.Anthropic:
    global _claude_client
    if _claude_client is None:
        _claude_client = anthropic.Anthropic(api_key=api_key)
    return _claude_client


# Cache CV text once
_cv_text: str | None = None


def _get_cv_text() -> str:
    global _cv_text
    if _cv_text is None:
        _cv_text = extract_cv_text(cv_path=_cv_path if _cv_path else None)
    return _cv_text


def prompt_visa_status() -> None:
    """Prompt the user for visa sponsorship status. Called once per run.

    Accepts y/n/s. The answer persists across all apply attempts in the
    same run. Subsequent calls are no-ops until configure() resets state.
    """
    global _visa_needs_sponsorship, _visa_prompted
    if _visa_prompted:
        return
    _visa_prompted = True
    if _visa_needs_sponsorship is not None:
        log.info("Visa status: loaded from config")
        return

    print("\n" + "-" * 50)
    print("  VISA / WORK AUTHORISATION")
    print("-" * 50)
    print("  Do you require visa sponsorship to work in")
    print("  the country where these jobs are located?")
    print()
    print("  [y] Yes - I require sponsorship")
    print("  [n] No  - I have the right to work")
    print("  [s] Skip - not applicable / prefer not to say")
    print("-" * 50)

    while True:
        try:
            choice = input("  Your answer [y/n/s]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            log.info("Visa prompt skipped (non-interactive) — deferring to Claude")
            return
        if choice in ("y", "yes"):
            _visa_needs_sponsorship = True
            log.info("Visa status: requires sponsorship — visa questions deferred to Claude")
            break
        elif choice in ("n", "no"):
            _visa_needs_sponsorship = False
            log.info("Visa status: does not require sponsorship — visa questions auto-answered")
            break
        elif choice in ("s", "skip", ""):
            log.info("Visa status: skipped — visa questions deferred to Claude")
            break
        else:
            print("  Please enter y, n, or s")


def _build_screening_system_prompt() -> str:
    """Build the system prompt for Claude screening answers, including visa context."""
    base = (
        "You are filling out a job application form. Answer screening questions "
        "honestly based on the CV. Always present the candidate positively. "
        "For Yes/No questions, answer Yes when the candidate has relevant experience. "
        "For years of experience, give realistic numbers based on the CV."
    )
    if _visa_needs_sponsorship is True:
        base += (
            " IMPORTANT: The candidate requires visa sponsorship to work in the "
            "country where this job is located. Answer all visa, work authorisation, "
            "and sponsorship questions honestly reflecting this requirement."
        )
    elif _visa_needs_sponsorship is False:
        auth_detail = ""
        if _work_authorisation:
            _AUTH_READABLE = {
                "citizen": "citizen / permanent resident",
                "pre_settled": "pre-settled or settled status",
                "valid_visa": "valid work visa",
                "skilled_worker_uk": "Skilled Worker visa (UK)",
                "other": "other authorisation",
            }
            label = _AUTH_READABLE.get(_work_authorisation, _work_authorisation.replace("_", " "))
            auth_detail = f" ({label})"
        base += (
            f" The candidate has full work authorisation{auth_detail} and does not require "
            "visa sponsorship in the country where this job is located."
        )
    return base


def _ask_claude(question: str, options: list[str] | None = None,
                job_title: str = "", field_type: str = "text") -> str:
    """Ask Claude Haiku to answer a screening question based on CV context."""
    cv = _get_cv_text()

    if options:
        options_str = "\n".join(f"- {o}" for o in options)
        instruction = f"""You must pick exactly ONE of these options:
{options_str}

Reply with ONLY the exact option text, nothing else."""
    elif field_type == "numeric":
        instruction = "Reply with ONLY a number (integer). No words, no units, just the number. If the candidate has transferable experience, count those years. Never answer 0 — use at least 1 if there is any related experience."
    else:
        instruction = "Reply with a short answer (1-2 sentences max). Be concise and professional."

    prompt = f"""You are answering a screening question on a job application for: {job_title}

The candidate's CV:
{cv[:2500]}

Question: {question}

{instruction}"""

    try:
        client = _get_claude_client(_api_key)
        response = client.messages.create(
            model=CLAUDE_MODEL_SCORING,
            max_tokens=256,
            temperature=0,
            system=_build_screening_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
        )
        answer = response.content[0].text.strip()

        # Clean up the answer
        answer = answer.strip('"\'` \n')

        # For options, find the best match
        if options:
            answer_lower = answer.lower()
            for opt in options:
                if opt.lower() in answer_lower or answer_lower in opt.lower():
                    return opt
            # If no match, return the first positive option
            for opt in options:
                if opt.lower() in ("yes", "true", "agree"):
                    return opt
            return options[0] if options else answer

        # For numeric, extract just the number
        if field_type == "numeric":
            nums = re.findall(r'\d+', answer)
            if nums:
                return nums[0]
            return "5"  # safe default

        return answer

    except anthropic.APIError as e:
        log.warning(f"Claude screening answer failed (API error): {e}")
        if options:
            for opt in options:
                if opt.lower() in ("yes", "true"):
                    return opt
            return options[0]
        if field_type == "numeric":
            return "5"
        return ""
    except Exception as e:
        log.warning(f"Claude screening answer failed: {e}")
        if options:
            for opt in options:
                if opt.lower() in ("yes", "true"):
                    return opt
            return options[0]
        if field_type == "numeric":
            return "5"
        return ""


# ── Rule-based quick answers (populated via configure()) ──


def _try_quick_answer(question: str, options: list[str] | None = None) -> str | None:
    """Try to answer with rules before falling back to Claude."""
    q = question.lower().strip()

    for pattern, answer in _QUICK_ANSWERS.items():
        if re.search(pattern, q):
            # If options provided, match the answer to an option
            if options:
                answer_lower = answer.lower()
                for opt in options:
                    if opt.lower() == answer_lower:
                        return opt
                # For yes/no, match positively
                if answer.lower() == "yes":
                    for opt in options:
                        if opt.lower() in ("yes", "true", "agree", "i agree"):
                            return opt
                if answer.lower() == "no":
                    for opt in options:
                        if opt.lower() in ("no", "false"):
                            return opt
                return None  # Can't match to options, use Claude
            return answer

    return None  # No rule matched


def _check_visa_question(question: str, options: list[str] | None = None) -> str | None:
    """Handle visa/sponsorship questions based on runtime visa status.

    Two detection categories with opposite answer polarities:
      - Right-to-work questions ("authorised to work?") -> Yes when no sponsorship.
      - Sponsorship questions ("require sponsorship?") -> No when no sponsorship.

    When the user requires sponsorship, all visa questions are deferred to
    _ask_claude() which receives visa context in its system prompt.

    Args:
        question: The screening question text.
        options: Available answer options (radio/select), if any.

    Returns:
        Answer string if auto-answerable, None to fall through to Claude.
    """
    if _visa_needs_sponsorship is None:
        return None  # Not prompted or skipped — fall through to Claude

    q = question.strip()
    is_sponsorship_q = _VISA_SPONSORSHIP_RE.search(q)
    is_right_to_work_q = _VISA_RIGHT_TO_WORK_RE.search(q)

    if not is_sponsorship_q and not is_right_to_work_q:
        return None  # Not a visa question

    if _visa_needs_sponsorship:
        # User requires sponsorship — defer to Claude for nuanced answers
        log.info(f"  Visa question (sponsorship required) — deferring to Claude: '{question[:60]}'")
        return None

    # User does NOT require sponsorship — auto-answer
    raw_answer = "No" if is_sponsorship_q else "Yes"

    # Match to provided options (radio/dropdown)
    if options:
        target = raw_answer.lower()
        matched = None
        for opt in options:
            opt_lower = opt.lower().strip()
            if opt_lower == target:
                matched = opt
                break
            if target == "yes" and opt_lower in ("yes", "true", "i agree", "agree"):
                matched = opt
                break
            if target == "no" and opt_lower in ("no", "false", "i disagree", "disagree"):
                matched = opt
                break
        if matched is None:
            log.debug(
                f"  Visa auto-answer '{raw_answer}' doesn't match options "
                f"{options} — deferring to Claude"
            )
            return None
        log.info(f"  Visa auto-answer for '{question[:60]}': {matched}")
        return matched

    log.info(f"  Visa auto-answer for '{question[:60]}': {raw_answer}")
    return raw_answer


def _build_quick_answers(config: dict) -> dict:
    """Build complete quick answers dict with personal info from config.

    Args:
        config: User config dict from config.json.

    Returns:
        Complete quick answers dict for _try_quick_answer().
    """
    name = config.get("applicant_name", "")
    first = name.split()[0] if name else ""
    last = name.split()[-1] if name and len(name.split()) > 1 else ""
    email = config.get("applicant_email", "")
    phone = config.get("applicant_phone", "")
    min_salary = config.get("min_salary", 0)
    location = config.get("location", "")

    answers: dict = {
        # Years of experience patterns
        r"year.*experience.*analytic": "5",
        r"year.*experience.*data": "5",
        r"year.*experience.*python": "4",
        r"year.*experience.*sql": "5",
        r"year.*experience.*machine.?learn": "3",
        r"year.*experience.*marketing": "5",
        r"year.*experience.*digital": "5",
        r"year.*experience.*power.?bi": "3",
        r"year.*experience.*google.?analytics": "4",
        r"year.*experience.*tableau": "2",
        r"year.*experience.*excel": "5",
        r"year.*experience.*javascript": "2",
        r"year.*experience.*cloud": "3",
        r"year.*experience.*aws": "2",
        r"year.*experience.*azure": "2",
        r"year.*experience.*gcp": "2",
        r"year.*experience.*spark": "2",
        r"year.*experience.*ai": "3",
        r"year.*experience.*llm": "2",
        r"year.*experience.*nlp": "2",
        r"year.*experience.*deep.?learn": "2",
        r"year.*experience.*manage": "3",
        r"year.*experience.*lead": "2",
        r"year.*experience": "5",
        r"how.many.year": "5",

        # Employment
        r"notice.?period": "1 month",
        r"start.?date|when.*start|earliest.*start": "Immediately",

        # Location-adjacent yes/no (not location field itself)
        r"willing.*relocate": "Yes",
        # NOTE: Visa/sponsorship/right-to-work patterns removed from quick answers.
        # Now handled by _check_visa_question() with runtime visa status prompt.

        # Common yes/no
        r"background.?check|criminal|dbs": "Yes",
        r"driver.*licen": "No",
        r"\b18\b.*or.*older|over.*18": "Yes",
        r"agree.*terms|consent|acknowledge": "Yes",
        r"willing.*travel": "Yes",
        r"commut": "Yes",

        # Personal info from config
        r"^first.?name$|^given.?name$": first,
        r"^last.?name$|^surname$|^family.?name$": last,
        r"middle.?name": "",
        r"^full.?name$|^name$": name,
        r"email|e-mail|email.address": email,
        r"phone|telephone|mobile|contact.number": phone,
        r"summary|about.*you|cover.*letter|introduction": "",
    }

    # Salary and location come from runtime config — omit if not set so Claude
    # handles these questions instead of returning a wrong hardcoded value.
    if min_salary:
        answers[r"salary|compensation|pay.*expect"] = str(int(min_salary))
    if location:
        answers[r"city|location|where.*based|where.*live"] = location

    return answers


def configure(api_key: str, config: dict, visible: bool = False, password: str | None = None) -> None:
    """Configure the applier with runtime credentials and settings.

    Args:
        api_key: Anthropic API key for Claude.
        config: Full user config dict from config.json.
        visible: If True, run browser in visible mode (overrides HEADLESS).
        password: LinkedIn password. If None, fetched from keychain/setup wizard.
    """
    global _api_key, _visible, _linkedin_email, _linkedin_password, _cv_path
    global _QUICK_ANSWERS, _claude_client, _cv_text
    global _visa_needs_sponsorship, _visa_prompted, _work_authorisation
    _api_key = api_key
    _visible = visible
    _linkedin_email = config.get("linkedin_email", "")
    _cv_path = config.get("cv_path", "")
    _QUICK_ANSWERS = _build_quick_answers(config)
    _claude_client = None  # Reset when API key changes
    _cv_text = None        # Reset cache when config changes
    # Load visa status from config if present; fall back to runtime prompt.
    # Backwards compat: configs without requires_sponsorship keep the old behaviour.
    _requires_sponsorship_cfg = config.get("requires_sponsorship")
    if _requires_sponsorship_cfg is not None:
        _visa_needs_sponsorship = bool(_requires_sponsorship_cfg)
    else:
        _visa_needs_sponsorship = None  # Reset — prompt again on next run
    _work_authorisation = config.get("work_authorisation", "")
    _visa_prompted = False
    if password is not None:
        _linkedin_password = password
    else:
        from auto_apply.setup_wizard import _get_linkedin_password
        _linkedin_password = _get_linkedin_password(_linkedin_email)


def answer_question(question: str, options: list[str] | None = None,
                    job_title: str = "", field_type: str = "text") -> str:
    """Answer a screening question — rules first, Claude as fallback."""
    # Skip Bengali-locale UI label strings that LinkedIn injects as question text.
    # "নির্বাচন" = select, "আপলোড" = upload, "চিহ্নিত" = marked.
    # These are navigation/widget labels, not actual screening questions.
    # Workaround for LinkedIn serving mixed-locale UI on some accounts.
    q_lower = question.lower()
    if any(skip in q_lower for skip in ["নির্বাচন", "আপলোড", "চিহ্নিত"]):
        return ""

    # Check visa question (uses runtime visa status, not static rules)
    visa_answer = _check_visa_question(question, options)
    if visa_answer is not None:
        return visa_answer

    # Try quick rule-based answer
    quick = _try_quick_answer(question, options)
    if quick is not None:
        log.debug(f"Quick answer for '{question[:50]}': {quick}")
        return quick

    # Fall back to Claude
    log.debug(f"Asking Claude for: '{question[:80]}'")
    return _ask_claude(question, options, job_title, field_type)


class LinkedInApplier(BaseApplier):
    name = "linkedin"

    async def _capture_diagnostics(
        self, page: Page, job: dict, stage: str
    ) -> tuple[str | None, str | None]:
        """Capture screenshot, URL, and page title at the point of apply failure.

        Saves a full-page screenshot to CONFIG_DIR/diagnostics/ and logs a
        structured WARNING with all context needed to diagnose the failure.
        Never raises — wraps all I/O in try/except so a capture failure cannot
        mask the original error or crash the pipeline.

        Filename format: {timestamp}_{job_id}_{job_slug}.png
        The job_id component ensures uniqueness across multiple failures in a
        single run even when they occur within the same second.

        Args:
            page: Playwright Page at the moment of failure (must still be open).
            job: Job dict — used for filename slug and log context.
            stage: Short identifier for the failure point (e.g. "button_not_found").

        Returns:
            (screenshot_path_str, failure_url) — both None if capture itself fails.
        """
        try:
            diag_dir = CONFIG_DIR / "diagnostics"
            diag_dir.mkdir(exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            job_id = job.get("id", "unknown")
            raw_title = job.get("title", "unknown")[:30]
            job_slug = re.sub(r"\W+", "_", raw_title).strip("_") or "job"
            fname = f"{timestamp}_{job_id}_{job_slug}.png"
            screenshot_path = diag_dir / fname

            # Capture URL before screenshot in case screenshot navigation changes it.
            failure_url = page.url  # synchronous property — always safe to read
            page_title = await page.title()

            await page.screenshot(path=str(screenshot_path), full_page=True)

            log.warning(
                f"  DIAGNOSTIC [{stage}] | job_id={job_id} | "
                f"title={job.get('title', '')!r} | "
                f"failure_url={failure_url!r} | "
                f"page_title={page_title!r} | "
                f"screenshot={screenshot_path}"
            )
            return str(screenshot_path), failure_url
        except Exception as diag_err:
            log.debug(f"  Diagnostic capture failed ({stage}): {diag_err}")
            try:
                return None, page.url
            except Exception:
                return None, None

    async def apply(
        self, job: dict, context: BrowserContext | None = None
    ) -> tuple[bool, str, str | None, str | None]:
        """Attempt to apply to a job via LinkedIn Easy Apply.

        Args:
            job: Job dict with 'url', 'title', 'easy_apply' keys.
            context: Shared BrowserContext from linkedin_session(). A new Page
                is created and closed within this context for each application —
                context is not touched on failure, so subsequent jobs are not
                affected. If None, a standalone browser lifecycle is used.

        Returns:
            (success, message, screenshot_path, failure_url).
            screenshot_path and failure_url are None on success or permanent skips.
        """
        if not _linkedin_email or not _linkedin_password:
            return False, "LinkedIn credentials not configured — run linkedin-autoapply setup", None, None
        if not job.get("easy_apply"):
            return False, "Not an Easy Apply job", None, None
        if not job.get("url", ""):
            return False, "No job URL", None, None

        if context is not None:
            # Shared-session path: new Page within the authenticated context.
            # Cookie restore and save are both handled by linkedin_session() —
            # no cookie I/O here. A page-level exception closes the Page only;
            # the shared context remains open for subsequent applications.
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)
            try:
                return await self._apply_on_page(page, job)
            except Exception as e:
                log.error(f"LinkedIn apply failed: {e}")
                screenshot_path, failure_url = await self._capture_diagnostics(
                    page, job, "unhandled_exception"
                )
                return False, f"Error: {str(e)[:200]}", screenshot_path, failure_url
            finally:
                await page.close()
        else:
            # Standalone path: own the full browser lifecycle.
            # Used when apply() is called outside of linkedin_session() context
            # (e.g. future standalone apply subcommand).
            _headless = False if _visible else HEADLESS
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=_headless)
                ctx = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                )
                page = await ctx.new_page()
                page.set_default_timeout(BROWSER_TIMEOUT)
                try:
                    if COOKIES_PATH.exists():
                        cookies = json.loads(COOKIES_PATH.read_text())
                        await ctx.add_cookies(cookies)
                    result = await self._apply_on_page(page, job)
                    cookies = await ctx.cookies()
                    COOKIES_PATH.write_text(json.dumps(cookies))
                    return result
                except Exception as e:
                    log.error(f"LinkedIn apply failed: {e}")
                    screenshot_path, failure_url = await self._capture_diagnostics(
                        page, job, "unhandled_exception"
                    )
                    return False, f"Error: {str(e)[:200]}", screenshot_path, failure_url
                finally:
                    await browser.close()

    async def _apply_on_page(
        self, page: Page, job: dict
    ) -> tuple[bool, str, str | None, str | None]:
        """Core apply logic. Assumes page is within an authenticated context.

        Three-stage detection sequence, in priority order:
          1. External-apply early exit — redirects to company ATS → skipped
          2. Connect button detection — job closed / recruiter-only → skipped
          3. Easy Apply button wait and click — in-platform form → proceed

        Stages 1 and 2 use query_selector (non-blocking) so they resolve in <1s
        when positive. Stage 3 uses wait_for_selector with a 10s timeout to allow
        for React hydration.

        All selector lists are derived from the module-level constants
        _EASY_APPLY_SELECTORS, _EXTERNAL_APPLY_SELECTORS, and
        _CONNECT_BUTTON_SELECTORS — edit those constants to update detection
        without touching this method.

        Args:
            page: Playwright Page — may be shared-context or standalone.
            job: Job dict with at least 'url' and 'title' keys.

        Returns:
            (success, message, screenshot_path, failure_url).
            screenshot_path and failure_url are None for skips and successes.
        """
        url = job.get("url", "")
        job_title = job.get("title", "Unknown")

        await page.goto(url, wait_until="domcontentloaded")

        # Auth safety net — should not trigger with shared context (scraper
        # already authenticated it) but kept for standalone path and edge cases.
        if "login" in page.url or "authwall" in page.url:
            await self._login(page)
            await page.goto(url, wait_until="domcontentloaded")

        # Stage 1: External-apply early exit.
        # Must run before the Easy Apply wait — avoids a 10s timeout on ATS-redirect
        # pages. Structural selectors only: the href*="externalApply" anchor is the
        # most reliable signal. Broad aria-label matches are avoided because labels
        # like "Apply now" could collide with Apply Connect post-rebrand.
        external_btn = await page.query_selector(", ".join(_EXTERNAL_APPLY_SELECTORS))
        if external_btn:
            return False, "External apply only (not Easy Apply)", None, None

        # Stage 2: Connect button detection.
        # The Connect CTA appears when: the job posting is closed, the recruiter
        # disabled the Apply integration, or it is a recruiter-profile post with no
        # Apply button. The page loads normally so the auth check above passes; the
        # failure mode is simply that no Apply button exists. Without this check the
        # code waits the full 10s timeout and emits "Easy Apply button not visible
        # after wait" — an unhelpful message that masks the real cause.
        connect_btn = await page.query_selector(", ".join(_CONNECT_BUTTON_SELECTORS))
        if connect_btn:
            log.info(f"Connect button found on {url!r} — job may be closed or recruiter-only")
            return False, "Connect button found — not an Apply job (job may be closed or recruiter-only)", None, None

        # Stage 3: Wait for Easy Apply / Apply Connect button (React hydration).
        # wait_for_selector fires as soon as ANY selector in the comma-separated CSS
        # string matches.
        _wait_sel = ", ".join(_EASY_APPLY_SELECTORS)
        _EASY_APPLY_TIMEOUT = 10_000  # 10s — enough for React hydration; fast exit for recruiter pages
        try:
            await page.wait_for_selector(_wait_sel, timeout=_EASY_APPLY_TIMEOUT, state="visible")
        except Exception:
            screenshot_path, failure_url = await self._capture_diagnostics(
                page, job, "button_not_found"
            )
            return False, "Easy Apply button not visible after wait", screenshot_path, failure_url

        # Open the apply form.  LinkedIn switched from <button> to <a> tags in 2026.
        # The <a> element has href="…/apply/?openSDUIApplyFlow=true" which opens the
        # artdeco-modal apply flow.  JS .click() on the <a> doesn't reliably trigger
        # React's client-side navigation, so we extract the href and navigate directly.
        # Falls back to Playwright .click() for legacy <button> DOM or missing href.
        _selectors_js = json.dumps(_EASY_APPLY_SELECTORS)
        apply_href = await page.evaluate(f"""() => {{
            const selectors = {_selectors_js};
            for (const sel of selectors) {{
                const el = document.querySelector(sel);
                if (el) {{
                    const href = el.getAttribute('href') || el.href;
                    if (href && href.includes('/apply')) return href;
                }}
            }}
            return null;
        }}""")

        if apply_href:
            # Navigate to the SDUI apply URL (opens the artdeco-modal form)
            if apply_href.startswith('/'):
                apply_href = "https://www.linkedin.com" + apply_href
            await page.goto(apply_href, wait_until="domcontentloaded")
        else:
            # Fallback: Playwright click for legacy <button> DOM
            for sel in _EASY_APPLY_SELECTORS:
                el = await page.query_selector(sel)
                if el:
                    await el.click()
                    break
            else:
                screenshot_path, failure_url = await self._capture_diagnostics(
                    page, job, "click_failed"
                )
                return False, "Easy Apply button found by wait but click failed", screenshot_path, failure_url

        await asyncio.sleep(3)

        success = await self._fill_modal(page, job_title)
        if success:
            return True, "Applied via LinkedIn Easy Apply", None, None
        screenshot_path, failure_url = await self._capture_diagnostics(
            page, job, "form_incomplete"
        )
        return False, "Form filling incomplete", screenshot_path, failure_url

    async def _login(self, page: Page):
        log.info("LinkedIn: logging in for apply...")
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        await asyncio.sleep(1)
        await page.fill('#username', _linkedin_email)
        await page.fill('#password', _linkedin_password)
        await page.click('button[type="submit"]')
        await asyncio.sleep(3)

    async def _fill_modal(self, page: Page, job_title: str) -> bool:
        """Fill LinkedIn Easy Apply modal step by step. Returns True if submitted."""
        max_steps = 10
        prev_field_ids = set()
        stuck_count = 0

        for step in range(max_steps):
            await asyncio.sleep(2)

            # Extract all form fields with their question labels
            fields = await self._extract_fields(page)
            log.debug(f"Step {step + 1}: {len(fields)} fields found")

            # Detect stuck — same fields as last step means page didn't advance
            current_ids = {f.get("id", f.get("question", ""))[:50] for f in fields}
            if current_ids and current_ids == prev_field_ids:
                stuck_count += 1
                if stuck_count >= 2:
                    log.debug("Stuck on same form step — cannot advance")
                    break
            else:
                stuck_count = 0
            prev_field_ids = current_ids

            # Fill each field
            for field in fields:
                await self._fill_field(page, field, job_title)

            # Upload CV if file input present
            has_file = await page.evaluate('''() => {
                const dialog = document.querySelector('div[role="dialog"]');
                if (!dialog) return false;
                const fi = dialog.querySelector('input[type="file"]');
                return fi !== null;
            }''')
            if has_file and _cv_path and Path(_cv_path).exists():
                try:
                    file_input = await page.query_selector('div[role="dialog"] input[type="file"]')
                    if file_input:
                        await file_input.set_input_files(_cv_path)
                        await asyncio.sleep(1)
                except Exception as e:
                    log.warning(f"CV upload failed: {e}")

            # Check checkboxes (like "top choice")
            await page.evaluate('''() => {
                const dialog = document.querySelector('div[role="dialog"]');
                if (!dialog) return;
                dialog.querySelectorAll('input[type="checkbox"]').forEach(cb => {
                    if (!cb.checked) cb.click();
                });
            }''')

            # Determine footer button action
            action = await self._get_footer_action(page)

            if action == "submit":
                await self._click_footer_primary(page)
                await asyncio.sleep(3)
                if await self._check_success(page):
                    log.info("LinkedIn Easy Apply submitted successfully")
                    return True
                # May have been a review step, continue
                continue

            elif action == "next":
                await self._click_footer_primary(page)
                continue

            else:
                log.debug("No actionable footer button found")
                break

        return False

    async def _extract_fields(self, page: Page) -> list[dict]:
        """Extract form fields with their associated question labels.

        LinkedIn structures forms as: <label for="input-id">Question</label>
        followed by the corresponding input/select/textarea element.
        """
        return await page.evaluate("""() => {
            const dialog = document.querySelector('div[role="dialog"]');
            if (!dialog) return [];

            const fields = [];
            const seen = new Set();

            // Strategy: iterate all labels, find their associated input via 'for' attr
            const labels = dialog.querySelectorAll('label');

            for (const label of labels) {
                const question = (label.innerText || '').trim().split('\\n')[0].trim();
                if (!question || question.length < 3 || question.length > 300) continue;
                if (question.toLowerCase().includes('select an option')) continue;
                if (question.toLowerCase().includes('upload resume')) continue;

                const forAttr = label.getAttribute('for') || '';
                if (!forAttr || seen.has(forAttr)) continue;
                seen.add(forAttr);

                const target = document.getElementById(forAttr);
                if (!target || target.offsetWidth === 0) continue;

                const tag = target.tagName.toLowerCase();
                const escapedId = CSS.escape(forAttr);

                if (tag === 'input') {
                    const inputType = target.type || 'text';

                    if (inputType === 'radio') {
                        // Radio — find all radios in same fieldset
                        const fieldset = target.closest('fieldset');
                        const radios = fieldset
                            ? fieldset.querySelectorAll('input[type="radio"]')
                            : [target];
                        const opts = Array.from(radios).map(r => ({
                            label: (r.parentElement.innerText || r.value || '').trim(),
                            id: r.id
                        }));
                        const anyChecked = Array.from(radios).some(r => r.checked);
                        if (!anyChecked) {
                            fields.push({
                                type: 'radio',
                                question: question,
                                options: opts.map(o => o.label),
                                radioIds: opts.map(o => o.id)
                            });
                        }
                    } else if (inputType === 'checkbox') {
                        // Skip checkboxes — handled separately
                    } else if (inputType === 'file') {
                        // Skip file inputs — handled separately
                    } else {
                        // Text or numeric input
                        const isNumeric = forAttr.includes('numeric') ||
                                         target.type === 'number' ||
                                         question.toLowerCase().includes('how many') ||
                                         question.toLowerCase().includes('years');
                        fields.push({
                            type: isNumeric ? 'numeric' : 'text',
                            question: question,
                            id: forAttr,
                            value: target.value,
                            selector: '#' + escapedId,
                            required: target.required
                        });
                    }
                } else if (tag === 'select') {
                    const options = Array.from(target.options)
                        .filter(o => o.value !== 'Select an option' && o.value !== '' && o.text !== 'Select an option')
                        .map(o => o.text);
                    fields.push({
                        type: 'select',
                        question: question,
                        id: forAttr,
                        options: options,
                        selectedIndex: target.selectedIndex,
                        selector: '#' + escapedId,
                        required: target.required
                    });
                } else if (tag === 'textarea') {
                    fields.push({
                        type: 'textarea',
                        question: question,
                        id: forAttr,
                        value: target.value,
                        selector: '#' + escapedId,
                        required: target.required
                    });
                }
            }

            // Also catch fieldsets with legends (radio groups without labels)
            dialog.querySelectorAll('fieldset').forEach(fs => {
                const legend = fs.querySelector('legend, span');
                const question = (legend ? legend.innerText : '').trim();
                if (!question || question.length < 3) return;
                const radios = fs.querySelectorAll('input[type="radio"]');
                if (radios.length === 0) return;
                const anyChecked = Array.from(radios).some(r => r.checked);
                if (anyChecked) return;

                const opts = Array.from(radios).map(r => ({
                    label: (r.parentElement.innerText || r.value || '').trim(),
                    id: r.id
                }));
                fields.push({
                    type: 'radio',
                    question: question,
                    options: opts.map(o => o.label),
                    radioIds: opts.map(o => o.id)
                });
            });

            return fields;
        }""")

    async def _fill_field(self, page: Page, field: dict, job_title: str):
        """Fill a single form field with a smart answer."""
        question = field.get("question", "")
        field_type = field.get("type", "text")
        current_value = field.get("value", "")

        # Skip already-filled fields
        if current_value and field_type in ("text", "numeric"):
            return
        if field_type == "select" and field.get("selectedIndex", 0) > 0:
            return

        if field_type in ("text", "numeric"):
            answer = answer_question(question, field_type=field_type, job_title=job_title)
            if answer == "":
                return  # Don't fill empty answers (e.g. middle name)
            # Enforce length limits for text inputs
            if field_type == "text" and len(answer) > 100:
                answer = answer[:100]
            selector = field.get("selector")
            if selector and answer:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        # Check if this is a typeahead/autocomplete field
                        is_typeahead = await page.evaluate(
                            "(sel) => { const el = document.querySelector(sel); "
                            "return el && (el.id.includes('typeahead') || "
                            "el.closest('[class*=\"typeahead\"]') !== null); }",
                            selector
                        )
                        q_lower = question.lower()
                        needs_autocomplete = is_typeahead or "city" in q_lower or "location" in q_lower

                        if needs_autocomplete:
                            # Type slowly to trigger autocomplete
                            await el.click()
                            await el.fill("")
                            await el.type(answer.split(",")[0], delay=50)  # Just "London"
                            await asyncio.sleep(1.5)
                            # Select first autocomplete option
                            option = await page.query_selector(
                                '[role="listbox"] [role="option"], '
                                '[class*="basic-typeahead"] li, '
                                '[class*="typeahead"] [role="option"]'
                            )
                            if option:
                                await option.click()
                                await asyncio.sleep(0.5)
                            else:
                                await el.press("Enter")
                                await asyncio.sleep(0.3)
                        else:
                            await el.fill(answer)
                            await asyncio.sleep(0.3)
                        log.debug(f"  Filled '{question[:50]}' → {answer}")
                except Exception as e:
                    log.debug(f"  Failed to fill '{question[:50]}': {e}")

        elif field_type == "select":
            options = field.get("options", [])
            if options:
                answer = answer_question(question, options=options, job_title=job_title)
                selector = field.get("selector")
                if selector and answer:
                    try:
                        await page.evaluate('''(args) => {
                            const sel = document.querySelector(args.sel);
                            if (!sel) return;
                            for (const opt of sel.options) {
                                if (opt.text === args.val || opt.value === args.val) {
                                    sel.value = opt.value;
                                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                                    break;
                                }
                            }
                        }''', {"sel": selector, "val": answer})
                        log.debug(f"  Selected '{question[:50]}' → {answer}")
                    except Exception as e:
                        log.debug(f"  Failed to select '{question[:50]}': {e}")

        elif field_type == "textarea":
            answer = answer_question(question, field_type="text", job_title=job_title)
            if answer == "":
                return
            selector = field.get("selector")
            if selector and answer:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.fill(answer)
                    log.debug(f"  Filled textarea '{question[:50]}' → {answer[:50]}")
                except Exception as e:
                    log.debug(f"  Failed to fill textarea: {e}")

        elif field_type == "radio":
            options = field.get("options", [])
            radio_ids = field.get("radioIds", [])
            if options and radio_ids:
                answer = answer_question(question, options=options, job_title=job_title)
                # Find which radio to click
                for i, opt in enumerate(options):
                    if opt == answer and i < len(radio_ids):
                        try:
                            rid = radio_ids[i]
                            await page.evaluate('''(id) => {
                                const el = document.getElementById(id);
                                if (el) el.click();
                            }''', rid)
                            log.debug(f"  Radio '{question[:50]}' → {answer}")
                        except Exception:
                            pass
                        break

    async def _get_footer_action(self, page: Page) -> str:
        """Determine what the footer primary button does: 'submit', 'next', or 'none'."""
        return await page.evaluate('''() => {
            const dialog = document.querySelector('div[role="dialog"]');
            if (!dialog) return 'none';
            const footer = dialog.querySelector('footer');
            if (!footer) return 'none';
            const primary = footer.querySelector('button[class*="artdeco-button--primary"]');
            if (!primary || primary.offsetWidth === 0) return 'none';
            const label = (primary.getAttribute('aria-label') || '').toLowerCase();
            const text = primary.innerText.toLowerCase();
            // Check for submit keywords in any language
            if (label.includes('submit') || label.includes('জমা') ||
                text.includes('submit') || text.includes('জমা')) {
                return 'submit';
            }
            return 'next';
        }''')

    async def _click_footer_primary(self, page: Page):
        """Click the primary button in the dialog footer via JS."""
        await page.evaluate('''() => {
            const dialog = document.querySelector('div[role="dialog"]');
            if (!dialog) return;
            const footer = dialog.querySelector('footer');
            if (!footer) return;
            const btn = footer.querySelector('button[class*="artdeco-button--primary"]');
            if (btn) btn.click();
        }''')

    async def _check_success(self, page: Page) -> bool:
        """Check if the application was submitted successfully."""
        return await page.evaluate('''() => {
            // Look for dismiss button (appears on success modal)
            const dismiss = document.querySelector('button[aria-label*="Dismiss"], button[aria-label*="dismiss"]');
            if (dismiss) return true;
            // Look for post-apply content
            const postApply = document.querySelector('[class*="post-apply"], [class*="jpac-modal"]');
            if (postApply) return true;
            // Check if dialog disappeared
            const dialog = document.querySelector('div[role="dialog"]');
            if (!dialog) return true;
            // Check body text for success indicators
            const text = document.body.innerText.toLowerCase();
            if (text.includes('application sent') || text.includes('আবেদন পাঠানো')) return true;
            return false;
        }''')
