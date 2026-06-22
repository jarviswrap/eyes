# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A system that daily fetches GitHub's Weekly Trending top 20 repos, analyzes each with LLM (DeepSeek) across four dimensions (functionality, tech stack, pain points, competitors), then generates a weekly trend summary. All data is stored in local SQLite and served via a web dashboard.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run once immediately (fetch + analyze + summary)
export LLM_API_KEY="sk-xxx"        # required for LLM analysis
export GITHUB_TOKEN="ghp_xxx"      # optional, increases API rate limit
python main.py --run-once

# Dry run: fetch only, no LLM calls, no DB writes
python main.py --dry-run

# Start scheduler daemon (runs daily at configured time)
python main.py

# Web dashboard
python web_server.py               # http://localhost:8080
python web_server.py --port 3000   # custom port
```

## Architecture

**Data flow**: `fetcher.py` → `database.py` → `analyzer.py` → `tracker.py` → `summarizer.py`, orchestrated by `scheduler.py`.

- **`src/fetcher.py`** — Primary: scrapes `github.com/trending?since=weekly` using browser headers (`BROWSER_HEADERS`). Fallback: GitHub Search API. Returns `TrendingRepo` dataclass objects (rank, full_name, stars_this_week, forks).
- **`src/database.py`** — SQLAlchemy 2.0 ORM with five tables: `repositories` (unique on `full_name`), `daily_rankings`, `project_analyses`, `weekly_summaries`, `consecutive_tracking`. `CRUD` class wraps all operations. `Database` and `CRUD` are module-level singletons via `get_database()` / `get_crud()`.
- **`src/analyzer.py`** — Calls DeepSeek API (OpenAI-compatible) for per-project analysis. `LLMAnalyzer.analyze_single()` fetches README, sends structured prompt, parses JSON response. `analyze_batch()` runs with `asyncio.Semaphore` concurrency control. Client is lazily initialized; returns `None` gracefully when `api_key` is empty.
- **`src/tracker.py`** — `ConsecutiveTracker.update()` counts each repo's appearances in `daily_rankings` over the last 5 days. Marks `is_active = True` when `appearance_days >= 2`. Not a streak tracker — it's a rolling 5-day window.
- **`src/summarizer.py`** — `WeeklySummarizer.generate_summary()` aggregates all analyses in a week range  and calls LLM to produce a structured trend report. Report is formatted as Markdown and saved to `weekly_summaries` table.
- **`src/scheduler.py`** — `TrendingJob.run_once()` executes the full daily pipeline. `create_scheduler()` sets up APScheduler with a cron trigger at `config.yaml`'s `run_time`. Summary generation only triggers on `summary_day` (0=Mon, 6=Sun).
- **`src/config.py`** — Loads `config.yaml`, resolves `${ENV_VAR}` placeholders from environment variables at startup.
- **`web_server.py`** — FastAPI app with `/api/*` JSON endpoints serving data from the same SQLite database. Also imports and reuses `main.py`'s core functions (`load_config`, `setup_logging`, `run_once`, `start_scheduler`) for the `/api/trigger/*` endpoints. API docs at `/docs`.
- **`src/web/templates/index.html`** — Single-page dashboard with five tabs (Dashboard, Daily Trending, Project Detail, Weekly Summaries, Highlights). Embedded CSS (dark theme), vanilla JS, and a built-in Markdown-to-HTML renderer. Header includes control buttons (Dry Run, Run Once, Daemon toggle) that call the trigger APIs.

## Key design decisions

- **LLM provider**: DeepSeek via OpenAI-compatible SDK. The `api_key` config field supports `${LLM_API_KEY}` env var. Both `LLMAnalyzer` and `WeeklySummarizer` use lazy client initialization — they don't crash when the key is missing, just log errors and skip.
- **GitHub Trending source**: Primary is scraping the trending HTML page (not Search API). The Search API `created:>=N` query was a poor approximation of "trending this week" and has been demoted to fallback.
- **Idempotency**: `CRUD.has_rankings_for_date()` prevents duplicate fetches on the same day. Re-run `--run-once` safely skips already-saved data. Pass `force=True` to `TrendingJob.run_once()` to bypass this check (used by the web trigger API).
- **`main.py` is importable**: Core functions (`setup_logging`, `load_config`, `run_once`, `dry_run`, `start_scheduler`) are designed to be imported by `web_server.py` so the web trigger buttons share the exact same code paths as the CLI. `sys.path` insertion is guarded by `if __name__ == "__main__"`.
- **Web trigger APIs**: `web_server.py` exposes `POST /api/trigger/run-once?force=true`, `POST /api/trigger/dry-run`, `POST /api/trigger/daemon/start|stop`, and `GET /api/trigger/daemon/status`. These reuse `main.py`'s functions directly.
- **DB path in config**: Default `data/trending.db`. Directory is auto-created. Delete this file to reset schema (there's no migration system — dev stage tradeoff).
- **`github_id` is nullable**: Trending page scraping doesn't provide numeric GitHub IDs. Lookups use `full_name` (unique).
- **`generated_at` timezone**: ISO strings in API responses include `Z` suffix so browsers correctly convert to the user's local timezone.
