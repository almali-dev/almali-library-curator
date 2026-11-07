#!/usr/bin/env bash
#
# Almali Library Curator for Plex — uninstaller for Unraid (T13, S8).
#
# It removes exactly what install-unraid.sh created, as recorded in the
# manifest it wrote in the app data directory:
#   - the container named in the manifest;
#   - the environment file named in the manifest;
#   - the signing key, only when the installer created it.
#
# The app data directory is kept unless the owner confirms otherwise (the
# default is to keep it), and the media is never touched: this script has no
# code path that writes inside the media share.
#
set -euo pipefail

APP_NAME="almali-library-curator"
DEFAULT_APPDATA="/mnt/user/appdata/almali-library-curator"
MANIFEST_NAME="install-manifest.json"
SKIP_CONFIRM=false
DEBUG=false

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
debug() { [[ "$DEBUG" == "true" ]] && echo -e "${DIM}[debug]${NC}  $*" >&2 || true; }

print_banner() {
    echo -e "  ${BOLD}Almali Library Curator for Plex${NC}"
    echo -e "  ${DIM}Uninstaller${NC}"
}

confirm() {
    local prompt="$1" default="${2:-N}" reply=""
    if [[ "$SKIP_CONFIRM" == "true" ]]; then
        return 1
    fi
    if [[ "$default" == "Y" ]]; then
        printf "  ${CYAN}>${NC} %s ${DIM}[Y/n]${NC}: " "$prompt" >&2
    else
        printf "  ${CYAN}>${NC} %s ${DIM}[y/N]${NC}: " "$prompt" >&2
    fi
    read -r reply || reply=""
    reply="${reply:-$default}"
    case "$reply" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# manifest_value KEY FILE -> the string value of a key, or nothing.
manifest_value() {
    local key="$1" file="$2"
    sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$file" | head -1 || true
}

# manifest_flag KEY FILE -> true or false.
manifest_flag() {
    local key="$1" file="$2"
    local value
    value="$(sed -n "s/.*\"${key}\"[[:space:]]*:[[:space:]]*\(true\|false\).*/\1/p" "$file" | head -1 || true)"
    [[ "$value" == "true" ]]
}

APP_DATA="${ALC_UNINSTALL_APPDATA:-$DEFAULT_APPDATA}"
MANIFEST=""
CONTAINER_NAME=""
ENV_FILE=""
SECRET_KEY=""
SECRET_KEY_CREATED="false"

load_manifest() {
    step "Reading the install manifest"
    MANIFEST="${APP_DATA}/${MANIFEST_NAME}"
    if [[ ! -f "$MANIFEST" ]]; then
        fatal "No ${MANIFEST_NAME} in ${APP_DATA}. Nothing is removed: this script only removes what the installer created."
    fi
    CONTAINER_NAME="$(manifest_value "container_name" "$MANIFEST")"
    ENV_FILE="$(manifest_value "env_file" "$MANIFEST")"
    SECRET_KEY="$(manifest_value "secret_key" "$MANIFEST")"
    if manifest_flag "secret_key_created" "$MANIFEST"; then
        SECRET_KEY_CREATED="true"
    fi
    [[ -n "$CONTAINER_NAME" ]] || fatal "The manifest ${MANIFEST} does not name a container."
    ok "Manifest: ${MANIFEST}"
    info "Container: ${CONTAINER_NAME}"
    info "Environment file: ${ENV_FILE:-<none>}"
    info "Signing key: ${SECRET_KEY:-<none>} (created by the installer: ${SECRET_KEY_CREATED})"
}

check_docker() {
    command -v docker >/dev/null 2>&1 || fatal "Docker is required and was not found on PATH."
    docker info >/dev/null 2>&1 || fatal "The Docker daemon is not running."
    ok "Docker is available"
}

remove_container() {
    step "Removing the container"
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        docker rm -f "$CONTAINER_NAME" || fatal "Could not remove the container ${CONTAINER_NAME}."
        ok "Removed the container ${CONTAINER_NAME}"
    else
        ok "No container named ${CONTAINER_NAME}; nothing to remove"
    fi
}

remove_env_file() {
    step "Removing the environment file"
    if [[ -n "$ENV_FILE" && -f "$ENV_FILE" ]]; then
        rm -f -- "$ENV_FILE" || fatal "Could not remove ${ENV_FILE}."
        ok "Removed ${ENV_FILE}"
    else
        ok "No environment file to remove"
    fi
}

remove_secret_key() {
    step "Removing the signing key"
    if [[ "$SECRET_KEY_CREATED" != "true" ]]; then
        ok "The signing key was not created by the installer; it is kept"
        return 0
    fi
    if [[ -n "$SECRET_KEY" && -f "$SECRET_KEY" ]]; then
        rm -f -- "$SECRET_KEY" || fatal "Could not remove ${SECRET_KEY}."
        ok "Removed ${SECRET_KEY}"
    else
        ok "No signing key to remove"
    fi
}

remove_app_data() {
    step "App data"
    if [[ ! -d "$APP_DATA" ]]; then
        ok "The app data directory ${APP_DATA} does not exist"
        return 0
    fi
    if ! confirm "Delete the app data directory ${APP_DATA} (settings, database, logs)?" "N"; then
        ok "Kept the app data directory ${APP_DATA}"
        return 0
    fi
    case "$APP_DATA" in
        /|/mnt|/mnt/user|/mnt/user/appdata|"")
            fatal "Refusing to delete ${APP_DATA}." ;;
    esac
    rm -rf -- "$APP_DATA" || fatal "Could not delete ${APP_DATA}."
    ok "Deleted ${APP_DATA}"
}

print_summary() {
    step "Uninstall complete"
    {
        printf '\n'
        printf '  %s: removed the container and the environment file the installer created.\n' "$APP_NAME"
        printf '  The media was never touched.\n'
        if [[ -d "$APP_DATA" ]]; then
            printf '  The app data directory was kept: %s\n' "$APP_DATA"
        fi
        printf '\n'
    } >&2
}

USAGE="Usage: uninstall-unraid.sh [-y|--yes] [--debug] [--app-data PATH]"

main() {
    setup_colors
    print_banner

    while [[ $# -gt 0 ]]; do
        case "$1" in
            -y|--yes) SKIP_CONFIRM=true; shift ;;
            --debug) DEBUG=true; shift ;;
            --app-data)
                [[ $# -ge 2 && -n "$2" ]] || { error "--app-data requires a path"; echo "$USAGE"; exit 1; }
                APP_DATA="$2"
                shift 2 ;;
            -h|--help) echo "$USAGE"; exit 0 ;;
            *) error "Unknown flag: $1"; echo "$USAGE"; exit 1 ;;
        esac
    done

    if [[ "$DEBUG" == "true" ]]; then
        set -x
    fi

    # An unattended run never asks: the app data is kept.
    case "$(printf '%s' "${ALC_UNINSTALL_NONINTERACTIVE:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) SKIP_CONFIRM=true ;;
    esac

    load_manifest || exit 1
    check_docker || exit 1
    remove_container
    remove_env_file
    remove_secret_key
    remove_app_data
    print_summary
}

main "$@"
