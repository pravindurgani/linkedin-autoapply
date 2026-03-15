"""CLI entry point for linkedin-autoapply."""

import asyncio
import logging
import sys
from pathlib import Path

import click

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auto_apply.pipeline import (
    run_scrape, run_apply,
    run_export, run_status, run_full_pipeline,
)


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING)


@click.group(invoke_without_command=True)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
@click.pass_context
def cli(ctx, verbose):
    """linkedin-autoapply — scrape, score, and apply to jobs on LinkedIn."""
    setup_logging(verbose)
    if ctx.invoked_subcommand is None:
        ctx.invoke(run)


@cli.command()
@click.option("--skip-cv-review", is_flag=True, help="Skip the Claude CV review step")
@click.option("--dry-run", is_flag=True, help="Score jobs but do not submit applications")
@click.option("--visible", is_flag=True, help="Run browser in visible mode (for 2FA/CAPTCHA on first login)")
@click.option("--max-applies", default=15, show_default=True, help="Maximum applications to attempt per run (each takes ~1 min with safety delays).")
def run(skip_cv_review, dry_run, visible, max_applies):
    """Run the full pipeline: scrape, score, and apply."""
    if max_applies <= 0:
        raise click.UsageError("--max-applies must be a positive integer")
    asyncio.run(run_full_pipeline(
        skip_cv_review=skip_cv_review,
        dry_run=dry_run,
        visible=visible,
        max_applies=max_applies,
    ))


@cli.command()
def setup():
    """Re-run the setup wizard to update your configuration."""
    from auto_apply.setup_wizard import run_wizard
    run_wizard()


@cli.command()
@click.option("--visible", is_flag=True, help="Run browser in visible mode (for 2FA/CAPTCHA on first login)")
def scrape(visible):
    """Scrape LinkedIn jobs only, no applying."""
    from auto_apply.setup_wizard import check_and_run_wizard
    config = check_and_run_wizard()
    asyncio.run(run_scrape(config, visible=visible))


@cli.command()
def status():
    """Show database statistics."""
    run_status()


@cli.command()
def export():
    """Export results to CSV."""
    run_export()


if __name__ == "__main__":
    cli()
