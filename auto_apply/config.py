"""Configuration — runtime constants and config loader."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── User config directory ──
CONFIG_DIR = Path.home() / ".linkedin_autoapply"
CONFIG_PATH = CONFIG_DIR / "config.json"


def ensure_config_dir() -> None:
    """Create CONFIG_DIR if it does not exist. Call before any file I/O under CONFIG_DIR."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = CONFIG_DIR / "jobs.db"
CV_CACHE_PATH = CONFIG_DIR / "cv_text.txt"
REPORT_PATH = CONFIG_DIR / "application_report.csv"

# Title keywords — a job must contain at least one of these (case-insensitive)
TITLE_MUST_CONTAIN = [
    "data", "analytics", "machine learning", "ml engineer",
    "ai engineer", "business intelligence", "bi engineer",
]

# Title exclusions — skip jobs containing these
TITLE_EXCLUDE = [
    "intern", "apprentice", "junior", "graduate", "entry level",
    "director", "vp ", "vice president", "chief", "head of",
    "contract", "freelance",
]

# ── Score Threshold ──
SCORE_THRESHOLD = 70

# ── Rate Limits ──
RATE_LIMIT_LINKEDIN: float = 4.0

# ── Claude AI ──
CLAUDE_MODEL_SCORING = "claude-haiku-4-5-20251001"
CLAUDE_MODEL_CV_REVIEW = "claude-sonnet-4-6"

# ── Playwright ──
HEADLESS = True  # Set False to debug browser interactions
BROWSER_TIMEOUT = 30_000  # ms


def load_config() -> dict:
    """Load user config from CONFIG_PATH. Returns empty dict if not found."""
    ensure_config_dir()
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception as e:
        log.debug("Config load failed: %s", e)
        return {}


def config_exists() -> bool:
    """Return True if config.json exists and is non-empty."""
    if not CONFIG_PATH.exists():
        return False
    try:
        return bool(load_config())
    except Exception as e:
        log.debug("Config exists check failed: %s", e)
        return False


def get_applicant_info() -> dict:
    """Load config.json and return applicant info dict.

    Returns:
        Dict with keys: name, email, phone, cv_path, linkedin_email,
        claude_api_key, job_titles, location, min_salary.
    """
    return load_config()
