# Lead Generation Pipeline (Apify)

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Playwright](https://img.shields.io/badge/Playwright-Browser%20Automation-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev)
[![Apify](https://img.shields.io/badge/Apify-Actors-FF5A1F?logo=apify&logoColor=white)](https://apify.com)
[![Windows-first](https://img.shields.io/badge/Workflow-Windows%20Local%20Runs-0078D4?logo=microsoft&logoColor=white)](https://www.microsoft.com/windows)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm%20Noncommercial%201.0.0-blue.svg?style=flat-square)](LICENSE)

A resilient Python pipeline for finding and qualifying sports-academy leads by combining Google Maps discovery, Apify social-data actors, and a smart Playwright fallback that crawls business websites for Instagram and Facebook links. The project is designed to be crash-safe, resumable, and practical for a solo local machine rather than a fully managed cloud workflow.

## Why this project exists

This project is not a generic site scraper. It is a lead-generation workflow focused on sports academies and similar local businesses:

- discover candidate businesses from Google Maps
- filter out junk or institutional domains before browser work
- inspect business websites with Playwright to find social links
- enrich each lead with Instagram and Facebook data via Apify actors
- persist state so interrupted runs can resume cleanly
- detect resource or connectivity problems and stop before wasting more work
- export a final CSV with qualified lead records

The codebase explicitly separates data configuration, orchestration, session recovery, and browser execution so each part remains easier to maintain and reason about.

## High-level architecture

The project is built around a few focused modules:

- `config.py` — single source of truth for Apify actor run inputs, category filters, location settings, and validation logic.
- `main_apify.py` — main orchestration script; runs the Maps → Playwright → Instagram → Ads pipeline and writes the final output.
- `pipeline_session.py` — SQLite-backed persistence layer for crash recovery, resumable Apify calls, dataset reconciliation, and execution logging.
- `health_monitor.py` — circuit breaker / health monitoring for network and system issues.
- `playwright_engine.py` — browser automation layer that finds Instagram/Facebook links on business websites using scoped scans, sitemap discovery, section-text fallback, and watchdog recovery.

## Pipeline flow

1. Google Maps search
   - Uses the configured search keywords and target polygon or geolocation.
   - Pulls candidate businesses as the starting dataset.

2. Website pre-filtering
   - Skips obviously junk or institutional domains before browser work begins.
   - This keeps the expensive browser step focused on realistic targets.

3. Playwright website enrichment
   - Visits each business website to discover social links.
   - Uses homepage scanning, sitemap discovery, and visible section-text candidates.
   - Stops on the first reliable match rather than brute-forcing many guessed paths.

4. Instagram + Facebook enrichment
   - Calls Apify actors to gather Instagram and ad-related metadata for matching leads.
   - Uses the configured actor payloads from `config.py` rather than burying them in the main script.

5. Durable state + resume support
   - Session state is stored in SQLite so a crash does not mean losing all earlier progress.
   - A resumed run can pick up incomplete steps and reconcile with dataset results.

6. CSV export
   - Final records are written to CSV, with sorting and tiering logic applied in the orchestration layer.

## Files and responsibilities

```text
src
└──/
   ├── main_apify.py              # Main pipeline orchestration
   ├── config.py                  # centralized config and Apify payloads
   ├── health_monitor.py          # circuit breaker and detectors
   ├── pipeline_session.py        # crash-safe session handling and resume logic
   ├── playwright_engine.py       # browser automation for social-link discovery
   ├── .env                       # local-only secrets, such as APIFY_API_TOKEN
├── logs/                      # execution logs
├── sessions/                  # persistent session database state
├── test/                      # project validation/test scripts
├── README.md                  # this file
└── ...
```

## Configuration model

A key design choice in this repository is that configuration is centralized instead of split across `.env`, JSON files, and the main script.

This is intentional and documented in `config.py`:

- search keywords and category filters are Python lists, not fragile dotenv-delimited strings
- Apify actor payloads live in one module
- location and geolocation validation happens centrally
- the pipeline does not depend on scattered config files that need manual syncing

This reduces the risk of schema drift from upstream Apify changes and eliminates common bugs caused by trying to shoehorn lists into environment variables.

## Environment setup

This project expects a local Python environment and an Apify API token.

### 1) Create a virtual environment

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

### 2) Install dependencies

```bash
pip install python-dotenv apify-client playwright psutil
python -m playwright install chromium
```

### 3) Add your Apify token

Create a `.env` file next to the project root:

```env
APIFY_API_TOKEN=your_token_here
```

## Running the pipeline

The main script is `main_apify.py`.

### Fresh run

```bash
python main_apify.py
```

### Resume a previous run

```bash
python main_apify.py --resume sessions\active\your_session.db
```

### Rescue flags for partial dataset recovery

The script supports recovery flags for incomplete Apify dataset state:

```bash
python main_apify.py \
  --resume sessions\active\your_session.db \
  --MapDatasetID <maps_dataset_id> \
  --igDatasetID <instagram_dataset_id>
```

## Operational features

### Crash recovery and session persistence

`pipeline_session.py` keeps structured session state in SQLite so runs can recover from:

- power loss
- interrupted execution
- network dropouts
- partial Apify output
- mid-run process termination

This is one of the project’s core differentiators: the run is designed to be resumable instead of all-or-nothing.

### Health monitoring and circuit breaker logic

`health_monitor.py` adds a lightweight health layer for network and resource problems. It can:

- detect repeated network failures
- detect system resource exhaustion such as high RAM or CPU pressure
- trip a circuit breaker when a pipeline is no longer healthy
- fail gracefully without hiding the underlying issue

### Browser recovery and social-link discovery

`playwright_engine.py` is intentionally designed to recover from browser instability:

- anti-bot browser session setup
- resource blocking to reduce noisy loads
- homepage scanning and multi-selector social detection
- sitemap checks for matching entries
- section-text fallback for actual visible navigation links
- watchdog-based worker replacement when a page hangs or a browser session becomes wedged

It is specifically tuned to find Instagram and Facebook links rather than to perform a generic website crawl.

## Output

The pipeline writes structured CSV results representing lead records after filtering and enrichment. The exact output file naming is controlled in the orchestration layer, and logs are kept under the `logs/` folder for each run.

## Notes for contributors

- Keep configuration values in `config.py` where they describe third-party actor inputs or validation rules.
- Keep project-specific filtering vocabulary in the orchestration layer, not in `config.py`.
- Treat the session layer and health monitor as separate concerns; they are intentionally composed together higher up in the pipeline.
- Preserve the resumable architecture when changing logic; this project is explicitly built for interrupted local runs.

## Typical use case

This repository is best suited for:

- local lead-generation research
- sports club and academy prospecting
- geofenced local discovery with social verification
- runs where reliability and resume support matter more than a stateless, fire-and-forget scraper

## License

This project is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE).

## Disclaimer

This tool is designed for legitimate business research and lead generation. Use it in compliance with local law, site terms, Apify usage limits, and the relevant privacy and anti-scraping policies of the target services.
