# ZenGate model sync service

This user-level systemd timer periodically reads the ZenGate model catalog and
updates the model lists in OpenCode and Pi. It preserves known per-model
metadata in a local state cache. The bundle includes a baseline metadata
template so a fresh WSL install can restore the same starting definitions.

The sync script reads the ZenGate base URL and API key from the global OpenCode
configuration under XDG_CONFIG_HOME (default: ~/.config/opencode/opencode.json).
It copies the URL and key into Pi's ~/.pi/agent/models.json provider entry while
refreshing the model list. The key is never stored in this repository.

## Restore or install

First install and configure OpenCode and Pi, including the ZenGate provider in
OpenCode and the opencode2api provider entry in Pi. Then, from the
wsl-housekeeping repository root, run:

~~~bash
./services/zengate-model-sync/install.sh
~~~

The installer places the runtime script under
~/.local/share/wsl-housekeeping/services/zengate-model-sync/, seeds the local
metadata cache only if it does not already exist, and enables the timer. The
timer runs two minutes after the user systemd manager starts and then every
15 minutes. WSL systemd must be enabled. If it should keep running after logout,
enable lingering for your user with sudo loginctl enable-linger.

## Check and operate

~~~bash
systemctl --user list-timers zengate-model-sync.timer
systemctl --user status zengate-model-sync.timer
journalctl --user -u zengate-model-sync.service -n 50
systemctl --user start zengate-model-sync.service
~~~

The last command runs the sync immediately and writes the refreshed model
lists to both client configurations. To stop automatic refreshes:

~~~bash
systemctl --user disable --now zengate-model-sync.timer
~~~

The model metadata cache is stored at
$XDG_STATE_HOME/opencode2api-deployment/model-metadata.json, or
~/.local/state/opencode2api-deployment/model-metadata.json when XDG_STATE_HOME
is unset. Keep that cache when reinstalling to preserve local model metadata
edits.
