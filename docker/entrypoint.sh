#!/bin/sh
# Container entrypoint: fix /config ownership, then run the app as PUID:PGID.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
CONFIG_DIR="${ALC_CONFIG_DIR:-/config}"
# A trailing slash would spell the same entry differently from what find
# prints, and the manifest below must be matched exactly.
CONFIG_DIR="${CONFIG_DIR%"${CONFIG_DIR##*[!/]}"}"
[ -n "$CONFIG_DIR" ] || CONFIG_DIR="/"
# #146: the root-run installer reads the host paths this file names (the media
# share an upgrade binds) and the root-run uninstaller deletes the paths it
# names. Root ownership is not a lock: /config belongs to PUID:PGID, so the
# container can unlink and recreate this file. What protects the host is that
# the scripts never trust its content: every path is validated before it is
# used. The chown below only keeps the file out of PUID:PGID's ownership; its
# owner-only mode is set by the installer, never here.
MANIFEST_NAME="install-manifest.json"
MANIFEST_PATH="${CONFIG_DIR%/}/${MANIFEST_NAME}"

for value in "$PUID" "$PGID"; do
    case "$value" in
        ''|*[!0-9]*)
            echo "entrypoint: PUID and PGID must be a non-root numeric id, got '$value'" >&2
            exit 64
            ;;
    esac
    if [ "$value" -eq 0 ]; then
        echo "entrypoint: PUID and PGID must be a non-root numeric id, got 0" >&2
        exit 64
    fi
done

if [ "$(id -u)" -ne 0 ]; then
    # Already started with --user: nothing to fix, nothing to drop.
    exec "$@"
fi

mkdir -p "$CONFIG_DIR"
# Only entries with the wrong owner are touched, so restarts stay cheap. The
# manifest is skipped: it is kept root-owned (mode 0600) rather than handed to
# PUID:PGID, and the scripts that read it as root never trust its content.
find "$CONFIG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) \
    ! -path "$MANIFEST_PATH" \
    -exec chown -h "$PUID:$PGID" {} +
# An installation made before this fix has a manifest an older entrypoint
# already handed over. Put it back: the wrong owner is the only thing touched.
if ! find "$CONFIG_DIR" -path "$MANIFEST_PATH" \
    \( ! -user 0 -o ! -group 0 \) -exec chown -h 0:0 {} +; then
    echo "entrypoint: could not give ${MANIFEST_PATH} back to root; it stays writable by the container user" >&2
fi

export HOME="$CONFIG_DIR"
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
