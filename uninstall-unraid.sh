#!/usr/bin/env bash
#
# Almali Library Curator for Plex — uninstaller for Unraid (T13, S8).
#
# It removes exactly what install-unraid.sh created, as recorded in the
# manifest it wrote in the app data directory:
#   - the container named in the manifest, but only when the container carries
#     the installer-managed label and the same ownership label the manifest
#     records;
#   - the environment file named in the manifest;
#   - the signing key, only when the installer created it.
#
# The manifest is a file inside the app data directory and the app container
# may write there, so the paths it names are never trusted on their own: the
# environment file, the password file and the signing key must be the files
# this installer creates, under the names it gives them (#146), or nothing is
# removed at all. The media path only feeds the overlap guard that keeps the
# app data away from the media share, and it is checked before that guard uses
# it: a value no installer run can write is refused, and every spelling the
# installer does record is compared as a resolved path (#156).
#
# A container of another installation is never removed: both labels are
# checked before anything is removed, and a manifest with no attempt to
# compare is only trusted for a container that carries the managed label.
#
# The app data directory is kept unless the owner confirms otherwise (the
# default is to keep it), and the media is never touched: this script has no
# code path that writes inside the media share.
#
set -euo pipefail

APP_NAME="almali-library-curator"
DEFAULT_APPDATA="/mnt/user/appdata/almali-library-curator"
MANIFEST_NAME="install-manifest.json"
# #71: the installer keeps the password in its own owner-only file and the
# environment only names it; the uninstaller removes it like the rest.
PASSWORD_FILE_NAME="${APP_NAME}.password"
# The names the installer gives its files, so a manifest that names anything
# else is refused before a root-run removal (#146).
ENV_FILE_NAME="${APP_NAME}.env"
SECRET_KEY_NAME="secret_key"
# Every container this installer creates carries the attempt that created
# it, and the manifest records the same value.
LABEL_ATTEMPT="ai.almali-dev.install-attempt"
# The label every container this installer creates carries. It is the only
# identity an installation keeps when the manifest has no attempt to compare.
LABEL_MANAGED="ai.almali-dev.managed"
SKIP_CONFIRM=false
DEBUG=false
SCRIPT_PATH="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)/$(basename -- "${BASH_SOURCE[0]}")"

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

verify_own_checksum() {
    local checksum="${SCRIPT_PATH}.sha256" expected actual
    if [[ ! -f "$checksum" ]]; then
        warn "No $(basename -- "$checksum") next to this script; the download was not verified."
        return 0
    fi
    expected="$(awk 'NF {print $1; exit}' "$checksum")"
    [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || fatal "The checksum file does not hold a sha256 digest."
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum -- "$SCRIPT_PATH" | awk '{print $1}')"
    elif command -v shasum >/dev/null 2>&1; then
        actual="$(shasum -a 256 -- "$SCRIPT_PATH" | awk '{print $1}')"
    else
        fatal "No sha256 tool found; the uninstaller download could not be verified."
    fi
    [[ "${expected,,}" == "${actual,,}" ]] \
        || fatal "Checksum mismatch: expected ${expected,,}, got ${actual,,}. Nothing was removed."
    ok "Checksum verified"
}

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
# paths_overlap A B -> success when A and B are the same directory or one
# holds the other (both resolved, so symlinks and ".." cannot hide it).
paths_overlap() {
    local a b
    a="$(realpath -m -- "$1")"
    b="$(realpath -m -- "$2")"
    [[ "$a" == "$b" || "$a" == "$b"/* || "$b" == "$a"/* ]]
}

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

# require_installer_file KEY VALUE NAME -> refuse unless VALUE is exactly the
# file the installer creates in the app data directory.
#
# #146: the manifest lives inside the app data directory, which the app
# container is allowed to write, and every path it names is deleted as root.
# Only the expected name directly inside the app data directory is accepted: a
# different name, a path outside it, a ".." component that climbs out, a
# directory-looking value with a trailing slash or a final component that is
# not the file itself (a symlink would be removed instead of the file) is
# refused before anything is removed. An empty value names no file and is left
# as it is (nothing is removed for it).
require_installer_file() {
    local key="$1" value="$2" name="$3"
    local expected="${APP_DATA}/${name}"
    [[ -n "$value" ]] || return 0
    if [[ "$value" == "$expected" ]]; then
        return 0
    fi
    # Every other spelling must still name the same regular file: the same last
    # component (so the removal hits the file and not a link to it), no ".."
    # and no trailing slash (a directory-looking value is not a file), and a
    # parent directory that resolves to the app data directory (a symlinked or
    # trailing-slash app data path is the same directory).
    case "$value" in
        */|*..*) ;;
        *)
            if [[ "$(basename -- "$value")" == "$name" ]] \
                && [[ "$(realpath -m -- "$value")" == "$(realpath -m -- "$expected")" ]]; then
                return 0
            fi
            ;;
    esac
    fatal "The manifest records ${key} ${value}, which is not the ${name} the installer creates (${expected}); nothing was removed."
}

