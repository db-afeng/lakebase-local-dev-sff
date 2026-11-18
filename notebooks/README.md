# Workspace setup

Each notebook is a guided workshop with prerequisites, input instructions,
Markdown explanations before execution steps, inline comments explaining the
API calls, SQL, rerun checks, and build operations, expected results, checkpoints,
and troubleshooting. Notebook 3 finishes with a storefront checkout exercise
and an optional transition to local development. Notebook 4 tears down the app
and the dedicated Lakebase project when the workshop is complete.

Import or clone the **complete repository** into a Databricks Workspace Git folder
(or workspace directory). Importing only the notebooks is insufficient:
they use the actual Alembic migrations and application source. All workshop
resource operations are written as procedural code directly in notebooks 1–4.
Run on Python 3.12+ compute with network access to Lakebase and the package registries. Notebook 3 additionally needs Node.js 20+; npm is
fetched with an integrity check if missing.

1. **`0_run_all`** runs notebooks 1, 2, and 3 in order with a single Python input
   block. Choose **Run all** to create/reuse the project, migrate/seed, and deploy.
   `%run` executes the procedural workshop notebooks in the same Python session;
   each child consumes its input overrides once. The child notebooks remain usable
   independently, and teardown is never part of this route.
2. **Optional: `1_optional_create_lakebase_project`** creates a Lakebase Autoscaling
   project. Prefer clickops if desired: create a project with its default
   `production` branch, read-write endpoint, and `databricks_postgres` database. New
   projects display as `DataCart Storefront your-cleaned-username`.
3. **`2_prepare_schema_and_seed`** accepts your project ID, applies Alembic, and
   seeds 5 customers, 12 products, 12 inventory rows, 6 orders, and 6 order items.
4. **`3_create_datacart_app`** accepts the same project ID, builds the React app,
   creates a Databricks App, waits up to five minutes for its service principal,
   binds its database, grants access, and deploys it.
5. **`4_teardown_datacart`** previews cleanup, then deletes the app followed by the
   entire Lakebase project when you set `DRY_RUN = False`. Enter the exact app name
   and project ID used in the workshop. A UI project UUID is resolved to its
   readable resource ID before checking the app binding; mismatches show both
   the selected project and actual binding paths. Already soft-deleted projects
   are skipped. Every project branch and database is in
   scope, including any local-development branches. Export data you want to keep
   and ensure no other apps or users depend on the project before deletion.
   Normal project retention is preserved; immediate purge is never requested.
   Source-release directories and the repository are retained.

Each workshop notebook begins with `%run ./setup`. The shared `setup` notebook
installs dependencies, imports libraries and constants, locates the repository, and
authenticates with your workspace identity. Keep it beside notebooks 0–4;
you do not need to run it separately. It does not restart Python inside `%run`.
Start from a fresh session; if incompatible libraries were already imported,
restart Python through the notebook UI and rerun from the beginning. Setup constrains
protobuf using the installed grpcio-status dependency metadata, supporting different
Databricks runtime versions without forcing an incompatible protobuf major version.
If an earlier run already imported an incompatible version, restart Python after
setup repairs the installed packages, then rerun from the beginning.

After teardown, Lakebase can still return a soft-deleted project's metadata during
retention, while its branches are unavailable. Notebooks 1–3 check this state before
branch discovery. To start fresh, enter a different `LAKEBASE_PROJECT_ID` in notebook
0 (or the individual notebooks). To recover retained data instead, explicitly restore
the project in Lakebase before rerunning. Setup never restores or purges it automatically.

Edit the plain Python variables in the **Your inputs** block near the top of each
notebook, then run cells in order. There are no widget or notebook task parameters.
If you change an input, rerun its block and the cells below it. Notebooks 2 and 3
also accept an existing branch ID and PostgreSQL database name. Notebook 3 has
editable `APP_NAME` and `SOURCE_PATH` variables. Shared setup derives the suffix
from the part of your Databricks username before `@`, lowercases it, replaces each
unsupported character with `-`, and trims edge hyphens. For example,
`alex.feng@example.com` gives project `datacart-alex-feng` and app
`datacart-storefront-alex-feng`. These defaults are assigned in the input blocks
of notebooks 0–3; override the project ID for an existing clickops project.
A blank `SOURCE_PATH` uses a release directory under your workspace user directory.

The suffix is never hashed or truncated. Project IDs allow up to 63 characters;
app names allow 30, so the `datacart-storefront-` prefix leaves 11 characters for
the suffix. Override an overlong default with a shorter valid input. Different
users can normalize to the same suffix, including users from different email
domains; use explicit names in that case. Existing resources are not renamed;
notebook 4 still requires the exact names used when they were created. Resource paths are discovered
from the API, so the database's API ID need not match its SQL name. Leave the
branch default only when you intend to use `production`.

Run as the project creator/database administrator with permission to create roles,
grant memberships, create schemas, and create/bind Apps resources. No workspace
SCIM group is required. A dedicated native `NOLOGIN` role,
`datacart_storefront_owner`, owns the schema. Your user and the app's own OAuth
service principal receive membership in this database-scoped owner role. App
sessions assume it via `PGOPTIONS`; database credentials are refreshed per new
connection. Existing schemas owned by another role are rejected instead of being
dropped or reassigned. This explicitly supports seeding before app deployment.

Reruns preserve existing fixture records and stock quantities. Use a dedicated
workshop database: fixture IDs are reserved, and unrelated data can conflict.
Database sequences are advanced without rewinding them. The app can be redeployed
only if the selected name is already bound to the same database; unrelated apps
are not overwritten. Each deployment uploads a new, runtime-only source directory;
older deployment directories remain available for inspection.

