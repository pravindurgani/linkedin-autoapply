"""CV review step using Claude API before applications begin."""

import logging

import anthropic

from auto_apply.config import CLAUDE_MODEL_CV_REVIEW
from auto_apply.cv_parser import extract_cv_text

log = logging.getLogger(__name__)


def build_cv_review_prompt(cv_text: str, job_titles: list[str]) -> str:
    """Build the CV review prompt for Claude.

    Args:
        cv_text: Full extracted text from the CV PDF.
        job_titles: List of target job titles from user config.

    Returns:
        Formatted prompt string.
    """
    titles_str = ", ".join(job_titles)
    return f"""You are a CV / resume expert. Please review the following CV for a candidate targeting these roles: {titles_str}

Provide a structured review covering:

1. **Strengths** — What makes this CV compelling for the target roles?
2. **Weaknesses / Gaps** — Specific areas where the CV falls short for these roles.
3. **ATS Optimisation** — Keywords missing for these roles that applicant tracking systems look for.
4. **Overall Readiness Score** — Rate out of 10 with justification.

CV:
---
{cv_text}
---"""


def run_cv_review(api_key: str, cv_path: str, job_titles: list[str]) -> bool:
    """Run the Claude CV review and prompt user to confirm or abort.

    Args:
        api_key: Anthropic API key.
        cv_path: Path to the CV PDF file.
        job_titles: Target job titles from config.

    Returns:
        True if user confirms to proceed, False if they want to abort.

    Raises:
        anthropic.APIError: Propagated if the API call fails fatally.
    """
    cv_text = extract_cv_text(cv_path=cv_path)
    prompt = build_cv_review_prompt(cv_text, job_titles)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL_CV_REVIEW,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as e:
        log.error(f"CV review authentication failed (cv_path={cv_path}): {e}")
        print(f"\nCV review failed: invalid API key — check your claude_api_key in config.")
        print("Tip: re-run with --skip-cv-review to bypass this step.")
        answer = input("Continue without CV review? [y/n]: ").strip().lower()
        return answer == "y"
    except Exception as e:
        log.error(f"CV review API call failed (cv_path={cv_path}): {e}")
        print(f"\nCV review failed: {e}")
        print("Tip: re-run with --skip-cv-review to bypass this step.")
        answer = input("Continue without CV review? [y/n]: ").strip().lower()
        return answer == "y"

    print("\n" + "=" * 60)
    print("  CV REVIEW")
    print("=" * 60)
    print(response.content[0].text)
    print("=" * 60 + "\n")

    answer = input("Are you happy to proceed with applications? [y/n]: ").strip().lower()
    return answer == "y"
