"""Exercise hook installation with real Git repos, without cloud access."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]


class GitHooksTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="datacart hook tests ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            HOME=self.base.as_posix(), GIT_CONFIG_GLOBAL=str(self.base / "global.config"),
            GIT_CONFIG_NOSYSTEM="1",
        )
        self.git("init", "-q")
        self.git("config", "user.name", "Hook Test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "scripts").mkdir()
        for name in ("manage_git_hooks.py", "manage-post-checkout-hook.sh", "python.sh"):
            shutil.copy2(SOURCE / "scripts" / name, self.repo / "scripts" / name)
        self.write_hook(self.repo / ".githooks/post-checkout", 'printf "app:%s:%s\\n" "$(git rev-parse --show-toplevel)" "$*" >> "$HOME/events"\n')
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def run_command(self, args, cwd=None, **kwargs):
        return subprocess.run(args, cwd=cwd or self.repo, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)

    def git(self, *args, cwd=None, check=True, **kwargs):
        return self.run_command(["git", *args], cwd=cwd, check=check, **kwargs)

    def manage(self, command, cwd=None, check=True):
        cwd = cwd or self.repo
        return self.run_command([sys.executable, str(cwd / "scripts/manage_git_hooks.py"), command], cwd=cwd, check=check)

    def write_hook(self, path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n" + body, newline="\n")
        path.chmod(0o755)
        return path

    def events(self):
        path = self.base / "events"
        return path.read_text().splitlines() if path.exists() else []

    def test_default_hooks_restore_and_repeat(self):
        hook = self.write_hook(self.repo / ".git/hooks/post-checkout", 'echo existing >> "$HOME/events"\n')
        content = hook.read_bytes()
        self.manage("install")
        path = self.git("config", "--get", "core.hooksPath").stdout.strip()
        self.manage("install")
        self.git("switch", "-qc", "feature")
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.events()[0], "existing")
        self.assertTrue(self.events()[1].startswith("app:"))
        self.manage("uninstall")
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath", check=False).returncode, 1)
        self.assertFalse(Path(path).exists())
        self.assertEqual(hook.read_bytes(), content)

    def test_global_hooks_and_other_hook_input_exit_status(self):
        hooks = self.base / "shared hooks"
        self.git("config", "--global", "core.hooksPath", str(hooks))
        self.write_hook(hooks / "post-checkout", 'echo global >> "$HOME/events"\nexit 7\n')
        self.write_hook(hooks / "pre-push", 'printf "%s\\n" "$@" > "$HOME/args"\ncat > "$HOME/stdin"\nexit 9\n')
        self.manage("install")
        result = self.git("hook", "run", "post-checkout", "--", "old", "new", "1", check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.events()[0], "global")
        self.assertEqual(len(self.events()), 2)
        managed = Path(self.git("config", "--get", "core.hooksPath").stdout.strip())
        bash = "bash"
        if os.name == "nt":
            git_path = Path(shutil.which("git"))
            bash = str(next(p / "bin/bash.exe" for p in git_path.parents if (p / "bin/bash.exe").is_file()))
        result = self.run_command([bash, (managed / "pre-push").as_posix(), "origin", "url with spaces"], input="input line\n", check=False)
        self.assertEqual(result.returncode, 9)
        self.assertEqual((self.base / "stdin").read_text(), "input line\n")
        self.assertEqual((self.base / "args").read_text(), "origin\nurl with spaces\n")
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), str(hooks))
        self.assertTrue((hooks / "post-checkout").exists())

    def test_relative_path_and_linked_worktree(self):
        self.git("config", "core.hooksPath", ".existing-hooks")
        self.write_hook(self.repo / ".existing-hooks/post-checkout", 'echo main-existing >> "$HOME/events"\n')
        self.manage("install")
        other = self.base / "linked worktree"
        self.git("worktree", "add", "-qb", "linked", str(other))
        (self.base / "events").unlink(missing_ok=True)
        self.write_hook(other / ".existing-hooks/post-checkout", 'echo linked-existing >> "$HOME/events"\n')
        self.git("switch", "-qc", "linked-feature", cwd=other)
        self.assertEqual(self.events()[0], "linked-existing")
        self.assertIn(f"app:{other.as_posix()}:", self.events()[1])
        self.manage("uninstall", cwd=other)
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".existing-hooks")

    def test_worktree_specific_override(self):
        self.git("config", "extensions.worktreeConfig", "true")
        self.git("config", "--local", "core.hooksPath", ".shared")
        self.git("config", "--worktree", "core.hooksPath", ".specific")
        self.write_hook(self.repo / ".specific/post-checkout", 'echo specific >> "$HOME/events"\n')
        self.manage("install")
        self.git("hook", "run", "post-checkout", "--", "old", "new", "1")
        self.assertEqual(self.events()[0], "specific")
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--worktree", "--get", "core.hooksPath").stdout.strip(), ".specific")
        self.assertEqual(self.git("config", "--local", "--get", "core.hooksPath").stdout.strip(), ".shared")

    def test_existing_repo_hook_runs_once(self):
        self.git("config", "core.hooksPath", ".githooks")
        # Model the skip guard in the real repo hook for a legacy dispatch chain.
        hook = self.repo / ".githooks/post-checkout"
        hook.write_text(hook.read_text().replace('#!/bin/sh\n', '#!/bin/sh\n[ "${DATACART_SKIP_POST_CHECKOUT:-0}" != "1" ] || exit 0\n'), newline="\n")
        self.manage("install")
        self.git("hook", "run", "post-checkout", "--", "old", "new", "1")
        self.assertEqual(len(self.events()), 1)
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".githooks")

    def test_multiple_local_values_are_restored(self):
        self.git("config", "--add", "core.hooksPath", ".one")
        self.git("config", "--add", "core.hooksPath", ".two")
        self.manage("install")
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--get-all", "core.hooksPath").stdout.splitlines(), [".one", ".two"])

    def test_config_changes_and_modified_wrappers_are_preserved(self):
        self.manage("install")
        managed = Path(self.git("config", "--get", "core.hooksPath").stdout.strip())
        hook = managed / "post-checkout"
        hook.write_text(hook.read_text() + "# user change\n", newline="\n")
        self.assertNotEqual(self.manage("uninstall", check=False).returncode, 0)
        self.assertTrue(hook.exists())
        self.git("config", "core.hooksPath", ".new-choice")
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), ".new-choice")
        self.assertTrue(hook.exists())

    def test_disabled_hooks_and_nonexecutable_hook(self):
        self.git("config", "core.hooksPath", "/dev/null")
        self.manage("install")
        self.git("hook", "run", "post-checkout", "--", "old", "new", "1")
        self.assertEqual(len(self.events()), 1)
        self.manage("uninstall")
        self.assertEqual(self.git("config", "--get", "core.hooksPath").stdout.strip(), "/dev/null")
        self.git("config", "--unset", "core.hooksPath")
        hook = self.write_hook(self.repo / ".git/hooks/post-checkout", "exit 17\n")
        hook.chmod(0o644)
        self.manage("install")
        # Windows does not provide POSIX executable permission bits.
        if os.name != "nt":
            self.git("hook", "run", "post-checkout", "--", "old", "new", "1")

    def test_lakebase_failure_does_not_mask_original_status(self):
        self.write_hook(self.repo / ".githooks/post-checkout", "exit 21\n")
        self.manage("install")
        result = self.git("hook", "run", "post-checkout", "--", "old", "new", "1", check=False)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
