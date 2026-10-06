#!/usr/bin/python3
"""Conservative WSL disk housekeeping. No third-party dependencies."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tarfile
import time


PACKAGE_PROCESSES = {"uv", "uvx", "pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo", "rustc"}
GENERATED_PARTS = {"node_modules", "__pycache__", ".venv", ".pytest_cache", ".ruff_cache", ".import_linter_cache"}
GENERATED_DIRS = {"engine/target", "dist", "web/dist", "ui/dist", "packages/cli/dist", "packages/core/dist", "packages/mcp/dist"}
GIB = 2**30
ARTIFACT_NAMES = ("target", "node_modules", ".next", ".turbo", ".venv")
ARTIFACT_MANIFESTS = {
    "target": ("Cargo.lock", "Cargo.toml"),
    "node_modules": ("pnpm-lock.yaml", "package-lock.json", "yarn.lock", "bun.lock", "bun.lockb", "package.json"),
    ".next": ("package.json",),
    ".turbo": ("package.json",),
    ".venv": ("uv.lock", "poetry.lock", "Pipfile.lock", "pyproject.toml", "requirements.txt", "setup.py"),
}
ARCHIVABLE = ("uncommitted or untracked files", "local data or non-disposable ignored files",
              "commit not merged into the primary checkout", "commit has no other retained Git reference")
# Caches holding several independently used models or browser builds.
GRANULAR_CACHES = {"huggingface": "hub/*", "ms-playwright": "*", "puppeteer": "*/*", "Cypress": "*", "x-growth": "browser/run-*"}
# Caches cleaned by their own tool rule in cleanup_caches.
TOOL_CACHES = {"uv", "pip", "yarn"}
PROTECTED_TMP_PREFIXES = ("tmux-", "ssh-", "systemd-private-", "snap-private-tmp", ".X11-unix", ".ICE-unix", ".font-unix", ".Test-unix")


class DeletionLedger:
    """Record every completed deletion or cache action."""

    def __init__(self, state):
        self.used = 0
        self.path = state / f"deletions-{time.time_ns()}.jsonl"
        self.file = None

    def record(self, category, path, size):
        if self.file is None:
            self.file = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
        self.used += size
        self.file.write(json.dumps({"time": time.time(), "category": category, "path": str(path), "before_bytes": size}) + "\n")
        self.file.flush()

    def close(self):
        if self.file is not None:
            self.file.close()


def inside(path, root):
    return path == root or root in path.parents


def parent_matches(process, comm, args):
    """Match the parent's name and argument prefix; a vanished parent never matches."""
    try:
        parent_id = next(line.split()[1] for line in (process / "status").read_text().splitlines() if line.startswith("PPid:"))
        parent = process.parent / parent_id
        return (parent / "comm").read_text().strip() == comm and (parent / "cmdline").read_bytes().decode(errors="replace").split("\0")[:len(args)] == args
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return False


def supervisor(process, comm, args):
    manager = ["/usr/lib/systemd/systemd", "--user"]
    if comm == "systemd" and args[:2] == manager:
        return True
    if comm == "(sd-pam)" and args[0] == "(sd-pam)":
        return parent_matches(process, "systemd", manager)
    if comm == "ssh-agent" and args[0] == "/usr/bin/ssh-agent":
        # gcr-ssh-agent's ssh-agent marks itself non-dumpable; it only holds sockets.
        return parent_matches(process, "gcr-ssh-agent", ["/usr/libexec/gcr-ssh-agent"])
    return False


