<!-- logo here -->

> **⚠️ ApplyPilot** is the original open-source project, created by [Pickle-Pixel](https://github.com/Pickle-Pixel) and first published on GitHub on **February 17, 2026**. We are **not affiliated** with applypilot.app, useapplypilot.com, or any other product using the "ApplyPilot" name. These sites are **not associated with this project** and may misrepresent what they offer. If you're looking for the autonomous, open-source job application agent — you're in the right place.

# ApplyPilot

**Applied to 1,000 jobs in 2 days. Fully autonomous. Open source.**

[![PyPI version](https://img.shields.io/pypi/v/applypilot?color=blue)](https://pypi.org/project/applypilot/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-green.svg)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/Pickle-Pixel/ApplyPilot?style=social)](https://github.com/Pickle-Pixel/ApplyPilot)
[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/S6S01UL5IO)




https://github.com/user-attachments/assets/7ee3417f-43d4-4245-9952-35df1e77f2df


---

## What It Does

ApplyPilot is a 6-stage autonomous job application pipeline. It discovers jobs across 5+ boards, scores them against your resume with AI, tailors your resume per job, writes cover letters, and **submits applications for you**. It navigates forms, uploads documents, answers screening questions, all hands-free.

Three commands. That's it.

```bash
pip install applypilot
pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex
applypilot init          # one-time setup: resume, profile, preferences, API keys
applypilot doctor        # verify your setup — shows what's installed and what's missing
applypilot run           # discover > enrich > score > tailor > cover letters
applypilot run -w 4      # same but parallel (4 threads for discovery/enrichment)
applypilot run score --score-workers 3  # parallel scoring with global rate pacing
applypilot apply         # autonomous browser-driven submission
applypilot apply -w 3    # parallel apply (3 Chrome instances)
applypilot apply --dry-run  # fill forms without submitting
```

> **Why two install commands?** `python-jobspy` pins an exact numpy version in its metadata that conflicts with pip's resolver, but works fine at runtime with any modern numpy. The `--no-deps` flag bypasses the resolver; the second command installs jobspy's actual runtime dependencies. Everything except `python-jobspy` installs normally.

---

## Two Paths

### Full Pipeline (recommended)
**Requires:** Python 3.11+, Node.js (for npx), Gemini API key (free), Claude Code CLI, Chrome

Runs all 6 stages, from job discovery to autonomous application submission. This is the full power of ApplyPilot.

### Discovery + Tailoring Only
**Requires:** Python 3.11+, Gemini API key (free)

Runs stages 1-5: discovers jobs, scores them, tailors your resume, generates cover letters. You submit applications manually with the AI-prepared materials.

---

## The Pipeline

| Stage | What Happens |
|-------|-------------|
| **1. Discover** | Searches major boards, public ATS feeds, direct career sites, and an optional attributed Startup Jobs feed |
| **2. Enrich** | Fetches full job descriptions via JSON-LD, CSS selectors, or AI-powered extraction |
| **3. Score** | AI rates every job 1-10 based on your resume and preferences. Only high-fit jobs proceed |
| **4. Tailor** | AI selects and reorganizes your existing resume entities and bullets per job. Bullet wording stays unchanged |
| **5. Cover Letter** | AI generates a targeted cover letter per job |
| **6. Auto-Apply** | Claude Code navigates application forms, fills fields, uploads documents, answers questions, and submits |

After a confirmed application, optional **Apollo outreach** finds up to five relevant employees, enriches only verified work emails, and prepares personalized messages for review. You can copy reviewed messages to your personal Gmail Drafts, then review and schedule each one in Gmail. ApplyPilot does not send Gmail drafts.

Each stage is independent. Run them all or pick what you need.

---

## ApplyPilot vs The Alternatives

| Feature | ApplyPilot | AIHawk | Manual |
|---------|-----------|--------|--------|
| Job discovery | 5 boards + Workday + direct sites | LinkedIn only | One board at a time |
| AI scoring | 1-10 fit score per job | Basic filtering | Your gut feeling |
| Resume tailoring | Per-job source selection | Template-based | Hours per application |
| Auto-apply | Full form navigation + submission | LinkedIn Easy Apply only | Click, type, repeat |
| Supported sites | Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs, 46 Workday portals, 28 direct sites | LinkedIn | Whatever you open |
| License | AGPL-3.0 | MIT | N/A |

---

## Requirements

| Component | Required For | Details |
|-----------|-------------|---------|
| Python 3.11+ | Everything | Core runtime |
| Node.js 18+ | Auto-apply | Needed for `npx` to run Playwright MCP server |
| Gemini API key | Scoring, tailoring, cover letters | Free tier (15 RPM / 1M tokens/day) is enough |
| Tectonic | LaTeX resume tailoring | Compiles the supported resume template to PDF |
| Poppler (`pdftotext`) | LaTeX resume tailoring | Performs the mandatory rendered-line audit |
| Chrome/Chromium | Auto-apply | Auto-detected on most systems |
| Claude Code CLI | Auto-apply | Install from [claude.ai/code](https://claude.ai/code) |

**Gemini API key is free.** Get one at [aistudio.google.com](https://aistudio.google.com). OpenAI and local models (Ollama/llama.cpp) are also supported.

### Optional

| Component | What It Does |
|-----------|-------------|
| CapSolver API key | Solves CAPTCHAs during auto-apply (hCaptcha, reCAPTCHA, Turnstile, FunCaptcha). Without it, CAPTCHA-blocked applications just fail gracefully |

> **Note:** python-jobspy is installed separately with `--no-deps` because it pins an exact numpy version in its metadata that conflicts with pip's resolver. It works fine with modern numpy at runtime.

---

## Configuration

All generated by `applypilot init`:

### `profile.json`
Your personal data in one structured file: contact info, work authorization, compensation, experience, skills, resume facts (preserved during tailoring), and EEO defaults. Powers scoring, tailoring, and form auto-fill.

### `searches.yaml`
Job search queries, target titles, locations, boards. Run multiple searches with different parameters.

### `.env`
API keys and runtime config: `GEMINI_API_KEY`, `LLM_MODEL`, `LLM_RPM`, `LLM_TPM`, and `CAPSOLVER_API_KEY`
(optional). Hosted LLMs default to 15 RPM; set either rate limit to `0` to disable it.

### Package configs (shipped with ApplyPilot)
- `config/employers.yaml` - Workday employer registry (48 preconfigured)
- `config/ashby_companies.yaml` - Ashby public job-board registry
- `config/lever_companies.yaml` - Lever public job-board registry (global and EU)
- `config/sites.yaml` - Direct career sites (30+), blocked sites, base URLs, manual ATS domains
- `config/searches.example.yaml` - Example search configuration

Set `STARTUP_JOBS_API_KEY` in `~/.applypilot/.env` to add recent listings from
[Startup Jobs](https://startup.jobs/api). The free API key is optional; when it
is absent this source is skipped without failing discovery. ApplyPilot retains
the Startup Jobs listing URL and labels the source in the dashboard to satisfy
the feed's attribution requirements. Wellfound is intentionally not scraped;
its listings can still be added individually through the dashboard.

---

## How Stages Work

### Discover
Queries Indeed, LinkedIn, Glassdoor, ZipRecruiter, Google Jobs via JobSpy. Scrapes configured Workday, Greenhouse, Ashby, and Lever employer portals, plus proprietary big-tech career sites. Deduplicates by URL.

### Enrich
Visits each job URL and extracts the full description. 3-tier cascade: JSON-LD structured data, then CSS selector patterns, then AI-powered extraction for unknown layouts.

### Score
AI scores every job 1-10 against your profile. Scoring uses three concurrent requests by default while a shared limiter
paces calls across all workers. Use `--score-workers` to change concurrency and `LLM_RPM`/`LLM_TPM` to match your
provider quota. 9-10 = strong match, 7-8 = good, and 6 = moderate. Valid scores from 1-5 are automatically removed;
score 0 is treated as an error and preserved for retry. Only jobs above your threshold proceed to tailoring.

### Tailor
Generates a custom resume per job: reorders experience, emphasizes relevant skills, incorporates keywords from the job description. Your `resume_facts` (companies, projects, metrics) are preserved exactly. The AI reorganizes but never fabricates.

### Cover Letter
Writes a targeted cover letter per job referencing the specific company, role, and how your experience maps to their requirements.

### Auto-Apply
Claude Code launches a Chrome instance, navigates to each application page, detects the form type, fills personal information and work history, uploads the tailored resume and cover letter, answers screening questions with AI, and submits. A live dashboard shows progress in real-time.

The Playwright MCP server is configured automatically at runtime per worker. No manual MCP setup needed.

```bash
# Utility modes (no Chrome/Claude needed)
applypilot apply --mark-applied URL    # manually mark a job as applied
applypilot apply --unmark-applied URL  # return an applied job to the active queue
applypilot apply --mark-failed URL     # manually mark a job as failed
applypilot apply --reset-failed        # reset all failed jobs for retry
applypilot apply --gen --url URL       # generate prompt file for manual debugging
```

### Employee Outreach (optional)

ApplyPilot can use Apollo's REST API to contact a balanced hiring circle: a likely manager, functional leader, recruiter, and relevant team members. People are ranked against the role and job location, with company-wide candidates retained as fallbacks for remote roles or sparse local results. At most ten profiles are enriched to find up to five verified work emails, and official company pages plus the job description ground the generated message.

1. Add `APOLLO_API_KEY` and `OUTREACH_ENABLED=true` to `~/.applypilot/.env`. Apollo finds the people; it does not need access to your Gmail account for draft creation.
2. Install Gmail support: `pip install 'applypilot[gmail]'` (or `pip install -e '.[gmail]'` from this repository).
3. In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable the Gmail API, configure the OAuth consent screen (External for a personal Gmail account; add your Gmail address as a test user if the app is in testing), create an **OAuth client ID → Desktop app**, and download its JSON file. An API key is not sufficient. The requested OAuth scope is `gmail.compose`, which permits composing and sending; ApplyPilot's Gmail integration only creates drafts.
4. Run `applypilot gmail-connect --credentials /path/to/oauth-client.json` and select your **personal** Google account in the browser. The dashboard will show the connected address before you confirm draft creation. OAuth tokens are stored with owner-only file permissions in `~/.applypilot/`; do not commit or share them.
5. Add 3–10 representative writing samples and your signature under **Profile → Employee Outreach** in the dashboard. Apply normally, edit and select recipients in the applied job's **Outreach** tab, then choose **Create selected Gmail drafts**. Open Gmail Drafts, review each message, and use Gmail's **Schedule send** to set the times. Gmail sends scheduled messages while ApplyPilot is closed.

If your Google OAuth consent screen remains **External / Testing**, Google expires its refresh token after seven days for Gmail scopes. Run `applypilot gmail-connect` again when the connection expires. Existing Gmail drafts and Gmail-scheduled messages are unaffected.

ApplyPilot records only that a Gmail draft was created. Scheduling, sending, deleting, and reply handling happen in Gmail; ApplyPilot does not automatically track those later changes or delete a Gmail draft when you change an application's status. Review the recipient, body, and signature in Gmail before scheduling. If draft creation is interrupted, check Gmail Drafts before using the dashboard's explicit retry recovery control, so you do not create a duplicate.

Apollo search does not reveal email addresses. ApplyPilot enriches candidates in rank order, which consumes Apollo credits, and stops after ten attempts. Personal emails and phone numbers are never requested. Gmail drafts are not sent or scheduled by ApplyPilot; the final browser confirmation creates drafts only. Existing Apollo-scheduled batches remain active until you cancel their remaining sends in the Outreach tab. The legacy Apollo dispatcher and its Profile schedule settings are retained only for previously approved batches/API compatibility; it requires `APOLLO_EMAIL_ACCOUNT_ID` and a running dashboard.

Recovery commands do not bypass review:

```bash
applypilot outreach --url URL             # show batch status
applypilot outreach --url URL --prepare   # prepare/re-prepare an unsent batch
applypilot outreach --url URL --retry     # retry failed preparation or sends
```

#### How `--limit` counts jobs

- `--limit` counts **every job the worker finishes processing**, including jobs the agent rejects as out-of-scope (e.g. freelance-only listings, ineligible locations, expired postings).
- If the first acquired job is out-of-scope and `--limit` is 1 (the default), the worker exits after that single rejection without attempting another job.
- Pass a higher `--limit` (e.g. `-l 10`) or use `--continuous` to keep going past rejections. Permanent failures are already marked in the database, so rejected jobs won't be re-picked on later runs.

---

## CLI Reference

```
applypilot init                         # First-time setup wizard
applypilot doctor                       # Verify setup, diagnose missing requirements
applypilot run [stages...]              # Run pipeline stages (or 'all')
applypilot run --workers 4              # Override the default 3 discovery/enrichment workers
applypilot run --score-workers 3        # Parallel scoring (paced by LLM_RPM/LLM_TPM)
applypilot run                          # Concurrent stages (streaming mode, default)
applypilot run --no-stream              # Run stages sequentially
applypilot run --min-score 8            # Override score threshold
applypilot run --dry-run                # Preview without executing
applypilot run --validation lenient     # Relax validation (recommended for Gemini free tier)
applypilot run --validation strict      # Strictest validation (retries on any banned word)
applypilot apply                        # Launch auto-apply
applypilot apply --workers 3            # Parallel browser workers
applypilot apply --dry-run              # Fill forms without submitting
applypilot apply --continuous           # Run forever, polling for new jobs
applypilot apply --headless             # Headless browser mode
applypilot apply --url URL              # Apply to a specific job
applypilot status                       # Pipeline statistics
applypilot dashboard                    # Open the live React dashboard
applypilot outreach --url URL           # Inspect/recover post-application outreach
```

The dashboard is bundled with ApplyPilot and runs entirely from the local Python server;
Node.js is not required for normal use. Job and task updates appear without page reloads,
and workspace filters and the selected job are preserved in the browser URL. Dashboard
contributors can find the Vite development and release workflow in
[CONTRIBUTING.md](CONTRIBUTING.md#dashboard-development).

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding standards, and PR guidelines.

---

## License

ApplyPilot is licensed under the [GNU Affero General Public License v3.0](LICENSE).

You are free to use, modify, and distribute this software. If you deploy a modified version as a service, you must release your source code under the same license.
