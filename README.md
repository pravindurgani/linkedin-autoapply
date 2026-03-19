# linkedin-autoapply

*Automated LinkedIn Easy Apply — scrape jobs, score them against your CV with Claude, and submit applications without touching a browser.*

A CLI tool that scrapes LinkedIn Easy Apply listings, scores each job against your CV using Claude AI, and submits applications via Playwright. All config lives at `~/.linkedin_autoapply/config.json` and is collected once via an interactive setup wizard.

> **Disclaimer:** This tool automates interaction with LinkedIn, which may violate the [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement). Use at your own risk. The author accepts no liability for account restrictions, bans, or any other consequences. You are solely responsible for compliance with applicable terms of service and laws.

---

## Prerequisites

- Python 3.11+
- A [Claude API key](https://console.anthropic.com/) (see [Getting a Claude API Key](#getting-a-claude-api-key))
- A LinkedIn account (Easy Apply is a feature on individual job postings, not an account setting — no setup needed)
- Your CV as a PDF file
- Playwright Chromium — installed automatically during setup

> **Salary filtering is GBP-only.** The `min_salary` value is compared in British pounds. Non-GBP salaries (USD, EUR, etc.) are stored as text but excluded from numeric filtering. Hourly rates are annualised at ×1,880 and daily rates at ×220. If you target non-UK roles, set `min_salary` to `0` to disable salary filtering.

## Installation

```bash
git clone https://github.com/pravindurgani/linkedin-autoapply
cd linkedin-autoapply
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -e .
linkedin-autoapply setup    # required — creates config, installs Playwright browser
```

> **Why a virtual environment?** Python 3.12+ on macOS and many Linux distros enforces [PEP 668](https://peps.python.org/pep-0668/) and will refuse `pip install` outside a venv with `error: externally-managed-environment`. Always use a venv.

The setup wizard validates your Claude API key, stores your LinkedIn password in the system keychain (not on disk), and installs Playwright Chromium if needed.

> **Headless Linux note:** On headless Linux without a secret service daemon (e.g. a VPS), the password cannot be saved to keyring and will be prompted on each run.

## First-Run Walkthrough

```
============================================================
  linkedin-autoapply — First-Run Setup
============================================================

Step 1/8: Claude API key
  Get yours at: https://console.anthropic.com
  API key (sk-ant-...):
  Validating... OK

Step 2/8: LinkedIn account email
  LinkedIn email: jane@example.com

Step 3/8: LinkedIn password
  LinkedIn password:

Step 4/8: Job titles to search for
  Enter one per line. Empty line when done.
  Title 1 (or Enter to finish): Data Analyst
  Title 2 (or Enter to finish): Analytics Engineer
  Title 3 (or Enter to finish):

Step 5/8: Location
  Location (e.g. London, UK) [London]: London, UK

Step 6/8: Minimum annual salary (GBP)
  Min salary (e.g. 90000): 85000

Step 7/8: CV and contact details
  Path to your CV PDF: /Users/jane/Documents/CV.pdf
  Full name: Jane Smith
  Contact email: jane@example.com
  Phone number (e.g. +447000000000): +44700000000

Step 8/8: Visa & Work Authorisation
  Do you have the right to work in your target country? [y/n]: y
  [1] Citizen / permanent resident
  [2] Pre-settled / settled status
  [3] Valid work visa (no sponsorship needed)
  [4] Other
  Select [1-4]: 1
```

If you answer **n** (require sponsorship), you'll see:

```
  [1] Skilled Worker visa (UK)
  [2] Other visa type
  Select [1-2]: 1
```

This affects job scoring (non-sponsoring companies are penalised) and form-filling (visa/sponsorship screening questions are answered automatically).

```
============================================================
  Configuration Summary
============================================================
  Claude API key:  sk-ant-...xxxxxx
  LinkedIn email:  jane@example.com
  Job titles:      Data Analyst, Analytics Engineer
  Location:        London, UK
  Min salary:      £85,000
  CV:              /Users/jane/Documents/CV.pdf
  Name:            Jane Smith
  Contact email:   jane@example.com
  Phone:           +44700000000
  Sponsorship:     Not required (Citizen / permanent resident)
  Password:        stored in system keyring
============================================================

Save this configuration? [y/n]: y

Configuration saved to /Users/jane/.linkedin_autoapply/config.json

Playwright Chromium browser not found.
Install it now? (recommended) [y/n]: y
```

## First Login and 2FA

LinkedIn may trigger two-factor authentication or a CAPTCHA on the first automated login. A headless browser cannot complete these challenges, so the session will fail silently.

Run the scrape command in visible mode once to solve the challenge manually:

```bash
linkedin-autoapply scrape --visible
```

A browser window opens. Complete any verification LinkedIn presents. Once you reach your feed, the session cookie is saved to `~/.linkedin_autoapply/linkedin_cookies.json` and all subsequent runs proceed headlessly. If LinkedIn re-challenges after a gap (days/weeks of inactivity), re-run `--visible`.

## Command Reference

| Command | Description |
|---------|-------------|
| `linkedin-autoapply run` | Full pipeline: scrape → score → CV review → apply → export |
| `linkedin-autoapply run --skip-cv-review` | Skip the Claude CV review before applying |
| `linkedin-autoapply run --dry-run` | Scrape and score only, no applications submitted |
| `linkedin-autoapply run --visible` | Full pipeline with visible browser (for 2FA/CAPTCHA) |
| `linkedin-autoapply run --max-applies 5` | Limit applications per run (default: 15) |
| `linkedin-autoapply run --retry-failed` | Clear failed application records and re-queue them |
| `linkedin-autoapply setup` | Run the setup wizard (required on first install, re-run to update config) |
| `linkedin-autoapply scrape` | Scrape LinkedIn jobs only — no scoring or applying |
| `linkedin-autoapply scrape --visible` | Scrape with visible browser (use for first login) |
| `linkedin-autoapply status` | Print database stats (jobs scraped, scored, applied, above threshold) |
| `linkedin-autoapply export` | Export results to `~/.linkedin_autoapply/application_report.csv` |
| `linkedin-autoapply --verbose run` | Enable debug logging (note: `--verbose` goes *before* the command) |

> **`scrape` vs `run`:** `run` always scrapes first, then scores, then applies. Running `scrape` separately is useful if you want to inspect jobs in the database (`status`) before applying. To score and apply without re-scraping, there is no separate command — `run` is the full pipeline.

## How the Pipeline Works

### CV review

Before applying, Claude Sonnet reviews your CV against your target job titles and provides a structured assessment: strengths, weaknesses, ATS keyword gaps, and a readiness score out of 10. You are then asked:

```
Are you happy to proceed with applications? [y/n]:
```

If you answer **n**, the pipeline stops — no applications are submitted. If the API call fails (quota exhausted, auth error), you're prompted to continue without the review. Skip entirely with `--skip-cv-review`.

### Scoring

Each scraped job is scored 0–100 by Claude Haiku based on title match, company, description content, salary, and visa/sponsorship fit. Only jobs scoring **70 or above** (the default threshold) enter the apply queue. The threshold is set in `auto_apply/config.py` (`SCORE_THRESHOLD = 70`).

### Rate limiting

Each run applies to at most `--max-applies` jobs (default: 15). A random delay of 45–90 seconds is inserted between applications. The tool tracks daily application counts and warns when approaching LinkedIn's suspected restriction thresholds. A hard daily cap of 15 is the default (configurable via `max_daily_applications` in `config.json`).

## Getting a Claude API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/) and create an account
2. Navigate to **API Keys** and generate a new key

The tool uses **Claude Haiku** for job scoring and form-fill (fast and cheap) and **Claude Sonnet** for the optional CV review step.

## Configuration Storage

All config is stored at `~/.linkedin_autoapply/` (your home directory):

```
~/.linkedin_autoapply/
├── config.json              # Your settings (chmod 600 — readable only by you)
├── jobs.db                  # SQLite database of scraped/scored/applied jobs
├── cv_text.txt              # Cached CV text extraction
├── linkedin_cookies.json    # Saved LinkedIn session — treat as a password
└── application_report.csv   # Exported results
```

> **Security note:** `linkedin_cookies.json` contains a fully-authenticated LinkedIn session. Treat it like a password — do not share, commit, or include in backups without encryption.

Your LinkedIn password is **not** stored in `config.json`. It is stored in your system keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) via the `keyring` library.

### Visa & sponsorship

The wizard sets three keys in `config.json` based on your Step 8 answers:

```json
"requires_sponsorship": false,
"work_authorisation": "citizen",
"visa_notes": ""
```

These feed into job scoring (sponsorship-requiring candidates are penalised for non-sponsoring companies) and form-filling (visa/sponsorship screening questions are answered automatically). If these keys are absent (legacy config), the tool prompts at runtime instead.

### Customising the title filter

`config.json` contains two lists you can edit directly to change which job titles are considered:

```json
"title_must_contain": ["data", "analytics", "machine learning", ...],
"title_exclude":      ["intern", "director", "contract", ...]
```

Set `title_must_contain` to `[]` to disable the keyword requirement and include all scraped titles. The defaults are tuned for data/analytics roles — adjust for your field.

## Troubleshooting

**`error: externally-managed-environment` during `pip install`**
You're installing outside a virtual environment. Python 3.12+ enforces this. Create and activate a venv first (see [Installation](#installation)).

**LinkedIn CAPTCHA or 2FA on subsequent runs**
Session cookies expire after prolonged inactivity. Re-run with `--visible` to solve the challenge manually:
```bash
linkedin-autoapply scrape --visible
```

**Claude API quota exhausted mid-run**
The pipeline logs the error and stops applying. Top up your [API credit balance](https://console.anthropic.com/), then re-run. Already-scored jobs are cached in `jobs.db` and won't re-consume API calls.

**Failed applications**
Use `--retry-failed` to clear failed records and re-queue them:
```bash
linkedin-autoapply run --retry-failed --skip-cv-review
```

**`jobs.db` growing large after many runs**
The database accumulates jobs across runs. To start fresh, delete it:
```bash
rm ~/.linkedin_autoapply/jobs.db
```
The next run creates a new database automatically.

**CLI entry point not found**
If `linkedin-autoapply` isn't on your PATH (e.g. venv not activated), use:
```bash
python -m auto_apply run
```

**Verifying installation**
Run the test suite to confirm everything is working:
```bash
pip install -e ".[dev]"
pytest
```
