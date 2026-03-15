"""LinkedIn Easy Apply — Playwright automation with smart screening question handling."""

import asyncio
import json
import logging
import re
from pathlib import Path

import anthropic
from playwright.async_api import async_playwright, Page

from auto_apply.config import (
    HEADLESS, BROWSER_TIMEOUT, CONFIG_DIR, CLAUDE_MODEL_SCORING,
)
from auto_apply.cv_parser import extract_cv_text
from auto_apply.applier.base import BaseApplier

log = logging.getLogger(__name__)
COOKIES_PATH = CONFIG_DIR / "linkedin_cookies.json"

# ── Runtime config (set via configure()) ──
_api_key: str = ""
_visible: bool = False
_linkedin_email: str = ""
_linkedin_password: str = ""
_cv_path: str = ""
_QUICK_ANSWERS: dict = {}

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
            system="You are filling out a job application form. Answer screening questions honestly based on the CV. Always present the candidate positively. For Yes/No questions, answer Yes when the candidate has relevant experience. For years of experience, give realistic numbers based on the CV.",
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

    except Exception as e:
        log.warning(f"Claude screening answer failed: {e}")
        # Fallback defaults
        if options:
            for opt in options:
                if opt.lower() in ("yes", "true"):
                    return opt
            return options[0]
        if field_type == "numeric":
            return "5"
        return "Yes"


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
        r"right.to.work|authorized.*work|eligible.*work|visa|sponsorship": "Yes",
        r"work.*permit|legally.*work": "Yes",

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
        r"summary|about.*you|cover.*letter|message|introduction": "",
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
    _api_key = api_key
    _visible = visible
    _linkedin_email = config.get("linkedin_email", "")
    _cv_path = config.get("cv_path", "")
    _QUICK_ANSWERS = _build_quick_answers(config)
    _claude_client = None  # Reset when API key changes
    _cv_text = None        # Reset cache when config changes
    if password is not None:
        _linkedin_password = password
    else:
        from auto_apply.setup_wizard import _get_linkedin_password
        _linkedin_password = _get_linkedin_password(_linkedin_email)


def answer_question(question: str, options: list[str] | None = None,
                    job_title: str = "", field_type: str = "text") -> str:
    """Answer a screening question — rules first, Claude as fallback."""
    # Skip non-English questions that are just UI labels (Bengali navigation text)
    # These are LinkedIn UI elements, not actual screening questions
    q_lower = question.lower()
    if any(skip in q_lower for skip in ["নির্বাচন", "আপলোড", "চিহ্নিত"]):
        return ""

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

    # ARCHITECTURE NOTE (Phase 10):
    # Each apply() call launches a fresh Playwright browser instance.
    # This is safe (isolated failures) but observable by LinkedIn's bot detection
    # when multiple applications are submitted in sequence from the same IP.
    # Phase 10 improvement: accept an optional pre-authenticated Page/Context
    # and reuse the scraper's existing browser session. This would also allow
    # description scraping (Phase 10 Gap 3 fix) to share the session.
    # Until Phase 10: the per-apply delay in pipeline.py (Step 9.1) is the
    # primary mitigation for session-frequency detection.
    async def apply(self, job: dict) -> tuple[bool, str]:
        if not _linkedin_email or not _linkedin_password:
            return False, "LinkedIn credentials not configured — run linkedin-autoapply setup"

        if not job.get("easy_apply"):
            return False, "Not an Easy Apply job"

        url = job.get("url", "")
        if not url:
            return False, "No job URL"

        job_title = job.get("title", "Unknown")
        _headless = False if _visible else HEADLESS

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=_headless)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            )
            page = await context.new_page()
            page.set_default_timeout(BROWSER_TIMEOUT)

            try:
                # Restore cookies
                if COOKIES_PATH.exists():
                    cookies = json.loads(COOKIES_PATH.read_text())
                    await context.add_cookies(cookies)

                await page.goto(url, wait_until="domcontentloaded")
                await asyncio.sleep(3)

                # Check if logged in
                if "login" in page.url or "authwall" in page.url:
                    await self._login(page)
                    await page.goto(url, wait_until="domcontentloaded")
                    await asyncio.sleep(3)

                # Click Easy Apply button via JS
                clicked = await page.evaluate('''() => {
                    const btn = document.querySelector('button[class*="jobs-apply-button"]');
                    if (btn) { btn.click(); return true; }
                    return false;
                }''')
                if not clicked:
                    return False, "Easy Apply button not found"

                await asyncio.sleep(3)

                # Fill the modal form step by step
                success = await self._fill_modal(page, job_title)

                # Save cookies
                cookies = await context.cookies()
                COOKIES_PATH.write_text(json.dumps(cookies))

                if success:
                    return True, "Applied via LinkedIn Easy Apply"
                return False, "Form filling incomplete"

            except Exception as e:
                log.error(f"LinkedIn apply failed: {e}")
                return False, f"Error: {str(e)[:200]}"
            finally:
                await browser.close()

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
                except Exception:
                    pass

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
