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
| `settings` | Key-value store (e.g., `active_class`, and `class_tpl::<class>` = the template id currently bound to a class) |

### Frontend

Static HTML served from `static/` (no build step): `score.html` (student scoring, full-screen popup, auto-detects class from `active_class`), `admin.html` (dashboard: classes, students, criteria editor, results, settings), `admin_login.html`.

### Class ↔ Standard ↔ Template Model (admin UI)

Classes and scoring standards are **decoupled and then combined**:
- The **current scoring class** is chosen on the 学生管理 tab (the `classSelect`, labeled "当前评分班级"), which publishes `active_class`.
- The 评分标准 tab has **no class selector** — it always targets the current scoring class (shown read-only). Its "📋 当前评分模板" dropdown **binds a template to that class**: changing it (after a confirm) immediately applies the template as the class's rubric (`POST /api/criteria`) and records the binding (`POST /api/class-template`).
- **Templates are the single source of truth for standards.** Two ways to persist editor changes, both apply to the current class and (re)bind it: **💾 保存到当前模板** (`saveToCurrentTemplate` → `PUT /api/templates/{id}`, overwrites the bound template in place) and **➕ 另存为新模板并启用** (`saveAsNewTemplateAndApply` → `POST /api/templates`, creates a new named template). There is no separate "save criteria to class only" button — a rubric change is always also a template change.
- **Option (等级) ordering** is drag-reorderable within a criterion (drag handle on each `.option-row`, HTML5 DnD, same-dimension only). Before every write the editor calls `normalizeSortOrders()` to set each criterion/option `sort_order = display index`, so saved order matches on-screen order. `save_criteria` honors the provided `sort_order` (falls back to score only when absent), and `get_criteria` returns options `ORDER BY sort_order, id` — order is thus independent of score.
- The 评分模板 tab is just the template library: each row has **修改** (`editTemplate` — loads it into the 评分标准 editor; then save in place or as new) and **删除**.
- Per-class binding is stored in `settings` under key `class_tpl::<class_name>` (value = template id, `''` = unbound). A binding whose template was deleted reads back as unbound; the editor still shows the class's real current rubric.

### Key Business Rules

- Each class has its own independent scoring rubric (`criteria.class_name`; `''` = global default), applied from a template (see model above). A class may switch rubrics over time; scores are **isolated per scoring standard** — the results/detail/export/clear all operate on the scores matching the class's *current* standard (see Results & Export). Switching rubrics never deletes historical scores; it just changes which standard's scores are shown.
- One **active** score per student per target group **within a standard**; students cannot score their own group. "Already scored" (`submit_score` duplicate check, and the student page's scored/`get_my_scores` state) is **standard-aware**: it only counts a score under the class's *current* standard. So after a rubric switch, a student may score the same group again under the new standard, and `submit_score` deletes their now-stale prior evaluation of that group (from the old standard) before inserting — keeping admin view and student view consistent and avoiding orphaned, unmanageable scores.
- **Server-side validation** (`_validate_selections` in `submit_score`, both DB classes): a submission must cover every dimension of the scorer's class rubric exactly once, each option must belong to that rubric, and the stored score/total are taken **from the DB, never from the client**. Rejections return `"invalid_option"` or `"incomplete"`. This is the guard against malformed/auto/tampered submissions.
- Student page shows a stronger confirmation when every dimension is set to its max option (guards against accidental all-max).

### Results & Export

- Results are **scoped to one class AND its current scoring standard**. The 成绩汇总 page has its own class selector (`resultsClassSelect`), defaulting to current class → server `active_class` → first class. Switching it reloads results and retargets the export link.
- **Scoring-standard isolation** (`_standard_key` / `_belongs_to_standard` in `db.py`): a "standard" is identified by its **set of dimension labels** (`criterion_label`). `get_results` / `get_group_detail` only include scores whose snapshot label set equals the class's *current* rubric's label set. So switching a class to a different rubric hides the old-standard scores (no residual totals), switching back reveals them again (data is never deleted — just filtered), and different standards used by the same class stay isolated. `criterion_id` is **not** used for matching (IDs get reassigned by `save_criteria`); labels are the stable key. Same rule drives per-dimension display in `admin.html` and `export_xlsx.py`.
- **清空本班当前标准评分** (🗑 button) → `POST /api/scores/clear` → `db.clear_class_scores(class_name)` deletes only the scores matching the class's *current* standard (label set); scores from other standards on the same class are kept. Empty class name is rejected to avoid mass-delete.
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
| `/api/criteria` / `/api/templates` | Required for writes | GET/POST/DELETE. `POST /api/templates` returns `{ok, id}` (new template id); `PUT /api/templates/{id}` overwrites an existing template in place (`update_template`). |
| `/api/class-template` | GET public / POST admin | per-class current-template binding (`{template_id}` / set `{class_name, template_id}`, backed by `settings` key `class_tpl::<class>`) |
| `/api/scores` | mixed | POST submit, GET check/my, DELETE `{score_id}` (reset one, admin), **POST `/clear` (clear a class's scores, admin)** |
| `/api/results` | Required | GET, `/export` (xlsx), `/group/{n}` |
| `/api/active-class` | GET public / POST admin | current class for the student page |

Admin score-management actions (成绩汇总 tab): **重置** button per row in a group's detail table → `DELETE /api/scores/{score_id}` → `db.delete_score()` (the student can then re-score); **🗑 清空当前标准评分** → `POST /api/scores/clear` → `db.clear_class_scores()` (wipe only the selected class's scores under its *current* standard, e.g. before reusing the class with a new rubric — scores from other standards are kept).

## Development Notes

- **Adding a feature**: New endpoints go in `main.py`. DB operations go in **both** `SQLiteDB` and `MySQLDB` — keep methods identical (SQLite uses `?` params, MySQL uses `%s` + `with self._get() as cur`).
- **Testing**: `test_e2e.py` runs a real FastAPI TestClient against a temp DB (40+ checks). Add cases for new functionality. Reading `.xlsx` in tests uses `openpyxl.load_workbook`.
- **Dependencies**: `openpyxl` (export) and `httpx` (tests) are in `requirements.txt`; `pymysql` only needed for MySQL. The Docker image installs all of these plus `pymysql`.
- **Git ignores** `data/`, `__pycache__/`, `.idea/`, and the old top-level `evaluation.db`/`.sessions/` (data now lives under `data/`).
