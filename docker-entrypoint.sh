#!/bin/sh
# Start the server as the unprivileged "marrow" user.
#
# The container starts as root only long enough to make sure the data folder
# (usually a Docker volume) belongs to that user. Volumes created by older
# images were written by root, so this keeps upgrades working without any
# manual chown. If the container is already started as a non-root user
# (docker run --user ...), the server simply runs as that user.
set -e

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$NEWS_DIR"
    chown -R marrow:marrow "$NEWS_DIR"
    exec setpriv --reuid=marrow --regid=marrow --init-groups "$@"
fi
exec "$@"
