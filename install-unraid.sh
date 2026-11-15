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
#   5. pulls the image and verifies its signature (when cosign is available);
#   6. records the files and intended container in
#      <app data>/install-manifest.json BEFORE creating the container on the
#      asked bind address, so even a failed docker run is recoverable;
#   7. waits, bounded, for the container to report a healthy state and marks
#      the manifest as verified only when it does.
#
# With --upgrade the manifest drives the whole run: the app data is backed
# up, the replacement is recorded before the old container is removed, and
# the container is created again with the same labels, mounts and
# verification.
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
DEFAULT_BIND_ADDRESS="0.0.0.0"
DEFAULT_START_TIMEOUT=120
DEFAULT_START_INTERVAL=2

# The keyless signature of the publish workflow: the certificate identity is
# the workflow file on a release tag of this repository.
COSIGN_IDENTITY_REGEXP='^https://github.com/almali-dev/almali-library-curator/\.github/workflows/publish\.yml@refs/tags/v'
COSIGN_OIDC_ISSUER="https://token.actions.githubusercontent.com"
DEFAULT_APPDATA="/mnt/user/appdata/almali-library-curator"
DEFAULT_MEDIA="/mnt/user/media"
DEFAULT_PUID=99
DEFAULT_PGID=100
DEFAULT_TZ="Etc/UTC"
DEFAULT_UNRAID_MNT="no"

CONFIG_CONTAINER_PATH="/config"
MEDIA_CONTAINER_PATH="/media"
# The working directory of the image (the Dockerfile sets none, so it is
# "/"). The application resolves a relative path against it.
CONTAINER_WORKDIR="/"
UNRAID_MNT_HOST="/mnt"
UNRAID_MNT_CONTAINER="/unraid/mnt"

ENV_FILE_NAME="${APP_NAME}.env"
MANIFEST_NAME="install-manifest.json"
MANIFEST_VERSION=1
# The manifest is written before docker run; it only says the start is
# verified once the container is healthy.
UPGRADE="false"
APPDATA_ARGUMENT=""
START_VERIFIED="false"
# The upgrade state recorded in the manifest: none, upgrading, rolled-back
# or complete.
UPGRADE_STATE="none"
# The identifier of this run, used as the container label and recorded in
# the manifest.
ATTEMPT_ID=""
# The attempt recorded in the manifest: the container the manifest names
# carries it, so only that container is ever replaced or removed.
MANIFEST_ATTEMPT=""
RECORDED_ATTEMPT=""
SECRET_KEY_NAME="secret_key"
SECRET_KEY_BYTES=64
MIN_PASSWORD_LENGTH=8

