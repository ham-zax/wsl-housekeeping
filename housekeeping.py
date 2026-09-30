#!/usr/bin/python3
"""Conservative WSL disk housekeeping. No third-party dependencies."""

import argparse
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


PACKAGE_PROCESSES = {"uv", "uvx", "pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo", "rustc"}
GENERATED_PARTS = {"node_modules", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".import_linter_cache"}
GENERATED_DIRS = {"engine/target", "dist", "web/dist", "ui/dist", "packages/cli/dist", "packages/core/dist", "packages/mcp/dist"}


def inside(path, root):
    return path == root or root in path.parents


def supervisor(process, comm, args):
    manager = ["/usr/lib/systemd/systemd", "--user"]
    if comm == "systemd" and args[:2] == manager:
        return True
    if comm == "(sd-pam)" and args[0] == "(sd-pam)":
        parent_id = next(line.split()[1] for line in (process / "status").read_text().splitlines() if line.startswith("PPid:"))
        parent = process.parent / parent_id
        return (parent / "comm").read_text().strip() == "systemd" and (parent / "cmdline").read_bytes().decode(errors="replace").split("\0")[:2] == manager
    return False


def process_snapshot(proc_root=Path("/proc")):
    """Keep paths referenced by live processes, including open file descriptors."""
    paths, package_busy = set(), False
    for process in proc_root.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            comm = (process / "comm").read_text().strip()
            args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
            # systemd --user and its PAM helper are deliberately non-dumpable.
            # They supervise services from /; inspect their children separately.
            # Do not treat an opaque application process as this exception.
            if supervisor(process, comm, args):
                continue
            package_busy |= comm in PACKAGE_PROCESSES or any(
                Path(arg).name in {"npm-cli.js", "pnpm.cjs", "yarn.js", "pip", "pip3"} for arg in args
            )
            refs = [arg for arg in args if arg.startswith("/")]
            for entry in [process / "cwd", process / "exe", *(process / "fd").iterdir()]:
                try:
                    refs.append(os.readlink(entry).removesuffix(" (deleted)"))
                except FileNotFoundError:
                    continue
                except PermissionError:
                    if process.stat().st_uid == os.getuid():
                        raise
            paths.update(Path(ref) for ref in refs if ref.startswith("/"))
        except FileNotFoundError:
            continue  # Process exited during the snapshot.
        except (PermissionError, ProcessLookupError):
            if process.exists() and process.stat().st_uid == os.getuid():
                raise RuntimeError(f"Cannot inspect own process {process.name}; refusing cleanup")
    return paths, package_busy


def active(path, snapshot):
    return any(inside(ref, path) for ref in snapshot)


def run(command, cwd=None, env=None):
    command_env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", **(env or {})}
    # npm/pnpm/yarn use /usr/bin/env node, including under a user systemd timer.
    command_env["PATH"] = str(Path(command[0]).parent) + os.pathsep + command_env.get("PATH", os.defpath)
    return subprocess.run(command, cwd=cwd, env=command_env, capture_output=True, text=True, timeout=600)


def git(repo, *args):
    result = run(["/usr/bin/git", "-C", str(repo), *args])
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed in {repo}: {result.stderr.strip()}")
    return result.stdout


def worktrees(repo):
    entries = []
    for block in git(repo, "worktree", "list", "--porcelain", "-z").split("\0\0"):
        entry = {}
        for line in block.split("\0"):
            key, _, value = line.partition(" ")
            if key:
                entry[key] = value or True
        if "worktree" in entry:
            entries.append(entry)
    return entries


def repositories(roots):
    seen = set()
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        choices = [root] if (root / ".git").exists() else sorted(root.iterdir())
        for path in choices:
            if path.is_symlink() or not (path / ".git").exists():
                continue
            common = Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
            if common not in seen:
                seen.add(common)
                yield path, common


def admin_directory(common, path):
    directory = common / "worktrees"
    if directory.is_dir():
        for admin in directory.iterdir():
            marker = admin / "gitdir"
            if marker.is_file() and Path(marker.read_text().strip()).parent == path:
                return admin
    raise RuntimeError(f"Missing worktree metadata for {path}")


