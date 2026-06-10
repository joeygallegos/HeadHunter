# HeadHunter
This suite of tools will help you apply and select jobs quicker. If you are specifically interested in being the first person to apply for jobs posted by particular companies, this might be the best way.

## Quick Start

```bash
# 1) Install
pip install -r requirements.txt
# .env should contain DATABASE_URL, optional logging & mode vars

# 2) Verify NLTK data
python run.py download

# 3) Run with your steps
python run.py steps

# or run test plan
python run.py test
```

## Ubuntu / OrangePi 5 Deployment

The scraper uses Selenium with Chrome-compatible browsers. On Ubuntu servers, Chromium is supported; set the scraper to headless mode and point Selenium at Chromium/ChromeDriver if auto-discovery cannot find them.

Install Python venv support, Chromium, and ChromeDriver:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip chromium-browser chromium-chromedriver
```

Some Ubuntu images package Chromium as `chromium` instead:

```bash
sudo apt install -y chromium chromium-driver
```

Create and activate the virtual environment from the repo root:

```bash
cd /path/to/JobScrape
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Recommended `.env` values for a headless OrangePi server:

```env
HEADLESS=true
CHROMIUM_BINARY_PATH=/usr/bin/chromium-browser
CHROMEDRIVER_PATH=/usr/bin/chromedriver
```

If Chromium is installed through Snap, use:

```env
CHROMIUM_BINARY_PATH=/snap/bin/chromium
```

The app also checks `BROWSER_BINARY_PATH`, `CHROME_BINARY_PATH`, `CHROMIUM_BINARY_PATH`, then common `PATH` names like `chromium`, `chromium-browser`, and `google-chrome`. If `chromedriver` is on `PATH`, `CHROMEDRIVER_PATH` can be omitted.

To run manually on the server:

```bash
source .venv/bin/activate
python run.py download
python run.py steps
```

The files under `scripts/run-scheduled.ps1`, `scripts/install-scheduled-task.ps1`, and `scheduler/` are Windows Task Scheduler support. For Ubuntu, schedule `/path/to/JobScrape/.venv/bin/python /path/to/JobScrape/run.py steps` with cron or a systemd timer.

### Dashboard systemd service

Install or refresh Python dependencies after activating the venv:

```bash
cd /opt/joey/JobScrape
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Create `/etc/systemd/system/jobs-dashboard.service`:

```ini
[Unit]
Description=JobScrape dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=joey
Group=joey
WorkingDirectory=/opt/joey/JobScrape
EnvironmentFile=/opt/joey/JobScrape/.env
ExecStart=/opt/joey/JobScrape/.venv/bin/gunicorn --workers 2 --bind 0.0.0.0:5000 dashboard:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now jobs-dashboard.service
sudo systemctl status jobs-dashboard.service
```

Useful service commands:

```bash
sudo journalctl -u jobs-dashboard.service -f
sudo systemctl restart jobs-dashboard.service
```

The dashboard will listen on port `5000`. From another machine on the LAN, open `http://<orange-pi-ip>:5000/`. If you only want it available on the OrangePi itself, change the Gunicorn bind address to `127.0.0.1:5000`.

The same service also hosts the Google Sheets-backed swipe review page at `http://<orange-pi-ip>:5000/swipe`; it uses `SHEET_ID`, `SHEET_NAME`, and `GOOGLE_CREDENTIALS_PATH` from `config.json` and requires `gspread` plus `google-auth` from `requirements.txt`.

### Cron scraper run

Edit the user crontab:

```bash
crontab -e
```

Run the scraper every day at 6:00 AM:

```cron
0 6 * * * cd /opt/joey/JobScrape && /opt/joey/JobScrape/.venv/bin/python /opt/joey/JobScrape/run.py steps >> /opt/joey/JobScrape/logs/cron-run.log 2>&1
```

Check cron output:

```bash
tail -f /opt/joey/JobScrape/logs/cron-run.log
```

## Features
- Delta tracking with `Job` + `JobChange` + `IntegrationRun`
- Three commit modes: `all_at_end`, `per_site`, `per_job`
- Structured logs (run/site/job) to `logs/app.log` and per-run files
- Savepoints to continue on single-row integrity errors
- Export raw site JSON snapshots (`output/<site>.json`)
- Optional DOM pagination support for multi-page job boards

