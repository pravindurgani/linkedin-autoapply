"""Main pipeline: scrape → filter → match → apply → report."""

import asyncio
import logging
import random
from datetime import datetime

import pandas as pd

from auto_apply.config import (
    TITLE_MUST_CONTAIN, TITLE_EXCLUDE, REPORT_PATH, SCORE_THRESHOLD,
    MAX_DAILY_APPLICATIONS, MAX_SESSION_MINUTES,
    APPLY_BUSINESS_HOURS_ONLY, APPLY_BUSINESS_HOURS_START, APPLY_BUSINESS_HOURS_END,
)

from auto_apply.models import (
    Job, ApplicationStatus, ApplyMethod, Application,
)
from auto_apply import store
from auto_apply.matcher import score_jobs_batch
from auto_apply.sources.linkedin import LinkedInSource
from auto_apply.applier.linkedin_apply import LinkedInApplier
from auto_apply.cv_parser import extract_cv_text
from playwright.async_api import BrowserContext

log = logging.getLogger(__name__)

# Phase 11.7 — LinkedIn's suspected daily Tier 1 restriction threshold.
# Not configurable — this is LinkedIn's inferred policy, not a user preference.
# Tier 1: features disabled 1-24h. Tier 2: account locked 3-14 days.
_LINKEDIN_TIER1_DAILY_THRESHOLD: int = 20


def _title_passes_filter(title: str, must_contain: list[str], exclude: list[str]) -> bool:
    """Check if a job title matches the configured keyword criteria."""
    t = title.lower()
    if must_contain and not any(kw in t for kw in must_contain):
        return False
    if any(ex in t for ex in exclude):
        return False
    return True


def _salary_passes_filter(job: Job, min_salary: int) -> bool:
    """Check if salary meets minimum threshold."""
    if job.salary_max and job.salary_max < min_salary:
        return False
    if job.salary_min and job.salary_min < min_salary * 0.85:
        return False
    return True


async def run_scrape(
    config: dict,
    visible: bool = False,
    password: str | None = None,
    context: BrowserContext | None = None,
) -> int:
    """Scrape LinkedIn and store jobs. Returns count of new jobs.

    Args:
        config: User config dict from config.json.
        visible: If True, run browser in visible mode.
        password: LinkedIn password. If None, fetched from keychain/setup wizard.
        context: Shared BrowserContext from linkedin_session(). If provided,
            passed through to LinkedInSource.scrape() for session reuse.
    """
    from auto_apply.setup_wizard import _get_linkedin_password
    from auto_apply.sources.linkedin import configure as configure_source

    email = config["linkedin_email"]
    if password is None:
        password = _get_linkedin_password(email)
    configure_source(email=email, password=password, visible=visible)

    search_titles = config["job_titles"]
    location = config["location"]
    min_salary = int(config.get("min_salary", 0))
    must_contain = config.get("title_must_contain", TITLE_MUST_CONTAIN)
    title_exclude = config.get("title_exclude", TITLE_EXCLUDE)

    log.info(f"=== SCRAPING: {len(search_titles)} search terms ===")
    source = LinkedInSource()
    jobs = await source.scrape(search_titles, location, min_salary, visible=visible, context=context)
    log.info(f"  Raw results: {len(jobs)}")

    filtered = [
        j for j in jobs
        if _title_passes_filter(j.title, must_contain, title_exclude)
        and _salary_passes_filter(j, min_salary)
    ]
    log.info(f"  After filter: {len(filtered)}")

    stored_count = 0
    for job in filtered:
        store.upsert_job(job)
        stored_count += 1

    log.info(f"=== SCRAPE COMPLETE: {stored_count} jobs stored ===")

    # Phase 10.5 — post-scrape location validation.
    # LinkedIn silently expands geography when a salary-filtered search returns
    # too few results (e.g. searching Manchester at £80k+ returns London jobs).
    # Compare distinct scraped locations against the searched location and emit
    # a structured WARNING when mismatches are found. Does not abort the pipeline.
    if stored_count > 0:
        scraped_locations = store.get_distinct_locations()
        if scraped_locations:
            # Match on city name only — "Manchester" should accept "Greater Manchester,
            # England" and "Manchester Area, UK" without false-positive mismatches.
            city = location.split(",")[0].strip().lower()
            mismatches = [loc for loc in scraped_locations if city not in loc.lower()]
            if mismatches:
                match_count = len(scraped_locations) - len(mismatches)
                match_rate = match_count / len(scraped_locations) * 100
                log.warning(
                    f"Location mismatch: searched '{location}' but "
                    f"{len(mismatches)}/{len(scraped_locations)} scraped locations "
                    f"do not match (match rate {match_rate:.0f}%). "
                    f"Non-matching locations: {mismatches[:5]}. "
                    f"LinkedIn may have expanded geographically due to sparse "
                    f"results at the configured salary band."
                )

    return stored_count


