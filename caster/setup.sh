#!/usr/bin/env bash
# Build and provision the NTRIP caster bundled with this repo (Millipede,
# https://github.com/pbeyssac/millipede-caster). Safe to re-run: the build is
# skipped if it already exists (use --rebuild to force), and config
# regeneration never touches a station's already-issued password.
#
# See caster/README.md before running this - it explains what "bundled"
# means here (own process, own port, pushed into by this repo's own
# streaming server), the one manual step this script does NOT do (opening
# the port in your firewall), and the Millipede "Mount Point Taken" trap
# this script's generated sourcetable already avoids.
set -euo pipefail

CASTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$CASTER_DIR")"
BUILD_DIR="$CASTER_DIR/millipede-caster"
REBUILD=false
USER_UNIT=true
for arg in "$@"; do
    case "$arg" in
        --rebuild)      REBUILD=true ;;
        # For a caller that runs the caster under its own (system) unit, e.g.
        # install.sh - which also avoids the fixed user-unit name colliding
        # between two instances on one host.
        --no-user-unit) USER_UNIT=false ;;
        *) echo "usage: $0 [--rebuild] [--no-user-unit]" >&2; exit 2 ;;
    esac
done

if [ ! -f "$REPO_DIR/.env" ]; then
    echo "error: $REPO_DIR/.env not found." >&2
    echo "Copy .env.example to .env, set STREAM_CASTER_STATIONS, then re-run this script." >&2
    exit 1
fi

# --- 1. Build dependencies (needs sudo once; skipped if already present) ----
if ! dpkg -s libevent-dev >/dev/null 2>&1 || ! dpkg -s libcyaml-dev >/dev/null 2>&1; then
    echo "Installing build dependencies (needs sudo)..."
    sudo apt-get update
    sudo apt-get install -y pkg-config libcyaml-dev libevent-dev libjson-c-dev libssl-dev git
else
    echo "Build dependencies already present, skipping apt."
fi

# --- 2. Clone + build, unprivileged, in place (no `make install`) -----------
if $REBUILD && [ -d "$BUILD_DIR" ]; then
    rm -rf "$BUILD_DIR"
fi
if [ ! -d "$BUILD_DIR" ]; then
    git clone https://github.com/pbeyssac/millipede-caster.git "$BUILD_DIR"
fi
make -C "$BUILD_DIR/caster"
echo "Built $BUILD_DIR/caster/caster"

# --- 3. Generate caster.yaml / sourcetable.dat / source.auth from .env ------
# Also writes STREAM_CASTER_PASSWORDS / STREAM_CASTER_ENABLE=true back into
# .env - see caster/generate_config.py.
PYTHON="$REPO_DIR/venv/bin/python3"
[ -x "$PYTHON" ] || PYTHON="python3"
"$PYTHON" "$CASTER_DIR/generate_config.py"

if ! $USER_UNIT; then
    echo "Caster built and configured; skipping the user unit (--no-user-unit)."
    exit 0
fi

# --- 4. User-level systemd unit, no sudo for routine start/stop/restart -----
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
sed -e "s#{{CASTER_BIN}}#$BUILD_DIR/caster/caster#" \
    -e "s#{{CASTER_YAML}}#$BUILD_DIR/etc/caster.yaml#" \
    "$CASTER_DIR/millipede-caster.service.template" > "$UNIT_DIR/millipede-caster.service"
systemctl --user daemon-reload

cat <<EOF

Done. Two things left, both manual by design (see caster/README.md):

1. One-time, needs sudo, only if you want the service to survive logout/reboot
   without an active session:
       sudo loginctl enable-linger "$USER"

2. Start it, and open the port in your firewall so rovers outside this host
   can reach it (this script never touches firewall rules):
       systemctl --user enable --now millipede-caster
       systemctl --user status millipede-caster
       # e.g. ufw:  sudo ufw allow <STREAM_CASTER_PORT>/tcp comment 'NTRIP caster (wormhole)'

Then start/restart the streaming server - STREAM_CASTER_ENABLE is now true in
.env, so it will push each configured station's RTCM3 into this caster.
EOF
