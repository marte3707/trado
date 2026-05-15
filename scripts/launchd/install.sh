#!/bin/bash
# Installa i LaunchAgent di Trado in ~/Library/LaunchAgents/
# Backend e worker partiranno automaticamente al login dell'utente e
# verranno rilanciati automaticamente in caso di crash.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${HOME}/Library/LaunchAgents"
LOGS_DIR="${HOME}/Library/Logs"

mkdir -p "${TARGET_DIR}" "${LOGS_DIR}"

for plist in com.trado.backend.plist com.trado.worker.plist; do
    src="${SCRIPT_DIR}/${plist}"
    dst="${TARGET_DIR}/${plist}"
    label="${plist%.plist}"

    echo "→ Installing ${plist}"
    cp "${src}" "${dst}"

    # Disinstalla la versione precedente, se presente, prima di ricaricare.
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || true

    launchctl bootstrap "gui/$(id -u)" "${dst}"
    launchctl enable "gui/$(id -u)/${label}"
    echo "  loaded ${label}"
done

echo
echo "✔ LaunchAgent installati. Verifica con:"
echo "  launchctl print gui/\$(id -u)/com.trado.backend | head -20"
echo "  launchctl print gui/\$(id -u)/com.trado.worker  | head -20"
echo
echo "Log file:"
echo "  ${LOGS_DIR}/trado-backend.{out,err}.log"
echo "  ${LOGS_DIR}/trado-worker.{out,err}.log"
