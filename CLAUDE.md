# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

实战答辩评分系统 — A LAN-based peer evaluation system for student defense presentations. Students score other groups via mobile/desktop; admins manage classes, students, scoring rubrics, and results. Built with FastAPI + vanilla HTML/CSS/JS frontend.

## Quick Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run (default: SQLite on port 8888). Data lives in ./data/ (see Data Location).
python main.py

# Run with MySQL
pip install pymysql
python main.py --db mysql --mysql-user root --mysql-password <password> --mysql-db evaluation

# Run e2e regression tests (uses isolated temp DB, auto-cleans)
python test_e2e.py

# Docker (recommended for deployment — auto-starts, data persisted to ./data/)
docker compose up -d          # build + start in background
docker compose up -d --build  # rebuild after code changes
docker compose down           # stop
docker compose logs -f        # tail logs
```

Default admin credentials: `admin` / `admin123` (override with `EVAL_ADMIN_PASSWORD`).

## Data Location

Both `python main.py` and Docker share the **same data** in `./data/`:
- `data/evaluation.db` — SQLite database
- `data/.sessions/` — admin session files

Defaults come from `EVAL_DATA_DIR` (defaults to `./data`). Docker sets `EVAL_DATA_DIR=/data` and bind-mounts `./data:/data`, so local and container runs read/write the same files. Don't run both simultaneously — they compete for port 8888. `data/` is git-ignored; back it up by copying the folder.

## Configuration (env vars)

`config.py` reads env vars (CLI args still take precedence). Key ones: `EVAL_DATA_DIR`, `EVAL_DB` (sqlite/mysql), `EVAL_PORT`, `EVAL_HOST`, `EVAL_ADMIN_USER`, `EVAL_ADMIN_PASSWORD`, `EVAL_SECRET_KEY`, `EVAL_MYSQL_*`. Also `EVAL_DB_PATH` / `EVAL_SESSION_DIR` to override individual paths.

## Architecture

### Core Modules

- **`main.py`** — FastAPI application entry point. All API routes are defined inline (page redirects, auth, students, criteria/templates, scores, results, admin settings). Uses `get_db_conn()` per request.
- **`db.py`** — Dual-database abstraction layer. Two parallel classes with **identical method signatures** — any DB change must be made in both:
  - `SQLiteDB` — wraps `sqlite3`, manages the SQLite file
  - `MySQLDB` — wraps `pymysql`
  - Factory `get_db()` picks the implementation based on `config.DB_TYPE`; `get_db_conn()` returns a fresh instance per request (no shared state).
  - Module-level helpers: `_validate_selections()` (server-side score validation) and `_rows_to_csv()` (used by legacy `export_csv`).
- **`export_xlsx.py`** — `build_results_xlsx(data)` turns `get_results()` output into a two-sheet `.xlsx` (see Export below). Uses `openpyxl`.
- **`auth.py`** — Session-based authentication. Sessions stored as files in `config.SESSION_DIR` (`data/.sessions/`, 7-day expiry).
- **`config.py`** — CLI args + env vars. Global `config` exposes `DB_TYPE`, `PORT`, `HOST`, `DATA_DIR`, `SQLITE_PATH`, `SESSION_DIR`, MySQL params, admin creds, secret key.

### Database Schema (key tables)

| Table | Purpose |
|-------|---------|
| `admins` | Admin accounts (username + SHA256 password hash) |
| `classes` | Class names |
| `students` | Name, group number, class_name |
| `criteria` + `criteria_options` | Scoring dimensions and their graded options (class_name `''` = global default) |
| `templates` | Reusable criteria templates |
| `scores` | One submission (scorer, target_group, total_score, comment, scorer_class) |
| `score_details` | Per-dimension breakdown of a score; `criterion_label` is snapshotted at submit time |
| `settings` | Key-value store (e.g., `active_class`) |

### Frontend

Static HTML served from `static/` (no build step): `score.html` (student scoring, full-screen popup, auto-detects class from `active_class`), `admin.html` (dashboard: classes, students, criteria editor, results, settings), `admin_login.html`.

### Key Business Rules

- Each class has its own independent scoring rubric (`criteria.class_name`; `''` = global default). Editing a rubric later doesn't corrupt historical scores (dimension names are snapshotted into `score_details`).
- One score per student per target group; students cannot score their own group.
- **Server-side validation** (`_validate_selections` in `submit_score`, both DB classes): a submission must cover every dimension of the scorer's class rubric exactly once, each option must belong to that rubric, and the stored score/total are taken **from the DB, never from the client**. Rejections return `"invalid_option"` or `"incomplete"`. This is the guard against malformed/auto/tampered submissions.
- Student page shows a stronger confirmation when every dimension is set to its max option (guards against accidental all-max).

### Results & Export

- Ranking averages are rounded to **2 decimals** (`get_results` `avg_total`; frontend `toFixed(2)`).
- `GET /api/results/export` returns an **`.xlsx`** (`openpyxl`) with two sheets:
  1. **评分明细** — full per-scorer detail (includes scorer names — this is the audit sheet).
  2. **排名与评语** — ranking + per-dimension averages + composite score for all groups, followed by each group's received comments listed **anonymously** (serial number + comment text, no scorer name).

### API Route Groups (in `main.py`)

| Prefix | Auth | Methods |
|--------|------|---------|
| `/api/admin/*` | Required (except check) | login, logout, change-password |
| `/api/students` | mixed | GET list, POST import, DELETE by id |
| `/api/classes` | mixed | GET list+counts, POST create, DELETE by name |
| `/api/criteria` / `/api/templates` | Required for writes | GET/POST/DELETE |
| `/api/scores` | mixed | POST submit, GET check/my, **DELETE `{score_id}` (reset a score, admin)** |
| `/api/results` | Required | GET, `/export` (xlsx), `/group/{n}` |
| `/api/active-class` | GET public / POST admin | current class for the student page |

Admin can reset (delete) a single score via the **重置** button in each row of a group's detail table (成绩汇总 → 查看明细), which calls `DELETE /api/scores/{score_id}` → `db.delete_score()`. The student can then re-score.

## Development Notes

- **Adding a feature**: New endpoints go in `main.py`. DB operations go in **both** `SQLiteDB` and `MySQLDB` — keep methods identical (SQLite uses `?` params, MySQL uses `%s` + `with self._get() as cur`).
- **Testing**: `test_e2e.py` runs a real FastAPI TestClient against a temp DB (40+ checks). Add cases for new functionality. Reading `.xlsx` in tests uses `openpyxl.load_workbook`.
- **Dependencies**: `openpyxl` (export) and `httpx` (tests) are in `requirements.txt`; `pymysql` only needed for MySQL. The Docker image installs all of these plus `pymysql`.
- **Git ignores** `data/`, `__pycache__/`, `.idea/`, and the old top-level `evaluation.db`/`.sessions/` (data now lives under `data/`).
