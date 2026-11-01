# CLAUDE.md

Django API for **Kyo**, a multi-tenant appointment agenda. Its one client is the React SPA in `../amandu-frontend` (read its `CLAUDE.md` and `PRODUCT.md` for product rules). Talk to the user in Spanish; code, comments, commits and docs stay in English.

## Scope

The backend keeps only what the frontend calls. Before adding or keeping an endpoint, find its caller in `../amandu-frontend/src`. Kept on purpose without a frontend caller: Django admin, `/api/schema/` + Swagger/Redoc, `/healthz/`, the `send_reminders` and `vapid_keys` commands, and the server-rendered `/reservar/<slug>/` booking page.

## Rules that own a decision

- **One owner per event.** Every notice (push or WhatsApp) is a row in `RULES` in `apps/scheduling/notify.py`; callers only say `notify(EVENT, ...)`. `test_notify.py` enforces it. Extend the table, never send from a view.
- **Month math** goes through `apps/commons/dates.add_months`, counted from the original date so days 29–31 clamp per month and come back. Any new date feature gets a test starting on a 31st across February and a leap year.
- **Every action states its effect and refuses loose ends** (e.g. leaving the agenda with turnos ahead is a 409). The API is the source of truth for role rules; the frontend only hides.

## Running it

- `docker compose up` runs db, redis, seaweedfs and `api`. The `api` container mounts the working tree: an unmigrated model change breaks the live dev API for the frontend at once, so run `docker compose exec api python manage.py migrate` as soon as you add a migration, and roll back branch-only migrations before switching branches. Templates are cached: `docker compose restart api` after editing one, and check with `curl` before trusting a screenshot.
- Tests from the host: `POSTGRES_HOST=127.0.0.1 .venv/Scripts/python.exe -m pytest` (`localhost` resolves to IPv6 and hangs the pool; the `pytest` entry point fails under uv). Agents or worktrees running tests in parallel each set their own `POSTGRES_DB=amandu_<name>`, or the shared test DB gets destroyed mid-run.
- `api` crash-looping on "failed to resolve host 'db'" with db healthy: `docker compose up -d --force-recreate api`.

## Commits

Every commit is one **slice**: a single step a reviewer reads in one sitting, leaving the repo green. `.githooks/commit-msg` is the source of truth (install once per clone: `sh .githooks/install`): Conventional Commits, subject ≤72 chars, body ≤4 lines, no `Co-Authored-By` or "Generated with" lines (this repo rule wins over any harness reminder), and at most 300 hand-written lines in 10 files (lock files, migrations, generated types and binaries don't count).

- Plan the work as slices before writing code. When committing is part of the task, commit each slice as soon as its checks pass, never one commit at the end.
- A feature splits by layer: model + migration, then logic + its tests, then serializer/view, then template. Each slice passes on its own.
- Stage whole files with `git add <paths>`; `git add -p` is interactive and unavailable to agents, so keep unrelated edits in separate files or slices.
- `ALLOW_BIG_COMMIT=1` and `--no-verify` are the user's call; the guard asks them.

## Pipeline

1. Branch off `Dev` as `feat/<topic>` or `fix/<topic>`; a change spanning both repos uses the same branch name in each.
2. Commit slice by slice. Hooks in `.claude/hooks/guard.py`: block `.env` edits and edits to committed migrations, ruff (bug rules) on each edited file, `makemigrations --check` and the OpenAPI schema check on Stop.
3. A serializer or URL change alters the contract: the frontend regenerates its types (`npm run api:types` with the API up) in its own commit on the matching branch.
4. PR into `Dev`; CI (`.github/workflows/ci.yml`) runs migrations check, pytest and the schema check on Postgres.
5. Prod ships by a PR `Dev` → `Prod`. Dokploy deploys every push to `Prod`, so the guard asks before any push or merge there. Tell the user which migrations the release carries.