def inspect_process(process):
    """Return (absolute references, package manager running) for one process, or None if exempt."""
    comm = (process / "comm").read_text().strip()
    args = (process / "cmdline").read_bytes().decode(errors="replace").split("\0")
    # systemd --user, its PAM helper and gcr's ssh-agent are deliberately non-dumpable.
    # They supervise services from /; inspect their children separately.
    # Do not treat an opaque application process as this exception.
    if supervisor(process, comm, args):
        return None
    package_busy = comm in PACKAGE_PROCESSES or any(
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
    return {Path(ref) for ref in refs if ref.startswith("/")}, package_busy


def process_snapshot(proc_root=Path("/proc"), attempts=3, delay=0.5):
    """Keep paths referenced by live processes, including open file descriptors."""
    paths, package_busy = set(), False
    for process in proc_root.iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        # A process briefly hides its fds while executing a setuid/setgid program.
        for attempt in range(attempts):
            try:
                result = inspect_process(process)
                if result is not None:
                    paths |= result[0]
                    package_busy |= result[1]
                break
            except FileNotFoundError:
                break  # Process exited during the snapshot.
            except (PermissionError, ProcessLookupError):
                try:
                    own = process.stat().st_uid == os.getuid()
                except FileNotFoundError:
                    break
                if not own:
                    break
                if attempt + 1 == attempts:
                    try:
                        name = (process / "comm").read_text().strip()
                    except OSError:
                        name = "?"
                    raise RuntimeError(f"Cannot inspect own process {process.name} ({name}); refusing cleanup")
                time.sleep(delay)
    return paths, package_busy


def active(path, snapshot):
    return any(inside(ref, path) for ref in snapshot)


def referenced_ancestors(snapshot):
    return {ancestor for ref in snapshot for ancestor in (ref, *ref.parents)}


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


def archive_worktree(repo, path, head, archive_root, max_bytes):
    """Save HEAD as a ref plus tracked edits and non-generated untracked/ignored files.

    Returns the archive directory, or None when the local files exceed max_bytes.
    """
    extras = [item.rstrip("/") for item in filter(None, git(path, "ls-files", "--others", "--directory", "-z").split("\0")) if not generated(item)]
    size = sum(tree_usage(path / item)[0] for item in extras if (path / item).exists() or (path / item).is_symlink())
    if size > max_bytes:
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = "".join(char if char.isalnum() or char in "-_." else "_" for char in path.name)
    target = archive_root / f"{stamp}-{name}"
    target.mkdir(parents=True)
    ref = f"refs/housekeeping/{stamp}-{name}"
    git(repo, "update-ref", ref, head)
    branch = git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
    (target / "README").write_text(
        f"worktree: {path}\nrepository: {repo}\nhead: {head}\nbranch: {branch}\nref: {ref}\n\n"
        f"Restore: git -C {repo} worktree add {path} {ref}\n"
        f"         git -C {path} apply --binary {target / 'tracked.patch'}\n"
        f"         tar -xzf {target / 'untracked.tar.gz'} -C {path}\n")
    # Bytes: tracked files need not be UTF-8.
    diff = subprocess.run(["/usr/bin/git", "-C", str(path), "diff", "--binary", "HEAD"], env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}, capture_output=True, timeout=600)
    if diff.returncode:
        raise RuntimeError(f"git diff failed in {path}: {diff.stderr.decode(errors='replace').strip()}")
    (target / "tracked.patch").write_bytes(diff.stdout)

    def keep(info):
        return None if generated(info.name) else info

    with tarfile.open(target / "untracked.tar.gz", "w:gz") as archive:
        for item in extras:
            archive.add(path / item, arcname=item, filter=keep)
    return target


def cleanup_worktrees(roots, days, apply, ledger=None, archive_days=None, archive_root=None, archive_max_bytes=GIB):
    total = 0
    for discovered, common in repositories(roots):
        entries = worktrees(discovered)
        primary = Path(entries[0]["worktree"])
        missing = []
        for entry in entries[1:]:
            path = Path(entry["worktree"])
            reason = eligible(primary, common, entry, roots, days, time.time())
            if reason in ARCHIVABLE and archive_days and path.is_dir() and archive_root is not None:
                admin = admin_directory(common, path)
                snapshot, _ = process_snapshot()
                if time.time() - recent_worktree_time(path, admin) >= archive_days * 86400 and idle_checkout(path, archive_days, time.time()) and not active(path, snapshot):
                    size = tree_usage(path)[0] if ledger else 0
                    print(f"{'ARCHIVE AND REMOVE' if apply else 'WOULD ARCHIVE AND REMOVE'} worktree {str(path)!r}: {reason}", flush=True)
                    if apply:
                        saved = archive_worktree(primary, path, entry["HEAD"], archive_root, archive_max_bytes)
                        if saved is None:
                            print(f"KEEP worktree {str(path)!r}: local files exceed the archive limit")
                            continue
                        git(primary, "worktree", "remove", "--force", str(path))
                        if path.exists():
                            raise RuntimeError(f"Removal verification failed for {path}")
                        print(f"Archived to {saved}")
                        if ledger:
                            ledger.record("worktree-archived", path, size)
                    total += 1
                    continue
            if reason:
                print(f"KEEP worktree {str(path)!r}: {reason}")
                if not path.exists():
                    missing.append((entry, False))
                continue
            if not path.exists():
                missing.append((entry, True))
                continue
            size = 0
            if ledger:
                size, _, mounted, incomplete = tree_usage(path)
                if mounted or incomplete:
                    continue
            print(f"{'REMOVE' if apply else 'WOULD REMOVE'} worktree {str(path)!r}", flush=True)
            if apply:
                # Git rechecks tracked/untracked changes and locks; never force.
                git(primary, "worktree", "remove", str(path))
                if path.exists() or not retained(primary, entry["HEAD"]):
                    raise RuntimeError(f"Removal verification failed for {path}")
                if ledger:
                    ledger.record("worktree", path, size)
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