## Commit mode
| Commit Mode      | Value         | Description                                                                 |
|------------------|---------------|-----------------------------------------------------------------------------|
| All at End       | `all_at_end`  | Commits all database changes after processing all jobs and sites.            |
| Per Site         | `per_site`    | Commits database changes after processing each job site.                     |
| Per Job          | `per_job`     | Commits database changes after processing each individual job.               |

Set the desired mode in your `.env` file using:

```env
DB_COMMIT_MODE=per_site   # all_at_end | per_site | per_job
```

## Configuration Reference

### `.env`

| Key | Controls |
|-----|----------|
| `DB_URL` | Full SQLAlchemy database URL; if set, it overrides the split `DB_USER`/`DB_PASS`/`DB_HOST`/`DB_PORT`/`DB_NAME` settings. |
| `DB_USER` | MySQL username used when `DB_URL` is not set. |
| `DB_PASS` | MySQL password used when `DB_URL` is not set. |
| `DB_HOST` | MySQL host used when `DB_URL` is not set. |
| `DB_PORT` | MySQL port used when `DB_URL` is not set, defaulting to `3306`. |
| `DB_NAME` | MySQL database name used when `DB_URL` is not set. |
| `DB_COMMIT_MODE` | Persistence strategy for scraper results: `all_at_end`, `per_site`, or `per_job`. |
| `MAILGUN_API_KEY` | Mailgun API key used by the job digest email sender. |
| `MAILGUN_DOMAIN` | Mailgun sending domain used by the job digest email sender. |
| `MAILGUN_FROM_EMAIL` | Sender email address for job digest emails. |
| `JOB_DIGEST_TO_EMAIL` | Recipient email address for job digest emails. |
| `MAILGUN_BASE_URL` | Mailgun API base URL, defaulting to `https://api.mailgun.net/v3`. |
| `HEADLESS` | Enables invisible browser automation when set to `true`. |
| `BROWSER_BINARY_PATH` | Explicit Chrome-compatible browser executable path, checked before Chrome- or Chromium-specific paths. |
| `CHROME_BINARY_PATH` | Explicit Google Chrome executable path for Selenium. |
| `CHROMIUM_BINARY_PATH` | Explicit Chromium executable path for Selenium, such as `/usr/bin/chromium-browser`. |
| `CHROMEDRIVER_PATH` | Explicit ChromeDriver executable path for Selenium. |
| `DEBUG_STEPS` | Prints verbose scraper step diagnostics when set to `true`. |
| `ITEM_DELAY_MS` | Delay in milliseconds between item-level scraper actions. |
| `LOG_LEVEL` | Application log level for console and file logging, such as `INFO` or `DEBUG`. |
| `LOG_SQL` | SQLAlchemy log level, usually `WARNING` unless debugging database queries. |
| `DASH_HOST` | Host address for `dashboard.py`, defaulting to `127.0.0.1`. |
| `DASH_PORT` | Port for `dashboard.py`, defaulting to `5000`. |
| `RESUME_PATH` | Resume text file used by `analyze_jobs_ollama.py`, defaulting to `resume.txt`. |
| `OLLAMA_MODEL` | Ollama model name used for AI job analysis. |
| `OLLAMA_BASE_URL` | Base URL for the Ollama API, defaulting to `http://localhost:11434`. |
| `OLLAMA_NUM_CTX` | Context window size sent to Ollama. |
| `OLLAMA_NUM_PREDICT` | Maximum generated tokens requested from Ollama. |
| `OLLAMA_KEEP_ALIVE` | How long Ollama should keep the model loaded after requests. |
| `OLLAMA_THINK` | Enables model thinking output handling when set to `true`. |
| `ONLY_EMPTY` | Limits AI analysis to jobs without existing AI analysis when set to `true`. |
| `SITE_FILTER` | Restricts AI analysis to a single site name when set. |
| `LIMIT` | Caps the number of jobs processed by AI analysis when greater than `0`. |
| `AI_TOKEN_THRESHOLD` | Legacy token threshold used by AI analysis config. |
| `AI_ANALYSIS_LOG` | Log file path for AI analysis output, defaulting to `ai_analysis.log`. |
| `AI_FIT_SUMMARY_MAX_CHARS` | Maximum character length for the AI fit summary. |
| `AI_MAX_ATTEMPTS` | Maximum retry attempts for one AI analysis request. |
| `MAX_RESUME_TOKENS` | Approximate token budget for resume text in the AI prompt. |
| `MAX_JOB_DESC_TOKENS` | Approximate token budget for job description text in the AI prompt. |
| `AI_REQUEST_TIMEOUT_SEC` | Timeout in seconds for each Ollama request. |
| `AI_CONCURRENCY` | Number of AI worker threads. |
| `AI_MAX_INFLIGHT` | Maximum number of submitted AI tasks allowed at once. |
| `AI_BATCH_LOG_EVERY` | Number of completed AI jobs between progress log messages. |
| `AI_WAIT_HEARTBEAT_SEC` | Seconds to wait before logging an AI progress heartbeat. |
| `AI_KEYWORD_LIST_LIMIT` | Maximum number of overlap and missing keyword items requested from AI output. |
| `JOBS_SQLITE_PATH` | SQLite database path used by the SQLite AI-column migration script. |

