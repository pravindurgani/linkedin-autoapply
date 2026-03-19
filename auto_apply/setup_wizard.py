"""First-run setup wizard for linkedin-autoapply."""

import getpass
import json
import re
import subprocess
import sys
from pathlib import Path

import anthropic
import keyring
import pdfplumber

from auto_apply.config import CONFIG_DIR, CONFIG_PATH, TITLE_MUST_CONTAIN, TITLE_EXCLUDE


def _validate_claude_api_key(key: str) -> tuple[bool, str]:
    """Validate Claude API key format and make a test API call.

    Args:
        key: The API key string to validate.

    Returns:
        Tuple of (is_valid, error_message). error_message is empty on success.
    """
    if not key.startswith("sk-ant-"):
        return False, "Key must start with 'sk-ant-'"
    try:
        client = anthropic.Anthropic(api_key=key)
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        return True, ""
    except anthropic.AuthenticationError:
        return False, "Invalid API key"
    except anthropic.APIConnectionError:
        return False, "Cannot reach Anthropic API — check your internet connection"
    except Exception as e:
        return False, f"Validation error: {e}"


def _validate_email(email: str) -> bool:
    """Basic email format validation via regex."""
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()))


def _validate_cv_path(path_str: str) -> tuple[bool, str]:
    """Validate that path exists and is a readable PDF.

    Args:
        path_str: File path string to validate.

    Returns:
        Tuple of (is_valid, error_message).
    """
    path = Path(path_str.strip())
    if not path.exists():
        return False, f"File not found: {path}"
    if path.suffix.lower() != ".pdf":
        return False, "File must be a PDF"
    try:
        with pdfplumber.open(path) as pdf:
            _ = pdf.pages[0]
        return True, ""
    except Exception as e:
        return False, str(e)


def _get_linkedin_password(email: str) -> str:
    """Retrieve LinkedIn password from keyring, or prompt at runtime if unavailable.

    Args:
        email: LinkedIn account email (keyring service key).

    Returns:
        Password string.
    """
    try:
        import keyring as _keyring
        password = _keyring.get_password("linkedin-autoapply", email)
        if password:
            return password
    except Exception:
        pass
    import getpass as _getpass
    return _getpass.getpass(f"Enter LinkedIn password for {email}: ")


def _check_playwright_browsers() -> None:
    """Check if Playwright Chromium is installed; install if not.

    Prompts the user before running playwright install chromium.
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return
    except Exception:
        pass
    print("\nPlaywright Chromium browser not found.")
    answer = input("Install it now? (recommended) [y/n]: ").strip().lower()
    if answer == "y":
        subprocess.run(["playwright", "install", "chromium"], check=True)
    else:
        print("Warning: you must run 'playwright install chromium' before using this tool.")


def save_config(config: dict) -> None:
    """Save config dict to CONFIG_PATH, creating parent dirs if needed.

    Args:
        config: Configuration dictionary to persist.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    CONFIG_PATH.chmod(0o600)


def run_wizard() -> dict:
    """Run the interactive first-run setup wizard.

    Returns:
        Dict of all collected config values (without password — that's in keyring).

    Raises:
        SystemExit: If the user aborts at any point (Ctrl+C).
    """
    try:
        return _run_wizard_prompts()
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)