async def run_match(
    api_key: str,
    cv_text: str,
    requires_sponsorship: bool | None = None,
) -> int:
    """Score unscored jobs against the CV using Claude. Returns count scored.

    Args:
        api_key: Anthropic API key.
        cv_text: Extracted CV text.
        requires_sponsorship: Whether the candidate requires visa sponsorship.
            Passed to scorer so Claude can penalise non-sponsoring roles.
    """
    unscored = store.get_unscored_jobs()
    if not unscored:
        log.info("No unscored jobs to match.")
        return 0

    log.info(f"=== MATCHING: {len(unscored)} jobs to score ===")
    results = score_jobs_batch(unscored, api_key, cv_text, requires_sponsorship=requires_sponsorship)

    for result in results:
        store.record_match(result)

    above = sum(1 for r in results if r.score >= SCORE_THRESHOLD)
    log.info(f"=== MATCH COMPLETE: {above}/{len(results)} above threshold ===")
    return len(results)


async def run_apply(
    max_applies: int = 15,
    context: BrowserContext | None = None,
    max_daily_applications: int = MAX_DAILY_APPLICATIONS,
    business_hours_only: bool = APPLY_BUSINESS_HOURS_ONLY,
) -> int:
    """Apply to unapplied jobs above threshold. Returns count applied.

    Args:
        max_applies: Maximum apply attempts this run (per-run cap).
        context: Shared BrowserContext from linkedin_session(). Each apply()
            call creates its own Page within this context and closes it on
            return — a page-level failure does not affect subsequent jobs.
            If None, apply() uses a standalone browser per application.
        max_daily_applications: Hard daily cap — refuse to apply once today's
            cumulative count reaches this limit. Resets at local midnight.
        business_hours_only: If True, skip the apply phase when the current
            local time is outside APPLY_BUSINESS_HOURS_START–END.
    """
    if max_applies <= 0:
        raise ValueError(f"max_applies must be a positive integer, got {max_applies}")

    # Phase 11.7 — Business hours gate.
    # Checked first so we abort before any browser or DB activity.
    if business_hours_only:
        current_hour = datetime.now().hour
        if not (APPLY_BUSINESS_HOURS_START <= current_hour < APPLY_BUSINESS_HOURS_END):
            log.warning(
                f"Apply phase skipped — current hour {current_hour:02d}:xx is outside "
                f"business hours ({APPLY_BUSINESS_HOURS_START:02d}:00–"
                f"{APPLY_BUSINESS_HOURS_END:02d}:00, business_hours_only=true). "
                f"Override in config.json: set business_hours_only to false."
            )
            return 0

    # Phase 11.7 — Daily application cap.
    # Query today's successful application count before touching candidates.
    today_count = store.get_daily_apply_count()
    remaining_today = max_daily_applications - today_count
    if remaining_today <= 0:
        log.warning(
            f"Daily application cap reached ({today_count}/{max_daily_applications} today). "
            f"Apply phase aborted. Cap resets at local midnight. "
            f"Adjust max_daily_applications in config.json to change the limit."
        )
        return 0
    if today_count >= int(max_daily_applications * 0.8):
        log.warning(
            f"Approaching daily cap: {today_count}/{max_daily_applications} applications "
            f"submitted today ({int(today_count / max_daily_applications * 100)}% of limit)."
        )

    # Phase 11.7 — LinkedIn Tier 1 threshold advisory.
    # Fires regardless of the user's cap setting to surface the risk.
    if today_count >= _LINKEDIN_TIER1_DAILY_THRESHOLD:
        log.warning(
            f"Applications today ({today_count}) are at or above LinkedIn's suspected "
            f"Tier 1 restriction threshold (~{_LINKEDIN_TIER1_DAILY_THRESHOLD}/day). "
            f"Account restrictions (feature lock 1-24h) may already be active."
        )
    elif today_count + max_applies > _LINKEDIN_TIER1_DAILY_THRESHOLD:
        projected = today_count + max_applies
        log.warning(
            f"This run may push daily total to ~{projected} — above LinkedIn's suspected "
            f"Tier 1 threshold (~{_LINKEDIN_TIER1_DAILY_THRESHOLD}/day). "
            f"Reduce max_daily_applications in config.json to stay under the threshold."
        )

    # Session health check — verify the shared context is authenticated before
    # attempting any applications. Aborts early with a clear error rather than
    # silently failing on every job in the batch.
    if context is not None:
        from auto_apply.browser import verify_session
        if not await verify_session(context):
            log.error(
                "Session health check failed — LinkedIn context is not authenticated. "
                "The scraper may have failed to log in or the session expired. "
                "Aborting apply phase. Run with --visible to diagnose login issues."
            )
            return 0

    candidates = store.get_unapplied_matches(SCORE_THRESHOLD)
    if not candidates:
        log.info("No unapplied matches to apply for.")
        return 0

    # Prompt for visa status once per run (no-op if already prompted).
    # Placed after candidate check so prompt only appears when there are
    # jobs to apply to. prompt_visa_status() is idempotent within a run.
    from auto_apply.applier.linkedin_apply import prompt_visa_status
    prompt_visa_status()

    # Cap to the lower of: per-run limit and remaining daily capacity.
    effective_cap = min(max_applies, remaining_today)
    if len(candidates) > effective_cap:
        reason = (
            f"daily cap ({remaining_today} remaining)"
            if effective_cap == remaining_today and remaining_today < max_applies
            else f"--max-applies ({max_applies})"
        )
        log.info(
            f"Capping at {effective_cap} attempts due to {reason} "
            f"(found {len(candidates)} candidates). Run again to continue."
        )
        candidates = candidates[:effective_cap]

    log.info(f"=== APPLYING: {len(candidates)} jobs to apply for ===")
    applied_count = 0
    applier = LinkedInApplier()

    for job in candidates:
        title = job["title"]
        company = job["company"]
        score = job["score"]
        job_id = job["id"]

        log.info(f"\nApplying [{score}/100]: {title} at {company}")

        if job.get("easy_apply"):
            try:
                success, message, screenshot_path, failure_url = await applier.apply(job, context=context)
                if success:
                    store.record_application(Application(
                        job_id=job_id,
                        status=ApplicationStatus.APPLIED,
                        method=ApplyMethod.EASY_APPLY,
                        applied_at=datetime.now(),
                    ))
                    applied_count += 1
                    log.info(f"  SUCCESS: {message}")
                else:
                    # Distinguish permanently-non-applicable jobs (skip) from transient
                    # failures (fail → retry next run via get_unapplied_matches JOIN fix).
                    # Skipped messages come from _apply_on_page() stages 1 and 2:
                    #   "External apply only"  — redirects to company ATS (stage 1)
                    #   "Connect button found" — job closed / recruiter-only (stage 2)
                    #   "not Easy Apply"       — easy_apply flag was wrong at DB level
                    _SKIP_PHRASES = (
                        "External apply only",
                        "Connect button found",
                        "not Easy Apply",
                    )
                    if any(phrase in message for phrase in _SKIP_PHRASES):
                        status = ApplicationStatus.SKIPPED
                    else:
                        status = ApplicationStatus.FAILED
                    store.record_application(Application(
                        job_id=job_id,
                        status=status,
                        method=ApplyMethod.EASY_APPLY,
                        error_message=message,
                        screenshot_path=screenshot_path,
                        failure_url=failure_url,
                    ))
                    if status == ApplicationStatus.SKIPPED:
                        log.info(f"  SKIPPED: {message}")
                    else:
                        log.warning(f"  FAILED: {message}")
            except Exception as e:
                store.record_application(Application(
                    job_id=job_id,
                    status=ApplicationStatus.FAILED,
                    method=ApplyMethod.EASY_APPLY,
                    error_message=str(e)[:500],
                ))
                log.error(f"  ERROR: {e}")
            delay = random.uniform(45, 90)
            log.info(f"  Waiting {delay:.0f}s before next application...")
            await asyncio.sleep(delay)
        else:
            store.record_application(Application(
                job_id=job_id,
                status=ApplicationStatus.SKIPPED,
                method=ApplyMethod.NONE,
                error_message="Not an Easy Apply job",
            ))
            log.info("  SKIPPED: Not an Easy Apply job")

    log.info(f"\n=== APPLY COMPLETE: {applied_count}/{len(candidates)} applied ===")
    return applied_count