The scripts use your **workspace notebook identity**. Do not put local CLI profile
names or tokens in the notebooks. For local validation, use the explicitly chosen
`lakebase-workshop` profile. To connect to workspace serverless compute:

```bash
databricks ssh connect --profile lakebase-workshop --name datacart-validation
```

For native local development against branches of the prepared project, set in
`lakebase.config`:

```bash
LAKEBASE_PROJECT_NAME="your-project-id"
LAKEBASE_PROFILE="your-local-profile"
LAKEBASE_PG_ROLE="your-workspace-user@example.com"
LAKEBASE_PG_OPTIONS="-c role=datacart_storefront_owner"
```

Your OAuth login needs to exist in the branch and retain membership in the owner
role. Then run `make dev-lakebase`; it generates `.env.lakebase` automatically.

Project/endpoint and app compute can incur costs. Stopping the notebook does not
stop them. Use notebook 4 to preview and explicitly delete the app and dedicated
project when finished, or manage them through the workspace UI. Supplier
demand and optional loyalty/reviews/promotions are not provisioned. The shared
demo customer and in-memory cart behavior remain unchanged.

## Validation — 18 November 2026

Fixed discovery of soft-deleted projects: retained metadata no longer counts as
an active project. Added coverage for creation/reuse and all three discovery
cells; the 23 local notebook tests pass.

Ran the workshop end to end on Databricks with the fresh project
`datacart-alex-feng-2`: [successful run](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/989211342127964/run/804091883714681).
The app reports running, `/api/health` returns HTTP 200, and `/api/dbtest` confirms
an active database connection and 12 seeded products. The project and app remain
available for the workshop; the earlier soft-deleted project was left untouched.

After refining setup to constrain protobuf using the runtime's grpcio-status
requirements, verified compatibility with both SDK and gRPC on serverless client
4 and on environment version 6 (matching the interactive notebook):
[environment 6 check](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/415004716921307/run/17832187191144).

## Validation — 17 November 2026

Validated with the explicitly selected `lakebase-workshop` profile on Azure.
Older run histories retain the notebook numbers used before the new run-all entry
point was added; references below use the current numbering.

- Ran `0_run_all` end to end with an explicit project path override:
  [run-all validation](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/416103270395539/run/122659805277128).
  Confirmed sequential project reuse, migrations/seeding, app build/deployment,
  and preservation of the caller's input values across nested `%run` calls.
  Teardown was not invoked. The first attempt hit the documented Node executable
  error; the platform retry succeeded. The temporary validation notebook was
  removed after execution.

- Connected with `databricks ssh connect` and executed the exported notebook
  sources on workspace serverless compute.
- Before the shared-setup/input-variable refactor, ran all three notebooks as native Databricks notebook tasks. All succeeded:
  [validation run](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/495284158507344/run/1048237150044685).
- After extracting shared setup and replacing inputs with Python variables, ran
  the notebook 2 prefix through `%run ./setup`, dependency installation, variable
  assignment, and read-only database discovery on serverless compute. All passed:
  [shared setup validation](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/726495044581190/run/942640430187583).
  This refactor check did not rerun migrations, seeding, or app deployment.
- Ran notebook 4 in preview mode against the existing validation resources:
  [teardown preview validation](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/74408454281704/run/570817594041063).
  Confirmed the app/project and branch inventory with `DRY_RUN = True`. Deletion
  order, mismatched bindings, partial reruns, and failure handling were tested
  with mocks locally; no live resources were deleted.
- After moving all resource logic into procedural notebook cells and removing the
  Python helper module locally and from the workspace, ran notebooks 1–4:
  [procedural notebook validation](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/59584224702722/run/952873784587894).
  Project reuse, migrations/seeding, app build/deployment, and teardown preview all
  passed. No app or project was deleted. Notebook 3 initially failed because the
  runtime's `/usr/local/bin/node` raised `Exec format error`; the platform retry
  succeeded. A separate diagnostic observed the same error on `aarch64` compute.
  A Node executable being present is insufficient: it must run on the selected
  compute architecture. Use compatible Node.js 20+ compute if this error recurs.
  The subsequent walkthrough-comment edits preserve the executable Python AST.
- Validated the UUID-aware teardown preview against `datacart-alex-feng` and
  `datacart-storefront-alex-feng` without deleting resources:
  [UUID binding preview](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/827377901884223/run/447393344324403).
  The temporary check notebook was removed afterward. Local tests cover delayed
  service-principal provisioning, its timeout, the project display-name suffix,
  explicit binding-mismatch diagnostics, and soft-deleted project reruns.
- Confirmed migration `0001`, initial seed counts, role-based app access, and
  successful reruns without resetting seeded data.
- Authenticated HTTP checks passed for the frontend, health, products, featured
  products, Alice's account, orders, and supplier page. A sample checkout created
  order 7, verifying the repaired sequences and app write permissions.

Validation resources used:

- Lakebase project: `datacart-workshop-validation`
- App: `datacart-workshop-validation` (deleted after validation)
- Notebook folder: `/Workspace/Users/alex.feng@databricks.com/datacart-storefront-workshop/notebooks`

The app and project were deleted at the user's request on 17 November 2026 by
running notebook 4 with `DRY_RUN = False`:
[teardown run](https://adb-7405612095630146.6.azuredatabricks.net/?o=7405612095630146#job/686933139782215/run/500946473381707).
Verified the app is absent and the project is soft-deleted. Lakebase reports its
scheduled permanent purge as 24 November 2026 at 1:56 pm AEDT. Workspace source
releases and workshop notebooks remain. Temporary execution/validation notebooks
have been removed, and the shared teardown notebook retains `DRY_RUN = True`.
