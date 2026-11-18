"""Native development supervisor for macOS, Linux, and Windows with Git Bash.

The supervisor owns each service tree and an authenticated loopback control socket.
Only the supervisor restarts the backend; a database exit always ends a session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

if os.name == "nt":
    import msvcrt
else:
    import fcntl

ROOT = Path(__file__).resolve().parents[1]
READY_PREFIX = "DATACART_READY "


class DevError(RuntimeError):
    pass


def fingerprint(pid: int) -> str:
    """Guard stale-state cleanup against PID reuse using process creation time."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return ""
            # GetProcessTimes also succeeds for a terminated, unreaped process.
            if times[1].dwLowDateTime or times[1].dwHighDateTime:
                return ""
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def stop_group(pid: int, started: str, timeout: float = 8, verify: bool = True) -> None:
    if verify and (not started or fingerprint(pid) != started):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        while fingerprint(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def lock_file(handle):
    """Acquire a nonblocking checkout lock, released when the handle closes."""
    if os.name == "nt":
        handle.seek(0)
        # Windows byte-range locks may extend beyond the end of the file.
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def venv_python(root):
    return Path(root) / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")


def service_command(command):
    """Resolve npm.cmd and Git Bash explicitly for native Windows subprocesses."""
    command = [str(arg) for arg in command]
    if command[0] == "bash" and os.name == "nt":
        # System32/bash.exe can be a WSL launcher. Prefer Bash shipped with Git.
        git = shutil.which("git")
        candidates = [] if not git else [Path(git).parent / "bash.exe",
            *(parent / "bin/bash.exe" for parent in Path(git).parents)]
        bash = next((str(p) for p in candidates if p.is_file()), None)
        if not bash:
            raise DevError("Git Bash is required; install Git for Windows and put Git on PATH.")
        command[0] = bash
        command[1:] = [arg.replace("\\", "/") for arg in command[1:]]
    elif command[0] == "npm":
        command[0] = shutil.which("npm") or "npm"
    return command


def clean_environment(source: dict[str, str]) -> dict[str, str]:
    # Inherited libpq service files/options must never redirect local startup.
    env = {k: v for k, v in source.items() if not k.startswith("PG")}
    for key in ("ENDPOINT_NAME", "LAKEBASE_PG_ROLE", "LAKEBASE_BRANCH_PATH",
                "DB_SOURCE", "DATABRICKS_APP_NAME", "DB_MODE", "WEB_CONCURRENCY"):
        env.pop(key, None)
    env.update(APP_RUNTIME="local", DB_SCHEMA="ecommerce", PYTHONUNBUFFERED="1")
    return env


def lakebase_environment(base: dict[str, str], path: Path) -> dict[str, str]:
    """Read the generator's literal KEY=value format, including spaces in PGOPTIONS."""
    env = clean_environment(base)
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not (key.startswith("PG") or key in {
            "ENDPOINT_NAME", "LAKEBASE_PG_ROLE", "LAKEBASE_BRANCH_PATH", "DB_SOURCE",
        }):
            raise DevError(f"Unexpected entry in {path.name}: {key}")
        env[key] = value
    if any(not env.get(key) for key in ("PGHOST", "PGUSER", "PGPASSWORD", "ENDPOINT_NAME")):
        raise DevError("Incomplete .env.lakebase; regenerate it with make dev-lakebase.")
    env.update(DB_MODE="lakebase", PGSSLMODE="require")
    return env


class Service:
    def __init__(self, name, command, root, env, log_path, pass_fds=()):
        self.name = name
        self.ready = {}
        self.log = log_path.open("a", buffering=1)
        if os.name == "nt":
            # Uvicorn sends console Ctrl+C during reload. A process group alone
            # cannot contain that event; give each service a hidden console.
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            process_options = {"creationflags": subprocess.CREATE_NEW_CONSOLE,
                               "startupinfo": startup}
        else:
            process_options = {"start_new_session": True, "pass_fds": pass_fds}
        try:
            self.process = subprocess.Popen(
                service_command(command), cwd=root, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                bufsize=1, encoding="utf-8", errors="replace",
                **process_options,
            )
        except OSError:
            self.log.close()
            raise
        self.started = fingerprint(self.process.pid)
        self.thread = threading.Thread(target=self.read_output, daemon=True)
        self.thread.start()

    def read_output(self):
        try:
            for line in self.process.stdout:
                self.log.write(line)
                print(f"[{self.name}] {line}", end="", flush=True)
                if line.startswith(READY_PREFIX):
                    self.ready = json.loads(line[len(READY_PREFIX):])
        finally:
            self.process.stdout.close()
            self.log.close()

    def metadata(self):
        return {"pid": self.process.pid, "started": self.started}

    def stop(self):
        # Reap the leader while waiting so an exited zombie doesn't add a delay.
        def reap():
            self.process.wait()
        reaper = threading.Thread(target=reap, daemon=True)
        reaper.start()
        stop_group(self.process.pid, self.started, verify=False)
        reaper.join(timeout=2)
        self.thread.join(timeout=2)


class Runtime:
    def __init__(self, root=ROOT):
        self.root = Path(root).resolve()
        self.directory = self.root / ".dev"
        self.state_path = self.directory / "session.json"
        self.services = {}
        self.stopping = False
        self.server = None
        self.backend_socket = None
        self.state = {}
        self.env = {}
        self.log_directory = None

    def lock(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        handle = (self.directory / "runner.lock").open("a")
        try:
            lock_file(handle)
        except OSError:
            handle.close()
            raise DevError("A development session is already running in this checkout; use make dev-down first.")
        return handle

    def read_state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def write_state(self):
        self.state["services"] = {name: service.metadata() for name, service in self.services.items()}
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, indent=2) + "\n")
        temporary.chmod(0o600)
        # Windows readers do not necessarily open with FILE_SHARE_DELETE.
        # Keep replacement atomic, retrying short sharing violations.
        deadline = time.monotonic() + 2
        while True:
            try:
                temporary.replace(self.state_path)
                break
            except PermissionError:
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def cleanup_stale(self):
        # Called only while holding the checkout lock; never signal unverified PIDs.
        for service in reversed(list(self.read_state().get("services", {}).values())):
            stop_group(service["pid"], service["started"], timeout=2)
        self.state_path.unlink(missing_ok=True)

    def request(self, action, timeout=900):
        state = self.read_state()
        request = {"action": action, "token": state.get("token")}
        if action == "refresh":
            request["environment"] = {
                key: value for key, value in os.environ.items()
                if key.startswith("LAKEBASE_") or key == "DATABRICKS_CONFIG_FILE"
            }
        with socket.socket() as connection:
            connection.settimeout(timeout)
            connection.connect(("127.0.0.1", state["control_port"]))
            connection.sendall((json.dumps(request) + "\n").encode())
            with connection.makefile("r") as stream:
                response = json.loads(stream.readline())
        if "error" in response:
            raise DevError(response["error"])
        return response

    def control(self):
        if self.server is None:
            return
        try:
            connection, _ = self.server.accept()
        except socket.timeout:
            return
        with connection:
            connection.settimeout(1)
            try:
                with connection.makefile("r") as stream:
                    line = stream.readline(8192)
                request = json.loads(line)
                if request.get("token") != self.state["token"]:
                    raise DevError("Development session identity changed; retry the command.")
                action = request.get("action")
                if action == "stop":
                    self.stopping = True
                elif action == "refresh":
                    self.refresh(request.get("environment"))
                elif action != "info":
                    raise DevError("Unknown development command.")
                response = {"state": self.state}
            except (DevError, ValueError, OSError) as error:
                response = {"error": str(error)}
            try:
                connection.sendall((json.dumps(response) + "\n").encode())
            except (BrokenPipeError, socket.timeout):
                pass

    def poll(self, ignored=()):
        if self.stopping:
            raise DevError("Development session stopped.")
        for name, service in self.services.items():
            if name not in ignored and service.process.poll() is not None:
                raise DevError(f"{name} exited unexpectedly; stopping this session. See {self.log_directory / (name + '.log')}")
        self.control()
        if self.server is None:
            time.sleep(0.05)
        if self.stopping:
            raise DevError("Development session stopped.")

    def start_service(self, name, command, env=None, cwd=None, pass_fds=()):
        service = Service(name, command, cwd or self.root, env or self.env,
                          self.log_directory / f"{name}.log", pass_fds)
        self.services[name] = service
        self.write_state()
        return service

    def run_command(self, name, command, env=None, cwd=None, accepted=(0,)):
        service = self.start_service(name, command, env, cwd)
        try:
            while service.process.poll() is None:
                self.poll(ignored=(name,))
            service.thread.join(timeout=2)
            if service.process.returncode not in accepted:
                raise DevError(f"{name} failed ({service.process.returncode}); see {self.log_directory / (name + '.log')}")
            return service.process.returncode
        finally:
            if service.process.poll() is not None:
                self.services.pop(name, None)
                self.write_state()

    def wait_for(self, condition, description, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.poll()
            if condition():
                return
        raise DevError(f"Timed out waiting for {description}; see {self.log_directory}")

    def context_id(self):
        return subprocess.check_output(
            service_command(["bash", self.root / "scripts/dev-context-id.sh"]), cwd=self.root, text=True,
        ).strip()

    def prepare_dependencies(self):
        # Checks/setup may run while a development session is active.
        with (self.directory / "dependencies.lock").open("a") as handle:
            while True:
                try:
                    lock_file(handle)
                    break
                except OSError:
                    self.poll()
            self._prepare_dependencies()

    def _prepare_dependencies(self):
        if sys.version_info < (3, 12):
            raise DevError("Python 3.12 or newer is required.")
        for tool in ("node", "npm", "git"):
            if not shutil.which(tool):
                raise DevError(f"{tool} is required for local development.")
        node_major = int(subprocess.check_output(["node", "-p", "process.versions.node.split('.')[0]"], text=True).strip())
        if node_major < 20:
            raise DevError("Node.js 20 or newer is required.")
        python = venv_python(self.root)
        if not python.exists():
            self.run_command("dependencies", [sys.executable, "-m", "venv", self.root / ".venv"])
        self.python = python
        version = subprocess.check_output([python, "-c", "import sys; print(sys.version)"], text=True)
        if tuple(map(int, version.split()[0].split(".")[:2])) < (3, 12):
            raise DevError("The existing .venv needs Python 3.12+; recreate it before starting.")
        signature = hashlib.sha256((version + (self.root / "requirements.txt").read_text()
                                    + (self.root / "pyproject.toml").read_text()).encode()).hexdigest()
        stamp = self.directory / "python-dependencies"
        imports_ok = subprocess.run(
            [python, "-c", "import alembic, fastapi, uvicorn, psycopg_pool, databricks.sdk, sqlalchemy"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0
        if not imports_ok or not stamp.exists() or stamp.read_text() != signature:
            self.run_command("dependencies", [python, "-m", "ensurepip", "--upgrade"])
            self.run_command("dependencies", [python, "-m", "pip", "install", "-r", "requirements.txt", "--disable-pip-version-check"])
            stamp.write_text(signature)
        for folder in (self.root / "scripts/dev", self.root / "frontend"):
            digest = hashlib.sha256((folder / "package.json").read_bytes() + (folder / "package-lock.json").read_bytes()).hexdigest()
            stamp = folder / "node_modules/.datacart-dependencies"
            manifest = json.loads((folder / "package.json").read_text())
            packages = manifest.get("dependencies", {}) | manifest.get("devDependencies", {})
            packages_present = all((folder / "node_modules" / package / "package.json").exists()
                                   for package in packages)
            if not packages_present or not stamp.exists() or stamp.read_text() != digest:
                command = ["npm", "ci", "--no-audit", "--no-fund"]
                if os.environ.get("NPM_REGISTRY"):
                    command += ["--registry", os.environ["NPM_REGISTRY"]]
                self.run_command("dependencies", command, cwd=folder)
                stamp.write_text(digest)

    def provision(self, refresh=False, overrides=None):
        script = self.root / "scripts/lakebase-branch.sh"
        # Use the user's environment/configuration, but never choose a CLI profile.
        configuration = dict(os.environ)
        configuration.update(overrides or {})
        if refresh:
            self.run_command("lakebase", ["bash", script, "--refresh-only"], env=configuration)
        else:
            status = self.run_command("lakebase", ["bash", script, "--wait-for-hook"],
                                      env=configuration, accepted=(0, 2))
            if status == 2:
                self.run_command("lakebase", ["bash", script], env=configuration)
        self.env = lakebase_environment(dict(os.environ), self.root / ".env.lakebase")

    def backend_ready(self):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.state["backend_url"] + "/api/dbtest", timeout=0.5) as response:
                data = json.load(response)
            return data.get("db_connected") is True and "product_count" in data
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def start_backend(self):
        if os.name == "nt":
            # Uvicorn's Windows reloader shares its own socket with spawned workers.
            # A POSIX descriptor inherited from this supervisor is not portable.
            port = self.state.get("backend_url", "").rsplit(":", 1)[-1] or "0"
            backend = self.start_service("backend", [
                self.python, self.root / "scripts/dev_backend.py", port,
            ])
            self.wait_for(lambda: bool(backend.ready), "backend socket")
            self.state["backend_url"] = backend.ready["url"]
            self.write_state()
            self.wait_for(self.backend_ready, "backend database readiness")
            return
        fd = self.backend_socket.fileno()
        self.start_service("backend", [
            self.python, "-m", "uvicorn", "app:app", "--fd", str(fd), "--reload",
            "--reload-exclude", "frontend/**", "--reload-exclude", "notebooks/**",
            "--reload-exclude", "tests/**", "--reload-exclude", "scripts/**",
        ], pass_fds=(fd,))
        self.wait_for(self.backend_ready, "backend database readiness")

    def refresh(self, overrides=None):
        if self.state.get("mode") != "lakebase" or self.state.get("phase") != "ready":
            raise DevError("Credential refresh requires a ready Lakebase session; start make dev-lakebase first.")
        if self.context_id() != self.state["context"]:
            raise DevError("Git context changed; stop and restart development before refreshing.")
        overrides = overrides or {}
        if not isinstance(overrides, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            or not (key.startswith("LAKEBASE_") or key == "DATABRICKS_CONFIG_FILE")
            for key, value in overrides.items()
        ):
            raise DevError("Invalid Lakebase refresh environment.")
        endpoint = self.env.get("ENDPOINT_NAME", "").split("/")
        if overrides.get("LAKEBASE_PROJECT_NAME") and (
            len(endpoint) < 2 or overrides["LAKEBASE_PROJECT_NAME"] != endpoint[1]
        ):
            raise DevError("Lakebase project changed; stop and restart development to change databases.")
        self.state["phase"] = "refreshing"
        self.write_state()
        backend_stopped = False
        previous_environment = self.env.copy()
        try:
            if overrides:
                self.provision(refresh=True, overrides=overrides)
            else:
                self.provision(refresh=True)
            if any(self.env.get(key) != previous_environment.get(key)
                   for key in ("ENDPOINT_NAME", "PGDATABASE")):
                raise DevError("Lakebase database changed; stop and restart development to change databases.")
            self.services.pop("backend").stop()
            backend_stopped = True
            self.start_backend()
        except Exception:
            if backend_stopped:
                self.stopping = True
            else:
                self.env = previous_environment
            raise
        finally:
            self.state["phase"] = "ready" if not self.stopping else "stopping"
            self.write_state()

    def run(self, mode):
        with self.lock():
            self.cleanup_stale()
            self.state = {"pid": os.getpid(), "mode": mode, "context": self.context_id(),
                          "phase": "starting", "token": secrets.token_hex(24)}
            self.log_directory = self.directory / self.state["context"]
            self.log_directory.mkdir(mode=0o700, exist_ok=True)
            self.env = clean_environment(dict(os.environ))
            for sig in (signal.SIGINT, signal.SIGTERM, *([signal.SIGBREAK] if os.name == "nt" else [])):
                signal.signal(sig, lambda *_: setattr(self, "stopping", True))
            self.server = socket.socket()
            self.server.bind(("127.0.0.1", 0))
            self.state["control_port"] = self.server.getsockname()[1]
            self.server.listen(4)
            self.server.settimeout(0.1)
            self.write_state()
            try:
                self.prepare_dependencies()
                if mode == "local":
                    self.env.update(DB_MODE="pglite", PGHOST="127.0.0.1", PGPORT="0",
                                    PGUSER="postgres", PGPASSWORD="postgres", PGDATABASE="postgres", PGSSLMODE="disable")
                    database = self.start_service("pglite", ["node", self.root / "scripts/dev/pglite.mjs"])
                    self.wait_for(lambda: bool(database.ready), "PGlite")
                    self.env["PGPORT"] = str(database.ready["port"])
                    self.state["database_port"] = int(self.env["PGPORT"])
                else:
                    self.provision()
                self.run_command("migrations", [self.python, "-m", "alembic", "upgrade", "head"])
                if os.name != "nt":
                    self.backend_socket = socket.socket()
                    self.backend_socket.bind(("127.0.0.1", 0))
                    self.backend_socket.listen(128)
                    self.state["backend_url"] = f"http://127.0.0.1:{self.backend_socket.getsockname()[1]}"
                self.start_backend()
                frontend_env = dict(self.env, DEV_BACKEND_URL=self.state["backend_url"])
                frontend = self.start_service("frontend", ["node", self.root / "frontend/dev.mjs"],
                                              env=frontend_env, cwd=self.root / "frontend")
                self.wait_for(lambda: bool(frontend.ready), "frontend")
                self.state.update(url=frontend.ready["url"], phase="ready")
                self.write_state()
                print(f"\nStorefront: {self.state['url']}\nLogs: {self.log_directory}\nPress Ctrl+C or run make dev-down to stop.\n", flush=True)
                while not self.stopping:
                    self.poll()
            except DevError:
                if not self.stopping:
                    raise
            finally:
                self.stopping = True
                self.state["phase"] = "stopping"
                self.write_state()
                for service in reversed(list(self.services.values())):
                    service.stop()
                self.services.clear()
                if self.backend_socket:
                    self.backend_socket.close()
                self.server.close()
                self.state_path.unlink(missing_ok=True)

    def check(self):
        self.command("setup")
        commands = [
            [venv_python(self.root), "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
            ["bash", "scripts/dev-context-id.sh", "--self-test"],
            ["bash", "scripts/lakebase-branch.sh", "--self-test"],
        ]
        for command in commands:
            subprocess.run(service_command(command), cwd=self.root, check=True)
        subprocess.run(service_command(["npm", "run", "build"]), cwd=self.root / "frontend", check=True)

    def command(self, action):
        if action == "check":
            self.check()
            return
        if action == "setup":
            self.directory.mkdir(mode=0o700, exist_ok=True)
            self.log_directory = self.directory / "setup"
            self.log_directory.mkdir(mode=0o700, exist_ok=True)
            self.state_path = self.directory / f"setup-{os.getpid()}.json"
            self.env = clean_environment(dict(os.environ))
            try:
                self.prepare_dependencies()
            finally:
                for service in self.services.values():
                    service.stop()
                self.state_path.unlink(missing_ok=True)
            return
        if action in {"local", "lakebase"}:
            self.run(action)
            return
        try:
            operation = "stop" if action in {"down", "destroy"} else "info" if action == "url" else action
            response = self.request(operation)
        except (OSError, ValueError, KeyError):
            # Never treat an unresponsive but locked runner as stale.
            with self.lock():
                previous = self.read_state()
                self.cleanup_stale()
            if action not in {"down", "destroy"}:
                raise DevError("Development is not running; start make dev-local or make dev-lakebase.")
            response = {"state": previous}
        if action in {"down", "destroy"}:
            # Hold the lock through deletion to prevent racing a new startup.
            deadline = time.monotonic() + 35
            while True:
                try:
                    handle = self.lock()
                    break
                except DevError:
                    if time.monotonic() > deadline:
                        raise DevError("Timed out stopping development; inspect its terminal and logs.")
                    time.sleep(0.1)
            with handle:
                if action == "destroy":
                    for folder in self.directory.iterdir():
                        if folder.is_dir() and len(folder.name) == 8 and all(c in "0123456789abcdef" for c in folder.name):
                            shutil.rmtree(folder)
            print("Development stopped." if action == "down" else "Development stopped; saved session logs and metadata removed.")
        elif action == "url":
            state = response["state"]
            if state.get("phase") != "ready":
                raise DevError("Frontend is not ready yet; inspect the development terminal.")
            print(state["url"])
        elif action == "refresh":
            print("Lakebase credentials refreshed; backend is ready.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("local", "lakebase", "url", "refresh", "down", "destroy", "setup", "check"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        Runtime().command(args.command)
    except (DevError, OSError, subprocess.SubprocessError) as error:
        print(f"Development error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
