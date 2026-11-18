"""Native runtime checks use disposable Git checkouts and real in-memory PGlite.

Lakebase provisioning is mocked; these tests never need cloud credentials.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.request

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_dev", SOURCE / "scripts/dev.py")
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


class RuntimeUnitTests(unittest.TestCase):
    def test_local_environment_cannot_inherit_remote_database_or_app_identity(self):
        env = dev.clean_environment({
            "PGHOST": "remote", "PGOPTIONS": "-c role=owner", "PGSERVICE": "remote",
            "PGPASSFILE": "/secret", "PGSSLMODE": "require", "ENDPOINT_NAME": "remote",
            "DATABRICKS_APP_NAME": "deployed", "DB_MODE": "lakebase", "WEB_CONCURRENCY": "8",
            "PATH": "/bin", "PIP_INDEX_URL": "https://example.invalid/simple",
        })
        self.assertFalse(any(key.startswith("PG") for key in env))
        self.assertNotIn("ENDPOINT_NAME", env)
        self.assertNotIn("DATABRICKS_APP_NAME", env)
        self.assertNotIn("WEB_CONCURRENCY", env)
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["APP_RUNTIME"], "local")

    def test_lakebase_env_preserves_literal_options_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env.lakebase"
            path.write_text("PGHOST=lakebase.example\nPGUSER=test@example.invalid\nPGPASSWORD=test\n"
                            "ENDPOINT_NAME=projects/project/branches/test/endpoints/dev\n"
                            "PGOPTIONS=-c role=datacart_storefront_owner\nPGSSLMODE=disable\n")
            env = dev.lakebase_environment({"PGSERVICE": "wrong"}, path)
            self.assertEqual(env["PGOPTIONS"], "-c role=datacart_storefront_owner")
            self.assertEqual(env["PGSSLMODE"], "require")
            self.assertNotIn("PGSERVICE", env)

    def test_runtime_metadata(self):
        from server.runtime import get_runtime_info
        self.assertEqual(get_runtime_info({}), {
            "backend": "local", "database": {"location": "local-pglite", "project": None, "branch": None},
        })
        info = get_runtime_info({"DATABRICKS_APP_NAME": "app", "ENDPOINT_NAME": "projects/p/branches/b/endpoints/e"})
        self.assertEqual(info, {"backend": "production", "database": {"location": "lakebase", "project": "p", "branch": "b"}})

    def test_refresh_restarts_only_backend_and_returns_after_readiness(self):
        runtime = dev.Runtime()
        runtime.state = {"mode": "lakebase", "phase": "ready", "context": "12345678", "url": "http://127.0.0.1:12345"}
        old_backend = Mock()
        frontend, database = Mock(), Mock()
        runtime.services = {"backend": old_backend, "frontend": frontend, "pglite": database}
        runtime.context_id = Mock(return_value="12345678")
        runtime.write_state = Mock()
        runtime.provision = Mock()
        runtime.start_backend = Mock()
        runtime.refresh()
        runtime.provision.assert_called_once_with(refresh=True)
        old_backend.stop.assert_called_once()
        runtime.start_backend.assert_called_once()
        frontend.stop.assert_not_called()
        database.stop.assert_not_called()
        self.assertEqual(runtime.state["url"], "http://127.0.0.1:12345")
        self.assertEqual(runtime.state["phase"], "ready")

    def test_refresh_refuses_local_starting_or_changed_context(self):
        for mode, phase, actual_context in (("local", "ready", "old"), ("lakebase", "starting", "old"), ("lakebase", "ready", "new")):
            runtime = dev.Runtime()
            runtime.state = {"mode": mode, "phase": phase, "context": "old"}
            runtime.context_id = Mock(return_value=actual_context)
            runtime.provision = Mock()
            with self.assertRaises(dev.DevError):
                runtime.refresh()
            runtime.provision.assert_not_called()

    def test_refresh_configuration_and_failed_refresh_preserve_live_database(self):
        runtime = dev.Runtime()
        runtime.state = {"mode": "lakebase", "phase": "ready", "context": "12345678"}
        original = {"ENDPOINT_NAME": "projects/original/branches/dev/endpoints/e", "PGDATABASE": "db"}
        runtime.env = original.copy()
        runtime.context_id = Mock(return_value="12345678")
        runtime.write_state = Mock()
        runtime.services = {"backend": Mock()}
        runtime.provision = Mock()
        with self.assertRaises(dev.DevError):
            runtime.refresh({"LAKEBASE_PROJECT_NAME": "different"})
        runtime.provision.assert_not_called()

        def changed_database(**kwargs):
            self.assertEqual(kwargs["overrides"], {"LAKEBASE_PROFILE": "chosen-profile"})
            runtime.env = dict(original, PGDATABASE="other")
        runtime.provision.side_effect = changed_database
        with self.assertRaises(dev.DevError):
            runtime.refresh({"LAKEBASE_PROFILE": "chosen-profile"})
        runtime.services["backend"].stop.assert_not_called()
        self.assertEqual(runtime.env, original)
        self.assertEqual(runtime.state["phase"], "ready")

    def test_provision_reuses_hook_or_falls_back_and_never_selects_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / ".env.lakebase").write_text("PGHOST=remote\nPGUSER=user\nPGPASSWORD=test\nENDPOINT_NAME=projects/p/branches/b/endpoints/e\nPGOPTIONS=-c role=owner\n")
            for status in (0, 2):
                runtime = dev.Runtime(root)
                runtime.run_command = Mock(side_effect=[status, 0])
                runtime.provision()
                self.assertEqual(runtime.run_command.call_count, 1 if status == 0 else 2)
                self.assertEqual(runtime.env["PGOPTIONS"], "-c role=owner")
                self.assertNotIn("--profile", runtime.run_command.call_args.args[1])

    def test_stale_pid_reuse_never_signals_an_unrelated_process(self):
        with patch.object(dev, "fingerprint", return_value="different start"), patch.object(dev.os, "killpg") as kill:
            dev.stop_group(12345, "original start")
        kill.assert_not_called()


class NativeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (SOURCE / "scripts/dev/node_modules/@electric-sql/pglite").exists():
            raise RuntimeError("Install development dependencies first: python3 scripts/dev.py setup")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="datacart native ")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repo = self.base / "checkout"
        shutil.copytree(SOURCE, self.repo, ignore=shutil.ignore_patterns(
            ".git", ".venv", ".dev", ".isaac", "node_modules", "dist", "__pycache__",
            ".env", ".env.lakebase", "lakebase-branch-post-commit.log",
        ))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "LAKEBASE_", "DATABRICKS_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
                        PGHOST="remote.invalid", PGOPTIONS="-c role=wrong", PGSERVICE="wrong",
                        DATABRICKS_APP_NAME="must-not-be-inherited", ENDPOINT_NAME="projects/wrong/branches/wrong/endpoints/wrong")
        self.sessions = []
        self.addCleanup(self.stop_all)
        self.git("init", "-q")
        self.git("config", "user.name", "Native Test")
        self.git("config", "user.email", "native@example.invalid")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")
        self.link_dependencies(self.repo)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env, check=True,
                              capture_output=True, text=True)

    def link_dependencies(self, repo):
        for relative in (".venv", "frontend/node_modules", "scripts/dev/node_modules"):
            (repo / relative).symlink_to(SOURCE / relative, target_is_directory=True)
        (repo / ".dev").mkdir(exist_ok=True)
        shutil.copy2(SOURCE / ".dev/python-dependencies", repo / ".dev/python-dependencies")

    def command(self, command, root=None, check=True):
        return subprocess.run([sys.executable, "scripts/dev.py", command], cwd=root or self.repo,
                              env=self.env, text=True, capture_output=True, check=check, timeout=50)

    def state(self, root=None):
        return json.loads(((root or self.repo) / ".dev/session.json").read_text())

    def eventually(self, predicate, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = predicate()
                if result:
                    return result
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        self.fail("Timed out waiting for native runtime; inspect test session output")

    def start(self, root=None, mode="local", script="scripts/dev.py"):
        root = root or self.repo
        output = (root / ".dev/test-output.log").open("a")
        process = subprocess.Popen([sys.executable, script, mode], cwd=root,
                                   env=self.env, stdout=output, stderr=subprocess.STDOUT)
        self.sessions.append((root, process, output))
        def ready():
            if process.poll() is not None:
                self.fail((root / ".dev/test-output.log").read_text())
            state = self.state(root)
            return state if state.get("phase") == "ready" else None
        return process, self.eventually(ready, timeout=60)

    def stop_all(self):
        for root, process, output in reversed(self.sessions):
            try:
                self.command("down", root=root)
                process.wait(timeout=12)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                output.close()

    def request(self, state, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(state["url"] + path, data=data,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            text = response.read().decode()
            return json.loads(text) if "application/json" in response.headers.get("Content-Type", "") else text

    def database_env(self, state):
        env = dev.clean_environment(self.env)
        env.update(DB_MODE="pglite", PGHOST="127.0.0.1", PGPORT=str(state["database_port"]),
                   PGUSER="postgres", PGPASSWORD="postgres", PGDATABASE="postgres", PGSSLMODE="disable")
        return env

    def test_application_transactions_reload_hot_reload_and_reset(self):
        process, state = self.start()
        self.assertEqual(self.command("url").stdout.strip(), state["url"])
        self.assertEqual(self.request(state, "/api/dbtest")["product_count"], 0)
        with self.assertRaises(urllib.error.HTTPError) as missing_customer:
            self.request(state, "/api/account")
        self.assertEqual(missing_customer.exception.code, 500)
        self.assertEqual(self.request(state, "/api/runtime")["database"]["location"], "local-pglite")
        self.assertIn("Supplier Demand View", self.request(state, "/supplier"))
        self.assertIn("/@vite/client", self.request(state, "/"))
        self.assertNotEqual(self.command("local", check=False).returncode, 0)
        self.assertNotEqual(self.command("lakebase", check=False).returncode, 0)
        self.assertNotEqual(self.command("refresh", check=False).returncode, 0)

        # Exercise optional features with an expired cache on every inspection,
        # including inspections nested inside a route's active transaction.
        result = subprocess.run([sys.executable, "-c", APP_FIXTURE_CHECK], cwd=self.repo,
                                env=self.database_env(state), text=True, capture_output=True, timeout=35)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.request(state, "/api/shop/products?search=widget")["total"], 2)
        self.assertEqual(self.request(state, "/api/account")["name"], "Alice")
        self.request(state, "/api/cart/add", {"product_id": 1, "quantity": 1})

        backend_log = self.repo / ".dev" / state["context"] / "backend.log"
        old_count = backend_log.read_text().count("Started server process")
        with (self.repo / "app.py").open("a") as handle:
            handle.write("\n# Integration-test backend reload.\n")
        self.eventually(lambda: backend_log.read_text().count("Started server process") > old_count)
        self.eventually(lambda: self.request(state, "/api/health")["status"] == "ok")
        self.assertEqual(self.request(state, "/api/dbtest")["product_count"], 2)
        self.assertEqual(self.request(state, "/api/cart")["items"], [])
        self.assertEqual(self.state()["services"]["pglite"], state["services"]["pglite"])

        # Receive an actual Vite HMR notification over its browser WebSocket.
        from websockets.sync.client import connect
        client = self.request(state, "/@vite/client")
        token = re.search(r'const wsToken = "([^"]+)"', client).group(1)
        self.request(state, "/src/index.css")
        with connect(state["url"].replace("http:", "ws:") + "/?token=" + token,
                     subprotocols=["vite-hmr"], origin=state["url"]) as websocket:
            self.assertEqual(json.loads(websocket.recv(timeout=5))["type"], "connected")
            with (self.repo / "frontend/src/index.css").open("a") as handle:
                handle.write("\n/* native-hmr-test */\n")
            self.assertIn(json.loads(websocket.recv(timeout=8))["type"], {"update", "full-reload"})
        self.assertIn("native-hmr-test", self.request(state, "/src/index.css"))

        process.send_signal(signal.SIGINT)
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertFalse((self.repo / ".dev/session.json").exists())
        _, fresh = self.start()
        self.assertEqual(self.request(fresh, "/api/dbtest")["product_count"], 0)
        self.command("down")
        self.command("down")
        self.command("destroy")
        self.assertFalse((self.repo / ".dev" / fresh["context"]).exists())
        self.assertTrue((self.repo / ".venv").exists())
        self.assertNotEqual(self.command("url", check=False).returncode, 0)

    def test_simultaneous_worktrees_and_database_failure_cleanup(self):
        process, first = self.start()
        other = self.base / "other worktree"
        self.git("worktree", "add", "-qb", "parallel", str(other))
        self.link_dependencies(other)
        _, second = self.start(other)
        self.assertNotEqual(first["url"], second["url"])
        self.assertNotEqual(first["backend_url"], second["backend_url"])
        self.assertNotEqual(first["database_port"], second["database_port"])
        os.kill(first["services"]["pglite"]["pid"], signal.SIGKILL)
        self.assertNotEqual(process.wait(timeout=20), 0)
        self.assertFalse((self.repo / ".dev/session.json").exists())
        for service in first["services"].values():
            self.assertNotEqual(dev.fingerprint(service["pid"]), service["started"])
        self.assertEqual(self.request(second, "/api/health")["status"], "ok")

    def test_stale_supervisor_cleanup_and_failed_startup(self):
        process, state = self.start()
        process.kill()  # Simulate a supervisor that cannot run its finally block.
        process.wait(timeout=10)
        self.command("down")
        for service in state["services"].values():
            self.assertNotEqual(dev.fingerprint(service["pid"]), service["started"])
        self.assertFalse((self.repo / ".dev/session.json").exists())
        # A migration error must tear down the newly started database, too.
        (self.repo / "migrations/env.py").write_text("raise RuntimeError('intentional migration failure')\n")
        result = self.command("local", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("migrations failed", result.stderr)
        self.assertFalse((self.repo / ".dev/session.json").exists())

    def test_mocked_lakebase_refresh_keeps_frontend_and_waits_for_backend(self):
        # Supply a disposable PGlite endpoint as the mocked remote database.
        # Only the fixture subclass overrides TLS/pool mode for that transport;
        # production Lakebase env loading is separately asserted to require TLS.
        external_log = self.base / "external-pglite.log"
        database = dev.Service("fixture-db", ["node", self.repo / "scripts/dev/pglite.mjs"],
                               self.repo, dict(self.env, PGPORT="0"), external_log)
        self.addCleanup(database.stop)
        self.eventually(lambda: bool(database.ready))
        port = database.ready["port"]
        fixture_script = self.repo / "scripts/mock-lakebase-runtime.py"
        fixture_script.write_text('''from dev import Runtime, main
class MockLakebaseRuntime(Runtime):
    def provision(self, refresh=False, overrides=None):
        super().provision(refresh, overrides)
        assert self.env['PGOPTIONS'] == '-c role=postgres'
        self.env.update(PGSSLMODE='disable', DB_MODE='pglite')
import dev
dev.Runtime = MockLakebaseRuntime
raise SystemExit(main())
''')
        (self.repo / "scripts/lakebase-branch.sh").write_text(f'''#!/bin/bash
set -eu
printf '%s\\n' "${{1:-initial}}" >> .dev/provision-calls
if [[ "${{1:-}}" == '--wait-for-hook' ]]; then exit 2; fi
cat > .env.lakebase <<'ENV'
PGHOST=127.0.0.1
PGPORT={port}
PGUSER=postgres
PGPASSWORD=fixture-token
PGDATABASE=postgres
ENDPOINT_NAME=projects/mock/branches/mock/endpoints/dev
PGOPTIONS=-c role=postgres
ENV
chmod 600 .env.lakebase
''')
        process, before = self.start(mode="lakebase", script="scripts/mock-lakebase-runtime.py")
        self.command("refresh")
        after = self.state()
        self.assertEqual(after["phase"], "ready")
        self.assertEqual(after["url"], before["url"])
        self.assertEqual(after["services"]["frontend"], before["services"]["frontend"])
        self.assertNotEqual(after["services"]["backend"]["pid"], before["services"]["backend"]["pid"])
        self.assertEqual(self.request(after, "/api/dbtest")["db_connected"], True)
        self.assertEqual((self.repo / ".dev/provision-calls").read_text().splitlines(),
                         ["--wait-for-hook", "initial", "--refresh-only"])
        self.command("down")
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertIsNone(database.process.poll())  # Stopping local services cannot stop a remote endpoint.


APP_FIXTURE_CHECK = r'''
from decimal import Decimal
import psycopg
from server.db import pool
from server import schema_detector as schema
from server.routes import shop, account, orders, cart, supplier

schema.CACHE_TTL = 0
pool.open(wait=True)
try:
    with pool.connection() as conn:
        assert conn.execute('SELECT version_num FROM ecommerce.alembic_version').fetchone() == ('0001',)
        assert conn.execute('SELECT count(*) FROM ecommerce.products').fetchone() == (0,)
        conn.execute("INSERT INTO ecommerce.customers(id,name,email) VALUES(1,'Alice','alice@example.invalid')")
        conn.execute("INSERT INTO ecommerce.products(id,name,price,category) VALUES(1,'Widget',10.25,'Test'),(2,'Other',20,'Test')")
        conn.execute("INSERT INTO ecommerce.inventory(product_id,quantity) VALUES(1,10),(2,20)")
    with pool.connection() as conn:
        conn.execute("UPDATE ecommerce.inventory SET quantity=0 WHERE product_id=1")
        conn.rollback()
        assert conn.execute('SELECT quantity FROM ecommerce.inventory WHERE product_id=1').fetchone() == (10,)
        assert schema.column_exists('products', 'price', conn)
    assert len(shop.list_products(category='',search='WIDGET',limit=50,offset=0)['products']) == 1
    assert account.get_account()['name'] == 'Alice'
    cart.add_to_cart(cart.CartItem(product_id=1,quantity=2))
    assert cart.get_cart()['item_count'] == 2
    ids = orders.checkout()['order_ids']
    assert len(orders.get_orders()['orders']) == 1
    assert len(orders.get_order_detail(ids[0])['items']) == 1
    with pool.connection() as conn:
        assert conn.execute('SELECT quantity FROM ecommerce.inventory WHERE product_id=1').fetchone() == (8,)
        conn.execute('ALTER TABLE ecommerce.customers ADD COLUMN loyalty_points integer DEFAULT 0, ADD COLUMN email_verified boolean DEFAULT true')
        conn.execute("ALTER TABLE ecommerce.orders ADD COLUMN priority text DEFAULT 'normal'")
        conn.execute('CREATE TABLE ecommerce.reviews(product_id integer, customer_id integer, rating integer, comment text, review_date timestamp DEFAULT NOW())')
        conn.execute("INSERT INTO ecommerce.reviews VALUES(1,1,4,'Good',NOW()),(1,1,5,'Great',NOW())")
        conn.execute('ALTER TABLE ecommerce.reviews ADD COLUMN id serial PRIMARY KEY')
        conn.execute('CREATE TABLE ecommerce.loyalty_members(email text, tier text, total_earned integer, enrolled_at timestamp DEFAULT NOW())')
        conn.execute("INSERT INTO ecommerce.loyalty_members(email,tier,total_earned) VALUES('alice@example.invalid','gold',20)")
        conn.execute('CREATE TABLE ecommerce.exchange_rates(currency text, rate numeric)')
        conn.execute('CREATE TABLE ecommerce.promotions(product_id integer, badge_text text, discount_pct numeric, sale_price numeric, promo_type text, is_active boolean)')
        conn.execute("INSERT INTO ecommerce.promotions VALUES(1,'Sale',10,9,'sale',true)")
        conn.execute('CREATE TABLE ecommerce.product_demand(product_id integer, product_name text, category text, views integer, clicks integer, add_to_carts integer, cart_rate numeric, units_sold integer, revenue numeric, conversion_rate numeric, in_stock integer, reorder_level integer, restock_needed boolean)')
        conn.execute("INSERT INTO ecommerce.product_demand VALUES(1,'Widget','Test',10,5,2,20,2,20.5,20,8,10,true)")
    assert all(schema.get_features().values()), schema.get_features()
    for _ in range(7):
        assert shop.list_products(category='',search='widget',limit=50,offset=0)['products'][0]['review_count'] == 2
        assert shop.get_product(1)['product']['avg_rating'] == 4.5
        assert shop.featured_products()['promo_deals'][0]['badge_text'] == 'Sale'
        assert account.get_account()['loyalty_tier'] == 'gold'
        assert orders.get_order_detail(ids[0])['order']['priority'] == 'normal'
    cart.add_to_cart(cart.CartItem(product_id=1,quantity=1))
    assert cart.get_cart()['items'][0]['sale_price'] == 9
    assert orders.checkout()['points_earned'] == 10
    assert supplier.supplier_demand()['total_views'] == 10
    with pool.connection() as conn:
        conn.execute('DROP TABLE ecommerce.reviews')
    assert schema.get_features()['reviews_active'] is False
finally:
    pool.close()
print('PGlite application, cache expiry, and transaction checks passed')
'''


if __name__ == "__main__":
    unittest.main()
