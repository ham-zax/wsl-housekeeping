# Windows housekeeping

The Windows companion cleans stale Windows files. The Linux scripts in
[wsl-housekeeping](https://github.com/ham-zax/wsl-housekeeping) manage WSL caches,
worktrees, and RAM. This script never starts WSL or clears Linux caches.

Requires Windows PowerShell 5.1 or newer. Run as your normal Windows user;
administrator access is unnecessary. From this directory in PowerShell:

```powershell
.\Cleanup-WSLDumps-OnStartOrWake-v2.ps1 -Mode Run          # preview
.\Cleanup-WSLDumps-OnStartOrWake-v2.ps1 -Mode Run -Apply   # delete eligible files
.\Cleanup-WSLDumps-OnStartOrWake-v2.ps1 -Mode Run -Apply -WhatIf
.\install.ps1
.\Cleanup-WSLDumps-OnStartOrWake-v2.ps1 -Mode Status
```

If local policy blocks the installer, invoke it with:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

Installation copies the script to `%LOCALAPPDATA%\WSLDumpCleanup` and replaces
the existing task named `Cleanup WSL codebase-memory dumps on logon or wake`.
The name is retained for compatibility. It does not create a second task.
Previous installed versions and task XML are backed up beside the installed script.
Installation schedules cleanup; it does not immediately delete cache files.

The task runs two minutes after your Windows login or a System-log wake event
(Power-Troubleshooter 1 or Kernel-Power 107). A restart is covered when you log
in. It requires your interactive Windows session and does not run before login.
The task ignores overlapping scheduled launches. A mutex also prevents overlap
with a manual run in the same Windows session. It has a five-minute runtime
limit and is allowed on battery power.

Files must have both creation and modification times older than seven days.
Use `-OlderThanMinutes` to configure another cutoff (minimum one day).

| Target | Behavior |
| --- | --- |
| WSL crash dumps in `Temp\wsl-crashes`, or loose dumps in `Temp` | Delete old `wsl-crash-*.dmp`, retaining the newest three in each location |
| Windows `CrashDumps` and Chrome Crashpad `reports` | Delete old `.dmp` files, retaining the newest three in each location |
| `Temp\DiagOutputDir` | Delete old `.etl` files |
| Loose files directly in Windows user `Temp` | Delete old `.tmp`, `.temp`, and `.log` files |
| `vscode-remote-wsl` staging | Delete old `.tar.gz`, `.tgz`, and `.zip` download archives when VS Code is closed |
| Zoom `tmp_bin` / `ZoomDownload` | Delete old `.exe`, `.msi`, and `.zip` installers when Zoom is closed |
| Notion / Stremio explicit cache directories | Delete old files within `Cache`, `GPUCache`, and `Code Cache`; also Notion `DawnCache`, when the app is closed |

`-KeepNewest` controls diagnostic retention (default three, minimum one).
Retention is sorted by modification time before applying the age cutoff.
Notion `Partitions`, app databases, Stremio `stremio-server`, unpacked server
directories, arbitrary Temp subdirectories, and all VM/swap disks are preserved.
The script does not compact virtual disks, trim application working sets,
drop Windows RAM caches, or remove installed software.

Cleanup skips links/junctions, apps observed running, and files that cannot be
opened with concurrent read/write access denied. It retains directory structures
and checks file ages again before deletion. This reduces interference with live
apps but cannot make process inspection and filesystem operations atomic: an app
can start after inspection. A preview writes a log but does not delete target files.

Logs: `%LOCALAPPDATA%\WSLDumpCleanup\cleanup.log`, with one rotated log after 1 MiB.
Deletion totals include only successfully deleted files. File-level skips are
logged; a successful task result can include skipped locked or inaccessible files.

```powershell
Get-Content "$env:LOCALAPPDATA\WSLDumpCleanup\cleanup.log" -Tail 30
.\uninstall.ps1 -WhatIf
.\uninstall.ps1  # removes task, installed script, backups, and logs
.\test-housekeeping.ps1
```

The regression checks use a disposable directory and temporary environment
overrides. They exercise preview/WhatIf, recent files, locked files, diagnostic
retention, active apps, app data, directory protection, junctions, and separation
from WSL. They do not delete real user caches. This Windows component is covered
by the adjacent MIT license, carried forward from `ham-zax/windows-wsl-autocleaner`.