### `config.json`

| Key | Controls |
|-----|----------|
| `SHEET_ID` | Google Sheets spreadsheet ID for integrations that read or write sheet data. |
| `SHEET_NAME` | Google Sheets tab name used by sheet integrations. |
| `GOOGLE_CREDENTIALS_PATH` | Path to the Google service account credentials JSON file. |
| `AI_JOBS_FILE` | JSON job export file used as AI-analysis input by legacy flows. |
| `AI_TOKEN_THRESHOLD` | Token threshold used by legacy AI-analysis config. |
| `DB_URL` | Optional database URL fallback read by `app/config.py` when the environment does not set `DB_URL`. |

## Security scan
The repo includes a local static scanner for common security mistakes:

- SQL injection probes and risky dynamic SQL, including tautology payloads like `1=1` and `OR 1=1`.
- Directory traversal payloads like `../`, encoded traversal variants, and file operations fed by config/args/env/input.
- Data exfiltration risks, including network egress calls, external URL literals, credential file references, and possible secret logging.

Run it from the project root:

```bash
python scripts/security_scan.py
```

By default, the scanner prints human-readable findings and exits non-zero if any `high` severity finding exists. For audit logs or CI artifacts, emit JSON:

```bash
python scripts/security_scan.py --json
```

For exploratory local runs where you want a report without failing the command:

```bash
python scripts/security_scan.py --fail-on none
```

The scanner is intentionally conservative. It does not attack remote systems or send payloads anywhere; it only reads local source/config files and flags patterns that a developer should review. Directories that usually contain generated or sensitive runtime data, such as `logs/`, `output/`, `data/`, `.git/`, and `__pycache__/`, are skipped.

## Pagination
Some job boards move older jobs onto later pages. If the scraper only reads page 1, an existing active job can be incorrectly marked missing even though it still appears on page 2 or later. Pagination support fixes that by scraping every visible result page before the database delta logic decides which jobs are missing.

At a high level, paginated `data_extract` works in two passes:

1. The scraper reads all list pages first. It extracts list-level fields like `JobID`, `JobTitle`, and `JobUrl`, clicks the next-page button, and stores the result page number in each raw job dictionary as `__page`.
2. After all list pages are collected, the scraper visits each collected `JobUrl` to hydrate detail-only fields like `JobDesc` and final `JobUrl`.
3. The normal save/delta code then receives one complete list of jobs for the site. No database schema change is required; `__page` is persisted only in the raw output JSON for debugging.

This two-pass behavior matters for Workday boards because visiting a job detail page during pagination can reset or confuse the browser's current result page. Collecting all list rows first keeps page traversal stable.

### Configuring a Workday board
Add a `pagination` block to the `data_extract` step for boards that expose a DOM next button:

