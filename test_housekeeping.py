"""Safety regressions using disposable repositories and caches only."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("housekeeping", Path(__file__).with_name("housekeeping.py"))
hk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hk)


class WorktreeSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "main"
        self.command("git", "init", "-q", str(self.repo))
        self.command("git", "-C", str(self.repo), "config", "user.name", "Fixture")
        self.command("git", "-C", str(self.repo), "config", "user.email", "fixture@example.invalid")
        (self.repo / "tracked").write_text("original\n")
        (self.repo / ".gitignore").write_text("node_modules/\ndata/\n")
        self.command("git", "-C", str(self.repo), "add", ".")
        self.command("git", "-C", str(self.repo), "commit", "-qm", "base")
        self.common = self.repo / ".git"

    def command(self, *args):
        return subprocess.run(args, capture_output=True, text=True, check=True).stdout

    def tree(self, name="old clean tree", branch=None):
        path = self.root / name
        args = ["git", "-C", str(self.repo), "worktree", "add", "-q"]
        args += ["-b", branch] if branch else ["--detach"]
        self.command(*args, str(path), "HEAD")
        return path

    def age(self, path):
        old = time.time() - 5 * 86400
        admin = hk.admin_directory(self.common, path)
        for entry in [path, path / ".git", admin, admin / "index", admin / "HEAD", admin / "logs/HEAD"]:
            if entry.exists():
                os.utime(entry, (old, old))

    def reason(self, path):
        entry = next(e for e in hk.worktrees(self.repo) if e["worktree"] == str(path))
        with patch.object(hk, "process_snapshot", return_value=(set(), False)):
            return hk.eligible(self.repo, self.common, entry, [self.root], 3, time.time())

    def clean(self, apply):
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            return hk.cleanup_worktrees([self.root], 3, apply)

    def test_preview_does_not_delete_and_apply_retains_branch(self):
        path = self.tree(branch="retained-feature")
        self.age(path)
        self.assertEqual(self.clean(False), 1)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.clean(True), 1)
        self.assertFalse(path.exists())
        self.command("git", "-C", str(self.repo), "show-ref", "--verify", "refs/heads/retained-feature")
        self.assertTrue((self.repo / "tracked").is_file())

    def test_dirty_tracked_work_is_preserved(self):
        path = self.tree()
        (path / "tracked").write_text("user edits\n")
        self.age(path)
        self.assertIn("uncommitted", self.reason(path))
        self.assertEqual(self.clean(True), 0)
        self.assertEqual((path / "tracked").read_text(), "user edits\n")

    def test_untracked_work_is_preserved(self):
        path = self.tree()
        (path / "notes").write_text("user notes")
        self.age(path)
        self.assertIn("untracked", self.reason(path))

    def test_ignored_data_is_preserved(self):
        path = self.tree()
        (path / "data").mkdir()
        (path / "data/tutor.db").write_text("learner state")
        self.age(path)
        self.assertIn("local data", self.reason(path))

    def test_ignored_dependencies_allow_removal(self):
        path = self.tree()
        (path / "node_modules").mkdir()
        (path / "node_modules/package").write_text("regenerable")
        self.age(path)
        self.assertIsNone(self.reason(path))
        self.assertEqual(self.clean(True), 1)

    def test_new_worktree_is_preserved(self):
        path = self.tree()
        self.assertIn("age limit", self.reason(path))

    def test_recent_index_activity_is_preserved(self):
        path = self.tree()
        self.age(path)
        (hk.admin_directory(self.common, path) / "index").touch()
        self.assertIn("age limit", self.reason(path))

    def test_active_process_reference_is_preserved(self):
        path = self.tree()
        self.age(path)
        entry = hk.worktrees(self.repo)[1]
        with patch.object(hk, "process_snapshot", return_value=({path / "tracked"}, False)):
            self.assertIn("live process", hk.eligible(self.repo, self.common, entry, [self.root], 3, time.time()))

    def test_unmerged_branch_is_preserved(self):
        path = self.tree(branch="unfinished")
        (path / "tracked").write_text("new committed work")
        self.command("git", "-C", str(path), "commit", "-qam", "unfinished")
        self.age(path)
        self.assertIn("not merged", self.reason(path))

    def test_locked_worktree_is_preserved(self):
        path = self.tree()
        self.age(path)
        self.command("git", "-C", str(self.repo), "worktree", "lock", str(path))
        self.assertIn("locked", self.reason(path))

    def test_prune_preserves_unreferenced_missing_commit(self):
        path = self.tree()
        (path / "tracked").write_text("detached unique commit")
        self.command("git", "-C", str(path), "commit", "-qam", "unique")
        self.age(path)
        import shutil
        shutil.rmtree(path)
        self.assertEqual(self.clean(True), 0)
        self.assertEqual(len(hk.worktrees(self.repo)), 2)

    def test_prune_removes_safe_missing_registration(self):
        path = self.tree()
        self.age(path)
        import shutil
        shutil.rmtree(path)
        self.clean(True)
        self.assertEqual(len(hk.worktrees(self.repo)), 1)

    def test_idle_lockfile_backed_artifact_is_removed_but_checkout_stays(self):
        (self.repo / "package-lock.json").write_text("{}")
        self.command("git", "-C", str(self.repo), "add", "package-lock.json")
        self.command("git", "-C", str(self.repo), "commit", "-qm", "lock")
        artifact = self.repo / "node_modules/package"
        artifact.parent.mkdir()
        artifact.write_text("downloaded")
        old = time.time() - 15 * 86400
        for path in [artifact, artifact.parent]:
            os.utime(path, (old, old))
        for name in filter(None, hk.git(self.repo, "ls-files", "-z").split("\0")):
            os.utime(self.repo / name, (old, old))
        for path in [self.repo, self.repo / ".git/index", self.repo / ".git/logs/HEAD"]:
            os.utime(path, (old, old))
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 14, False), 1)
            self.assertTrue(artifact.exists())
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 14, True), 1)
        self.assertFalse(artifact.exists())
        self.assertTrue((self.repo / "tracked").exists())

    def test_artifact_without_lockfile_is_preserved(self):
        artifact = self.repo / "node_modules/package"
        artifact.parent.mkdir()
        artifact.write_text("downloaded")
        old = time.time() - 15 * 86400
        for name in filter(None, hk.git(self.repo, "ls-files", "-z").split("\0")):
            os.utime(self.repo / name, (old, old))
        for path in [self.repo, self.repo / ".git/index", self.repo / ".git/logs/HEAD"]:
            os.utime(path, (old, old))
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 14, True), 0)
        self.assertTrue(artifact.exists())

    def test_idle_dirty_worktree_is_archived_then_removed(self):
        path = self.tree()
        (path / "tracked").write_text("user edits\n")
        (path / "notes").write_text("user notes")
        (path / "node_modules").mkdir()
        (path / "node_modules/package").write_text("regenerable")
        old = time.time() - 5 * 86400
        for entry in [path / "tracked", path / ".gitignore", path / "notes"]:
            os.utime(entry, (old, old))
        self.age(path)
        head = hk.git(path, "rev-parse", "HEAD").strip()
        archives = self.root / "archives"
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_worktrees([self.root], 3, False, archive_days=3, archive_root=archives), 1)
            self.assertTrue(path.is_dir())
            self.assertEqual(hk.cleanup_worktrees([self.root], 3, True, archive_days=3, archive_root=archives), 1)
        self.assertFalse(path.exists())
        [archive] = archives.iterdir()
        self.assertIn(b"user edits", (archive / "tracked.patch").read_bytes())
        import tarfile
        with tarfile.open(archive / "untracked.tar.gz") as saved:
            self.assertEqual(saved.getnames(), ["notes"])
        ref = f"refs/housekeeping/{archive.name}"
        self.assertEqual(hk.git(self.repo, "rev-parse", ref).strip(), head)

    def test_recently_edited_dirty_worktree_is_not_archived(self):
        path = self.tree()
        (path / "tracked").write_text("user edits\n")
        self.age(path)
        archives = self.root / "archives"
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_worktrees([self.root], 3, True, archive_days=3, archive_root=archives), 0)
        self.assertEqual((path / "tracked").read_text(), "user edits\n")
        self.assertFalse(archives.exists())

    def test_nested_manifest_only_venv_in_dirty_checkout_is_removed(self):
        project = self.repo / "services/api"
        project.mkdir(parents=True)
        (project / "pyproject.toml").write_text("[project]\nname = 'api'\n")
        (self.repo / ".gitignore").write_text(".venv/\n")
        self.command("git", "-C", str(self.repo), "add", ".")
        self.command("git", "-C", str(self.repo), "commit", "-qm", "api")
        (self.repo / "tracked").write_text("uncommitted user work")
        venv = project / ".venv/lib"
        venv.mkdir(parents=True)
        (venv / "module.py").write_text("installed")
        old = time.time() - 15 * 86400
        for path in [venv / "module.py", venv, venv.parent]:
            os.utime(path, (old, old))
        for name in filter(None, hk.git(self.repo, "ls-files", "-z").split("\0")):
            os.utime(self.repo / name, (old, old))
        for path in [self.repo, self.repo / ".git/index", self.repo / ".git/logs/HEAD"]:
            os.utime(path, (old, old))
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 7, True), 1)
        self.assertFalse((project / ".venv").exists())
        self.assertEqual((self.repo / "tracked").read_text(), "uncommitted user work")

    def test_oversized_rust_target_prunes_only_old_incremental_with_source_edits(self):
        (self.repo / ".gitignore").write_text("target/\n")
        (self.repo / "Cargo.lock").write_text("# fixture\n")
        self.command("git", "-C", str(self.repo), "add", ".")
        self.command("git", "-C", str(self.repo), "commit", "-qm", "rust")
        (self.repo / "tracked").write_text("uncommitted user work")
        incremental = self.repo / "target/debug/incremental"
        incremental.mkdir(parents=True)
        (incremental / "cache").write_bytes(b"x" * 8192)
        binary = self.repo / "target/debug/application"
        binary.write_text("keep compiled binary")
        old = time.time() - 3 * 86400
        for path in [incremental, incremental / "cache"]:
            os.utime(path, (old, old))
        with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 14, False, rust_max_bytes=1), 1)
            self.assertTrue(incremental.exists())
            self.assertEqual(hk.cleanup_idle_artifacts([self.root], 14, True, rust_max_bytes=1), 1)
        self.assertFalse(incremental.exists())
        self.assertTrue(binary.exists())
        self.assertEqual((self.repo / "tracked").read_text(), "uncommitted user work")

    def test_active_build_protects_incremental_cache(self):
        target = self.repo / "target"
        incremental = target / "debug/incremental"
        incremental.mkdir(parents=True)
        old = time.time() - 3 * 86400
        os.utime(incremental, (old, old))
        with patch.object(hk, "process_snapshot", return_value=(set(), True)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hk.cleanup_incremental(target, 2, True), 0)
        self.assertTrue(incremental.exists())


class CacheSafety(unittest.TestCase):
    def test_bun_removes_only_the_configured_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".bun/install/cache"
            cache.mkdir(parents=True)
            (cache / "package").write_bytes(b"x" * 8192)
            outside = home / "important"
            outside.mkdir()
            (outside / "file").write_text("keep")
            (cache / "linked").symlink_to(outside, target_is_directory=True)
            with patch.dict(os.environ, {"BUN_INSTALL_CACHE_DIR": str(cache)}), patch.object(hk, "tool", side_effect=lambda name, home: Path("/usr/bin/bun") if name == "bun" else None), patch.object(hk, "process_snapshot", return_value=(set(), False)), patch.object(hk, "run") as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_caches(home, 2, True, max_bytes=1), 1)
                run.assert_not_called()
            self.assertFalse(cache.exists())
            self.assertTrue((outside / "file").exists())

    def test_oversized_recent_uv_cache_uses_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".cache/uv"
            cache.mkdir(parents=True)
            (cache / "download").write_bytes(b"x" * 8192)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(home / ".cache"), "UV_CACHE_DIR": str(cache)}), patch.object(hk, "tool", return_value=Path("/usr/bin/uv")), patch.object(hk, "process_snapshot", return_value=(set(), False)), patch.object(hk, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_caches(home, 7, True, max_bytes=4096), 1)
            self.assertEqual(run.call_args.args[0][1:3], ["cache", "clean"])

    def test_stale_download_removal_keeps_recent_cache_and_external_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            parent = home / ".npm/_npx"
            old, recent, outside = parent / "old", parent / "recent", home / "important"
            for path in [old, recent, outside]:
                path.mkdir(parents=True)
                (path / "file").write_text("keep unless stale")
            (parent / "link").symlink_to(outside, target_is_directory=True)
            stale = time.time() - 10 * 86400
            for path in [old, old / "file"]:
                os.utime(path, (stale, stale))
            with patch.object(hk, "tool", return_value=None), patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_caches(home, 7, False), 1)
                self.assertTrue(old.exists())
                self.assertEqual(hk.cleanup_caches(home, 7, True), 1)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue((outside / "file").exists())

    def test_active_old_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = home / ".npm/_npx/old"
            path.mkdir(parents=True)
            stale = time.time() - 10 * 86400
            os.utime(path, (stale, stale))
            with patch.object(hk, "tool", return_value=None), patch.object(hk, "process_snapshot", return_value=({path / "live.js"}, False)):
                self.assertEqual(hk.cleanup_caches(home, 7, True), 0)
            self.assertTrue(path.exists())

    def test_opaque_application_blocks_cleanup_but_supervisors_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager, pam, app = root / "41", root / "42", root / "43"
            for path in [manager, pam, app]:
                path.mkdir()
                (path / "fd").mkdir()
            (manager / "comm").write_text("systemd")
            (manager / "cmdline").write_bytes(b"/usr/lib/systemd/systemd\0--user\0")
            (pam / "comm").write_text("(sd-pam)")
            (pam / "cmdline").write_bytes(b"(sd-pam)\0")
            (pam / "status").write_text("PPid:\t41\n")
            (app / "comm").write_text("application")
            (app / "cmdline").write_bytes(b"/usr/bin/application\0")
            with patch.object(hk.os, "readlink", side_effect=PermissionError):
                with self.assertRaisesRegex(RuntimeError, "Cannot inspect own process"):
                    hk.process_snapshot(root)
                import shutil
                shutil.rmtree(app)
                self.assertEqual(hk.process_snapshot(root), (set(), False))

    def test_gcr_ssh_agent_is_a_supervisor_but_a_lookalike_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gcr, agent = root / "51", root / "52"
            for path in [gcr, agent]:
                path.mkdir()
                (path / "fd").mkdir()
            (gcr / "comm").write_text("gcr-ssh-agent")
            (gcr / "cmdline").write_bytes(b"/usr/libexec/gcr-ssh-agent\0--base-dir\0/run/user/1000/gcr\0")
            (agent / "comm").write_text("ssh-agent")
            (agent / "cmdline").write_bytes(b"/usr/bin/ssh-agent\0-D\0")
            (agent / "status").write_text("PPid:\t51\n")
            def opaque_agent(entry):
                if "52" in {entry.parent.name, entry.parent.parent.name}:
                    raise PermissionError
                return "/"
            with patch.object(hk.os, "readlink", side_effect=opaque_agent):
                self.assertEqual(hk.process_snapshot(root)[0], {Path("/"), Path("/usr/libexec/gcr-ssh-agent"), Path("/run/user/1000/gcr")})
                # A vanished or differently named parent must not exempt the agent.
                for parent in ["1", "51"]:
                    (agent / "status").write_text(f"PPid:\t{parent}\n")
                    (gcr / "comm").write_text("bash")
                    with self.assertRaisesRegex(RuntimeError, "Cannot inspect own process 52"):
                        hk.process_snapshot(root)

    def test_briefly_opaque_process_is_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "61"
            (app / "fd").mkdir(parents=True)
            (app / "comm").write_text("sudo")
            (app / "cmdline").write_bytes(b"/usr/bin/sudo\0")
            with patch.object(hk.os, "readlink", side_effect=[PermissionError, "/work", "/usr/bin/sudo"]):
                self.assertEqual(hk.process_snapshot(root, delay=0)[0], {Path("/usr/bin/sudo"), Path("/work")})
            with patch.object(hk.os, "readlink", side_effect=PermissionError):
                with self.assertRaisesRegex(RuntimeError, r"own process 61 \(sudo\)"):
                    hk.process_snapshot(root, delay=0)

    def test_daily_when_idle_waits_for_interval_and_idle_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stamp, loadavg = root / "last-success", root / "loadavg"
            cpus = os.cpu_count() or 1
            loadavg.write_text(f"9 {cpus * 0.1} 9 1/1 1\n")
            self.assertIsNone(hk.due_skip_reason(stamp, 20, 0.25, 72, loadavg=loadavg))
            stamp.touch()
            now = stamp.stat().st_mtime
            self.assertIn("last successful run", hk.due_skip_reason(stamp, 20, 0.25, 72, now + 3600, loadavg))
            self.assertIsNone(hk.due_skip_reason(stamp, 20, 0.25, 72, now + 21 * 3600, loadavg))
            loadavg.write_text(f"0 {cpus * 0.5} 0 1/1 1\n")
            self.assertIn("CPU busy", hk.due_skip_reason(stamp, 20, 0.25, 72, now + 21 * 3600, loadavg))
            self.assertIsNone(hk.due_skip_reason(stamp, 20, 0.25, 72, now + 73 * 3600, loadavg))

    def test_idle_cache_uses_mtime_and_honours_granular_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            old = time.time() - 30 * 86400
            for name in ["stale/file", "fresh/file", "uv/file", "x-growth/browser/run-a/profile", "x-growth/browser/run-b/profile"]:
                (cache / name).parent.mkdir(parents=True, exist_ok=True)
                (cache / name).write_text("data")
            for name in ["stale/file", "stale", "uv/file", "uv", "x-growth/browser/run-a/profile", "x-growth/browser/run-a"]:
                os.utime(cache / name, (time.time(), old))
            with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_idle_cache_dirs(cache, 14, True), 2)
            self.assertFalse((cache / "stale").exists())
            self.assertFalse((cache / "x-growth/browser/run-a").exists())
            for name in ["fresh", "uv", "x-growth/browser/run-b"]:
                self.assertTrue((cache / name).exists(), name)

    def test_codex_releases_keep_current_newest_and_running(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            releases = home / ".codex/packages/daemon/releases"
            for index, name in enumerate(["1", "2", "3", "4", "5"]):
                (releases / name).mkdir(parents=True)
                (releases / name / "codex").write_text("binary")
                stamp = time.time() - (10 - index) * 86400
                os.utime(releases / name / "codex", (stamp, stamp))
                os.utime(releases / name, (stamp, stamp))
            (releases.parent / "current").symlink_to(releases / "3")
            with patch.object(hk, "process_snapshot", return_value=({releases / "1/codex"}, False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_codex_releases(home, 2, True), 2)
            self.assertEqual(sorted(path.name for path in releases.iterdir()), ["1", "3", "5"])

    def test_package_manager_in_progress_preserves_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".cache/uv"
            cache.mkdir(parents=True)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(home / ".cache"), "UV_CACHE_DIR": str(cache)}), patch.object(hk, "tool", return_value=Path("/usr/bin/uv")), patch.object(hk, "process_snapshot", return_value=(set(), True)), patch.object(hk, "run") as run:
                self.assertEqual(hk.cleanup_caches(home, 7, True), 0)
                run.assert_not_called()


class TempSafety(unittest.TestCase):
    def test_deletion_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp = root / "tmp"
            temp.mkdir()
            old = temp / "old"
            old.write_bytes(b"x" * 8192)
            stale = time.time() - 2 * 86400
            os.utime(old, (stale, stale))
            ledger = hk.DeletionLedger(root)
            with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_tmp(temp, 1, 16 * hk.GIB, True, ledger), 1)
            ledger.close()
            record = json.loads(ledger.path.read_text().strip())
            self.assertEqual((record["category"], record["path"]), ("temp", str(old)))
            self.assertFalse(old.exists())

    def test_stale_entries_and_nested_claude_scratch_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            temp = root / "tmp"
            temp.mkdir()
            old, recent, protected = temp / "old", temp / "recent", temp / "tmux-123"
            claude = temp / f"claude-{os.getuid()}"
            nested = claude / "old-project"
            outside = root / "important"
            for path in [old, recent, protected, nested, outside]:
                path.mkdir(parents=True)
                (path / "file").write_text("keep or remove")
            (temp / "linked").symlink_to(outside, target_is_directory=True)
            stale = time.time() - 2 * 86400
            for path in [old, old / "file", protected, protected / "file", nested, nested / "file"]:
                os.utime(path, (stale, stale))
            with patch.object(hk, "process_snapshot", return_value=(set(), False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_tmp(temp, 1, 16 * hk.GIB, False), 2)
                self.assertTrue(old.exists())
                self.assertEqual(hk.cleanup_tmp(temp, 1, 16 * hk.GIB, True), 2)
            self.assertFalse(old.exists())
            self.assertFalse(nested.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(protected.exists())
            self.assertTrue(outside.exists())
            self.assertTrue((temp / "linked").is_symlink())

    def test_live_reference_preserves_old_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old"
            old.mkdir()
            (old / "file").write_text("live")
            stale = time.time() - 2 * 86400
            for path in [old, old / "file"]:
                os.utime(path, (stale, stale))
            with patch.object(hk, "process_snapshot", return_value=({old / "file"}, False)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(hk.cleanup_tmp(root, 1, 16 * hk.GIB, True), 0)
            self.assertTrue((old / "file").exists())


class HostReport(unittest.TestCase):
    def test_only_wsl_budget_emits_a_threshold_warning(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            hk.print_host_usage("Before:", (1 * hk.GIB, 151 * hk.GIB, 1), 150 * hk.GIB)
        self.assertIn("Windows C: 1.00 GiB free", output.getvalue())
        self.assertIn("WSL VHD allocation exceeds", output.getvalue())
        self.assertNotIn("C: is below", output.getvalue())


if __name__ == "__main__":
    unittest.main()