def recent_worktree_time(path, admin):
    # .git alone records creation, not recent commits/checkouts. Index and
    # HEAD reflog timestamps also protect recently used clean worktrees.
    entries = [path, path / ".git", admin, admin / "HEAD", admin / "index", admin / "logs/HEAD"]
    return max(entry.stat().st_mtime for entry in entries if entry.exists())


def generated(path):
    return bool(set(Path(path).parts) & GENERATED_PARTS) or path.endswith(".tsbuildinfo") or path.rstrip("/") in GENERATED_DIRS


def retained(repo, head):
    return bool(git(repo, "for-each-ref", "--contains=" + head, "--format=%(refname)", "refs/heads", "refs/tags", "refs/remotes").strip())


def eligible(repo, common, entry, roots, days, now):
    path = Path(entry["worktree"])
    if path.is_symlink() or not any(inside(path, root) for root in roots):
        return "outside configured roots or symlink"
    if "locked" in entry or "bare" in entry:
        return "locked or bare"
    if now - recent_worktree_time(path, admin_directory(common, path)) < days * 86400:
        return "used within the age limit"
    if not retained(repo, entry["HEAD"]):
        return "commit has no other retained Git reference"
    if not path.exists():
        return None if "prunable" in entry else "missing but not prunable"
    status = git(path, "status", "--porcelain=v1", "-z", "--ignored=matching", "--untracked-files=all").split("\0")
    for item in filter(None, status):
        if not item.startswith("!! "):
            return "uncommitted or untracked files"
        if not generated(item[3:]):
            return "local data or non-disposable ignored files"
    ancestor = run(["/usr/bin/git", "-C", str(repo), "merge-base", "--is-ancestor", entry["HEAD"], "HEAD"])
    if ancestor.returncode == 1:
        return "commit not merged into the primary checkout"
    if ancestor.returncode:
        raise RuntimeError(ancestor.stderr.strip())
    if git(path, "rev-parse", "HEAD").strip() != entry["HEAD"]:
        return "HEAD changed during inspection"
    snapshot, _ = process_snapshot()
    return "referenced by a live process" if active(path, snapshot) else None


def cleanup_worktrees(roots, days, apply):
    total = 0
    for discovered, common in repositories(roots):
        entries = worktrees(discovered)
        primary = Path(entries[0]["worktree"])
        missing = []
        for entry in entries[1:]:
            path = Path(entry["worktree"])
            reason = eligible(primary, common, entry, roots, days, time.time())
            if reason:
                print(f"KEEP worktree {str(path)!r}: {reason}")
                if not path.exists():
                    missing.append((entry, False))
                continue
            if not path.exists():
                missing.append((entry, True))
                continue
            print(f"{'REMOVE' if apply else 'WOULD REMOVE'} worktree {str(path)!r}", flush=True)
            if apply:
                # Git rechecks tracked/untracked changes and locks; never force.
                git(primary, "worktree", "remove", str(path))
                if path.exists() or not retained(primary, entry["HEAD"]):
                    raise RuntimeError(f"Removal verification failed for {path}")
            total += 1
        # prune affects every missing registration, so require ALL to qualify.
        if missing and all(safe for _, safe in missing):
            fresh = worktrees(primary)
            candidates = [entry for entry in fresh[1:] if not Path(entry["worktree"]).exists()]
            if all(eligible(primary, common, entry, roots, days, time.time()) is None for entry in candidates):
                print(f"{'PRUNE' if apply else 'WOULD PRUNE'} {len(candidates)} missing registrations in {str(primary)!r}")
                if apply:
                    git(primary, "worktree", "prune", "--expire", "now")
    return total


def newest_time(path):
    """No symlink traversal: external environments/models are never visited."""
    newest = path.lstat().st_mtime
    def failed(error):
        raise error
    for root, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        for name in dirs + files:
            newest = max(newest, (Path(root) / name).lstat().st_mtime)
    return newest


def tool(name, home):
    found = shutil.which(name)
    if found:
        return Path(found)
    options = [home / ".local/bin" / name]
    def version(path):
        try:
            return tuple(int(part) for part in path.parent.parent.name.removeprefix("v").split("."))
        except ValueError:
            return ()
    options += sorted((home / ".nvm/versions/node").glob(f"*/bin/{name}"), key=version, reverse=True)
    return next((path for path in options if path.is_file() and os.access(path, os.X_OK)), None)


