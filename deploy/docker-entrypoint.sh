#!/bin/sh
# Container entrypoint: prepare the persistent volume, then run as the unprivileged `hermes` user.
set -e
HOME_DIR="${HERMES_HOME:-/data/hermes}"
WORK_DIR="${HERMES_CWD:-/data/workspace}"
mkdir -p "$HOME_DIR" "$WORK_DIR" 2>/dev/null || true
if [ "$(id -u)" = "0" ]; then
  # Hosting platforms (Render disks, Fly volumes, Docker named volumes) usually mount
  # the volume root-owned; hand it to the app user and drop root before starting.
  chown -R hermes:hermes "$HOME_DIR" "$WORK_DIR" 2>/dev/null || true
  cd "$WORK_DIR"
  exec setpriv --reuid=hermes --regid=hermes --init-groups -- "$@"
fi
cd "$WORK_DIR" 2>/dev/null || true
exec "$@"