def tree_usage(path, best_effort=False):
    """Allocated bytes and newest mtime without following links or mounts."""
    first = path.lstat()
    size, newest, mounted, incomplete = first.st_blocks * 512, first.st_mtime, False, False
    seen = {(first.st_dev, first.st_ino)} if stat.S_ISREG(first.st_mode) else set()
    if not stat.S_ISDIR(first.st_mode):
        return size, newest, mounted, incomplete

    def failed(error):
        nonlocal incomplete
        if best_effort:
            incomplete = True
        else:
            raise error

    for root, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        for name in dirs + files:
            entry = Path(root) / name
            try:
                info = entry.lstat()
            except OSError:
                if not best_effort:
                    raise
                incomplete = True
                continue
            if info.st_dev != first.st_dev or (stat.S_ISDIR(info.st_mode) and os.path.ismount(entry)):
                mounted = True
                if name in dirs:
                    dirs.remove(name)
                continue
            newest = max(newest, info.st_mtime)
            inode = (info.st_dev, info.st_ino)
            if not stat.S_ISREG(info.st_mode) or inode not in seen:
                size += info.st_blocks * 512
                seen.add(inode)
    return size, newest, mounted, incomplete


def newest_time(path):
    return tree_usage(path)[1]


def remove_idle_tree(path, days, apply, ledger, category, label):
    """Remove one idle, unreferenced directory tree; return 1 when selected."""
    if not path.is_dir() or path.is_symlink() or time.time() - newest_time(path) < days * 86400:
        return 0
    size, _, mounted, incomplete = tree_usage(path, best_effort=True)
    if mounted or incomplete or active(path, process_snapshot()[0]):
        return 0
    print(f"{'REMOVE' if apply else 'WOULD REMOVE'} {label} {str(path)!r}: {size / GIB:.2f} GiB", flush=True)
    if apply:
        if time.time() - newest_time(path) < days * 86400 or active(path, process_snapshot()[0]):
            return 0
        shutil.rmtree(path)
        if ledger:
            ledger.record(category, path, size)
    return 1


def cleanup_idle_cache_dirs(cache, days, apply, ledger=None):
    """XDG caches are disposable: drop entries nobody wrote within the age limit.

    Atime is useless here because file indexers and scanners read every file daily."""
    if not cache.is_dir() or cache.is_symlink():
        return 0
    total = 0
    for child in sorted(cache.iterdir()):
        if child.name in TOOL_CACHES or child.is_symlink() or not child.is_dir():
            continue
        pattern = GRANULAR_CACHES.get(child.name)
        targets = sorted(child.glob(pattern)) if pattern else [child]
        for path in targets:
            if any(parent.is_symlink() for parent in (path, *path.parents) if inside(parent, child)):
                continue
            try:
                total += remove_idle_tree(path, days, apply, ledger, "idle-cache", "idle cache")
            except (FileNotFoundError, PermissionError) as error:
                print(f"KEEP idle cache {str(path)!r}: {error}")
    return total


