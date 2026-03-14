"""Main pipeline: scrape → filter → match → apply → report."""

import asyncio
import logging
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


async def run_scrape(config: dict, visible: bool = False) -> int:
    """Scrape LinkedIn and store jobs. Returns count of new jobs.

    Args:
        config: User config dict from config.json.
        visible: If True, run browser in visible mode.
    """
    from auto_apply.setup_wizard import _get_linkedin_password
    from auto_apply.sources.linkedin import configure as configure_source

    email = config["linkedin_email"]
    password = _get_linkedin_password(email)
    configure_source(email=email, password=password, visible=visible)

    search_titles = config["job_titles"]
    location = config["location"]
    min_salary = int(config.get("min_salary", 0))
    must_contain = config.get("title_must_contain", TITLE_MUST_CONTAIN)
    title_exclude = config.get("title_exclude", TITLE_EXCLUDE)

    log.info(f"=== SCRAPING: {len(search_titles)} search terms ===")
    source = LinkedInSource()
    jobs = await source.scrape(search_titles, location, min_salary, visible=visible)
    log.info(f"  Raw results: {len(jobs)}")

    filtered = [
        j for j in jobs
        if _title_passes_filter(j.title, must_contain, title_exclude)
        and _salary_passes_filter(j, min_salary)
    ]
    log.info(f"  After filter: {len(filtered)}")

    new_count = 0
    for job in filtered:
        store.upsert_job(job)
        new_count += 1

    log.info(f"=== SCRAPE COMPLETE: {new_count} jobs stored ===")
    return new_count


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


async def run_apply() -> int:
    """Apply to unapplied jobs above threshold. Returns count applied."""
    candidates = store.get_unapplied_matches(SCORE_THRESHOLD)
    if not candidates:
        log.info("No unapplied matches to apply for.")
        return 0

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
                success, message = await applier.apply(job)
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
                    store.record_application(Application(
                        job_id=job_id,
                        status=ApplicationStatus.FAILED,
                        method=ApplyMethod.EASY_APPLY,
                        error_message=message,
                    ))
                    log.warning(f"  FAILED: {message}")
            except Exception as e:
                store.record_application(Application(
                    job_id=job_id,
                    status=ApplicationStatus.FAILED,
                    method=ApplyMethod.EASY_APPLY,
                    error_message=str(e)[:500],
                ))
                log.error(f"  ERROR: {e}")
        else:
            store.record_application(Application(
                job_id=job_id,
                status=ApplicationStatus.SKIPPED,
                method=ApplyMethod.NONE,
                error_message="Not an Easy Apply job",
            ))
            log.info("  SKIPPED: Not an Easy Apply job")

        await asyncio.sleep(2)

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

    # 3. Configure modules with runtime credentials
    configure_applier(api_key=api_key, config=config, visible=visible)
    password = _get_linkedin_password(config["linkedin_email"])
    configure_source(email=config["linkedin_email"], password=password, visible=visible)

    # 4. Scrape
    await run_scrape(config, visible=visible)

    # 5. Score
    cv_text = extract_cv_text()
    await run_match(api_key, cv_text)

    # 6. Apply (unless dry run)
    if dry_run:
        log.info("--dry-run: skipping application submission")
    else:
        await run_apply()

    # 7. Report
    run_export()
    run_status()