```json
{
  "action": "data_extract",
  "focus_scope": "section[data-automation-id='jobResults']>ul[role='list']>li",
  "pagination": {
    "mode": "click_next",
    "max_pages": 10,
    "current_page_css": "nav[aria-label='pagination'] button[aria-current='page']",
    "next_page_css": "nav[aria-label='pagination'] button[aria-label='next']",
    "next_disabled_css": "button[aria-label='next'][disabled], button[aria-label='next'][aria-disabled='true']",
    "page_wait_ms": 1200,
    "page_as_column": "__page"
  },
  "extract_steps": [
    {
      "action": "extract",
      "as_column": "JobID",
      "xpath": "ul[data-automation-id='subtitle']"
    },
    {
      "action": "extract",
      "as_column": "JobTitle",
      "xpath": "a[data-automation-id='jobTitle']"
    },
    {
      "action": "extract",
      "data_type": "url",
      "as_column": "JobUrl",
      "xpath": "a[data-automation-id='jobTitle']",
      "attr_target": "href"
    },
    {
      "action": "redirect",
      "using_column": "JobUrl"
    },
    {
      "action": "extract",
      "as_column": "JobDesc",
      "xpath": "div[data-automation-id='jobPostingDescription']"
    },
    {
      "action": "next"
    }
  ]
}
```

The scraper treats steps before `redirect` as list-page work and steps after `redirect` as detail-page work. The `next` step still ends extraction for each job item.

### Pagination fields
| Field | Purpose |
|-------|---------|
| `mode` | Currently supports `click_next`, which clicks a DOM next-page control. |
| `max_pages` | Safety cap to prevent infinite loops if a page never reports the end. |
| `current_page_css` | Selector used to read the active page number. Falls back to incrementing locally if unavailable. |
| `next_page_css` | Selector for the next-page button. |
| `next_disabled_css` | Selector that matches the disabled next button state. |
| `page_wait_ms` | Extra wait after clicking next, useful for Workday UI hydration. |
| `page_as_column` | Raw output field for the discovered page number. Use `__page`. |

Pagination stops when the scraper sees an empty page, a duplicate page signature, a disabled/missing next button, a failed next click, or no page/list change after clicking next.

### Current limits
This pagination mode is for DOM-based next buttons. API-backed boards that paginate with query parameters, such as `pageSize` and `offset`, need separate JSON/API pagination support.

# Problems
Sometimes if the job description is too large, we can run out of tokens and the AI will start to hallucinate the JSON response.
Seems that the WORKING token size for the job description is around 700 (max)

## Future Features
- Suggest jobs based on the resume data loaded into the app
- Parse user resumes to extract key skills, experiences, and preferences
- Continuously scrape job listings from multiple job boards and compile them into a unified database instead of just JSON files
- Generate a list of interview questions that might come up for a particular job based on the description
- Integrate with a lot of remote-first companies
- If you determine that the job is not fully remote, set the match_percentage to 0 and leave the feedback arrays empty
- Leave at least two positive and two negative feedback items
- Find exact duplicate sentences that appear in all jobs from that company and remove them from each job
- If job title "Senior Manager" then manager should trump senior in job level determination
### Ollama Implementation
In order to locally run Ollama, use these commands:

- `ollama serve`
- `ollama list`
- `ollama rm`
- `ollama pull deepseek-r1:70b`

You can update the system environment variable OLLAMA_MODELS to be your new save path instead of the default, which is on the C drive.

## Upgrade to latest Ollama
pip install -U langchain-ollama

## Windows AI Analysis Scheduled Task

The AI analysis stays on the Windows GPU machine and can be scheduled hourly with a lock so a new run is skipped when the previous run is still active.

Install or replace the scheduled task from the repo root:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install-ai-analysis-task.ps1 -Force
```

The task runs `scripts\run-ai-analysis-scheduled.ps1` once per hour and uses both Task Scheduler `IgnoreNew` behavior and `logs\ai-analysis.lock` to prevent overlapping runs.

By default the wrapper uses `C:\Users\Joey\scoop\apps\python312\current\python.exe`; override it before registration if needed:

```powershell
[Environment]::SetEnvironmentVariable("JOBSCRAPE_PYTHON_EXE", "C:\Path\To\python.exe", "User")
```

Useful commands:

```powershell
Start-ScheduledTask -TaskName "JobScrape AI Analysis Hourly"
Get-ScheduledTaskInfo -TaskName "JobScrape AI Analysis Hourly"
Get-Content logs\ai-analysis-scheduled.log -Wait
```

### TODO
- Dequeue process for daily digest email notification