def cleanup_codex_releases(home, days, apply, ledger=None):
    """Codex keeps every downloaded app-server release; keep current plus the newest other.

    Releases are immutable once unpacked, so age is irrelevant; the process snapshot protects running ones."""
    total = 0
    for package in sorted((home / ".codex/packages").glob("*")):
        releases, current = package / "releases", package / "current"
        if not releases.is_dir() or releases.is_symlink() or not current.is_symlink():
            continue
        live = Path(os.path.realpath(current))
        if live.parent != releases:
            continue
        others = sorted((path for path in releases.iterdir() if path.is_dir() and not path.is_symlink() and path != live), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in others[1:]:
            total += remove_idle_tree(path, 0, apply, ledger, "release", "old Codex release")
    return total


def idle_checkout(path, days, now):
    """Require old index, reflog, and tracked files; uncommitted but untouched edits are fine."""
    metadata = [path, Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "index").strip()),
                Path(git(path, "rev-parse", "--path-format=absolute", "--git-path", "logs/HEAD").strip())]
    if any(item.exists() and now - item.stat().st_mtime < days * 86400 for item in metadata):
        return False
    for name in filter(None, git(path, "ls-files", "-z").split("\0")):
        tracked = path / name
        if tracked.exists() and now - tracked.lstat().st_mtime < days * 86400:
            return False
    return True


def artifact_candidates(path, depth=3):
    """Yield known build/dependency directories next to a manifest that can recreate them."""
    for name in ARTIFACT_NAMES:
        candidate = path / name
        if candidate.is_dir() and not candidate.is_symlink() and any((path / manifest).is_file() for manifest in ARTIFACT_MANIFESTS[name]):
            yield candidate
    if depth <= 1:
        return
    try:
        children = sorted(path.iterdir())
    except OSError:
        return
    for child in children:
        if child.name.startswith(".") or child.name in ARTIFACT_NAMES or child.is_symlink() or not child.is_dir() or (child / ".git").exists():
            continue
        yield from artifact_candidates(child, depth - 1)


def cleanup_incremental(target, days, apply, ledger=None):
    total = 0
    candidates = set()
    for pattern in ("*/incremental", "*/*/incremental", "*/*/*/incremental"):
        candidates.update(target.glob(pattern))
    for path in sorted(candidates):
        if path.parent.name not in {"debug", "release"} or not path.is_dir():
            continue
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            continue
        size, newest, mounted, incomplete = tree_usage(path)
        if mounted or incomplete or time.time() - newest < days * 86400:
            continue
        snapshot, busy = process_snapshot()
        if busy or active(target, snapshot):
            continue
        print(f"{'REMOVE' if apply else 'WOULD REMOVE'} Rust incremental cache {str(path)!r}: {size / GIB:.2f} GiB", flush=True)
        if apply:
            fresh_size, newest, mounted, incomplete = tree_usage(path)
            snapshot, busy = process_snapshot()
            if busy or active(target, snapshot) or mounted or incomplete or time.time() - newest < days * 86400:
                continue
            shutil.rmtree(path)
            if ledger:
                ledger.record("incremental", path, fresh_size)
        total += 1
    return total


def cleanup_idle_artifacts(roots, days, apply, ledger=None, rust_days=2, rust_max_bytes=5 * GIB):
    total = 0
    now = time.time()
    for discovered, _ in repositories(roots):
        for entry in worktrees(discovered):
            path = Path(entry["worktree"])
            if "locked" in entry or "bare" in entry or not path.is_dir() or path.is_symlink() or not any(inside(path, root) for root in roots):
                continue
            idle = idle_checkout(path, days, now)
            snapshot, _ = process_snapshot()
            if active(path, snapshot):
                continue
            for candidate in artifact_candidates(path):
                relative = candidate.relative_to(path)
                if git(path, "ls-files", "-z", "--", str(relative)):
                    continue
                ignored = run(["/usr/bin/git", "-C", str(path), "check-ignore", "-q", "--", str(relative)])
                if ignored.returncode != 0:
                    continue
                size, newest, mounted, incomplete = tree_usage(candidate)
                if mounted or incomplete:
                    continue
                if not idle or now - newest < days * 86400:
                    if candidate.name == "target" and size > rust_max_bytes:
                        total += cleanup_incremental(candidate, rust_days, apply, ledger)
                    continue
                print(f"{'REMOVE' if apply else 'WOULD REMOVE'} idle artifact {str(candidate)!r}: {size / GIB:.2f} GiB", flush=True)
                if apply:
                    # Recheck activity and Git state immediately before removal.
                    fresh, _ = process_snapshot()
                    if active(path, fresh) or not idle_checkout(path, days, time.time()):
                        continue
                    _, newest, mounted, incomplete = tree_usage(candidate)
                    if mounted or incomplete or time.time() - newest < days * 86400:
                        continue
                    shutil.rmtree(candidate)
                    if ledger:
                        ledger.record("artifact", candidate, size)
                total += 1
    return total