LABEL_APP="ai.almali-dev.app"
LABEL_INSTALLER="ai.almali-dev.installer"
LABEL_MANAGED="ai.almali-dev.managed"
# Every container this installer creates carries the attempt that created
# it, so a container of another installation is never mistaken for ours.
LABEL_ATTEMPT="ai.almali-dev.install-attempt"

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
        *[Bb]ind*)
            if [[ -z "$value" ]]; then
                error "The bind address cannot be empty"
                return 1
            fi
            if ! [[ "$value" =~ ^[A-Za-z0-9._:%-]+$ ]]; then
                error "The bind address must be an IP address or a host name"
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
            fi
            if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
                error "The user name cannot contain a line break"
                return 1
            fi ;;
        *directory*|*share*|*Directory*|*Share*)
            if [[ "$value" != /* ]]; then
                error "${prompt} must be an absolute path"
                return 1
            fi
            # Docker splits a bind it is given as SRC:DST[:MODE], and a
            # --mount specification separates its options with commas. A
            # separator inside a host path would bind the wrong directory.
            if [[ "$value" == *:* || "$value" == *,* ]]; then
                error "${prompt} cannot contain ':' or ','"
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

# ask PROMPT DEFAULT RESULT_VAR [ENVIRONMENT_VARIABLE] [FALLBACK]
#
# A value given through the environment variable is used as it is and
# validated once: an unusable value stops the installer instead of asking
# the same question again. With FALLBACK=default the default is kept when
# the variable is not set, also in a non-interactive run.
ask() {
    local prompt="$1" default="$2" result_var="$3" env_name="${4:-}" fallback="${5:-strict}"
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
        if [[ "$fallback" == "default" ]]; then
            printf -v "$result_var" '%s' "$default"
            info "${prompt}: ${default} (default)"
            return 0
        fi
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
        error "Checksum mismatch: $(basename -- "$SCRIPT_PATH") does not match $(basename -- "$CHECKSUM_PATH")."
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

# paths_overlap A B -> success when A and B are the same directory or one
# holds the other (both resolved, so symlinks and ".." cannot hide it).
paths_overlap() {
    local a b
    a="$(realpath -m -- "$1")"
    b="$(realpath -m -- "$2")"
    [[ "$a" == "$b" || "$a" == "$b"/* || "$b" == "$a"/* ]]
}

# --- The answers ------------------------------------------------------------

collect_answers() {
    step "Configuration"
    # The helpers set the variable they are given by name, so they are called
    # directly: a command substitution would run them in a subshell and the
    # answer would be lost.
    if [[ -n "$APPDATA_ARGUMENT" ]]; then
        ALC_INSTALL_APPDATA="$APPDATA_ARGUMENT"
    fi
    ask "App data directory" "$DEFAULT_APPDATA" APPDATA ALC_INSTALL_APPDATA
    ask "Media share (mounted read-only)" "$DEFAULT_MEDIA" MEDIA ALC_INSTALL_MEDIA
    ask_yes_no "Mount /mnt read-only as ${UNRAID_MNT_CONTAINER}?" "$DEFAULT_UNRAID_MNT" UNRAID_MNT ALC_INSTALL_UNRAID_MNT
    ask "Web UI port" "$DEFAULT_PORT" PORT ALC_INSTALL_PORT
    ask "Bind address" "$DEFAULT_BIND_ADDRESS" BIND ALC_INSTALL_BIND default
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
    local interval="${ALC_INSTALL_START_INTERVAL:-$DEFAULT_START_INTERVAL}"
    if ! [[ "$interval" =~ ^[1-9][0-9]*$ ]]; then
        fatal "The start poll interval must be a positive number of seconds."
    fi
    [[ -d "$MEDIA" ]] || fatal "The media share ${MEDIA} does not exist."
    if paths_overlap "$APPDATA" "$MEDIA"; then
        fatal "The app data directory ${APPDATA} and the media share ${MEDIA} overlap; choose separate directories."
    fi
    ok "Media share: ${MEDIA} (read-only)"
    if [[ "$UNRAID_MNT" == "yes" ]]; then
        ok "Host ${UNRAID_MNT_HOST} is mounted read-only as ${UNRAID_MNT_CONTAINER}"
    else
        info "The optional ${UNRAID_MNT_CONTAINER} mount is not used."
    fi
    ok "App data: ${APPDATA}"
    # An IPv6 address is published between brackets, as Docker expects.
    if [[ "$BIND" == *:* ]]; then
        PUBLISH_ADDRESS="[${BIND}]"
    else
        PUBLISH_ADDRESS="${BIND}"
    fi
    ok "Web UI: http://<unraid-ip>:${PORT} (published on ${BIND}:${PORT})"
    ok "Container: ${CONTAINER_NAME} (${IMAGE})"
}

# Refuse before anything is written, so a rerun never changes an existing
# installation's login and never claims files it did not create.
check_conflicts() {
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        fatal "A container named '${CONTAINER_NAME}' already exists. Use --upgrade for a script installation, or uninstall-unraid.sh first."
    fi
    if [[ -e "$ENV_FILE" ]]; then
        fatal "${ENV_FILE} already exists from an earlier installation; use --upgrade or run uninstall-unraid.sh first."
    fi
}

# --- Manifest-driven upgrade -----------------------------------------------

# jq is required only for upgrades: parse data, never source the manifest or
# environment file as shell code. Older v1 manifests lack unraid_mnt; recover
# that one answer from the existing environment file without evaluating it.
load_upgrade_manifest() {
    step "Reading the upgrade manifest"
    command -v jq >/dev/null 2>&1 || fatal "jq is required to read the upgrade manifest."
    APPDATA="${APPDATA_ARGUMENT:-${ALC_INSTALL_APPDATA:-$DEFAULT_APPDATA}}"
    validate_answer "App data directory" "$APPDATA" \
        || fatal "Invalid app data directory '${APPDATA}' in the upgrade manifest; nothing was changed."
    MANIFEST_PATH="${APPDATA}/${MANIFEST_NAME}"
    [[ -f "$MANIFEST_PATH" ]] || fatal "No ${MANIFEST_NAME} in ${APPDATA}; only script installations can be upgraded."
    if ! jq -e '
        .version == 1 and
        ([.app_data, .media, .container_name, .image, .env_file, .secret_key, .bind_address]
         | all(.[]; type == "string" and length > 0 and
               (explode | all(.[]; . >= 32 and . != 34 and . != 92)))) and
        (.port | type == "number" and floor == . and . >= 1 and . <= 65535) and
        (.secret_key_created | type == "boolean") and
        ((has("unraid_mnt") | not) or .unraid_mnt == "yes" or .unraid_mnt == "no") and
        ((has("upgrade_state") | not) or (.upgrade_state | type == "string" and length > 0)) and
        ((has("install_attempt") | not) or (.install_attempt | type == "string" and length > 0))
    ' "$MANIFEST_PATH" >/dev/null 2>&1; then
        fatal "Invalid upgrade manifest ${MANIFEST_PATH}; nothing was changed."
    fi
    local recorded_appdata
    recorded_appdata="$(jq -r '.app_data' "$MANIFEST_PATH")"
    [[ "$(realpath -m -- "$APPDATA")" == "$(realpath -m -- "$recorded_appdata")" ]] \
        || fatal "The manifest belongs to a different app data directory."
    # Retain the exact recorded paths, including custom paths with spaces.
    APPDATA="$recorded_appdata"
    MEDIA="$(jq -r '.media' "$MANIFEST_PATH")"
    # The media path of an upgrade is not prompted: it is bound exactly like a
    # fresh install binds the answer, so it passes the same check. A relative
    # path would be tested and bound against the caller's directory, and a
    # separator would be split by Docker long before the container starts.
    validate_answer "Media share (mounted read-only)" "$MEDIA" \
        || fatal "Invalid media share '${MEDIA}' in the upgrade manifest (${MANIFEST_PATH}); nothing was changed."
    CONTAINER_NAME="$(jq -r '.container_name' "$MANIFEST_PATH")"
    ENV_FILE="$(jq -r '.env_file' "$MANIFEST_PATH")"
    SECRET_KEY_PATH="$(jq -r '.secret_key' "$MANIFEST_PATH")"
    SECRET_KEY_CREATED="$(jq -r '.secret_key_created' "$MANIFEST_PATH")"
    BIND="$(jq -r '.bind_address' "$MANIFEST_PATH")"
    PORT="$(jq -r '.port' "$MANIFEST_PATH")"
    [[ "$ENV_FILE" == "${APPDATA}/${ENV_FILE_NAME}" && -f "$ENV_FILE" ]] \
        || fatal "The manifest environment file is missing or outside app data."
    [[ "$SECRET_KEY_PATH" == "${APPDATA}/${SECRET_KEY_NAME}" && -f "$SECRET_KEY_PATH" ]] \
        || fatal "The manifest signing key is missing or outside app data."
    [[ "$CONTAINER_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] \
        || fatal "Invalid container name in upgrade manifest."
    validate_answer "Bind address" "$BIND" || fatal "Invalid bind address in upgrade manifest."
    UNRAID_MNT="$(jq -r '.unraid_mnt // empty' "$MANIFEST_PATH")"
    if [[ -z "$UNRAID_MNT" ]]; then
        UNRAID_MNT="no"
        if grep -qx 'ALC_UNRAID_ROOT=/unraid/mnt' "$ENV_FILE"; then
            UNRAID_MNT="yes"
        fi
    fi
    RECORDED_ATTEMPT="$(jq -r '.install_attempt // empty' "$MANIFEST_PATH")"
    MANIFEST_ATTEMPT="$RECORDED_ATTEMPT"
    if [[ -n "${ALC_INSTALL_IMAGE:-}" ]]; then
        IMAGE="$ALC_INSTALL_IMAGE"
    elif [[ -n "${ALC_INSTALL_TAG:-}" ]]; then
        IMAGE="${IMAGE_REPOSITORY}:${ALC_INSTALL_TAG}"
    else
        IMAGE="$(jq -r '.image' "$MANIFEST_PATH")"
    fi
    # An upgrade never silently downgrades signature verification.
    ALC_INSTALL_REQUIRE_SIGNATURE=1
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        local managed attempt
        managed="$(docker inspect --format "{{index .Config.Labels \"${LABEL_MANAGED}\"}}" "$CONTAINER_NAME")"
        [[ "$managed" == "true" ]] || fatal "The manifest container is not installer-managed; nothing was changed."
        # The manifest and the container carry the same attempt: only the
        # container this installation created may be replaced. A manifest
        # without an attempt comes from an older installer, and it may only
        # replace a container that carries no attempt label either.
        attempt="$(docker inspect --format "{{index .Config.Labels \"${LABEL_ATTEMPT}\"}}" "$CONTAINER_NAME" || true)"
        if [[ "$attempt" == "<no value>" ]]; then
            attempt=""
        fi
        if [[ -n "$RECORDED_ATTEMPT" ]]; then
            [[ "$attempt" == "$RECORDED_ATTEMPT" ]] \
                || fatal "The manifest container was not created by this installation; nothing was changed."
        else
            [[ -z "$attempt" ]] \
                || fatal "The manifest container carries the attempt '${attempt}' of another installation; nothing was changed."
        fi
    fi
}

# The automatic archive covers the app data directory and nothing else. A
# file the app reads from outside it (the target of a symlink, an external
# database or config path) is not in that archive, so the upgrade refuses
# instead of replacing a container over data that was never backed up.
#
# Every path is resolved in the CONTAINER namespace, because that is where
# the variables are read (see almali_library_curator/config.py): the app data
# directory is ${CONFIG_CONTAINER_PATH} there and the media is
# ${MEDIA_CONTAINER_PATH}. A path that resolves inside ${CONFIG_CONTAINER_PATH}
# is covered by the archive; a path that resolves outside it is not, even
# when the same name does not exist on the host.
normalize_container_path() {
    local path="$1" part out=""
    local -a parts=()
    IFS='/' read -r -a parts <<< "$path"
    for part in "${parts[@]}"; do
        case "$part" in
            ''|'.') continue ;;
            '..') out="${out%/*}" ;;
            *) out="${out}/${part}" ;;
        esac
    done
    printf '%s' "${out:-/}"
}

# The path the container resolves for a configured value. A relative path is
# resolved against the working directory of the image, which is how the
# application resolves it.
container_path_of() {
    local value="$1"
    if [[ "$value" == /* ]]; then
        normalize_container_path "$value"
    else
        normalize_container_path "${CONTAINER_WORKDIR}/${value}"
    fi
}

# inside_config PATH -> success when the container path is the archived app
# data directory or lives inside it.
inside_config() {
    local path="$1"
    [[ "$path" == "$CONFIG_CONTAINER_PATH" || "$path" == "$CONFIG_CONTAINER_PATH"/* ]]
}

check_external_data() {
    local link target key value relative container_link container_target external=""
    for key in ALC_DATABASE_PATH ALC_CONFIG_DIR; do
        value="$(sed -n "s/^${key}=//p" "$ENV_FILE" 2>/dev/null | tail -n 1)"
        [[ -n "$value" ]] || continue
        target="$(container_path_of "$value")"
        if inside_config "$target"; then
            continue
        fi
        external="${external} ${value} (in the container: ${target})"
    done
    while IFS= read -r link; do
        relative="${link#"$APPDATA"}"
        container_link="${CONFIG_CONTAINER_PATH}${relative}"
        target="$(readlink -- "$link" 2>/dev/null || true)"
        [[ -n "$target" ]] || continue
        if [[ "$target" == /* ]]; then
            container_target="$(normalize_container_path "$target")"
        else
            container_target="$(normalize_container_path "$(dirname -- "$container_link")/${target}")"
        fi
        if inside_config "$container_target"; then
            continue
        fi
        external="${external} ${target} (in the container: ${container_target})"
    done < <(find "$APPDATA" -path "${APPDATA}/backups" -prune -o -type l -print 2>/dev/null)
    [[ -n "$external" ]] || return 0
    if is_true "${ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL:-}"; then
        warn "These paths resolve outside ${CONFIG_CONTAINER_PATH} (the app data directory inside the container) and are NOT in the automatic backup:${external}"
        warn "Back them up yourself before the upgrade; the upgrade cannot restore them."
        return 0
    fi
    fatal "The upgrade refuses to continue: these paths resolve outside ${CONFIG_CONTAINER_PATH} (the app data directory inside the container) and are not in the automatic backup:${external}. Back them up (tar -C <directory> -cf <archive> <file>), move them into the app data directory ${APPDATA}, or set ALC_INSTALL_UPGRADE_ALLOW_EXTERNAL=1 to continue after your own backup."
}

# The label every container this installer creates carries. A container with
# another attempt's label, or with none at all, was not created by this run.
container_is_ours() {
    local label=""
    label="$(docker inspect --format "{{index .Config.Labels \"${LABEL_ATTEMPT}\"}}" \
        "$CONTAINER_NAME" 2>/dev/null || true)"
    [[ "$label" == "$ATTEMPT_ID" ]]
}

# Undo what this run created when the container name turned out to belong to
# another installation. Nothing is left that the recovery procedure could use
# to remove a container this installation did not create.
#
# The upgrade reaches this rollback only after the old container was removed,
# so the manifest is the only record the uninstaller can verify ownership
# with. It is kept, together with the environment file, the signing key and
# the app data: the uninstaller refuses to remove a container whose ownership
# label does not match, and it can finish the cleanup once the name is free.
rollback_failed_install() {
    if [[ "$UPGRADE" == "true" ]]; then
        return 0
    fi
    rm -f -- "$MANIFEST_PATH"
    rm -f -- "$ENV_FILE"
    if [[ "$SECRET_KEY_CREATED" == "true" ]]; then
        rm -f -- "$SECRET_KEY_PATH"
    fi
    return 0
}

backup_for_upgrade() {
    step "Backing up app data before the upgrade"
    check_external_data
    local backup was_running="false" exists="false" kept="kept" restarted="false"
    # Create the backup destination before stopping anything; a bad path or
    # permission error must not interrupt a working installation.
    ( umask 077; mkdir -p "${APPDATA}/backups" ) || fatal "Could not create the backup directory."
    backup="$(mktemp "${APPDATA}/backups/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.partial")" \
        || fatal "Could not create the upgrade backup."
    if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1; then
        exists="true"
        was_running="$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME")"
        docker stop "$CONTAINER_NAME" || fatal "Could not stop the container for a consistent backup."
    fi
    # A cold copy includes SQLite's WAL and sidecars, settings and encryption
    # keys. Never copy just the live .db, and never include previous backups.
    if ! tar -C "$APPDATA" --exclude='./backups' -cf "$backup" . \
       || ! mv -- "$backup" "${backup%.partial}.tar"; then
        # A half written archive is worse than none: it fills the app data
        # volume and it cannot be restored.
        rm -f -- "$backup"
        if [[ "$was_running" == "true" ]]; then
            docker start "$CONTAINER_NAME" || warn "Could not restart ${CONTAINER_NAME}; start it manually."
        fi
        fatal "The upgrade backup failed; the old container and data were kept and no archive was left behind."
    fi
    ok "Upgrade backup: ${backup%.partial}.tar (owner-only; keep it private)"
    # The replacement is recorded before the old container goes away: an
    # interruption must not leave a stale verified manifest behind.
    UPGRADE_STATE="upgrading"
    START_VERIFIED="false"
    if ! write_manifest "the old container is being replaced"; then
        if [[ "$was_running" == "true" ]]; then
            docker start "$CONTAINER_NAME" || warn "Could not restart ${CONTAINER_NAME}; start it manually."
        fi
        fatal "Could not write ${MANIFEST_PATH}; the old container and the backup were kept."
    fi
    if [[ "$exists" == "true" ]]; then
        if ! docker rm "$CONTAINER_NAME"; then
            # The old container is still there: put it back to work.
            if [[ "$was_running" == "true" ]]; then
                if docker start "$CONTAINER_NAME"; then
                    kept="kept and started again"
                    restarted="true"
                else
                    kept="kept but could not be started again; start it manually"
                    warn "Could not restart ${CONTAINER_NAME}; start it manually."
                fi
            fi
            UPGRADE_STATE="rolled-back"
            if [[ "$restarted" == "true" ]]; then
                START_VERIFIED="true"
            else
                START_VERIFIED="false"
            fi
            write_manifest "the old container was kept" || true
            fatal "Could not remove the old container ${CONTAINER_NAME}; it was ${kept}. The backup was kept: ${backup%.partial}.tar"
        fi
    fi
    # The old container is gone: the manifest now names the attempt of the
    # replacement, so the recovery procedure can never point at a container
    # of another installation.
    MANIFEST_ATTEMPT="$ATTEMPT_ID"
    if ! write_manifest "the old container was removed"; then
        fatal "Could not write ${MANIFEST_PATH}; the backup was kept: ${backup%.partial}.tar"
    fi
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
    local requested="${IMAGE%%@*}" repository="" component="" digests="" entry=""
    component="${requested##*/}"
    if [[ "$component" == *:* ]]; then
        repository="${requested%:*}"
    else
        repository="$requested"
    fi
    digests="$(docker image inspect --format \
        '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE")" \
        || fatal "Could not resolve the pulled image digest."
    PULLED_IMAGE=""
    while IFS= read -r entry; do
        if [[ "${entry%@*}" == "$repository" ]]; then
            PULLED_IMAGE="$entry"
            break
        fi
    done <<< "$digests"
    [[ -n "$PULLED_IMAGE" ]] \
        || fatal "The pulled image has no digest for repository ${repository}."
    [[ "$PULLED_IMAGE" =~ ^[^[:space:]]+@sha256:[[:xdigit:]]{64}$ ]] \
        || fatal "The pulled image has no valid immutable digest."
    ok "Pulled ${IMAGE} (${PULLED_IMAGE})"
}