def _run_wizard_prompts() -> dict:
    """Execute the wizard prompt sequence."""
    print("=" * 60)
    print("  linkedin-autoapply — First-Run Setup")
    print("=" * 60)
    print()

    # Step 1: Claude API key
    print("Step 1/8: Claude API key")
    print("  Get yours at: https://console.anthropic.com")
    while True:
        api_key = getpass.getpass("  API key (sk-ant-...): ").strip()
        print("  Validating...", end="", flush=True)
        valid, err = _validate_claude_api_key(api_key)
        if valid:
            print(" OK")
            break
        print(f" FAILED\n  {err}")

    # Step 2: LinkedIn email
    print("\nStep 2/8: LinkedIn account email")
    while True:
        linkedin_email = input("  LinkedIn email: ").strip()
        if _validate_email(linkedin_email):
            break
        print("  Invalid email format.")

    # Step 3: LinkedIn password (stored in keyring, not config)
    print("\nStep 3/8: LinkedIn password")
    while True:
        linkedin_password = getpass.getpass("  LinkedIn password: ")
        if linkedin_password:
            break
        print("  Password cannot be empty.")
    try:
        keyring.set_password("linkedin-autoapply", linkedin_email, linkedin_password)
    except Exception as e:
        print(
            f"\n  Warning: could not save password to system keyring ({e}).\n"
            "  You will be prompted for your LinkedIn password on each run."
        )

    # Step 4: Job titles
    print("\nStep 4/8: Job titles to search for")
    print("  Enter one per line. Empty line when done.")
    job_titles: list[str] = []
    while True:
        title = input(f"  Title {len(job_titles) + 1} (or Enter to finish): ").strip()
        if not title:
            if job_titles:
                break
            print("  At least one job title required.")
        else:
            job_titles.append(title)

    # Step 5: Location
    print("\nStep 5/8: Location")
    location = input("  Location (e.g. London, UK) [London]: ").strip() or "London"

    # Step 6: Minimum salary
    print("\nStep 6/8: Minimum annual salary (GBP)")
    while True:
        salary_str = input("  Min salary (e.g. 90000): ").strip().replace(",", "").replace("£", "")
        try:
            min_salary = int(salary_str)
            if min_salary > 0:
                break
            print("  Must be a positive number.")
        except ValueError:
            print("  Enter a number, e.g. 90000")

    # Step 7: CV path + applicant info
    print("\nStep 7/8: CV and contact details")
    while True:
        cv_path = input("  Path to your CV PDF: ").strip()
        valid, err = _validate_cv_path(cv_path)
        if valid:
            break
        print(f"  {err}")

    while True:
        applicant_name = input("  Full name: ").strip()
        if applicant_name:
            break
        print("  Name cannot be empty.")

    while True:
        applicant_email = input("  Contact email: ").strip()
        if _validate_email(applicant_email):
            break
        print("  Invalid email format.")

    applicant_phone = input("  Phone number (e.g. +447000000000): ").strip()

    # Step 8: Visa & Work Authorisation
    print("\nStep 8/8: Visa & Work Authorisation")
    while True:
        rtw = input("  Do you have the right to work in your target country? [y/n]: ").strip().lower()
        if rtw in ("y", "yes", "n", "no"):
            break
        print("  Please enter y or n.")

    requires_sponsorship: bool
    work_authorisation: str
    visa_notes: str = ""

    if rtw in ("y", "yes"):
        print("  What is your authorisation type?")
        print("    [1] Citizen / permanent resident")
        print("    [2] Pre-settled / settled status (EU Settlement Scheme)")
        print("    [3] Valid work visa (no sponsorship needed)")
        print("    [4] Other")
        while True:
            auth_choice = input("  Select [1-4]: ").strip()
            if auth_choice in ("1", "2", "3", "4"):
                break
            print("  Please enter 1, 2, 3, or 4.")
        _AUTH_KEYS = {"1": "citizen", "2": "pre_settled", "3": "valid_visa", "4": "other"}
        work_authorisation = _AUTH_KEYS[auth_choice]
        requires_sponsorship = False
        if work_authorisation in ("valid_visa", "other"):
            visa_notes = input(
                "  Any notes about your visa? (e.g. limited to 20 hrs/week): "
            ).strip()
    else:
        print("  What type of sponsorship do you need?")
        print("    [1] Skilled Worker visa (UK)")
        print("    [2] Other visa type")
        while True:
            spons_choice = input("  Select [1-2]: ").strip()
            if spons_choice in ("1", "2"):
                break
            print("  Please enter 1 or 2.")
        work_authorisation = "skilled_worker_uk" if spons_choice == "1" else "other"
        requires_sponsorship = True
        if work_authorisation == "other":
            visa_notes = input(
                "  Any notes about your visa requirements? "
            ).strip()

    config = {
        "claude_api_key": api_key,
        "linkedin_email": linkedin_email,
        "job_titles": job_titles,
        "location": location,
        "min_salary": min_salary,
        "cv_path": cv_path,
        "applicant_name": applicant_name,
        "applicant_email": applicant_email,
        "applicant_phone": applicant_phone,
        "requires_sponsorship": requires_sponsorship,
        "work_authorisation": work_authorisation,
        "visa_notes": visa_notes,
        # Title filters — edit ~/.linkedin_autoapply/config.json to customise
        "title_must_contain": TITLE_MUST_CONTAIN,
        "title_exclude": TITLE_EXCLUDE,
    }

    # Summary
    print("\n" + "=" * 60)
    print("  Configuration Summary")
    print("=" * 60)
    print(f"  Claude API key:  sk-ant-...{api_key[-6:]}")
    print(f"  LinkedIn email:  {linkedin_email}")
    print(f"  Job titles:      {', '.join(job_titles)}")
    print(f"  Location:        {location}")
    print(f"  Min salary:      £{min_salary:,}")
    print(f"  CV:              {cv_path}")
    print(f"  Name:            {applicant_name}")
    print(f"  Contact email:   {applicant_email}")
    print(f"  Phone:           {applicant_phone or '(not set)'}")
    _VISA_DISPLAY = {
        "citizen": "Not required (Citizen / permanent resident)",
        "pre_settled": "Not required (Pre-settled / settled status)",
        "valid_visa": "Not required (Valid work visa)",
        "skilled_worker_uk": "Required (Skilled Worker visa, UK)",
        "other": ("Required" if requires_sponsorship else "Not required") + " (Other)",
    }
    print(f"  Sponsorship:     {_VISA_DISPLAY.get(work_authorisation, work_authorisation)}")
    if visa_notes:
        print(f"  Visa notes:      {visa_notes}")
    print(f"  Password:        stored in system keyring")
    print("=" * 60)

    answer = input("\nSave this configuration? [y/n]: ").strip().lower()
    if answer != "y":
        print("Aborted.")
        sys.exit(0)

    save_config(config)
    print(f"\nConfiguration saved to {CONFIG_PATH}")

    _check_playwright_browsers()

    return config


def check_and_run_wizard() -> dict:
    """Check if config exists; run wizard if not. Return loaded config.

    Returns:
        The loaded or freshly created config dict.
    """
    from .config import config_exists, load_config

    if not config_exists():
        print("Welcome to linkedin-autoapply! Let's set things up first.\n")
        config = run_wizard()
    else:
        config = load_config()
    return config