def run_export():
    """Export all jobs with status to CSV."""
    rows = store.get_all_jobs_with_status()
    if not rows:
        log.info("No jobs to export.")
        return

    df = pd.DataFrame(rows)
    df.to_csv(str(REPORT_PATH), index=False)
    log.info(f"Report exported: {REPORT_PATH} ({len(df)} rows)")


def run_status():
    """Print summary statistics."""
    stats = store.get_stats()
    print("\n" + "=" * 50)
    print("  LINKEDIN AUTOAPPLY — STATUS")
    print("=" * 50)
    print(f"  Total jobs scraped:    {stats['total_jobs']}")
    print(f"  Jobs scored:           {stats['scored']}")
    print(f"  Above threshold (70+): {stats['above_threshold']}")
    print(f"  Applied:               {stats['applied']}")
    print(f"  Failed:                {stats['failed']}")
    print("-" * 50)
    print("  By source:")
    for source, count in stats.get("by_source", {}).items():
        print(f"    {source:15s}  {count}")
    print("=" * 50 + "\n")


async def run_full_pipeline(
    skip_cv_review: bool = False,
    dry_run: bool = False,
    visible: bool = False,
    max_applies: int = 15,
) -> None:
    """Run the full linkedin-autoapply pipeline.

    Args:
        skip_cv_review: If True, bypass the Claude CV review step.
        dry_run: If True, scrape and score but do not submit applications.
        visible: If True, run browser in visible mode for this session.
    """
    from auto_apply.setup_wizard import check_and_run_wizard, _get_linkedin_password
    from auto_apply.cv_review import run_cv_review
    from auto_apply.applier.linkedin_apply import configure as configure_applier
    from auto_apply.sources.linkedin import configure as configure_source

    # 1. Load config (runs wizard on first use)
    config = check_and_run_wizard()
    api_key = config["claude_api_key"]

    log.info("=" * 60)
    log.info("  LINKEDIN AUTOAPPLY — FULL PIPELINE")
    log.info(f"  Targets: {', '.join(config['job_titles'])}")
    log.info(f"  Location: {config['location']} | Min: £{int(config.get('min_salary', 0)):,}")
    log.info("=" * 60)

    # 2. CV review gate
    if not skip_cv_review:
        proceed = run_cv_review(api_key, config["cv_path"], config["job_titles"])
        if not proceed:
            print("Exiting. Update your CV and re-run when ready.")
            return

    # 3. Configure modules with runtime credentials (once, before browser opens)
    password = _get_linkedin_password(config["linkedin_email"])
    configure_applier(api_key=api_key, config=config, visible=visible, password=password)
    configure_source(email=config["linkedin_email"], password=password, visible=visible)

    # Phase 11.7 — read rate-limit settings from config (fall back to system defaults).
    max_daily_applications = int(config.get("max_daily_applications", MAX_DAILY_APPLICATIONS))
    max_session_minutes = int(config.get("max_session_minutes", MAX_SESSION_MINUTES))
    business_hours_only = bool(config.get("business_hours_only", APPLY_BUSINESS_HOURS_ONLY))

    # 4–6. Run scrape + score + apply inside a single shared browser session.
    # The scraper authenticates the context once; the applier reuses it for every
    # application in the batch — preserving localStorage/sessionStorage/IndexedDB
    # tokens that LinkedIn requires for full authentication.
    from auto_apply.browser import linkedin_session

    session_start = datetime.now()  # Phase 11.7: track session duration

    async with linkedin_session(visible=visible) as context:
        # 4. Scrape
        await run_scrape(config, visible=visible, password=password, context=context)

        # 5. Score (no browser needed — pure API calls)
        cv_text = extract_cv_text()
        requires_sponsorship = config.get("requires_sponsorship")
        await run_match(api_key, cv_text, requires_sponsorship=requires_sponsorship)

        # 6. Apply (session health check runs inside run_apply before the loop)
        if dry_run:
            log.info("--dry-run: skipping application submission")
        else:
            # Phase 11.7 — session duration gate before apply phase.
            elapsed_min = (datetime.now() - session_start).total_seconds() / 60
            if elapsed_min >= max_session_minutes:
                log.warning(
                    f"Session duration limit reached ({elapsed_min:.0f}/{max_session_minutes} min) "
                    f"— skipping apply phase to limit account exposure. "
                    f"Adjust max_session_minutes in config.json to change the limit."
                )
            else:
                if elapsed_min >= max_session_minutes * 0.8:
                    log.warning(
                        f"Session approaching duration limit "
                        f"({elapsed_min:.0f}/{max_session_minutes} min, "
                        f"{int(elapsed_min / max_session_minutes * 100)}% elapsed)."
                    )
                await run_apply(
                    max_applies=max_applies,
                    context=context,
                    max_daily_applications=max_daily_applications,
                    business_hours_only=business_hours_only,
                )

    # 7. Report (browser already closed by linkedin_session finally block)
    run_export()
    run_status()