# The image is published with a keyless signature (the publish workflow on a
# release tag). When cosign is on the PATH the signature is verified before
# anything is created; without cosign the installation warns and continues,
# unless ALC_INSTALL_REQUIRE_SIGNATURE=1 makes it stop.
verify_image_signature() {
    step "Verifying the image signature"
    if ! command -v cosign >/dev/null 2>&1; then
        if is_true "${ALC_INSTALL_REQUIRE_SIGNATURE:-}"; then
            fatal "cosign is not on PATH and ALC_INSTALL_REQUIRE_SIGNATURE=1: the signature of ${IMAGE} was not verified and nothing was created."
        fi
        warn "cosign is not on PATH: the signature of ${IMAGE} was not verified."
        warn "Install cosign, or set ALC_INSTALL_REQUIRE_SIGNATURE=1 to stop the installation when it is missing."
        return 0
    fi
    if ! cosign verify \
        --certificate-identity-regexp "${COSIGN_IDENTITY_REGEXP}" \
        --certificate-oidc-issuer "${COSIGN_OIDC_ISSUER}" \
        "${PULLED_IMAGE}"; then
        fatal "The signature of ${IMAGE} could not be verified; nothing was created."
    fi
    ok "The signature of ${IMAGE} is valid"
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
        --label "${LABEL_ATTEMPT}=${ATTEMPT_ID}"
        --env-file "$ENV_FILE"
        -p "${PUBLISH_ADDRESS}:${PORT}:${CONTAINER_PORT}"
        -v "${APPDATA}:${CONFIG_CONTAINER_PATH}"
        -v "${MEDIA}:${MEDIA_CONTAINER_PATH}:ro"
    )
    if [[ "$UNRAID_MNT" == "yes" ]]; then
        arguments+=(-v "${UNRAID_MNT_HOST}:${UNRAID_MNT_CONTAINER}:ro")
    fi
    arguments+=("$PULLED_IMAGE")
    if ! docker "${arguments[@]}"; then
        # Another installation may have taken the name between the conflict
        # check and this run. Its container is not ours, and the recovery
        # procedure must never be able to remove it.
        if docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1 && ! container_is_ours; then
            rollback_failed_install
            if [[ "$UPGRADE" == "true" ]]; then
                fatal "The container name '${CONTAINER_NAME}' belongs to another installation; this run created nothing. The manifest ${MANIFEST_PATH}, the environment file, the signing key and the app data were kept: the uninstaller refuses to remove a container of another installation and can finish the cleanup once the name is free."
            fi
            fatal "The container name '${CONTAINER_NAME}' belongs to another installation; this run created nothing and left no manifest."
        fi
        fatal "The container could not be created."
    fi
    ok "Created the container ${CONTAINER_NAME}"
}

