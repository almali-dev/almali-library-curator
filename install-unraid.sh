#!/usr/bin/env bash
#
# Almali Library Curator for Plex — installer for Unraid (T13, S8).
#
# What it does, in this order:
#   1. verifies its own download checksum when an install-unraid.sh.sha256
#      file sits next to it (the published curl + sha256sum flow);
#   2. asks for the app data directory, the media share, the optional
#      /unraid/mnt mount, the web port, PUID/PGID, the timezone and the
#      owner login;
#   3. writes an owner-only (0600) environment file in the app data
#      directory — the password is never passed on a command line, so it is
#      never visible in `ps`, and it is never written to a log;
#   4. generates the session signing key (<app data>/secret_key) when it is
#      missing (the app reads that file, see app.py `_signing_key`);
#   5. pulls the image and creates the container;
#   6. records what it created in <app data>/install-manifest.json, so
#      uninstall-unraid.sh removes exactly that and nothing else.
#
# Every answer can be given through an environment variable, and
# ALC_INSTALL_NONINTERACTIVE=1 turns the installer into a silent run for
# automation and for tests.
#
set -euo pipefail

APP_NAME="almali-library-curator"
IMAGE_REPOSITORY="ghcr.io/almali-dev/almali-library-curator"
DEFAULT_IMAGE_TAG="latest"
DEFAULT_CONTAINER_NAME="almali-library-curator"
DEFAULT_PORT=17540
CONTAINER_PORT=17540
DEFAULT_APPDATA="/mnt/user/appdata/almali-library-curator"
DEFAULT_MEDIA="/mnt/user/media"
DEFAULT_PUID=99
DEFAULT_PGID=100
DEFAULT_TZ="Etc/UTC"
DEFAULT_UNRAID_MNT="no"

CONFIG_CONTAINER_PATH="/config"
MEDIA_CONTAINER_PATH="/media"
UNRAID_MNT_HOST="/mnt"
UNRAID_MNT_CONTAINER="/unraid/mnt"

ENV_FILE_NAME="${APP_NAME}.env"
MANIFEST_NAME="install-manifest.json"
MANIFEST_VERSION=1
SECRET_KEY_NAME="secret_key"
SECRET_KEY_BYTES=64
MIN_PASSWORD_LENGTH=8

LABEL_APP="ai.almali-dev.app"
LABEL_INSTALLER="ai.almali-dev.installer"
LABEL_MANAGED="ai.almali-dev.managed"

NONINTERACTIVE_VARIABLE="ALC_INSTALL_NONINTERACTIVE"
SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")"
CHECKSUM_PATH="${SCRIPT_PATH}.sha256"

setup_colors() {
    if [ -t 1 ] && [ "${TERM:-}" != "dumb" ]; then
        RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
        BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'
    else
        RED='' GREEN='' YELLOW='' BLUE='' CYAN='' BOLD='' DIM='' NC=''
    fi
}
info()  { echo -e "${BLUE}[info]${NC}    $*" >&2; }
ok()    { echo -e "${GREEN}[ok]${NC}      $*" >&2; }
warn()  { echo -e "${YELLOW}[warn]${NC}    $*" >&2; }
error() { echo -e "${RED}[error]${NC}   $*" >&2; }
fatal() { error "$@"; exit 1; }
step()  { echo -e "\n${CYAN}${BOLD}==>${NC} ${BOLD}$*${NC}" >&2; }

print_banner() {
    echo -e "  ${BOLD}Almali Library Curator for Plex${NC}"
    echo -e "  ${DIM}Unraid installer${NC}"
}

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

# --- Answers ---------------------------------------------------------------
#
# Every answer comes from an environment variable when it is set, so the
# installer can run without a terminal. The variables are read only inside
# this script: they are never exported to the container.

NONINTERACTIVE="false"
if is_true "${!NONINTERACTIVE_VARIABLE:-}"; then
    NONINTERACTIVE="true"
fi