def temp_entries(root, uid):
    for path in sorted(root.iterdir()):
        if path.name.startswith(PROTECTED_TMP_PREFIXES):
            continue
        if path.name == f"claude-{uid}" and path.is_dir() and not path.is_symlink() and path.lstat().st_uid == uid:
            yield from sorted(path.iterdir())
        else:
            yield path


def cleanup_tmp(root, days, max_bytes, apply, ledger=None):
    if not root.is_dir() or root.is_symlink():
        print(f"KEEP temp {str(root)!r}: unavailable or linked")
        return 0
    before, _, _, incomplete_before = tree_usage(root, best_effort=True)
    now = time.time()
    snapshot, _ = process_snapshot()
    referenced = referenced_ancestors(snapshot)
    eligible = []
    for path in temp_entries(root, os.getuid()):
        try:
            info = path.lstat()
            if info.st_uid != os.getuid() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            if path.is_symlink() or os.path.ismount(path) or path.parent.is_symlink():
                continue
            size, newest, mounted, incomplete = tree_usage(path)
            if mounted or incomplete or now - newest < days * 86400 or path in referenced:
                continue
            eligible.append((size, path, info.st_dev, info.st_ino))
        except FileNotFoundError:
            continue
        except PermissionError:
            print(f"KEEP temp {str(path)!r}: cannot inspect")
    eligible.sort(reverse=True)
    for size, path, _, _ in eligible[:20]:
        print(f"{'REMOVE' if apply else 'WOULD REMOVE'} temp {str(path)!r}: {size / GIB:.2f} GiB")
    if len(eligible) > 20:
        print(f"... and {len(eligible) - 20} smaller eligible temp entries")
    print(f"Temp candidates: {len(eligible)} entries, {sum(item[0] for item in eligible) / GIB:.2f} GiB")

    removed = 0
    if apply:
        checked_at = 0.0
        for size, path, device, inode in eligible:
            if time.monotonic() - checked_at > 5:
                snapshot, _ = process_snapshot()
                referenced = referenced_ancestors(snapshot)
                checked_at = time.monotonic()
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino) != (device, inode) or path.is_symlink() or path.parent.is_symlink() or path in referenced:
                    continue
                _, newest, mounted, incomplete = tree_usage(path)
                if mounted or incomplete or time.time() - newest < days * 86400:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    shutil.rmtree(path)
                elif stat.S_ISREG(info.st_mode):
                    path.unlink()
                removed += 1
                if ledger:
                    ledger.record("temp", path, size)
            except FileNotFoundError:
                continue
        print(f"Removed {removed} stale temp entries")
    current, _, _, incomplete = tree_usage(root, best_effort=True) if apply else (before, 0, False, incomplete_before)
    estimate = current if apply else max(0, current - sum(item[0] for item in eligible))
    qualifier = "at least " if incomplete else ""
    print(f"Temp allocated: {qualifier}{current / GIB:.2f} GiB; limit: {max_bytes / GIB:.0f} GiB")
    if estimate > max_bytes:
        print(f"WARNING: temp remains above the measured limit after eligible cleanup; recent, active, or protected entries need inspection")
    return removed if apply else len(eligible)


def tool(name, home):
    found = shutil.which(name)
    if found:
        return Path(found)
    options = [home / ".local/bin" / name, home / ".bun/bin" / name]
    def version(path):
        try:
            return tuple(int(part) for part in path.parent.parent.name.removeprefix("v").split("."))
        except ValueError:
            return ()
    options += sorted((home / ".nvm/versions/node").glob(f"*/bin/{name}"), key=version, reverse=True)
    return next((path for path in options if path.is_file() and os.access(path, os.X_OK)), None)


