# linkedin-autoapply

Automated LinkedIn Easy Apply CLI powered by Claude AI. Scrapes LinkedIn job listings, scores them against your CV using Claude Haiku, and submits Easy Apply applications on your behalf — all from a single command.

---

> **Disclaimer:** This tool automates interaction with LinkedIn, which may violate [LinkedIn's User Agreement](https://www.linkedin.com/legal/user-agreement). Use at your own risk. The author accepts no liability for account restrictions, bans, or any other consequences arising from use of this tool. No warranty is provided. You are solely responsible for ensuring your usage complies with applicable terms of service and laws.

---

## Prerequisites

- Python 3.11+
- An active LinkedIn account with Easy Apply enabled
- A [Claude API key](https://console.anthropic.com/) (Anthropic account required — Haiku tier is sufficient and inexpensive)
- Your CV as a PDF file

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/linkedin-autoapply
cd linkedin-autoapply
pip install -e .
linkedin-autoapply setup    # interactive wizard — also installs Playwright browsers
```

The setup wizard will prompt you for:
- Your Claude API key
- Your LinkedIn email address
- Your LinkedIn password (stored securely in your system keychain, not in any file)
- Target job titles and location
- Minimum salary expectation
- Path to your CV PDF
- Your name, email, and phone number (used to fill application forms)

## First-Run Walkthrough

```
$ linkedin-autoapply setup

linkedin-autoapply setup wizard
================================
Claude API key: sk-ant-...
LinkedIn email: you@email.com
LinkedIn password: (hidden)

Target job titles (comma-separated): Data Analyst, Analytics Engineer, ML Engineer
Location: London, United Kingdom
Minimum salary (£): 80000
CV path (PDF): /Users/you/Documents/CV.pdf

Applicant name: Jane Smith
Applicant email: jane@email.com
Applicant phone: +447700000000

Installing Playwright browsers... done.
Config saved to /Users/you/.linkedin_autoapply/config.json

Setup complete. Run 'linkedin-autoapply' to start.
```

## Command Reference

| Command | Description |
|---------|-------------|
| `linkedin-autoapply` | Run full pipeline (scrape → score → apply) |
| `linkedin-autoapply setup` | Re-run the setup wizard to update config |
| `linkedin-autoapply scrape` | Scrape LinkedIn jobs only, no applying |
| `linkedin-autoapply status` | Show database stats |
| `linkedin-autoapply export` | Export results to CSV |
| `--skip-cv-review` | Skip the Claude CV review step |
| `--dry-run` | Score jobs but do not submit applications |
| `--visible` | Run browser in visible mode (required for first login / 2FA) |

## First Login and 2FA

On your first run, LinkedIn may trigger a two-factor authentication challenge or CAPTCHA that cannot be solved in a headless browser. To handle this, run the scrape command in visible mode once to complete the login manually:

```bash
linkedin-autoapply scrape --visible
```

A browser window will open. Complete any verification challenge LinkedIn presents. Once you reach your LinkedIn feed, the session cookie is saved and subsequent runs can proceed headlessly.

## Getting a Claude API Key

1. Go to [console.anthropic.com](https://console.anthropic.com/)
2. Create an account or sign in
3. Navigate to **API Keys** and create a new key
4. The tool uses `claude-haiku-4-5` for job scoring and form-fill (low cost) and `claude-sonnet-4-6` for the CV review step

## Repo Structure Note

This repository contains additional subdirectories (`scraper/`, `streamlit_app/`) that are unrelated personal projects. Only the `auto_apply/` package and the root `pyproject.toml` are part of `linkedin-autoapply`. These subdirectories will be removed in a future cleanup commit.

## Configuration Storage

All config is stored at `~/.linkedin_autoapply/` (your home directory):

```
~/.linkedin_autoapply/
├── config.json          # Your settings (chmod 600 — readable only by you)
├── jobs.db              # SQLite database of scraped/scored/applied jobs
├── cv_text.txt          # Cached CV text extraction
├── linkedin_cookies.json  # Saved LinkedIn session cookie
└── application_report.csv  # Exported results
```

Your LinkedIn password is **not** stored in `config.json`. It is stored in your system keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) via the `keyring` library.