def cleanup_caches(home, days, apply):
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache")))
    uv_cache = Path(os.environ.get("UV_CACHE_DIR", str(cache / "uv")))
    definitions = [
        ("uv", uv_cache, ["cache", "prune", "--cache-dir", str(uv_cache)], False, {}),
        ("pnpm", home / ".local/share/pnpm/store", ["store", "prune", "--store-dir", str(home / ".local/share/pnpm/store")], False, {}),
        ("pip", cache / "pip", ["cache", "purge"], True, {"PIP_CACHE_DIR": str(cache / "pip")}),
        ("npm", home / ".npm/_cacache", ["cache", "clean", "--force", "--cache", str(home / ".npm")], True, {}),
        ("yarn", cache / "yarn", ["cache", "clean", "--cache-folder", str(cache / "yarn")], True, {}),
    ]
    total = 0
    for name, path, args, stale_only, env in definitions:
        executable = tool(name, home)
        if executable is None or not path.is_dir() or path.is_symlink():
            continue
        if stale_only and time.time() - newest_time(path) < days * 86400:
            print(f"KEEP cache {str(path)!r}: recently updated")
            continue
        snapshot, package_busy = process_snapshot()
        if package_busy or active(path, snapshot):
            print(f"KEEP cache {str(path)!r}: package manager or cache currently in use")
            continue
        print(f"{'CLEAN' if apply else 'WOULD CLEAN'} cache {str(path)!r}: {' '.join([name, *args])}", flush=True)
        if apply:
            result = run([str(executable), *args], cwd=home, env=env)
            if result.returncode:
                raise RuntimeError(f"{name} cache cleanup failed: {result.stderr.strip()}")
            print((result.stdout + result.stderr).strip()[-1500:])
        total += 1
    for parent in [home / ".npm/_npx", home / ".nvm/.cache/bin"]:
        if not parent.is_dir() or parent.is_symlink():
            continue
        for path in sorted(parent.iterdir()):
            if not path.is_dir() or path.is_symlink() or time.time() - newest_time(path) < days * 86400:
                continue
            snapshot, package_busy = process_snapshot()
            if package_busy or active(path, snapshot):
                continue
            print(f"{'REMOVE' if apply else 'WOULD REMOVE'} stale download cache {str(path)!r}", flush=True)
            if apply:
                shutil.rmtree(path)
            total += 1
    return total


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview stale WSL caches and safe worktrees; add --apply to clean. Keeps models, browsers, installed tools, user data and unmerged work.")
    parser.add_argument("--apply", action="store_true", help="perform cleanup (default: preview only)")
    parser.add_argument("--days", type=int, default=3, help="minimum worktree inactivity in days (default: 3)")
    parser.add_argument("--cache-days", type=int, default=7, help="keep download caches updated within this many days (default: 7)")
    parser.add_argument("--root", action="append", type=Path, help="repository/worktree root; repeat to replace ~/repo and ~/worktrees")
    parser.add_argument("--skip-caches", action="store_true")
    parser.add_argument("--skip-worktrees", action="store_true")
    args = parser.parse_args(argv)
    if args.days < 1 or args.cache_days < 1:
        parser.error("age limits must be at least one day")
    if os.geteuid() == 0:
        parser.error("run as your normal WSL user, not root")
    home = Path.home()
    roots = [path.expanduser().absolute() for path in (args.root or [home / "repo", home / "worktrees"])]
    state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state"))) / "wsl-housekeeping"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("SKIP: another housekeeping run is active")
            return 0
        try:
            print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}; worktrees: {args.days}+ days; download caches: {args.cache_days}+ days", flush=True)
            before = shutil.disk_usage(home).free
            trees = 0 if args.skip_worktrees else cleanup_worktrees(roots, args.days, args.apply)
            caches = 0 if args.skip_caches else cleanup_caches(home, args.cache_days, args.apply)
            reclaimed = (shutil.disk_usage(home).free - before) / 2**30
            print(f"Done: {trees} worktrees, {caches} cache actions; filesystem free-space change {reclaimed:+.2f} GiB")
            return 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