def cleanup_caches(home, days, apply, max_bytes=2 * GIB, ledger=None):
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache")))
    uv_cache = Path(os.environ.get("UV_CACHE_DIR", str(cache / "uv")))
    bun_cache = Path(os.environ.get("BUN_INSTALL_CACHE_DIR", str(Path(os.environ.get("BUN_INSTALL", str(home / ".bun"))) / "install/cache")))
    definitions = [
        ("uv", uv_cache, ["cache", "prune", "--cache-dir", str(uv_cache)], False, {}),
        ("pnpm", home / ".local/share/pnpm/store", ["store", "prune", "--store-dir", str(home / ".local/share/pnpm/store")], False, {}),
        ("pnpm", home / ".pnpm-store", ["store", "prune", "--store-dir", str(home / ".pnpm-store")], False, {}),
        ("pip", cache / "pip", ["cache", "purge"], True, {"PIP_CACHE_DIR": str(cache / "pip")}),
        ("npm", home / ".npm/_cacache", ["cache", "clean", "--force", "--cache", str(home / ".npm")], True, {}),
        ("yarn", cache / "yarn", ["cache", "clean", "--cache-folder", str(cache / "yarn")], True, {}),
        ("bun", bun_cache, [], True, {}),
    ]
    total = 0
    for name, path, args, stale_only, env in definitions:
        executable = tool(name, home)
        if executable is None or not path.is_dir() or path.is_symlink():
            continue
        if name == "bun" and (not path.is_absolute() or inside(home, path) or any(parent.is_symlink() for parent in path.parents)):
            print(f"KEEP cache {str(path)!r}: unsafe Bun cache root")
            continue
        size, newest, mounted, incomplete = tree_usage(path)
        if mounted or incomplete:
            print(f"KEEP cache {str(path)!r}: contains a mount or cannot be fully inspected")
            continue
        oversized = name != "pnpm" and size > max_bytes
        if stale_only and not oversized and time.time() - newest < days * 86400:
            print(f"KEEP cache {str(path)!r}: recently updated, {size / GIB:.2f} GiB")
            continue
        snapshot, package_busy = process_snapshot()
        if package_busy or active(path, snapshot):
            print(f"KEEP cache {str(path)!r}: package manager or cache currently in use")
            continue
        if name == "uv" and oversized:
            args = ["cache", "clean", "--cache-dir", str(path)]
        print(f"{'CLEAN' if apply else 'WOULD CLEAN'} cache {str(path)!r}: {size / GIB:.2f} GiB; {' '.join([name, *args])}", flush=True)
        if apply:
            if name == "bun":
                # bun pm cache rm can ignore --cache-dir and also clears bunx.
                # Remove only the directory inspected above.
                shutil.rmtree(path)
                if ledger:
                    ledger.record("cache", path, size)
                total += 1
                continue
            else:
                result = run([str(executable), *args], cwd=home, env=env)
            if result.returncode:
                raise RuntimeError(f"{name} cache cleanup failed: {result.stderr.strip()}")
            print((result.stdout + result.stderr).strip()[-1500:])
            if ledger:
                ledger.record("cache", path, size)
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
            size = tree_usage(path)[0] if ledger else 0
            print(f"{'REMOVE' if apply else 'WOULD REMOVE'} stale download cache {str(path)!r}", flush=True)
            if apply:
                shutil.rmtree(path)
                if ledger:
                    ledger.record("download", path, size)
            total += 1
    return total


def host_usage(home, drive=Path("/mnt/c")):
    """Observe Windows free space and allocated WSL VHD blocks when mounted."""
    if not drive.is_dir():
        return None
    free = shutil.disk_usage(drive).free
    override = os.environ.get("WSL_HOUSEKEEPING_VHDX")
    if override:
        candidates = [Path(override)]
    else:
        local = drive / "Users" / home.name / "AppData" / "Local"
        candidates = [*local.glob("wsl/*/ext4.vhdx"), *local.glob("Packages/*/LocalState/ext4.vhdx")]
    disks = [path for path in candidates if path.is_file() and not path.is_symlink()]
    allocated = sum(path.stat().st_blocks * 512 for path in disks) if disks else None
    return free, allocated, len(disks)


