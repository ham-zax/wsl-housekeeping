#!/usr/bin/env bash
# Install daily housekeeping for the current user. Does not need root.
set -euo pipefail

if (( EUID == 0 )); then
  echo 'Run install.sh as your normal WSL user, without sudo.' >&2
  exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
data_dir="${HOME}/.local/share/wsl-housekeeping"
bin_dir="${HOME}/.local/bin"
unit_dir="${HOME}/.config/systemd/user"

mkdir -p -- "$data_dir" "$bin_dir" "$unit_dir"
install -m 0755 -- "$source_dir/housekeeping.py" "$data_dir/housekeeping.py"
if [[ -e "$bin_dir/wsl-housekeeping" || -L "$bin_dir/wsl-housekeeping" ]]; then
  if [[ ! -L "$bin_dir/wsl-housekeeping" || $(readlink -- "$bin_dir/wsl-housekeeping") != "$data_dir/housekeeping.py" ]]; then
    echo "Refusing to replace an unrelated command: $bin_dir/wsl-housekeeping" >&2
    exit 1
  fi
else
  ln -s -- "$data_dir/housekeeping.py" "$bin_dir/wsl-housekeeping"
fi
install -m 0644 -- "$source_dir/systemd/user/wsl-housekeeping.service" "$unit_dir/wsl-housekeeping.service"
install -m 0644 -- "$source_dir/systemd/user/wsl-housekeeping.timer" "$unit_dir/wsl-housekeeping.timer"
systemd-analyze --user verify "$unit_dir/wsl-housekeeping.service" "$unit_dir/wsl-housekeeping.timer"
systemctl --user daemon-reload
systemctl --user enable --now wsl-housekeeping.timer
echo 'Installed daily housekeeping. Preview with ~/.local/bin/wsl-housekeeping.'
