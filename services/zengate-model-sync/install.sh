#!/usr/bin/env bash
set -euo pipefail

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
home_dir="${HOME:?HOME must be set}"
config_home="${XDG_CONFIG_HOME:-$home_dir/.config}"
state_home="${XDG_STATE_HOME:-$home_dir/.local/state}"
app_dir="$home_dir/.local/share/wsl-housekeeping/services/zengate-model-sync"
unit_dir="$config_home/systemd/user"
cache_dir="$state_home/opencode2api-deployment"
opencode_config="$config_home/opencode/opencode.json"
pi_models="$home_dir/.pi/agent/models.json"

for command in python3 systemd-analyze systemctl install; do
  command -v "$command" >/dev/null || {
    printf 'Missing required command: %s\n' "$command" >&2
    exit 1
  }
done

[[ -r "$opencode_config" ]] || {
  printf 'Configure the ZenGate provider first: %s\n' "$opencode_config" >&2
  exit 1
}
[[ -r "$pi_models" ]] || {
  printf 'Configure Pi models first: %s\n' "$pi_models" >&2
  exit 1
}

python3 - "$opencode_config" "$pi_models" <<'PY'
import json
import sys
from pathlib import Path

try:
    opencode = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    pi = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"Cannot read client configuration: {error}")

providers = opencode.get("provider")
provider = providers.get("zengate") if isinstance(providers, dict) else None
options = provider.get("options") if isinstance(provider, dict) else None
if not isinstance(options, dict):
    raise SystemExit("OpenCode must define provider.zengate.options")
if not isinstance(options.get("baseURL"), str) or not options["baseURL"].endswith("/v1"):
    raise SystemExit("OpenCode must define provider.zengate.options.baseURL ending in /v1")
if not isinstance(options.get("apiKey"), str) or not options["apiKey"]:
    raise SystemExit("OpenCode must define provider.zengate.options.apiKey")
pi_providers = pi.get("providers")
if not isinstance(pi_providers, dict) or not isinstance(pi_providers.get("opencode2api"), dict):
    raise SystemExit("Pi must define providers.opencode2api before installing this service")
PY

if ! systemctl --user show-environment >/dev/null 2>&1; then
  printf 'The systemd user manager is unavailable. Enable WSL systemd and retry from a user session.\n' >&2
  exit 1
fi

install -d -m 0755 -- "$app_dir" "$unit_dir"
install -d -m 0700 -- "$cache_dir"
install -m 0755 -- "$source_dir/sync-zengate-models" "$app_dir/sync-zengate-models"
install -m 0644 -- "$source_dir/model-metadata.json" "$app_dir/model-metadata.json"
if [[ ! -e "$cache_dir/model-metadata.json" && ! -L "$cache_dir/model-metadata.json" ]]; then
  install -m 0600 -- "$source_dir/model-metadata.json" "$cache_dir/model-metadata.json"
fi
install -m 0644 -- "$source_dir/zengate-model-sync.service" "$unit_dir/zengate-model-sync.service"
install -m 0644 -- "$source_dir/zengate-model-sync.timer" "$unit_dir/zengate-model-sync.timer"

systemd-analyze --user verify "$unit_dir/zengate-model-sync.service" "$unit_dir/zengate-model-sync.timer"
systemctl --user daemon-reload
systemctl --user enable --now zengate-model-sync.timer

printf 'Installed ZenGate model sync. Check with: systemctl --user list-timers zengate-model-sync.timer\n'
