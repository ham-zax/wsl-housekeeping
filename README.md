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

The default repository roots are `~/repo`, `~/work`, and `~/worktrees`. The script discovers
Git repositories directly inside those roots, then inspects their registered
worktrees. Specify other roots with repeated `--root` arguments:

```bash
python3 housekeeping.py --root ~/projects --root ~/branches
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

Branches and commits are retained. Missing registrations are pruned only when
every missing registration in that repository passes the age, scope, lock, and
retained-commit checks.

Worktrees kept only for uncommitted, untracked, or ignored local files, or for a
commit not otherwise retained, are archived and then removed once the index,
HEAD reflog, checkout directory, and every tracked file are untouched for three
days (`--archive-days`) and no live process references them. Each archive under
`~/.local/state/wsl-housekeeping/worktree-archives/<stamp>-<name>/` contains:

- `refs/housekeeping/<stamp>-<name>` in the repository, pinning HEAD;
- `tracked.patch`, the binary `git diff HEAD`;
- `untracked.tar.gz`, untracked and ignored files except recognized generated
  directories such as `node_modules` or `.venv`;
- a `README` with the restore commands:

```bash
git -C REPO worktree add PATH refs/housekeeping/STAMP-NAME
git -C PATH apply --binary ARCHIVE/tracked.patch
tar -xzf ARCHIVE/untracked.tar.gz -C PATH
```

A worktree whose local files exceed 1 GiB (`--archive-max-gib`) is kept.
`--skip-archive` disables the rule. Locked worktrees are never archived.

For a checkout idle for at least seven days (`--artifact-days`), the script can
also remove ignored `target`, `node_modules`, `.next`, `.turbo`, or `.venv`
directories up to three levels deep when a manifest that recreates them sits
beside them: `Cargo.toml`/`Cargo.lock`, `package.json` or a JavaScript lockfile,
or `pyproject.toml`, `requirements.txt`, `setup.py`, or a Python lockfile.
Uncommitted edits do not block this, because only regenerable directories are
removed. It checks the Git index, HEAD reflog, tracked-file timestamps, the
artifact's newest file, and live process references. This applies to primary
checkouts and unmerged worktrees; it keeps their source files, commits, and
branches. `--skip-artifacts` disables this rule.

Rust targets above 5 GiB get a separate rule: incremental compiler caches idle
for two days can be removed even when the checkout has source edits. Compiled
binaries are kept. The target must be ignored by Git, contain no tracked files,
and have a nearby Cargo.lock. Live project references or package managers block
the cleanup. Set `--rust-cache-days` or `--rust-target-max-gib` to adjust it.

Cache cleanup uses `uv cache prune` and `pnpm store prune` to remove dangling or
unreferenced entries. A uv cache over 2 GiB is cleared. Pip, npm, and Yarn
download caches are cleared after two idle days or when they exceed 2 GiB.
Bun's download cache uses the same rule, removing only its configured cache
directory. Bun's native removal command can also touch other caches.
Individual old npx environments and Node download archives use the two-day
cutoff. Active package managers and caches they reference are protected.

Other `~/.cache` entries are treated as disposable once nothing inside them has
been written for 14 days (`--idle-cache-days`; `--skip-idle-caches` disables
it). Access times are ignored because file indexers read every file daily.
Hugging Face models, Playwright, Puppeteer, and Cypress browsers, and x-growth
browser run profiles are judged per model, browser version, or run. The uv,
pip, and Yarn caches use the rules above instead. Downloaded Codex app-server
releases are removed except `current`, the newest other release, and any
release a live process uses.

The script also removes your own `/tmp` entries untouched for at least one day.
It checks the newest file inside each entry and live process references before
removing anything. Protected socket and session directories are skipped. Claude
scratch subdirectories are considered individually, so a recent session does
not keep old scratch from the same parent forever. `/tmp` above 16 GiB after
eligible cleanup is reported for inspection; recent or active files are kept.

Each run reports the WSL VHD's allocated size and Windows C: free space before
and after cleanup. The 150 GiB WSL budget raises a warning when exceeded.
It is an alert, not a hard disk quota: applications can write faster than
housekeeping can remove safe disposable files. A real VHD maximum requires an
offline WSL disk resize, which this running-session timer cannot perform.
When the budget remains exceeded, the report lists the largest other caches
for inspection without counting their size as reclaimable space.

Applied runs remove all eligible entries without a per-run size cap.
Completed deletions and cache actions are recorded as JSON Lines under
`~/.local/state/wsl-housekeeping/deletions-*.jsonl` with paths and sizes before
cleanup. Cache actions may reclaim less than their recorded size.

The script keeps recently written caches, installed tools and Python runtimes,
and user data outside `~/.cache`. It skips cache operations while a package
manager is running or a process references the cache. Source edits are deleted
only after being archived as described above.

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
once a day at whatever time the machine is idle. The timer fires 15 minutes
after the user manager starts and then every 30 minutes; each check exits with
`SKIP` unless the last successful `--apply` run is at least 20 hours old and the
5-minute load average is at most 0.25 per CPU (`--max-load`). After 72 hours
without success it runs regardless of load. A failed run leaves the stamp
(`~/.local/state/wsl-housekeeping/last-success`) unchanged, so it retries on the
next check. WSL must be running for the timer to execute. If the user manager needs to remain active after logout,
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

## Optional user services

Reusable personal systemd user-service bundles live in [services](services/README.md),
separate from the core disk and RAM housekeeping timers. The ZenGate model-list
sync bundle can be installed from the repository root with:

~~~bash
./services/zengate-model-sync/install.sh
~~~

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
