#!/bin/bash
# Rimuove i LaunchAgent di Trado e ferma i processi gestiti da launchd.

set -euo pipefail

TARGET_DIR="${HOME}/Library/LaunchAgents"

for label in com.trado.backend com.trado.worker; do
    echo "→ Removing ${label}"
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true
    rm -f "${TARGET_DIR}/${label}.plist"
done

echo "✔ LaunchAgent rimossi. I processi backend/worker non ripartiranno automaticamente."
