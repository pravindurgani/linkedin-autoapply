"""Main pipeline: scrape → filter → match → apply → report."""

import asyncio
import logging
import random
from datetime import datetime

import pandas as pd

from auto_apply.config import (
    TITLE_MUST_CONTAIN, TITLE_EXCLUDE, REPORT_PATH, SCORE_THRESHOLD,
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


async def run_match(api_key: str, cv_text: str) -> int:
    """Score unscored jobs against the CV using Claude. Returns count scored.

    Args:
        api_key: Anthropic API key.
        cv_text: Extracted CV text.
    """
    unscored = store.get_unscored_jobs()
    if not unscored:
        log.info("No unscored jobs to match.")
        return 0

    log.info(f"=== MATCHING: {len(unscored)} jobs to score ===")
    results = score_jobs_batch(unscored, api_key, cv_text)

    for result in results:
        store.record_match(result)

    above = sum(1 for r in results if r.score >= SCORE_THRESHOLD)
    log.info(f"=== MATCH COMPLETE: {above}/{len(results)} above threshold ===")
    return len(results)


async def run_apply(
    max_applies: int = 15,
    context: BrowserContext | None = None,
) -> int:
    """Apply to unapplied jobs above threshold. Returns count applied.

    Args:
        max_applies: Maximum number of apply attempts this run.
        context: Shared BrowserContext from linkedin_session(). Each apply()
            call creates its own Page within this context and closes it on
            return — a page-level failure does not affect subsequent jobs.
            If None, apply() uses a standalone browser per application.
    """
    if max_applies <= 0:
        raise ValueError(f"max_applies must be a positive integer, got {max_applies}")

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

    if len(candidates) > max_applies:
        log.info(f"Capping at {max_applies} attempts (found {len(candidates)} candidates). Run again to continue.")
        candidates = candidates[:max_applies]

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

    # 4–6. Run scrape + score + apply inside a single shared browser session.
    # The scraper authenticates the context once; the applier reuses it for every
    # application in the batch — preserving localStorage/sessionStorage/IndexedDB
    # tokens that LinkedIn requires for full authentication.
    from auto_apply.browser import linkedin_session

    async with linkedin_session(visible=visible) as context:
        # 4. Scrape
        await run_scrape(config, visible=visible, password=password, context=context)

        # 5. Score (no browser needed — pure API calls)
        cv_text = extract_cv_text()
        await run_match(api_key, cv_text)

        # 6. Apply (session health check runs inside run_apply before the loop)
        if dry_run:
            log.info("--dry-run: skipping application submission")
        else:
            await run_apply(max_applies=max_applies, context=context)

    # 7. Report (browser already closed by linkedin_session finally block)
    run_export()
    run_status()
