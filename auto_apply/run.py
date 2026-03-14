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


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose):
    """linkedin-autoapply — scrape, score, and apply to jobs on LinkedIn."""
    setup_logging(verbose)


@cli.command()
@click.option("--skip-cv-review", is_flag=True, help="Skip the Claude CV review step")
@click.option("--dry-run", is_flag=True, help="Score jobs but do not submit applications")
@click.option("--visible", is_flag=True, help="Run browser in visible mode (for 2FA/CAPTCHA on first login)")
def run(skip_cv_review, dry_run, visible):
    """Run the full pipeline: scrape, score, and apply."""
    asyncio.run(run_full_pipeline(
        skip_cv_review=skip_cv_review,
        dry_run=dry_run,
        visible=visible,
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