# The container must report a healthy state before the installation counts
# as done. The wait is bounded (ALC_INSTALL_START_TIMEOUT seconds, 120 by
# default), so a container that never comes up stops the installation
# instead of hanging forever.
fail_start() {
    error "$1"
    error "Read its logs with: docker logs ${CONTAINER_NAME}"
    error "The manifest ${MANIFEST_PATH} records the incomplete installation (start_verified: false)."
    error "Remove what was created with: bash uninstall-unraid.sh --app-data ${APPDATA}"
    exit 1
}

wait_for_healthy() {
    step "Waiting for the container to become healthy"
    local timeout="${ALC_INSTALL_START_TIMEOUT:-$DEFAULT_START_TIMEOUT}"
    local interval="${ALC_INSTALL_START_INTERVAL:-$DEFAULT_START_INTERVAL}"
    local started=$SECONDS health="" state=""
    if ! [[ "$timeout" =~ ^[0-9]+$ ]]; then
        timeout="$DEFAULT_START_TIMEOUT"
    fi
    if ! [[ "$interval" =~ ^[0-9]+$ ]]; then
        interval="$DEFAULT_START_INTERVAL"
    fi
    while true; do
        local remaining=$((timeout - (SECONDS - started)))
        read -r state health <<< "$(timeout "${remaining}s" docker inspect --format \
            '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' \
            "$CONTAINER_NAME" 2>/dev/null || true)"
        if [[ "$state" == "exited" || "$state" == "dead" ]]; then
            fail_start "The container ${CONTAINER_NAME} exited (state: ${state})."
        fi
        if [[ "$state" == "running" && "$health" == "healthy" ]]; then
            ok "The container ${CONTAINER_NAME} is healthy"
            return 0
        fi
        if [[ "$health" == "unhealthy" ]]; then
            fail_start "The container ${CONTAINER_NAME} is unhealthy."
        fi
        if (( SECONDS - started >= timeout )); then
            fail_start "The container ${CONTAINER_NAME} did not become healthy within ${timeout} seconds (last health status: ${health:-none})."
        fi
        remaining=$((timeout - (SECONDS - started)))
        sleep "$(( interval < remaining ? interval : remaining ))"
    done
}