# validate_answer PROMPT VALUE -> 0 when the value is acceptable.
validate_answer() {
    local prompt="$1" value="$2"
    case "$prompt" in
        *[Pp]ort*)
            if ! [[ "$value" =~ ^[0-9]{1,5}$ ]] || (( 10#$value < 1 || 10#$value > 65535 )); then
                error "The port must be a number between 1 and 65535"
                return 1
            fi ;;
        PUID|PGID)
            if ! [[ "$value" =~ ^[0-9]{1,10}$ ]] || (( 10#$value == 0 )); then
                error "${prompt} must be a non-zero number (0 would run the container as root)"
                return 1
            fi ;;
        *password*|*Password*)
            if ! validate_password "$value"; then
                return 1
            fi ;;
        *user*|*User*)
            if [[ -z "$value" ]]; then
                error "The user name cannot be empty"
                return 1
            fi ;;
        *directory*|*share*|*Directory*|*Share*)
            if [[ "$value" != /* ]]; then
                error "${prompt} must be an absolute path"
                return 1
            fi ;;
        *imezone*)
            if ! [[ "$value" =~ ^[A-Za-z0-9_+./-]+$ ]]; then
                error "The timezone must look like Europe/Madrid"
                return 1
            fi ;;
    esac
    return 0
}

validate_password() {
    local value="$1"
    if [[ ${#value} -lt $MIN_PASSWORD_LENGTH ]]; then
        error "The password must be at least ${MIN_PASSWORD_LENGTH} characters"
        return 1
    fi
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
        error "The password cannot contain a line break"
        return 1
    fi
    if [[ "$value" != "${value#"${value%%[![:space:]]*}"}" ]] \
       || [[ "$value" != "${value%"${value##*[![:space:]]}"}" ]]; then
        error "The password cannot start or end with a space"
        return 1
    fi
    return 0
}

# ask PROMPT DEFAULT RESULT_VAR [ENVIRONMENT_VARIABLE]
#
# A value given through the environment variable is used as it is and
# validated once: an unusable value stops the installer instead of asking
# the same question again.
ask() {
    local prompt="$1" default="$2" result_var="$3" env_name="${4:-}"
    local value=""
    if [[ -n "$env_name" && -n "${!env_name:-}" ]]; then
        value="${!env_name}"
        info "${prompt}: ${value}"
        if ! validate_answer "$prompt" "$value"; then
            fatal "Invalid value for '${prompt}' (from ${env_name})."
        fi
        printf -v "$result_var" '%s' "$value"
        return 0
    fi
    if [[ "$NONINTERACTIVE" == "true" ]]; then
        fatal "Missing value for '${prompt}'. Set ${env_name} or run the installer interactively."
    fi
    while true; do
        printf "  ${CYAN}>${NC} %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
        read -r value || value=""
        value="${value:-$default}"
        if validate_answer "$prompt" "$value"; then
            printf -v "$result_var" '%s' "$value"
            return 0
        fi
    done
}

# ask_yes_no PROMPT DEFAULT RESULT_VAR [ENVIRONMENT_VARIABLE]
ask_yes_no() {
    local prompt="$1" default="$2" result_var="$3" env_name="${4:-}"
    local value="" reply=""
    if [[ -n "$env_name" && -n "${!env_name:-}" ]]; then
        value="${!env_name}"
        info "${prompt}: ${value}"
        case "$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')" in
            y|yes|true|1|on) printf -v "$result_var" '%s' "yes"; return 0 ;;
            n|no|false|0|off) printf -v "$result_var" '%s' "no"; return 0 ;;
        esac
        fatal "Invalid value for '${prompt}' (from ${env_name}): answer yes or no."
    fi
    if [[ "$NONINTERACTIVE" == "true" ]]; then
        fatal "Missing value for '${prompt}'. Set ${env_name} or run the installer interactively."
    fi
    while true; do
        printf "  ${CYAN}>${NC} %s ${DIM}[%s]${NC}: " "$prompt" "$default" >&2
        read -r reply || reply=""
        reply="${reply:-$default}"
        case "$(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]')" in
            y|yes|true|1|on) printf -v "$result_var" '%s' "yes"; return 0 ;;
            n|no|false|0|off) printf -v "$result_var" '%s' "no"; return 0 ;;
        esac
        error "Answer yes or no"
    done
}

# ask_password RESULT_VAR [ENVIRONMENT_VARIABLE]
#
# The password is read silently, confirmed, and never echoed. It is only
# written to the owner-only environment file, never to a command line.
ask_password() {
    local result_var="$1" env_name="${2:-ALC_INSTALL_PASSWORD}"
    local first="" second=""
    if [[ -n "${!env_name:-}" ]]; then
        if ! validate_password "${!env_name}"; then
            fatal "Invalid password in ${env_name}."
        fi
        info "Owner password: (taken from ${env_name})"
        printf -v "$result_var" '%s' "${!env_name}"
        return 0
    fi
    if [[ "$NONINTERACTIVE" == "true" ]]; then
        fatal "Missing owner password. Set ${env_name} or run the installer interactively."
    fi
    while true; do
        printf "  ${CYAN}>${NC} Owner password ${DIM}(at least ${MIN_PASSWORD_LENGTH} characters)${NC}: " >&2
        read -r -s first || first=""
        printf "\n" >&2
        printf "  ${CYAN}>${NC} Repeat the password: " >&2
        read -r -s second || second=""
        printf "\n" >&2
        if [[ "$first" != "$second" ]]; then
            error "The two passwords do not match"
            continue
        fi
        if validate_password "$first"; then
            printf -v "$result_var" '%s' "$first"
            return 0
        fi
    done
}

# --- The download checksum --------------------------------------------------

sha256_of() {
    local path="$1" output=""
    if command -v sha256sum >/dev/null 2>&1; then
        output="$(sha256sum -- "$path" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        output="$(shasum -a 256 -- "$path" | awk '{print $1}')"
    else
        return 1
    fi
    [[ -n "$output" ]] || return 1
    printf '%s' "$output"
}

# verify_own_checksum: refuse to run when the published checksum does not
# match the downloaded script.
verify_own_checksum() {
    step "Verifying the download"
    if [[ ! -f "$CHECKSUM_PATH" ]]; then
        warn "No $(basename -- "$CHECKSUM_PATH") next to this script; the download was not verified."
        warn "Download it with 'curl -fsSLO .../install-unraid.sh.sha256' and check it with 'sha256sum -c'."
        return 0
    fi
    local expected="" actual=""
    expected="$(awk 'NF {print $1; exit}' "$CHECKSUM_PATH")"
    if [[ ! "$expected" =~ ^[0-9a-fA-F]{64}$ ]]; then
        fatal "The checksum file $(basename -- "$CHECKSUM_PATH") does not hold a sha256 digest."
    fi
    if ! actual="$(sha256_of "$SCRIPT_PATH")"; then
        warn "No sha256 tool found; the download was not verified."
        return 0
    fi
    if [[ "${expected,,}" != "${actual,,}" ]]; then
        fatal "Checksum mismatch: $(basename -- "$SCRIPT_PATH") does not match $(basename -- "$CHECKSUM_PATH")."
        fatal "Expected ${expected,,}, got ${actual,,}. The download was not run."
    fi
    ok "Checksum verified"
}

# --- The environment --------------------------------------------------------

check_environment() {
    step "Checking the environment"
    if [[ -d /mnt/user ]] || [[ -f /etc/unraid-version ]]; then
        ok "Unraid environment detected"
    else
        warn "Unraid was not detected; continuing anyway."
    fi
    command -v docker >/dev/null 2>&1 || fatal "Docker is required and was not found on PATH."
    if docker info >/dev/null 2>&1; then
        ok "Docker daemon is running"
    else
        fatal "The Docker daemon is not running."
    fi
}

# --- The answers ------------------------------------------------------------

collect_answers() {
    step "Configuration"
    # The helpers set the variable they are given by name, so they are called
    # directly: a command substitution would run them in a subshell and the
    # answer would be lost.
    ask "App data directory" "$DEFAULT_APPDATA" APPDATA ALC_INSTALL_APPDATA
    ask "Media share (mounted read-only)" "$DEFAULT_MEDIA" MEDIA ALC_INSTALL_MEDIA
    ask_yes_no "Mount /mnt read-only as ${UNRAID_MNT_CONTAINER}?" "$DEFAULT_UNRAID_MNT" UNRAID_MNT ALC_INSTALL_UNRAID_MNT
    ask "Web UI port" "$DEFAULT_PORT" PORT ALC_INSTALL_PORT
    ask "PUID" "$DEFAULT_PUID" PUID ALC_INSTALL_PUID
    ask "PGID" "$DEFAULT_PGID" PGID ALC_INSTALL_PGID
    ask "Timezone" "$DEFAULT_TZ" TZ_VALUE ALC_INSTALL_TZ
    ask "Owner user name" "admin" AUTH_USER ALC_INSTALL_USER
    ask_password AUTH_PASSWORD ALC_INSTALL_PASSWORD

    CONTAINER_NAME="${ALC_INSTALL_CONTAINER_NAME:-$DEFAULT_CONTAINER_NAME}"
    IMAGE="${ALC_INSTALL_IMAGE:-${IMAGE_REPOSITORY}:${ALC_INSTALL_TAG:-$DEFAULT_IMAGE_TAG}}"
    ENV_FILE="${APPDATA}/${ENV_FILE_NAME}"
    SECRET_KEY_PATH="${APPDATA}/${SECRET_KEY_NAME}"
    MANIFEST_PATH="${APPDATA}/${MANIFEST_NAME}"
}

validate_answers() {
    step "Validating the configuration"
    [[ -d "$MEDIA" ]] || fatal "The media share ${MEDIA} does not exist."
    ok "Media share: ${MEDIA} (read-only)"
    if [[ "$UNRAID_MNT" == "yes" ]]; then
        ok "Host ${UNRAID_MNT_HOST} is mounted read-only as ${UNRAID_MNT_CONTAINER}"
    else
        info "The optional ${UNRAID_MNT_CONTAINER} mount is not used."
    fi
    ok "App data: ${APPDATA}"
    ok "Web UI: http://<unraid-ip>:${PORT}"
    ok "Container: ${CONTAINER_NAME} (${IMAGE})"
}

# --- The files --------------------------------------------------------------

prepare_app_data() {
    if [[ ! -d "$APPDATA" ]]; then
        mkdir -p "$APPDATA" || fatal "Could not create the app data directory ${APPDATA}."
        ok "Created ${APPDATA}"
    fi
}

random_bytes() {
    local count="$1"
    if command -v openssl >/dev/null 2>&1; then
        openssl rand "$count"
    elif [[ -r /dev/urandom ]]; then
        head -c "$count" /dev/urandom
    else
        fatal "No source of random bytes was found."
    fi
}

# The app reads its session signing key from <config dir>/secret_key and
# expects at least 32 bytes, owner-only. An existing key is never replaced.
ensure_secret_key() {
    step "Preparing the signing key"
    if [[ -f "$SECRET_KEY_PATH" ]]; then
        SECRET_KEY_CREATED="false"
        ok "Keeping the existing ${SECRET_KEY_NAME}"
        return 0
    fi
    ( umask 077; random_bytes "$SECRET_KEY_BYTES" > "$SECRET_KEY_PATH" ) \
        || fatal "Could not write ${SECRET_KEY_PATH}."
    chmod 600 "$SECRET_KEY_PATH"
    SECRET_KEY_CREATED="true"
    ok "Created ${SECRET_KEY_PATH} (owner-only, ${SECRET_KEY_BYTES} bytes)"
}

write_env_file() {
    step "Writing the environment file"
    local temporary
    temporary="$(mktemp "${APPDATA}/.env.XXXXXX")" || fatal "Could not create a temporary file in ${APPDATA}."
    chmod 600 "$temporary"
    {
        printf 'ALC_AUTH_USER=%s\n' "$AUTH_USER"
        printf 'ALC_AUTH_PASSWORD=%s\n' "$AUTH_PASSWORD"
        printf 'PUID=%s\n' "$PUID"
        printf 'PGID=%s\n' "$PGID"
        printf 'TZ=%s\n' "$TZ_VALUE"
        if [[ "$UNRAID_MNT" == "yes" ]]; then
            printf 'ALC_UNRAID_ROOT=%s\n' "$UNRAID_MNT_CONTAINER"
        fi
    } > "$temporary"
    mv -f "$temporary" "$ENV_FILE" || fatal "Could not write ${ENV_FILE}."
    chmod 600 "$ENV_FILE"
    ok "Wrote ${ENV_FILE} (owner-only; the password is never on a command line)"
}

# --- The container ----------------------------------------------------------

pull_image() {
    step "Pulling the image"
    docker pull "$IMAGE" || fatal "Could not pull ${IMAGE}."
    ok "Pulled ${IMAGE}"
}

create_container() {
    step "Creating the container"
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        fatal "A container named '${CONTAINER_NAME}' already exists. Remove it first, or set ALC_INSTALL_CONTAINER_NAME."
    fi
    local -a arguments=(
        run -d
        --restart unless-stopped
        --name "$CONTAINER_NAME"
        --label "${LABEL_APP}=${APP_NAME}"
        --label "${LABEL_INSTALLER}=install-unraid.sh"
        --label "${LABEL_MANAGED}=true"
        --env-file "$ENV_FILE"
        -p "${PORT}:${CONTAINER_PORT}"
        -v "${APPDATA}:${CONFIG_CONTAINER_PATH}"
        -v "${MEDIA}:${MEDIA_CONTAINER_PATH}:ro"
    )
    if [[ "$UNRAID_MNT" == "yes" ]]; then
        arguments+=(-v "${UNRAID_MNT_HOST}:${UNRAID_MNT_CONTAINER}:ro")
    fi
    arguments+=("$IMAGE")
    docker "${arguments[@]}" || fatal "The container could not be created."
    ok "Created the container ${CONTAINER_NAME}"
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s' "$value"
}

write_manifest() {
    step "Recording what was created"
    local timestamp
    timestamp="$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
    local temporary
    temporary="$(mktemp "${APPDATA}/.manifest.XXXXXX")" || fatal "Could not create a temporary file in ${APPDATA}."
    chmod 600 "$temporary"
    {
        printf '{\n'
        printf '  "version": %s,\n' "$MANIFEST_VERSION"
        printf '  "installed_at": "%s",\n' "$(json_escape "$timestamp")"
        printf '  "app_data": "%s",\n' "$(json_escape "$APPDATA")"
        printf '  "container_name": "%s",\n' "$(json_escape "$CONTAINER_NAME")"
        printf '  "image": "%s",\n' "$(json_escape "$IMAGE")"
        printf '  "env_file": "%s",\n' "$(json_escape "$ENV_FILE")"
        printf '  "secret_key": "%s",\n' "$(json_escape "$SECRET_KEY_PATH")"
        printf '  "secret_key_created": %s,\n' "$SECRET_KEY_CREATED"
        printf '  "port": %s\n' "$PORT"
        printf '}\n'
    } > "$temporary"
    mv -f "$temporary" "$MANIFEST_PATH" || fatal "Could not write ${MANIFEST_PATH}."
    chmod 600 "$MANIFEST_PATH"
    ok "Wrote ${MANIFEST_PATH}"
}

print_summary() {
    step "Installation complete"
    {
        printf '\n'
        printf '  The web UI is at http://<unraid-ip>:%s ; sign in with the owner login.\n' "$PORT"
        printf '\n'
        printf '  Created by this installer:\n'
        printf '    - container      %s\n' "$CONTAINER_NAME"
        printf '    - environment    %s\n' "$ENV_FILE"
        printf '    - signing key    %s (created: %s)\n' "$SECRET_KEY_PATH" "$SECRET_KEY_CREATED"
        printf '    - manifest       %s\n' "$MANIFEST_PATH"
        printf '\n'
        printf '  Uninstall with: bash uninstall-unraid.sh --app-data %s\n' "$APPDATA"
    } >&2
}

main() {
    setup_colors
    print_banner
    verify_own_checksum || exit 1
    check_environment || exit 1
    collect_answers
    validate_answers
    prepare_app_data
    ensure_secret_key
    write_env_file
    pull_image
    create_container
    write_manifest
    print_summary
}

main "$@"
