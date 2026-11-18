# Databricks notebook source
# MAGIC %md
# MAGIC # 3 · Deploy DataCart Storefront as a Databricks App
# MAGIC
# MAGIC **Workshop goal:** connect a working web application to the Lakebase database you
# MAGIC prepared. You will select that database, choose an app name, build the React
# MAGIC frontend, deploy the FastAPI backend, and complete a purchase in the browser.
# MAGIC
# MAGIC Start here after **2_prepare_schema_and_seed** completes. The project can have
# MAGIC been created through clickops or optional notebook 1; you only need its project ID.
# MAGIC
# MAGIC ### What you need
# MAGIC
# MAGIC - The same project, branch, and database used in notebook 2.
# MAGIC - Permission to create Databricks Apps and bind the selected database resource.
# MAGIC - Database permissions to create the app's OAuth role if needed and grant it membership in the storefront owner role.
# MAGIC - Python 3.12+ compute with **Node.js 20+**, and network access to Lakebase and package registries.
# MAGIC
# MAGIC If npm is absent, this notebook downloads a checksum-verified copy into temporary
# MAGIC storage. Node.js must already be available. Restricted compute must allow the
# MAGIC package downloads used by the Python and npm installation steps.
# MAGIC
# MAGIC **Outcome:** a running, authenticated DataCart Storefront backed by your seeded
# MAGIC Lakebase database. This notebook starts billable app compute.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 · Run shared setup
# MAGIC
# MAGIC Run the next cell first. It installs the workshop libraries, locates the complete
# MAGIC repository, and authenticates with your workspace identity. Open **setup** for
# MAGIC shared prerequisites and troubleshooting.
# MAGIC
# MAGIC Edit the Python input block below before running the remaining cells in order.
# MAGIC If you change an input later, rerun that block and all cells below it.

# COMMAND ----------

# MAGIC %run ./setup

# COMMAND ----------

# MAGIC %md
# MAGIC ## Your inputs
# MAGIC
# MAGIC `LAKEBASE_PROJECT_ID` defaults to `datacart-<username-before-@>` from shared setup.
# MAGIC For example, `alex.feng@example.com` becomes `datacart-alex-feng`.
# MAGIC Override it with your existing project ID if you used clickops.
# MAGIC A full `projects/<id>` resource path is also accepted. Use the ID rather than its
# MAGIC display name or PostgreSQL hostname. IDs have 3–63 lowercase letters, digits, or
# MAGIC hyphens, start with a letter, and end with a letter or digit.
# MAGIC
# MAGIC Use the same destination in notebooks 2 and 3. Keep `BRANCH_ID = "production"`
# MAGIC and `DATABASE_NAME = "databricks_postgres"` for a new project, or enter the
# MAGIC existing branch and SQL database name you intend to use.
# MAGIC
# MAGIC `APP_NAME` defaults to `datacart-storefront-<username-before-@>`. You can also
# MAGIC choose a workspace-unique name of 2–30 lowercase letters, digits, or hyphens,
# MAGIC starting with a letter and ending with a letter or digit. Leave `SOURCE_PATH`
# MAGIC empty to stage releases under your workspace user directory, or provide a
# MAGIC writable absolute `/Workspace/...` directory.
# MAGIC
# MAGIC The full username suffix is preserved. If the generated app name exceeds 30
# MAGIC characters, supply a shorter valid `APP_NAME` before continuing. Usernames that
# MAGIC normalize to the same suffix can collide; override names when necessary.

# COMMAND ----------

LAKEBASE_PROJECT_ID = (
    DEFAULT_PROJECT_ID  # Override for a project created through clickops.
)
BRANCH_ID = "production"
DATABASE_NAME = "databricks_postgres"
APP_NAME = (
    DEFAULT_APP_NAME  # Override if this name is taken or longer than 30 characters.
)
SOURCE_PATH = ""  # Optional: blank uses your workspace user directory.

# COMMAND ----------

# Notebook 0 passes its settings in the shared %run session. Consume them once
# so a later standalone run returns to the editable defaults above.
if "_DATACART_NEXT_INPUTS" in globals():
    _step_inputs = globals().pop("_DATACART_NEXT_INPUTS")
    LAKEBASE_PROJECT_ID = _step_inputs["LAKEBASE_PROJECT_ID"]
    BRANCH_ID = _step_inputs["BRANCH_ID"]
    DATABASE_NAME = _step_inputs["DATABASE_NAME"]
    APP_NAME = _step_inputs["APP_NAME"]
    SOURCE_PATH = _step_inputs["SOURCE_PATH"]
    del _step_inputs


# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 · Inspect the selected database
# MAGIC
# MAGIC The next cell discovers your existing branch, database resource, and read-write
# MAGIC endpoint. A SQL name such as `databricks_postgres` can have an API ID such as
# MAGIC `databricks-postgres`; the code resolves that distinction.
# MAGIC
# MAGIC **Check the printed destination before continuing.** Later steps act on that
# MAGIC database. This discovery step does not create any resources.

# COMMAND ----------

# Accept either the project ID or its full projects/<id> resource path.
project_id = LAKEBASE_PROJECT_ID.strip().removeprefix("projects/")
if not re.fullmatch(r"[a-z][a-z0-9-]{1,61}[a-z0-9]", project_id):
    raise ValueError(
        "Enter your Lakebase project ID (3–63 lowercase letters, digits, hyphens)."
    )
project = f"projects/{project_id}"
if not re.fullmatch("[a-z][a-z0-9-]*", BRANCH_ID):
    raise ValueError("Enter a branch ID, not a full resource path.")
# Deleted projects remain readable during retention, but their branches are unavailable.
# Stop before database access; recovering old data must be an explicit choice.
project_resource = w.postgres.get_project(name=project)
if project_resource.delete_time:
    raise RuntimeError(
        f"{project} is soft-deleted (deleted {project_resource.delete_time}; "
        f"scheduled purge {project_resource.purge_time or 'not reported'}). "
        "Choose a different LAKEBASE_PROJECT_ID for a fresh workshop, or explicitly "
        "restore this project in Lakebase before rerunning. No resources were changed."
    )
branch = f"{project}/branches/{BRANCH_ID}"
w.postgres.get_branch(name=branch)
# Match the SQL database name from its status; the API resource ID can use hyphens.
databases = list(w.postgres.list_databases(parent=branch))
matches = [
    d for d in databases if d.status and d.status.postgres_database == DATABASE_NAME
]
if len(matches) != 1:
    raise RuntimeError(
        f"Database {DATABASE_NAME!r} not found uniquely in {branch}. Select an existing database."
    )
# Select the writable endpoint. Depending on the API response, its type can appear
# in the requested specification or in the observed status.
endpoints = [
    e
    for e in w.postgres.list_endpoints(parent=branch)
    if (
        (e.spec.endpoint_type if e.spec else None)
        or (e.status.endpoint_type if e.status else None)
    )
    == postgres.EndpointType.ENDPOINT_TYPE_READ_WRITE
]
if len(endpoints) != 1:
    raise RuntimeError(
        f"Expected one read-write endpoint in {branch}; found {len(endpoints)}."
    )
# Fetch current endpoint status for the hostname instead of constructing one.
endpoint = w.postgres.get_endpoint(name=endpoints[0].name)
host = endpoint.status.hosts.host if endpoint.status and endpoint.status.hosts else None
if not host:
    raise RuntimeError("Endpoint has no hostname yet. Wait for provisioning and retry.")
