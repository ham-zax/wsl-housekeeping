# Optional user services

This directory is an inventory of personal systemd user services that Hamza
may want to restore on a fresh WSL installation. These bundles are separate
from the core housekeeping installer and are enabled individually.

## Bundles

- [ZenGate model sync](zengate-model-sync/README.md) refreshes the available
  model IDs in OpenCode and Pi from the configured ZenGate gateway.

Each bundle keeps its runnable script, systemd unit files, installer, and
restore instructions together. Credentials stay in local configuration and
must not be committed here.