def print_host_usage(label, usage, vhd_budget):
    if usage is None:
        print(f"{label} Windows C: unavailable (not mounted at /mnt/c)")
        return
    free, allocated, count = usage
    vhd_text = f"{allocated / GIB:.2f} GiB allocated across {count} VHD(s)" if allocated is not None else "VHD not found"
    print(f"{label} Windows C: {free / GIB:.2f} GiB free; WSL {vhd_text}")
    if allocated is not None and allocated > vhd_budget:
        print(f"WARNING: WSL VHD allocation exceeds the {vhd_budget / GIB:.0f} GiB budget")


def report_large_caches(home):
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache")))
    if not cache.is_dir() or cache.is_symlink():
        return
    items = []
    for path in cache.iterdir():
        if path.name in {"uv", "pip", "yarn"} or path.is_symlink() or not path.is_dir():
            continue
        try:
            size, _, _, incomplete = tree_usage(path, best_effort=True)
        except OSError:
            continue
        if size >= 256 * 2**20:
            items.append((size, path, incomplete))
    print("Largest caches without an automatic deletion rule (sizes are not reclaim estimates):")
    for size, path, incomplete in sorted(items, reverse=True)[:10]:
        print(f"  {str(path)!r}: {'at least ' if incomplete else ''}{size / GIB:.2f} GiB")