json_escape() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s' "$value"
}

write_manifest() {
    local note="${1:-}"
    step "Recording what was created"
    local timestamp
    timestamp="$(date -Iseconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
    local temporary
    temporary="$(mktemp "${APPDATA}/.manifest.XXXXXX")" || return 1
    chmod 600 "$temporary"
    # The guarded call sites ("if ! write_manifest" and "write_manifest ... ||")
    # disable `set -e` inside this function, so the write is checked here: a
    # failed or partial write must leave the previous manifest in place rather
    # than move the broken temporary file over it. The document is built in
    # memory and written by one simple command, whose failed redirection or
    # write returns non-zero (a failed redirection of a negated group command
    # does not), then its size is compared with what was meant to be written.
    local content
    content="$(
        printf '{\n'
        printf '  "version": %s,\n' "$MANIFEST_VERSION"
        printf '  "installed_at": "%s",\n' "$(json_escape "$timestamp")"
        printf '  "app_data": "%s",\n' "$(json_escape "$APPDATA")"
        printf '  "media": "%s",\n' "$(json_escape "$MEDIA")"
        printf '  "container_name": "%s",\n' "$(json_escape "$CONTAINER_NAME")"
        printf '  "image": "%s",\n' "$(json_escape "$IMAGE")"
        printf '  "env_file": "%s",\n' "$(json_escape "$ENV_FILE")"
        printf '  "secret_key": "%s",\n' "$(json_escape "$SECRET_KEY_PATH")"
        printf '  "secret_key_created": %s,\n' "$SECRET_KEY_CREATED"
        printf '  "unraid_mnt": "%s",\n' "$(json_escape "$UNRAID_MNT")"
        printf '  "bind_address": "%s",\n' "$(json_escape "$BIND")"
        printf '  "port": %s,\n' "$PORT"
        if [[ -n "$MANIFEST_ATTEMPT" ]]; then
            printf '  "install_attempt": "%s",\n' "$(json_escape "$MANIFEST_ATTEMPT")"
        fi
        printf '  "upgrade_state": "%s",\n' "$(json_escape "$UPGRADE_STATE")"
        printf '  "start_verified": %s\n' "$START_VERIFIED"
        printf '}\n'
    )"
    content+=$'\n'
    local written="" expected=""
    expected="$(printf '%s' "$content" | LC_ALL=C wc -c)"
    if ! printf '%s' "$content" > "$temporary" \
        || [[ ! -f "$temporary" || -L "$temporary" ]] \
        || ! written="$(LC_ALL=C wc -c < "$temporary" 2>/dev/null)" \
        || (( written != expected )); then
        rm -f -- "$temporary"
        error "The manifest could not be written to ${temporary}; the previous ${MANIFEST_PATH} was left unchanged."
        return 1
    fi
    if ! mv -f -- "$temporary" "$MANIFEST_PATH"; then
        rm -f -- "$temporary"
        error "The new manifest could not replace ${MANIFEST_PATH}; the previous manifest was left unchanged."
        return 1
    fi
    chmod 600 "$MANIFEST_PATH" || return 1
    if [[ -n "$note" ]]; then
        ok "Wrote ${MANIFEST_PATH} (${note})"
    else
        ok "Wrote ${MANIFEST_PATH}"
    fi
    return 0
}

