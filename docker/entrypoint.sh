#!/bin/sh
# Container entrypoint: fix /config ownership, then run the app as PUID:PGID.
set -eu

PUID="${PUID:-99}"
PGID="${PGID:-100}"
CONFIG_DIR="${ALC_CONFIG_DIR:-/config}"

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
# Only entries with the wrong owner are touched, so restarts stay cheap.
find "$CONFIG_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) \
    -exec chown -h "$PUID:$PGID" {} +

export HOME="$CONFIG_DIR"
exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