# Keep the resolved paths and hostname together for the following cells.
# These values are connection metadata, not database passwords.
target = {
    "project": project,
    "branch": branch,
    "database": matches[0].name,
    "database_name": DATABASE_NAME,
    "endpoint": endpoint.name,
    "host": host,
}
print(json.dumps(target, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 · Confirm the app name and verify the database
# MAGIC
# MAGIC The next cell resolves blank optional inputs using your workspace username.
# MAGIC The suffix is the part before `@`, lowercased with unsupported characters replaced
# MAGIC by hyphens and edge hyphens trimmed. No hash or truncation is used. It checks that
# MAGIC the migration revision and customer ID 1 exist; if either check fails, complete
# MAGIC notebook 2 before deploying.
# MAGIC
# MAGIC An existing app is reused only when its database binding matches the selected
# MAGIC branch and database. Choose another name if that app belongs to a different setup.

# COMMAND ----------

username = w.current_user.me().user_name
# Use the same readable username suffix as the project default from shared setup.
if not APP_NAME.strip():
    APP_NAME = DEFAULT_APP_NAME
else:
    APP_NAME = APP_NAME.strip()
# Put releases under your own workspace directory unless you supplied a location.
if not SOURCE_PATH.strip():
    SOURCE_PATH = f"/Workspace/Users/{username}/datacart-app-releases/{APP_NAME}"
else:
    SOURCE_PATH = SOURCE_PATH.strip()
print(f"App: {APP_NAME}\nSource releases: {SOURCE_PATH}")

# Authenticate this database session as the notebook's workspace user.
username = w.current_user.me().user_name
# Generate a fresh short-lived OAuth password for each connection attempt.
for attempt in range(4):
    credential = w.postgres.generate_database_credential(endpoint=target["endpoint"])
    try:
        conn = psycopg.connect(
            host=target["host"],
            dbname=target["database_name"],
            user=username,
            password=credential.token,
            sslmode="require",  # Encrypt the connection to Lakebase.
            connect_timeout=20,
            # Assume the shared owner role after authenticating as yourself.
            options=f"-c role={OWNER_ROLE}",
        )
        break
    except psycopg.OperationalError:
        if attempt == 3:
            raise
        # Allow a recently provisioned or sleeping endpoint time to become available.
        time.sleep(2**attempt)
# Do not leave the OAuth credential object in the notebook's global variables.
del credential
# The connection context commits on success, rolls back on error, and closes.
with conn:
    assert conn.execute(
        "SELECT version_num FROM ecommerce.alembic_version"
    ).fetchone(), "Run notebook 2 first."
    assert conn.execute("SELECT id FROM ecommerce.customers WHERE id=1").fetchone(), (
        "Run the seed cell in notebook 2."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 · Understand the deployment
# MAGIC
# MAGIC The next execution cell performs the following sequence. Read the steps, then
# MAGIC run the cell once and allow it to complete; app compute startup can take several
# MAGIC minutes.
# MAGIC
# MAGIC ### A. Create the app identity and bind the database
# MAGIC
# MAGIC The first cell creates or reuses your Databricks App and attaches the selected
# MAGIC Lakebase database with `CAN_CONNECT_AND_CREATE`. The app authenticates with its
# MAGIC own service principal, rather than the notebook user's identity.
# MAGIC
# MAGIC It then grants that PostgreSQL login membership in
# MAGIC `datacart_storefront_owner`. Database sessions assume the owner role using
# MAGIC `PGOPTIONS`, so the app can use and migrate the objects created in notebook 2.
# MAGIC This avoids needing a workspace SCIM group.
# MAGIC
# MAGIC ### B. Build and stage the application
# MAGIC
# MAGIC The build cell runs `npm ci` and the frontend build in driver-local temporary
# MAGIC storage. It stages the built React assets, FastAPI source, Python requirements,
# MAGIC Alembic files, and generated `app.yaml` runtime configuration.
# MAGIC
# MAGIC Only runtime files are uploaded. Local credentials, `.env` files, installed
# MAGIC `node_modules`, and notebook code are not included in the deployment source.
# MAGIC Database OAuth credentials are generated per new connection at runtime.
# MAGIC
# MAGIC ### C. Start compute and deploy a source snapshot
# MAGIC
# MAGIC Each deployment uploads to a fresh release directory, starts app compute if
# MAGIC needed, and deploys a snapshot. App startup applies outstanding Alembic
# MAGIC migrations before starting the web server.
# MAGIC
# MAGIC **Expected output:** frontend build messages followed by a result containing
# MAGIC `app`, `url`, `deployment_id`, and `source_code_path`. Previous source-release
# MAGIC directories remain available for inspection on reruns.

# COMMAND ----------

if not re.fullmatch("[a-z][a-z0-9-]{0,28}[a-z0-9]", APP_NAME):
    raise ValueError(
        "App name must be 2–30 lowercase letters, digits, or hyphens. Override APP_NAME with a shorter name if the default is too long."
    )
# Bind this app to the exact branch and database discovered above.
expected = apps.AppResource(
    name="postgres",
    postgres=apps.AppResourcePostgres(
        branch=target["branch"],
        database=target["database"],
        permission=apps.AppResourcePostgresPostgresPermission.CAN_CONNECT_AND_CREATE,
    ),
)
try:
    app = w.apps.get(name=APP_NAME)
    # An existing name is reusable only when it already points to this database.
    if not any(
        (
            r.name == "postgres"
            and r.postgres
            and (r.postgres.database == target["database"])
            and (r.postgres.branch == target["branch"])
            for r in app.resources or []
        )
    ):
        raise RuntimeError(
            "App name already exists with a different database. Choose a new app name."
        )
except NotFound:
    # Create the app identity without starting billable app compute yet.
    # Read .response: waiting for an ACTIVE app here would stall because compute is off.
    app = w.apps.create(
        app=apps.App(
            name=APP_NAME, description="DataCart Storefront", resources=[expected]
        ),
        no_compute=True,
    ).response
# App creation returns before its service principal is always populated. Poll the
# app resource, not the compute state: compute intentionally remains stopped here.
client_id = app.service_principal_client_id
identity_deadline = time.monotonic() + 300
if not client_id:
    print("Waiting for the app service principal to be provisioned...")
while not client_id:
    if time.monotonic() >= identity_deadline:
        raise TimeoutError(
            f"Service principal for {APP_NAME} was not provisioned within 5 minutes. "
            "Inspect the app in the workspace for a provisioning error."
        )
    time.sleep(2)
    try:
        app = w.apps.get(name=APP_NAME)
    except NotFound:
        # A new app can briefly be absent from reads while creation propagates.
        continue
    client_id = app.service_principal_client_id
print(f"App service principal ready: {client_id}")

# COMMAND ----------

# MAGIC %md
# MAGIC The creation cell waits up to five minutes for the service principal to become
# MAGIC available before proceeding; app compute stays stopped during this wait.
# MAGIC
# MAGIC ### Grant the app access to the storefront schema
# MAGIC
# MAGIC Use your database-administrator connection to ensure the app's OAuth login exists
# MAGIC and grant it membership in the shared schema-owner role.

# COMMAND ----------

# Authenticate this database session as the notebook's workspace user.
username = w.current_user.me().user_name
# Generate a fresh short-lived OAuth password for each connection attempt.
for attempt in range(4):
    credential = w.postgres.generate_database_credential(endpoint=target["endpoint"])
    try:
        conn = psycopg.connect(
            host=target["host"],
            dbname=target["database_name"],
            user=username,
            password=credential.token,
            sslmode="require",  # Encrypt the connection to Lakebase.
            connect_timeout=20,
            options="",  # Keep your administrator role for role creation/grants.
        )
        break
    except psycopg.OperationalError:
        if attempt == 3:
            raise
        # Allow a recently provisioned or sleeping endpoint time to become available.
        time.sleep(2**attempt)
# Do not leave the OAuth credential object in the notebook's global variables.
del credential
# The connection context commits on success, rolls back on error, and closes.
with conn:
    # The resource binding normally creates the app's OAuth login. Create it if needed.
    exists = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname=%s", (client_id,)
    ).fetchone()
    if not exists:
        w.postgres.create_role(
            parent=target["branch"],
            role_id=client_id,
            role=postgres.Role(
                spec=postgres.RoleRoleSpec(
                    postgres_role=client_id,
                    identity_type=postgres.RoleIdentityType.SERVICE_PRINCIPAL,
                    auth_method=postgres.RoleAuthMethod.LAKEBASE_OAUTH_V1,
                )
            ),
        ).wait()
    # Let app sessions assume the same schema owner that notebook 2 used.
    conn.execute(
        sql.SQL("GRANT {} TO {}").format(
            sql.Identifier(OWNER_ROLE), sql.Identifier(client_id)
        )
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ### Build the frontend and upload a source release
# MAGIC
# MAGIC Copy runtime files into temporary storage, check Node.js, install npm if missing,
# MAGIC build React, and write `app.yaml`. Upload the completed release to a fresh
# MAGIC workspace directory. Temporary local build files are removed after upload.

# COMMAND ----------

# Stage an explicit list of runtime files so local credentials, notebooks, and
# installed dependencies are not copied into the deployed app.
with tempfile.TemporaryDirectory(prefix="datacart-app-") as temporary:
    stage = Path(temporary)
    for name in ("app.py", "requirements.txt", "alembic.ini", "server", "migrations"):
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(
                source,
                stage / name,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(source, stage / name)
    # Build frontend assets in driver-local temporary storage, not the repository.
    with tempfile.TemporaryDirectory(prefix="datacart-frontend-") as temporary:
        build = Path(temporary)
        frontend = build / "frontend"
        shutil.copytree(
            ROOT / "frontend",
            frontend,
            ignore=shutil.ignore_patterns("node_modules", "dist"),
        )
        # Serverless compute can have Node.js without npm; verify Node first.
        node = shutil.which("node")
        if not node:
            raise RuntimeError(
                "Node.js 20+ is required. Install Node in your compute environment, then rerun notebook 3."
            )
        major = int(
            subprocess.check_output([node, "--version"], text=True)
            .strip()
            .lstrip("v")
            .split(".")[0]
        )
        if major < 20:
            raise RuntimeError("Node.js 20+ is required to build the frontend.")
        # Reuse installed npm, otherwise fetch the pinned package and verify its hash.
        if shutil.which("npm"):
            npm = [shutil.which("npm")]
        else:
            with urlopen(
                "https://registry.npmjs.org/npm/10.9.2", timeout=60
            ) as response:
                metadata = json.load(response)
            with urlopen(metadata["dist"]["tarball"], timeout=60) as response:
                content = response.read()
            # Compare the downloaded bytes with the registry's published integrity value.
            actual = (
                "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
            )
            if metadata["dist"]["integrity"] != actual:
                raise RuntimeError("npm archive integrity check failed.")
            archive = build / "npm.tgz"
            archive.write_bytes(content)
            with tarfile.open(archive) as package:
                # The data filter rejects unsafe archive paths and special files.
                package.extractall(build / "npm", filter="data")
            npm = [node, str(build / "npm/package/bin/npm-cli.js")]
        # npm ci uses the committed lockfile; build emits the production React assets.
        subprocess.run(
            [*npm, "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True
        )
        subprocess.run([*npm, "run", "build"], cwd=frontend, check=True)
        shutil.copytree(frontend / "dist", stage / "frontend/dist")
    # Generate runtime startup and database configuration without embedding tokens.
    # App sessions authenticate as the app's service principal and assume OWNER_ROLE.
    config = {
        "command": [
            "sh",
            "-c",
            'python -m alembic upgrade head && exec python -m uvicorn app:app --host 0.0.0.0 --port "${DATABRICKS_APP_PORT:-8000}"',
        ],
        "env": [
            {"name": key, "value": value}
            for key, value in {
                "ENDPOINT_NAME": target["endpoint"],
                "DB_SCHEMA": SCHEMA,
                "LAKEBASE_PG_ROLE": client_id,
                "PGOPTIONS": f"-c role={OWNER_ROLE}",
            }.items()
        ],
    }
    (stage / "app.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
    # A fresh release directory prevents files from an older build leaking into this one.
    path = SOURCE_PATH.rstrip("/") + "/" + uuid4().hex[:12]
    # Upload the staged runtime files and preserve their relative directory layout.
    for local in sorted(stage.rglob("*")):
        if local.is_file():
            remote = f"{path}/{local.relative_to(stage).as_posix()}"
            w.workspace.mkdirs(path=remote.rsplit("/", 1)[0])
            w.workspace.upload(
                path=remote,
                content=local.read_bytes(),
                format=workspace.ImportFormat.AUTO,
                overwrite=True,
            )

# COMMAND ----------

# MAGIC %md
# MAGIC ### Start app compute and deploy
# MAGIC
# MAGIC Start app compute if needed, deploy a snapshot of the uploaded release, and wait
# MAGIC for deployment completion. The app runs Alembic before starting its API.

# COMMAND ----------

app = w.apps.get(name=APP_NAME)
# Start compute only after the source and database permissions are ready.
if not app.compute_status or app.compute_status.state != apps.ComputeState.ACTIVE:
    w.apps.start(name=APP_NAME).result(timeout=timedelta(minutes=20))
# SNAPSHOT copies this release into an immutable deployment; wait for it to finish.
deployment = w.apps.deploy(
    app_name=APP_NAME,
    app_deployment=apps.AppDeployment(
        source_code_path=path, mode=apps.AppDeploymentMode.SNAPSHOT
    ),
).result(timeout=timedelta(minutes=20))
app = w.apps.get(name=APP_NAME)
print(
    json.dumps(
        {
            "app": app.name,
            "url": app.url,
            "deployment_id": deployment.deployment_id,
            "source_code_path": path,
        },
        indent=2,
    )
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 · Open your storefront
# MAGIC
# MAGIC Run the next cell to print the app URL and the database resource it uses.
# MAGIC Open the URL in a browser signed into your Databricks workspace. If the browser
# MAGIC asks you to sign in, complete workspace authentication and return to the app.
# MAGIC
# MAGIC The notebook does not make the app public or grant access to every workspace
# MAGIC user. Share access through the app's workspace permissions when appropriate.

# COMMAND ----------

# The workspace app URL requires Databricks authentication. Open the printed URL,
# verify products and Alice's account, then add a product and complete checkout.
# Access is not made public, and this notebook does not grant all workspace users access.
print("DataCart Storefront:", app.url)
print("Database:", target["database"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Exercise · Complete a purchase
# MAGIC
# MAGIC Work through the following checks in the browser:
# MAGIC
# MAGIC 1. Open **Shop** and confirm the catalog contains the 12 seeded products.
# MAGIC 2. Open a product to inspect its price and stock availability.
# MAGIC 3. Add one in-stock product to the cart and confirm the quantity and subtotal.
# MAGIC 4. Complete checkout. You should receive an order confirmation.
# MAGIC 5. Open **Orders** and confirm the new order appears alongside Alice's seeded history.
# MAGIC
# MAGIC For a quick application check, append `/api/health` to the app URL. It should
# MAGIC return a JSON response with `status` equal to `ok`. Product and order pages
# MAGIC provide the additional check that the app can read and write the database.
# MAGIC
# MAGIC **What to notice:** your app's service principal performs the database operations,
# MAGIC while the demo's shopper is still Alice, customer ID 1. Workspace authentication
# MAGIC and the storefront's demo customer are different concepts.
# MAGIC
# MAGIC ### Expected demo limitations
# MAGIC
# MAGIC Carts are held in backend memory and shared through the demo customer. A backend
# MAGIC restart or redeployment clears them. This workshop does not implement a
# MAGIC multi-user customer account system. Reviews, loyalty, promotions, and supplier
# MAGIC summaries remain optional; an empty supplier page is expected without its
# MAGIC external summary table.
# MAGIC
# MAGIC ## If a step fails
# MAGIC
# MAGIC | What you see | What to do |
# MAGIC | --- | --- |
# MAGIC | Database preflight fails | Run notebook 2 against the same project, branch, and database |
# MAGIC | App name already uses a different database | Choose a new app name; the cell will not replace that binding |
# MAGIC | Node.js 20+ is required | Use compute with Node.js 20+ available, then rerun deployment |
# MAGIC | `Exec format error` from Node | The installed Node binary cannot run on this compute. Use a compatible Node.js 20+ installation for its CPU architecture; the validation run succeeded on a platform retry |
# MAGIC | npm or Python package download fails | Check compute egress and registry configuration before retrying |
# MAGIC | Binding or role-grant permission error | Use an identity permitted to bind this database and grant the owner role |
# MAGIC | App URL requires sign-in or access is denied | Sign into the correct workspace and check the app's user permissions |
# MAGIC | Deployment fails after upload | Inspect the app's deployment logs in the workspace, fix the reported issue, and rerun the deployment cell |
# MAGIC
# MAGIC ## Optional · Continue with local development
# MAGIC
# MAGIC The app authenticates as its service principal; local development authenticates
# MAGIC as your own authorized PostgreSQL OAuth identity. In your **local repository**,
# MAGIC set the following in `lakebase.config`:
# MAGIC
# MAGIC ```bash
# MAGIC LAKEBASE_PROJECT_NAME="your-project-id"
# MAGIC LAKEBASE_PROFILE="your-local-cli-profile"
# MAGIC LAKEBASE_PG_ROLE="your-workspace-user@example.com"
# MAGIC LAKEBASE_PG_OPTIONS="-c role=datacart_storefront_owner"
# MAGIC ```
# MAGIC
# MAGIC Your local CLI profile supplies the workspace host and authentication. If needed,
# MAGIC configure it locally with `databricks auth login --host <workspace-url> --profile <profile>`.
# MAGIC Then run `make dev-lakebase`. It creates a development branch from the configured
# MAGIC parent, inherits the seeded data, and generates `.env.lakebase` for native local development.
# MAGIC The session-role option keeps local migration ownership consistent with the app.
# MAGIC
# MAGIC `make hooks-install` is optional: it automates development-branch provisioning
# MAGIC on Git checkouts while preserving existing hooks. It does not start local services.
# MAGIC
# MAGIC ## Finish · Review and clean up your workshop resources
# MAGIC
# MAGIC You now have a versioned, seeded PostgreSQL database and a deployed storefront
# MAGIC that uses it. Keep the project and app if you want to continue experimenting.
# MAGIC
# MAGIC Stopping notebook compute does **not** stop the app or Lakebase compute. When
# MAGIC finished, use **4_teardown_datacart** to preview and delete the app and dedicated
# MAGIC project, or manage them through the workspace UI. Review the selected resources
# MAGIC before deleting them.