print_summary() {
    step "Installation complete"
    {
        printf '\n'
        printf '  The web UI is at http://<unraid-ip>:%s ; sign in with the owner login.\n' "$PORT"
        printf '  The web port is published on %s:%s.\n' "$BIND" "$PORT"
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
    # One identifier per run: it labels the container this run creates and is
    # recorded in the manifest, so a container of another installation is
    # never mistaken for ours.
    ATTEMPT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$-${RANDOM}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --upgrade) UPGRADE="true"; shift ;;
            --app-data)
                [[ $# -ge 2 && -n "$2" ]] || fatal "--app-data requires a path."
                APPDATA_ARGUMENT="$2"; shift 2 ;;
            -h|--help)
                echo "Usage: install-unraid.sh [--upgrade] [--app-data PATH]"
                echo "--upgrade: reuse the manifest configuration, require cosign and jq, and back up app data before replacing the container."
                echo "Re-pulls the recorded image unless ALC_INSTALL_IMAGE or ALC_INSTALL_TAG selects another; other configuration answers are ignored."
                return 0 ;;
            *) fatal "Unknown flag: $1. Use --help." ;;
        esac
    done
    print_banner
    verify_own_checksum || exit 1
    check_environment || exit 1
    if [[ "$UPGRADE" == "true" ]]; then
        load_upgrade_manifest
    else
        collect_answers
    fi
    validate_answers
    if [[ "$UPGRADE" != "true" ]]; then
        check_conflicts
    fi
    pull_image
    verify_image_signature
    if [[ "$UPGRADE" == "true" ]]; then
        # The manifest is written before the old container is removed.
        backup_for_upgrade
    else
        prepare_app_data
        ensure_secret_key
        write_env_file
        MANIFEST_ATTEMPT="$ATTEMPT_ID"
        # Record intent before Docker can fail (for example, a busy host port).
        write_manifest "the start is not verified yet" || fatal "Could not write ${MANIFEST_PATH}."
    fi
    create_container
    wait_for_healthy
    START_VERIFIED="true"
    if [[ "$UPGRADE" == "true" ]]; then
        UPGRADE_STATE="complete"
        # The new container carries this attempt.
        MANIFEST_ATTEMPT="$ATTEMPT_ID"
    fi
    write_manifest "the start is verified" || fatal "Could not write ${MANIFEST_PATH}."
    print_summary
}

main "$@"
