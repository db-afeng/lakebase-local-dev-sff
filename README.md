# DataCart Storefront

Includes the React frontend, FastAPI backend, Alembic migrations, native local
development tooling, and Lakebase branch tooling. Credentials, caches, build
output, and installed dependencies are generated locally.

Prerequisites for macOS/Linux: Python 3.12+, Node.js 20+ (a supported LTS release
is recommended), npm, Make, and Git with `user.name` configured. Local development
uses PGlite, an in-memory PostgreSQL build, and needs no Databricks credentials
or PostgreSQL installation. Lakebase mode additionally needs the Databricks CLI
and `jq`.

## Prepare a Databricks workspace

Start with [notebook 0, Run all](notebooks/0_run_all.py), to create/reuse the
Lakebase project, apply migrations and seed data, and deploy the app with one
Python input block. For a guided workshop, run the individual notebooks instead:
project creation (1, optional), schema and seed (2), and app deployment (3).
Notebook 4 previews and explicitly tears down the app and dedicated project.
You can use an existing clickops project in notebook 0 or start directly at 2.
Import the complete repository so supporting files remain available.

## Local development

```bash
make dev-local
```

This creates a checkout-local `.venv`, installs missing or changed Python and
Node dependencies, and starts three native processes: PGlite, FastAPI with
Python reload, and Vite with frontend hot reload. Alembic applies the existing
PostgreSQL migrations before FastAPI starts. PGlite packages have a separate
lockfile under `scripts/dev/`; they are not production app dependencies.

Package installation honours your pip/npm configuration. Override
`PIP_INDEX_URL` or `NPM_REGISTRY` when different package indexes are required.
The first startup needs network access to install dependencies.

Each session uses available loopback ports. From a second terminal, print the
storefront URL with:

```bash
make url
```

Append `/supplier` for the supplier view or `/api/health` for backend health.
Vite proxies `/api` and `/supplier` to FastAPI. All services bind to `127.0.0.1`.
The runtime indicator identifies the local database as PGlite.

A local database starts with **schema only, without seed data**. Records added
manually survive frontend edits, browser refreshes, and Python reloads. They
are discarded when PGlite or the development session stops. Carts remain
backend memory and still reset on backend reload. Optional feature tables are
not created automatically.

The terminal streams labelled service logs, also saved under
`.dev/<context-id>/`. Use Ctrl+C or a second terminal to stop development:

```bash
make dev-down
make dev-destroy  # Also removes this checkout's saved session logs/metadata.
```

Both commands are safe to repeat. They retain installed dependencies and do not
delete remote Lakebase resources. `make compose` has been removed; builds and
restarts no longer require container commands. Python and frontend edits reload
automatically. Restart the session to apply migration changes.

Each development context has an eight-character ID. Cursor worktrees reuse the
ID embedded in an exact `cursor/<id>` Git branch; other branches derive it from
the raw Git user and branch names. Runtime state is checkout-local; separate
worktrees get independent processes, ports, logs, and in-memory databases. Only
one session can run in a checkout at a time. Run `make dev-down` before changing
Git branches; shutdown still finds the checkout's session after a branch change.
Duplicate startup or changing modes requires stopping the existing session first.

A startup failure or unexpected service exit stops the whole session. PGlite is
never restarted automatically with an empty replacement database. A later
explicit startup detects stale metadata and cleans up verifiable leftover
processes. Never remove `.dev/` while a session is running.

PGlite uses one underlying PostgreSQL session. The app uses a one-connection
pool, so local requests serialize database access. An additional socket client
is allowed for maintenance (host `127.0.0.1`, port `database_port` in
`.dev/session.json`, database/user/password `postgres`, SSL disabled). Close
maintenance transactions promptly. Use Lakebase mode for realistic concurrency,
permissions, and PostgreSQL integration validation.

## Local development with Lakebase

Lakebase mode starts the same local frontend and backend, connecting FastAPI to
an ephemeral Lakebase Autoscaling branch copied from `production`. It does not
start PGlite.

Additional prerequisites:

- Databricks CLI with `databricks postgres` support (0.294.0 or newer)
- An explicitly selected authenticated CLI profile and `jq`
- An existing Lakebase project, specified by `LAKEBASE_PROJECT_NAME`
- An authorized PostgreSQL OAuth role; use your workspace username after notebook setup

Set `LAKEBASE_PROJECT_NAME` and your chosen `LAKEBASE_PROFILE` in your environment
or `lakebase.config`; no profile is selected by default. Connections use the
`lakebase-app-schema-owner` group role by default. For the notebook setup, use
your workspace username as `LAKEBASE_PG_ROLE` and set
`LAKEBASE_PG_OPTIONS="-c role=datacart_storefront_owner"`; see the notebook README.

