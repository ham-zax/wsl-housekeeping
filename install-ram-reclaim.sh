#!/usr/bin/env bash
# Install the system RAM-reclaim service and timer; requires root.
set -euo pipefail

if (( EUID != 0 )); then
  echo 'Run this installer with sudo; it installs a system service.' >&2
  exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
backup_dir="/usr/local/share/wsl-housekeeping/backups/$(date +%Y%m%d-%H%M%S)-${BASHPID}"
mkdir -p -- "$backup_dir"
for previous in /usr/local/sbin/wsl-reclaim /etc/systemd/system/wsl-reclaim.service /etc/systemd/system/wsl-reclaim.timer; do
  if [[ -e "$previous" ]]; then
    cp -a -- "$previous" "$backup_dir/"
  fi
done
for unit in wsl-reclaim.timer wsl-reclaim.service; do
  if systemctl is-active --quiet "$unit"; then
    systemctl stop "$unit"
  fi
done
install -m 0755 -- "$source_dir/ram_reclaim.py" /usr/local/sbin/wsl-reclaim
install -m 0644 -- "$source_dir/systemd/system/wsl-reclaim.service" /etc/systemd/system/wsl-reclaim.service
install -m 0644 -- "$source_dir/systemd/system/wsl-reclaim.timer" /etc/systemd/system/wsl-reclaim.timer
systemd-analyze verify /etc/systemd/system/wsl-reclaim.service /etc/systemd/system/wsl-reclaim.timer
systemctl daemon-reload
systemctl enable --now wsl-reclaim.timer
echo "Installed idle RAM reclamation. Previous files saved in $backup_dir."
