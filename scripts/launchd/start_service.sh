#!/bin/bash

set -euo pipefail

if (( $# < 2 )); then
    echo "usage: $0 <service-name> <command> [args...]" >&2
    exit 64
fi

SERVICE_NAME="$1"
shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

/bin/bash "${SCRIPT_DIR}/ensure_docker_postgres.sh" "${SERVICE_NAME}"

printf '[trado-launchd][%s] exec %s\n' "${SERVICE_NAME}" "$*" >&2
exec "$@"
