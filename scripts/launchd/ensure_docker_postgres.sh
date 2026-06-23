#!/bin/bash

set -euo pipefail

SERVICE_NAME="${1:-unknown}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKER_BIN="${DOCKER_BIN:-/usr/local/bin/docker}"
OPEN_BIN="${OPEN_BIN:-/usr/bin/open}"
DOCKER_APP="${DOCKER_APP:-/Applications/Docker.app}"
COMPOSE_FILE="${REPO_ROOT}/docker-compose.yml"
POSTGRES_SERVICE="${TRADO_POSTGRES_SERVICE:-postgres}"
DB_HOST="${TRADO_DB_HOST:-127.0.0.1}"
DB_PORT="${TRADO_DB_PORT:-5433}"
DOCKER_WAIT_SECONDS="${TRADO_DOCKER_WAIT_SECONDS:-180}"
POSTGRES_WAIT_SECONDS="${TRADO_POSTGRES_WAIT_SECONDS:-120}"
POLL_SECONDS="${TRADO_WAIT_POLL_SECONDS:-2}"

log() {
    printf '[trado-launchd][%s] %s\n' "${SERVICE_NAME}" "$*" >&2
}

docker_ready() {
    "${DOCKER_BIN}" info >/dev/null 2>&1
}

start_docker_desktop() {
    if docker_ready; then
        return 0
    fi

    if [[ ! -d "${DOCKER_APP}" ]]; then
        log "Docker.app non trovato in ${DOCKER_APP}"
        return 1
    fi

    log "Avvio Docker Desktop"
    if ! "${OPEN_BIN}" -gja "${DOCKER_APP}" >/dev/null 2>&1; then
        log "open Docker.app ha restituito un errore; continuo ad attendere Docker Desktop"
    fi
}

wait_for_docker() {
    local deadline
    deadline=$((SECONDS + DOCKER_WAIT_SECONDS))

    start_docker_desktop
    until docker_ready; do
        if (( SECONDS >= deadline )); then
            log "Timeout in attesa di Docker Desktop"
            return 1
        fi
        sleep "${POLL_SECONDS}"
    done
    log "Docker Desktop pronto"
}

compose_up_postgres() {
    log "Avvio container ${POSTGRES_SERVICE}"
    "${DOCKER_BIN}" compose -f "${COMPOSE_FILE}" up -d "${POSTGRES_SERVICE}" >/dev/null
}

postgres_container_id() {
    "${DOCKER_BIN}" compose -f "${COMPOSE_FILE}" ps -q "${POSTGRES_SERVICE}" 2>/dev/null
}

container_health() {
    local container_id="$1"
    "${DOCKER_BIN}" inspect \
        --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "${container_id}" 2>/dev/null
}

wait_for_postgres() {
    local deadline container_id status
    deadline=$((SECONDS + POSTGRES_WAIT_SECONDS))
    container_id="$(postgres_container_id)"

    if [[ -z "${container_id}" ]]; then
        log "Container ${POSTGRES_SERVICE} non trovato dopo docker compose up"
        return 1
    fi

    while true; do
        status="$(container_health "${container_id}")"
        if [[ "${status}" == "healthy" ]]; then
            break
        fi
        if (( SECONDS >= deadline )); then
            log "Timeout in attesa di PostgreSQL healthy (stato=${status:-unknown})"
            return 1
        fi
        sleep "${POLL_SECONDS}"
    done

    if ! (echo >"/dev/tcp/${DB_HOST}/${DB_PORT}") >/dev/null 2>&1; then
        log "PostgreSQL container healthy ma porta ${DB_HOST}:${DB_PORT} non raggiungibile"
        return 1
    fi

    log "PostgreSQL pronto su ${DB_HOST}:${DB_PORT}"
}

wait_for_docker
compose_up_postgres
wait_for_postgres
