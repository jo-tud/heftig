#!/bin/sh
# Run Heftig with sensible file ownership on the bind-mounted archive:
# - rootful Docker/Podman: container root is real root -> drop to PUID:PGID (default 1000:1000)
# - rootless Podman/Docker: container root already is your unprivileged host user -> keep it,
#   so files in ./archive belong to you on the host
set -e
# files and directories Heftig creates are for the owner only (exports, backups included)
umask 077
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    # first line of uid_map: "<inside> <outside> <count>"; outside 0 == really root
    outside=$(awk 'NR==1 {print $2}' /proc/self/uid_map 2>/dev/null || echo 0)
    if [ "$outside" = "0" ]; then
        for d in "${HEFTIG_ARCHIVE_DIR:-/archive}" "${HEFTIG_CONSUME_DIR:-/consume}" "${HEFTIG_FOLDER_DIR:-/folder}"; do
            [ -d "$d" ] || mkdir -p "$d"
            # only the top-level directory, never recursive
            if [ "$(stat -c %u "$d")" != "$PUID" ]; then chown "$PUID:$PGID" "$d" || true; fi
        done
        exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --inh-caps=-all -- "$@"
    fi
fi
exec "$@"