# media_path_problem VALUE -> the reason VALUE is not a media path the
# installer records, or nothing at all when it is.
#
# #156: the media path only feeds the guard that refuses to delete an app data
# directory holding the media, and it comes from the same writable manifest, so
# only a value no installer run can write is refused: exactly what
# validate_answer rejects for the media answer - empty, not absolute, or
# containing ":" or "," (Docker splits a bind on both, #83). A trailing slash,
# a ".." or a symlinked share are spellings the installer records verbatim
# (L2, L3), so they are accepted: the guard compares resolved paths, which is
# the safe comparison for them.
media_path_problem() {
    local value="$1"
    if [[ -z "$value" ]]; then
        printf '%s' "it names no media share"
        return 0
    fi
    if [[ "$value" != /* ]]; then
        printf '%s' "it is not an absolute path"
        return 0
    fi
    case "$value" in
        *:*|*,*)
            printf '%s' "it contains ':' or ',', which Docker splits a bind on"
            return 0 ;;
    esac
    return 1
}

APP_DATA="${ALC_UNINSTALL_APPDATA:-$DEFAULT_APPDATA}"
MANIFEST=""
CONTAINER_NAME=""
ENV_FILE=""
PASSWORD_FILE=""
SECRET_KEY=""
SECRET_KEY_CREATED="false"
# The attempt the manifest records: the container it names carries it. An
# empty value comes from a manifest written by an older installer.
MANIFEST_ATTEMPT=""

load_manifest() {
    step "Reading the install manifest"
    local media_problem=""
    MANIFEST="${APP_DATA}/${MANIFEST_NAME}"
    if [[ ! -f "$MANIFEST" ]]; then
        fatal "No ${MANIFEST_NAME} in ${APP_DATA}. Nothing is removed: this script only removes what the installer created."
    fi
    CONTAINER_NAME="$(manifest_value "container_name" "$MANIFEST")"
    ENV_FILE="$(manifest_value "env_file" "$MANIFEST")"
    PASSWORD_FILE="$(manifest_value "password_file" "$MANIFEST")"
    SECRET_KEY="$(manifest_value "secret_key" "$MANIFEST")"
    MEDIA_PATH="$(manifest_value "media" "$MANIFEST")"
    MANIFEST_ATTEMPT="$(manifest_value "install_attempt" "$MANIFEST")"
    if manifest_flag "secret_key_created" "$MANIFEST"; then
        SECRET_KEY_CREATED="true"
    fi
    [[ -n "$CONTAINER_NAME" ]] || fatal "The manifest ${MANIFEST} does not name a container."
    # Every path the manifest names is removed as root: only the files this
    # installer creates are accepted (#146).
    require_installer_file "env_file" "$ENV_FILE" "$ENV_FILE_NAME"
    require_installer_file "secret_key" "$SECRET_KEY" "$SECRET_KEY_NAME"
    require_installer_file "password_file" "$PASSWORD_FILE" "$PASSWORD_FILE_NAME"
    # #156: the media path is what the overlap guard compares the app data
    # with before a root-run removal, so a value no installer run can write
    # (empty, relative, or with ":" or ",") is refused here, when the refusal
    # is still known up front, and never reaches the guard (#146). Every
    # spelling the installer records is accepted: the guard resolves it.
    if media_problem="$(media_path_problem "$MEDIA_PATH")"; then
        fatal "The manifest records the media share '${MEDIA_PATH}', which the installer never writes: ${media_problem}. Nothing was removed."
    fi
    ok "Manifest: ${MANIFEST}"
    info "Container: ${CONTAINER_NAME}"
    info "Ownership attempt: ${MANIFEST_ATTEMPT:-<none recorded>}"
    info "Environment file: ${ENV_FILE:-<none>}"
    info "Password file: ${PASSWORD_FILE:-<none>}"
    info "Signing key: ${SECRET_KEY:-<none>} (created by the installer: ${SECRET_KEY_CREATED})"
    info "Media share: ${MEDIA_PATH}"
}

check_docker() {
    command -v docker >/dev/null 2>&1 || fatal "Docker is required and was not found on PATH."
    docker info >/dev/null 2>&1 || fatal "The Docker daemon is not running."
    ok "Docker is available"
}

# The container is only removed when it carries the installer-managed label
# and the ownership label the manifest records. A container of another
# installation is never removed.
verify_container_ownership() {
    local label="" managed="" inspect_error="" status=0 error_file=""
    # Docker's own error is kept apart from the labels: a failing `docker
    # inspect` is not a container without an ownership label, and reporting
    # it as one names the wrong cause for the refusal. A warning on a
    # successful inspect never pollutes the label either.
    error_file="$(mktemp)" || fatal "A temporary file for docker's error could not be created; nothing was removed."
    label="$(docker inspect --format "{{index .Config.Labels \"${LABEL_ATTEMPT}\"}}" \
        "$CONTAINER_NAME" 2>"$error_file")" || status=$?
    inspect_error="$(cat "$error_file" 2>/dev/null || true)"
    rm -f "$error_file"
    if (( status != 0 )); then
        fatal "The ownership of the container ${CONTAINER_NAME} could not be checked: docker inspect failed (${inspect_error:-exit ${status}}); nothing was removed."
    fi
    if [[ "$label" == "<no value>" ]]; then
        label=""
    fi
    # #146: the label, not the manifest, proves the container belongs to this
    # installer. A manifest with no attempt to compare (an older installation)
    # is only trusted for a container that carries this label.
    managed="$(docker inspect --format "{{index .Config.Labels \"${LABEL_MANAGED}\"}}" "$CONTAINER_NAME" 2>/dev/null || true)"
    if [[ "$managed" == "<no value>" ]]; then
        managed=""
    fi
    if [[ "$managed" != "true" ]]; then
        fatal "The container ${CONTAINER_NAME} is not installer-managed (the label ${LABEL_MANAGED} is '${managed:-missing}'); it was not removed."
    fi
    if [[ -n "$MANIFEST_ATTEMPT" ]]; then
        if [[ "$label" != "$MANIFEST_ATTEMPT" ]]; then
            fatal "The container ${CONTAINER_NAME} was not created by this installation (the manifest records the attempt '${MANIFEST_ATTEMPT}', the container carries '${label:-no ownership label}'); it was not removed."
        fi
        return 0
    fi
    # A manifest without an attempt comes from an older installer: it may
    # only remove a container that carries no ownership label either.
    if [[ -n "$label" ]]; then
        fatal "The container ${CONTAINER_NAME} carries the ownership label '${label}' of another installation; it was not removed."
    fi
}

remove_container() {
    step "Removing the container"
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        verify_container_ownership
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

remove_password_file() {
    step "Removing the password file"
    if [[ -z "$PASSWORD_FILE" ]]; then
        ok "No password file to remove"
        return 0
    fi
    # The path was checked against the one this installer creates before
    # anything was removed (see load_manifest).
    if [[ -f "$PASSWORD_FILE" ]]; then
        rm -f -- "$PASSWORD_FILE" || fatal "Could not remove ${PASSWORD_FILE}."
        ok "Removed ${PASSWORD_FILE}"
    else
        ok "No password file to remove"
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
    # The media is never touched: the media path was checked before anything
    # was removed, so it is one the installer writes (#156), and the resolved
    # paths are compared here, so any spelling (a trailing slash, a ".." or a
    # symlink) that lands on the media is caught and the app data is kept.
    if [[ -z "$MEDIA_PATH" ]] || paths_overlap "$APP_DATA" "$MEDIA_PATH"; then
        fatal "Refusing to delete ${APP_DATA}: it may hold the media (${MEDIA_PATH:-unknown})."
    fi
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

    verify_own_checksum
    load_manifest || exit 1
    check_docker || exit 1
    remove_container
    remove_env_file
    remove_password_file
    remove_secret_key
    remove_app_data
    print_summary
}

main "$@"
