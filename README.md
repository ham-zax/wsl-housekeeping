# WSL housekeeping

Conservative disk housekeeping and idle RAM-cache reclamation for WSL 2,
plus a Windows housekeeping companion. The two Python scripts and Windows
script have fixture-based safety tests.

Requires Linux Python 3.10+, Git, systemd, and Linux memory pressure statistics
at `/proc/pressure/memory`. Package managers are optional: missing tools are
skipped. The RAM reclaimer requires root; disk housekeeping runs as your normal
user. Enable systemd in WSL before installing the timers.

## Disk housekeeping

Preview candidates without deleting anything:

```bash
python3 housekeeping.py
```

Apply cleanup:

```bash
python3 housekeeping.py --apply
```

The default repository roots are `~/repo` and `~/worktrees`. The script discovers
Git repositories directly inside those roots, then inspects their registered
worktrees. Specify other roots with repeated `--root` arguments:

```bash
python3 housekeeping.py --root ~/projects --root ~/branches
python3 housekeeping.py --days 7 --cache-days 14
```

A worktree qualifies only when all of these conditions hold:

- It has been inactive for at least three days. Creation, index, directory, and
  HEAD reflog timestamps contribute to the activity check.
- It is inside a configured root, is not the primary checkout, and is not locked.
- It has no staged, unstaged, or untracked files.
- Its commit is already an ancestor of the primary checkout's current HEAD and
  remains reachable through another branch, tag, or remote-tracking reference.
- Its ignored files consist only of recognized disposable dependencies or build
  outputs. Local databases, reports, evidence, and other ignored data keep the
  worktree protected.
- No inspected live process references it through its command, working
  directory, executable, or open files.

Branches and commits are retained. Git removal is never forced. Missing
registrations are pruned only when every missing registration in that repository
passes the age, scope, lock, and retained-commit checks.

Cache cleanup uses `uv cache prune` and `pnpm store prune` to remove dangling or
unreferenced entries. Pip, npm, and Yarn download caches are cleared only when
their contents have not been updated for seven days. Individual old npx
environments and Node download archives use the same seven-day cutoff.

The script keeps recently updated download caches, installed tools and Python
runtimes, model files, browser binaries, and user data. It skips cache operations
while a package manager is running or a process references the cache. It does
not delete source files, uninstall packages, clean Rust builds, or clear the
entire `~/.cache` directory.

Concurrent runs are prevented with a per-user lock. An inability to inspect an
application process or verify Git state stops cleanup rather than assuming it
is safe. The systemd user supervisor and its PAM helper are recognized as
supervisors; their application children are still inspected.

### Daily automatic cleanup

```bash
git clone https://github.com/ham-zax/wsl-housekeeping.git
cd wsl-housekeeping
./install.sh
```

This installs `~/.local/bin/wsl-housekeeping` and enables a user timer that runs
once daily, shortly after midnight with up to 30 minutes of randomized delay.
Missed runs are caught up when the user manager starts. WSL must be running for
the timer to execute. If the user manager needs to remain active after logout,
enable lingering for your user with `sudo loginctl enable-linger "$USER"`.
Ensure `~/.local/bin` is in your shell's `PATH` to use the short command below;
the timer invokes the installed command by its full path.

```bash
wsl-housekeeping                  # preview
wsl-housekeeping --apply          # run now
systemctl --user list-timers wsl-housekeeping.timer
journalctl --user -u wsl-housekeeping.service -n 50
systemctl --user disable --now wsl-housekeeping.timer
```

## RAM-cache reclamation

```bash
python3 ram_reclaim.py --dry-run
sudo ./install-ram-reclaim.sh
```

The system timer checks after boot and then about every 15 minutes after the
previous run finishes. Reclamation requires all of these conditions:

- At least 1,024 MiB of disposable cache, estimated as
  `Cached + Buffers - Shmem` to exclude tmpfs/shared memory.
- One-minute load at or below 0.5, before and after sampling.
- Aggregate CPU activity at or below 15% and each individual CPU core at or
  below 20% during a ten-second sample. I/O wait counts as activity.
- Low memory pressure and no more than 16 MiB of dirty/writeback memory.

Conditions are checked again immediately before reclamation. Only
`/proc/sys/vm/drop_caches` is written, with value `1`, to discard clean page cache.
The script does not force a global filesystem sync or memory compaction.

The per-core check protects a single-threaded job that would be hidden by a
machine-wide CPU average. The longer interval avoids repeatedly evicting useful
cache every minute. It cannot release memory held by processes or repair a
process leak. Linux does not provide an atomic activity-check-and-drop operation;
a workload can start just after the final checks.

Environment overrides: `MIN_CACHE_MB`, `MAX_CPU_PCT`, `MAX_CORE_CPU_PCT`,
`MAX_LOAD_AVG`, `SAMPLE_SEC`, and `DRY_RUN=1`. To tune the system service, use
`sudo systemctl edit wsl-reclaim.service` and add `Environment=` settings under
`[Service]`, then reload systemd. Invalid settings or unreadable/malformed
required kernel statistics produce a nonzero error without dropping cache.

```bash
/usr/local/sbin/wsl-reclaim --dry-run
systemctl list-timers wsl-reclaim.timer
journalctl -u wsl-reclaim.service -n 50
sudo systemctl disable --now wsl-reclaim.timer
```

The RAM installer saves previous scripts and units under
`/usr/local/share/wsl-housekeeping/backups/` before replacing them.

## Windows housekeeping

The [Windows companion](windows/README.md) handles stale Windows temp files,
diagnostic dumps, download staging, and explicit application caches. It runs
two minutes after Windows login or wake, keeping recent files and crash evidence.
It preserves VM/swap disks and application data, and leaves Linux cleanup to the
WSL housekeeping script.

From the `windows` directory in Windows PowerShell:

```powershell
.\Cleanup-WSLDumps-OnStartOrWake-v2.ps1 -Mode Run        # preview
.\install.ps1                                        # schedule login/wake cleanup
.\test-housekeeping.ps1                              # disposable fixtures
```

## Verification

```bash
python3 -m unittest -v test_housekeeping.py test_ram_reclaim.py
shellcheck install.sh install-ram-reclaim.sh
```

Tests operate on disposable Git repositories and fake kernel statistics. They
do not drop real RAM cache or delete real worktrees. Coverage includes dirty,
active, recent, locked, unmerged, and data-bearing worktrees; missing detached
commits; cache symlinks and active package managers; a saturated core among 16
cores; changing load; memory pressure; dirty pages; and dry-run behavior.
