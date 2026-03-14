# linkedin-autoapply

*Automated LinkedIn Easy Apply — scrape jobs, score them against your CV with Claude, and submit applications without touching a browser.*

A CLI tool that scrapes LinkedIn Easy Apply listings, scores each job against your CV using Claude Haiku (fast, cheap), reviews your CV with Claude Sonnet before applying, and submits applications via Playwright. All config lives at `~/.linkedin_autoapply/config.json` and is collected once via an interactive setup wizard.

> **⚠ Disclaimer:** This tool automates interaction with LinkedIn, which may violate the [LinkedIn User Agreement](https://www.linkedin.com/legal/user-agreement). Use at your own risk. The author accepts no liability for account restrictions, bans, or any other consequences arising from use of this tool. No warranty is provided. You are solely responsible for ensuring your usage complies with applicable terms of service and laws.

---

## Prerequisites

- Python 3.11+
- A [Claude API key](https://console.anthropic.com/)
- An active LinkedIn account with Easy Apply enabled
- Your CV as a PDF file
- Playwright Chromium — installed automatically by `linkedin-autoapply setup`

## Installation

```bash
git clone https://github.com/pravindurgani/linkedin-autoapply
cd linkedin-autoapply
pip install -e .
linkedin-autoapply setup
```

The wizard validates your Claude API key, stores your LinkedIn password in the system keychain (not on disk), and installs Playwright Chromium if needed.

> **Headless Linux note:** On headless Linux without a secret service daemon (e.g. a VPS), the password cannot be saved to keyring and will be prompted on each run.

## First-Run Walkthrough

```
============================================================
  linkedin-autoapply — First-Run Setup
============================================================

Step 1/7: Claude API key
  Get yours at: https://console.anthropic.com
  API key (sk-ant-...):
  Validating... OK

Step 2/7: LinkedIn account email
  LinkedIn email: jane@example.com

Step 3/7: LinkedIn password
  LinkedIn password:

Step 4/7: Job titles to search for
  Enter one per line. Empty line when done.
  Title 1 (or Enter to finish): Data Analyst
  Title 2 (or Enter to finish): Analytics Engineer
  Title 3 (or Enter to finish):

Step 5/7: Location
  Location (e.g. London, UK) [London]: London, UK

Step 6/7: Minimum annual salary (GBP)
  Min salary (e.g. 90000): 85000

Step 7/7: CV and contact details
  Path to your CV PDF: /Users/jane/Documents/CV.pdf
  Full name: Jane Smith
  Contact email: jane@example.com
  Phone number (e.g. +447000000000): +44700000000

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
  Password:        stored in system keyring
============================================================

Save this configuration? [y/n]: y

Configuration saved to /Users/jane/.linkedin_autoapply/config.json

Playwright Chromium browser not found.
Install it now? (recommended) [y/n]: y
```

## Command Reference

| Command | Description |
|---------|-------------|
| `linkedin-autoapply run` | Run full pipeline (scrape → score → apply → export) |
| `linkedin-autoapply run --skip-cv-review` | Skip the Claude CV review before applying |
| `linkedin-autoapply run --dry-run` | Scrape and score, no applications submitted |
| `linkedin-autoapply run --visible` | Full pipeline with visible browser (for 2FA/CAPTCHA) |
| `linkedin-autoapply setup` | Re-run the setup wizard to update config |
| `linkedin-autoapply scrape` | Scrape LinkedIn jobs only, no scoring or applying |
| `linkedin-autoapply scrape --visible` | Scrape with visible browser (use for first login) |
| `linkedin-autoapply status` | Print database stats |
| `linkedin-autoapply export` | Export results to `~/.linkedin_autoapply/application_report.csv` |
| `linkedin-autoapply --verbose <command>` | Enable debug logging on any command |

## First Login and 2FA

LinkedIn may trigger two-factor authentication or a CAPTCHA on the first automated login. A headless browser cannot complete these challenges, so the session will fail silently.

Run the scrape command in visible mode once to solve the challenge manually:

```bash
linkedin-autoapply scrape --visible
```

A browser window opens. Complete any verification LinkedIn presents. Once you reach your feed, the session cookie is saved to `~/.linkedin_autoapply/linkedin_cookies.json` and all subsequent runs proceed headlessly. If LinkedIn re-challenges after a long gap, re-run `--visible`.

## Getting a Claude API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/) and create an account
2. Navigate to **API Keys** and generate a new key

The tool uses **Claude Haiku** for job scoring and form-fill (fast and cheap) and **Claude Sonnet** for the optional CV review step. The CV review can be skipped with `--skip-cv-review` if you want to apply without it.

Salary filtering uses GBP (£). Adjust `min_salary` in the wizard to match your target market.

## Configuration Storage

All config is stored at `~/.linkedin_autoapply/` (your home directory):

```
~/.linkedin_autoapply/
├── config.json              # Your settings (chmod 600 — readable only by you)
├── jobs.db                  # SQLite database of scraped/scored/applied jobs
├── cv_text.txt              # Cached CV text extraction
├── linkedin_cookies.json    # Saved LinkedIn session cookie
└── application_report.csv   # Exported results
```

Your LinkedIn password is **not** stored in `config.json`. It is stored in your system keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) via the `keyring` library.
