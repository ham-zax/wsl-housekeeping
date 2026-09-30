"""Safety regressions using disposable repositories and caches only."""

import contextlib
import importlib.util
import io
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


class CacheSafety(unittest.TestCase):
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

    def test_package_manager_in_progress_preserves_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            cache = home / ".cache/uv"
            cache.mkdir(parents=True)
            with patch.dict(os.environ, {"XDG_CACHE_HOME": str(home / ".cache"), "UV_CACHE_DIR": str(cache)}), patch.object(hk, "tool", return_value=Path("/usr/bin/uv")), patch.object(hk, "process_snapshot", return_value=(set(), True)), patch.object(hk, "run") as run:
                self.assertEqual(hk.cleanup_caches(home, 7, True), 0)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