```bash
LAKEBASE_PROJECT_NAME=<project-id> LAKEBASE_PROFILE=<profile> make dev-lakebase
```

This creates or reuses a branch named
`dev-<sanitized git user.name>-<sanitized git branch>-<context-id>`, omitting a
duplicate suffix when a Cursor branch already ends in that ID. It waits for the
read-write endpoint, writes a mode-0600 worktree-local `.env.lakebase`, and starts
the app. For non-Cursor branches, the derived ID preserves distinctions between
Git refs that sanitize to the same text. `LAKEBASE_PG_OPTIONS` preserves the
session role and migration ownership used by the seeded parent database.
Development branches inherit their parent's data; startup does not seed them.

Generated credentials expire after about an hour. While a Lakebase session is
running, refresh credentials and restart only its backend with:

```bash
LAKEBASE_PROJECT_NAME=<project-id> LAKEBASE_PROFILE=<profile> make refresh-lakebase
```

Refresh returns when the backend is database-ready. It keeps the frontend
process and URL, and refuses inactive sessions, local PGlite sessions, and
sessions whose Git context has changed. Deployed apps retain their existing
OAuth token generation behavior.

Stop either mode with `make dev-down`. Development branches expire after six
hours; stopping the local processes does not delete one immediately.

### Optional post-checkout provisioning

To enable checkout-triggered provisioning, first set your chosen
`LAKEBASE_PROFILE` and `LAKEBASE_PROJECT_NAME` in `lakebase.config`
(or export them in your Git environment),
then register this clone:

```bash
make hooks-install
```

The installer works with standard Git and Python 3. It sets a repository-local
`core.hooksPath` to a generated dispatcher directory in Git's metadata. Existing
hooks stay in their original location, including hooks selected by a global or
repository-local `core.hooksPath`. The dispatcher forwards all standard Git hooks
(and custom hook files present at installation) with their arguments, input, and
exit status. Existing `post-checkout` runs first; DataCart setup runs afterward,
even if the existing hook fails. DataCart provisioning failures do not change the
existing hook's exit status.

Relative hook paths are resolved from Git's hook working directory, so linked
worktrees use their own checkout's hooks and `lakebase.config`. The installation
is shared by the clone's worktrees unless a worktree has an explicit
`core.hooksPath` override. Run installation in that worktree to configure it
separately. The installer never edits global configuration or existing scripts.
Installation is idempotent and does not provision Lakebase immediately.

Branch checkouts provision Lakebase asynchronously; file checkouts are ignored.
Each checkout writes progress to `lakebase-branch-post-commit.log`. The hook
prepares `.env.lakebase` but does not restart running services. `make dev-lakebase`
waits for provisioning or retries synchronously when necessary.

To remove the dispatcher and restore the previous local setting (or resume the
inherited global/default hooks directory):

```bash
make hooks-uninstall
```

Uninstall refuses to overwrite subsequently changed configuration or remove
modified wrapper files. If `core.hooksPath` was changed to a different directory
after installation, uninstall leaves it and the saved installation alone; restore
the DataCart path first to use automatic cleanup. Keep generated hook files
unchanged. Reinstall to capture additional nonstandard hook names.

Earlier versions registered a user hook under `~/.databricks/user-githooks/`.
This installer leaves that external file alone. While forwarding an older hook,
the dispatcher suppresses duplicate DataCart provisioning.

Run the local checks (including isolated PGlite integration tests and a frontend build) with:

```bash
make check
```

## Current limitations

- Local PGlite has no automatic seed data. Add products, inventory, and customer ID 1
  before exercising a purchase. Verified: `/api/account` currently returns HTTP
  500 when that customer is missing.
- Carts live in backend memory and use a shared demo customer (ID 1). They reset
  when the backend restarts; this is not a multi-user account system.
- The optional supplier view reads an externally maintained `product_demand`
  table. This app does not create or update demand summaries; the view is empty
  without that table and can show stale data if its external source stops updating.
  It has no separate supplier authorization.
- Reviews, loyalty, promotions, and other optional features require additional
  tables or columns beyond the initial migration.
- Lakebase requires an existing project, selected CLI profile, and the configured
  group role. Credentials expire after about an hour; use `make refresh-lakebase`.
- Deployment configuration is a template: supply `project_id` explicitly (for
  example, `--var project_id=<project-id>`), review the workspace host and group
  role, and build the frontend before deploying. The bundle name is now
  `datacart-storefront`; existing deployments under a different bundle name keep
  their original state and require an explicit migration if they are to be reused.