def due_skip_reason(stamp, interval_hours, max_load, force_hours, now=None, loadavg=Path("/proc/loadavg")):
    """Return why a scheduled run should wait, or None when it should run now."""
    now = time.time() if now is None else now
    try:
        age = (now - stamp.stat().st_mtime) / 3600
    except FileNotFoundError:
        age = None
    if age is not None and age < interval_hours:
        return f"last successful run {age:.1f} h ago"
    load = float(loadavg.read_text().split()[1]) / (os.cpu_count() or 1)
    if load > max_load and (age is None or age < force_hours):
        return f"CPU busy (5-minute load {load:.2f} per CPU > {max_load})"
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="Preview stale WSL caches and safe worktrees; add --apply to clean. Keeps models, browsers, installed tools, user data and unmerged work.")
    parser.add_argument("--apply", action="store_true", help="perform cleanup (default: preview only)")
    parser.add_argument("--days", type=int, default=3, help="minimum worktree inactivity in days (default: 3)")
    parser.add_argument("--artifact-days", type=int, default=7, help="minimum checkout inactivity before removing manifest-backed build/dependency directories (default: 7)")
    parser.add_argument("--archive-days", type=int, default=3, help="archive then remove worktrees with local work idle this long (default: 3)")
    parser.add_argument("--archive-max-gib", type=int, default=1, help="keep a worktree whose non-generated local files exceed this size (default: 1)")
    parser.add_argument("--idle-cache-days", type=int, default=14, help="remove ~/.cache entries not read or written within this many days (default: 14)")
    parser.add_argument("--rust-cache-days", type=int, default=2, help="minimum incremental compiler cache inactivity (default: 2)")
    parser.add_argument("--rust-target-max-gib", type=int, default=5, help="prune idle incremental caches when a Rust target exceeds this size (default: 5)")
    parser.add_argument("--cache-days", type=int, default=2, help="keep download caches updated within this many days (default: 2)")
    parser.add_argument("--cache-max-gib", type=int, default=2, help="clean a package download cache above this size even when recently updated (default: 2)")
    parser.add_argument("--tmp-days", type=int, default=1, help="minimum temp inactivity in days (default: 1)")
    parser.add_argument("--tmp-max-gib", type=int, default=16, help="warn when /tmp stays above this size after safe cleanup (default: 16)")
    parser.add_argument("--vhd-budget-gib", type=int, default=150, help="warn when discovered WSL VHD allocation exceeds this size (default: 150)")
    parser.add_argument("--root", action="append", type=Path, help="repository/worktree root; repeat to replace ~/repo, ~/worktrees and ~/work")
    parser.add_argument("--skip-caches", action="store_true")
    parser.add_argument("--skip-worktrees", action="store_true")
    parser.add_argument("--skip-artifacts", action="store_true")
    parser.add_argument("--skip-tmp", action="store_true")
    parser.add_argument("--skip-idle-caches", action="store_true")
    parser.add_argument("--skip-archive", action="store_true", help="never remove worktrees that hold local work")
    parser.add_argument("--daily-when-idle", action="store_true", help="for the timer: run only if the last successful --apply is 20+ hours old and the CPU is idle")
    parser.add_argument("--max-load", type=float, default=0.25, help="with --daily-when-idle, maximum 5-minute load average per CPU (default: 0.25)")
    args = parser.parse_args(argv)
    if min(args.days, args.artifact_days, args.rust_cache_days, args.rust_target_max_gib, args.cache_days, args.tmp_days, args.cache_max_gib, args.tmp_max_gib, args.vhd_budget_gib, args.archive_days, args.archive_max_gib, args.idle_cache_days) < 1:
        parser.error("age and size limits must be at least one")
    if os.geteuid() == 0:
        parser.error("run as your normal WSL user, not root")
    home = Path.home()
    roots = [path.expanduser().absolute() for path in (args.root or [home / "repo", home / "worktrees", home / "work"])]
    state = Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state"))) / "wsl-housekeeping"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("SKIP: another housekeeping run is active")
            return 0
        stamp = state / "last-success"
        if args.daily_when_idle and (reason := due_skip_reason(stamp, 20, args.max_load, 72)):
            print(f"SKIP: {reason}")
            return 0
        ledger = DeletionLedger(state) if args.apply else None
        try:
            print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}; worktrees: {args.days}+ days; artifacts: {args.artifact_days}+ days; Rust incremental: {args.rust_cache_days}+ days in targets over {args.rust_target_max_gib} GiB; caches: {args.cache_days}+ days or over {args.cache_max_gib} GiB; idle caches: {args.idle_cache_days}+ days; archive worktrees with local work: {'off' if args.skip_archive else f'{args.archive_days}+ days'}; temp: {args.tmp_days}+ days", flush=True)
            host_before = host_usage(home)
            print_host_usage("Before:", host_before, args.vhd_budget_gib * GIB)
            before = shutil.disk_usage(home).free
            archive = None if args.skip_archive else args.archive_days
            trees = 0 if args.skip_worktrees else cleanup_worktrees(roots, args.days, args.apply, ledger, archive, state / "worktree-archives", args.archive_max_gib * GIB)
            artifacts = 0 if args.skip_artifacts else cleanup_idle_artifacts(roots, args.artifact_days, args.apply, ledger, args.rust_cache_days, args.rust_target_max_gib * GIB)
            caches = 0 if args.skip_caches else cleanup_caches(home, args.cache_days, args.apply, args.cache_max_gib * GIB, ledger)
            if not args.skip_caches:
                caches += cleanup_codex_releases(home, args.cache_days, args.apply, ledger)
            if not args.skip_idle_caches:
                caches += cleanup_idle_cache_dirs(Path(os.environ.get("XDG_CACHE_HOME", str(home / ".cache"))), args.idle_cache_days, args.apply, ledger)
            temp = 0 if args.skip_tmp else cleanup_tmp(Path("/tmp"), args.tmp_days, args.tmp_max_gib * GIB, args.apply, ledger)
            reclaimed = (shutil.disk_usage(home).free - before) / 2**30
            print(f"Done: {trees} worktrees, {artifacts} idle artifacts, {caches} cache actions, {temp} temp entries; Linux free-space change {reclaimed:+.2f} GiB")
            host_after = host_usage(home)
            print_host_usage("After:", host_after, args.vhd_budget_gib * GIB)
            if host_after is not None and host_after[1] is not None and host_after[1] > args.vhd_budget_gib * GIB:
                report_large_caches(home)
            if host_before is not None and host_after is not None:
                print(f"Observed C: free-space change {(host_after[0] - host_before[0]) / GIB:+.2f} GiB (includes other Windows activity)")
                if host_before[1] is not None and host_after[1] is not None:
                    print(f"Observed WSL VHD allocation change {(host_after[1] - host_before[1]) / GIB:+.2f} GiB")
            if ledger and ledger.file is not None:
                print(f"Deletion manifest: {ledger.path}; estimated selected size {ledger.used / GIB:.2f} GiB")
            if args.apply:
                stamp.touch()
            return 0
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        finally:
            if ledger:
                ledger.close()


if __name__ == "__main__":
    sys.exit(main())
