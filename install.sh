#!/usr/bin/env bash
# Wormhole installer - installs, tests, updates and removes wormhole instances.
#
# One run of "Install" sets up one complete instance: code, Python environment,
# .env with generated secrets, the optional bundled NTRIP caster, systemd units,
# firewall rules and a server test. Every further instance on the same host is
# one more run with its own port block, directory and units.
#
#   bash install.sh                        menu (install / manage)
#   bash install.sh --unattended FILE      install from an answers file
#   bash install.sh status|test|update|restart|uninstall NAME [--purge] [--yes]
#
# Run it as a normal user with sudo rights, from a terminal. Do not pipe it into
# bash: the dialogs need a terminal on stdin. Download it first:
#
#   curl -fsSLO https://raw.githubusercontent.com/al3xand3w0lf/wormhole/main/install.sh
#   bash install.sh
#
# What it deliberately never does: open the admin port (it stays on 127.0.0.1),
# overwrite an existing .env, or delete recorded data without the instance name
# being typed in. Documentation: docs/installer-2026-09-23.md.

set -uo pipefail

# Where the code comes from when the installer was downloaded on its own. Run
# from inside a git checkout, it installs that checkout's origin and branch
# instead (see detect_source) - the code it was written for.
DEFAULT_REPO_URL="https://github.com/al3xand3w0lf/wormhole.git"
DEFAULT_BRANCH="main"
MILLIPEDE_URL="https://github.com/pbeyssac/millipede-caster.git"
REGISTRY_DIR="/etc/wormhole/instances"
SETUP_LINK="/usr/local/bin/wormhole-setup"
SYSTEM_USER="wormhole"
SYSTEM_USER_HOME="/var/lib/wormhole"
SELFTEST_STATION=9999
BASE_CANDIDATES=(9000 10000 11000 12000 13000 14000 15000 16000 17000 18000 19000 20000)
MIN_FREE_GB=10
MIN_PYTHON="3.10"      # the code uses PEP 604 unions at runtime (X | None)
TITLE="Wormhole setup"
LOG_FILE="${HOME:-/tmp}/wormhole-setup.log"

UNATTENDED=false
declare -A ANSWER=()

# ============================================================== basics ======

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >>"$LOG_FILE"; }
die() { echo "error: $*" >&2; log "ERROR: $*"; exit 1; }

if [ "$(id -u)" -eq 0 ]; then SUDO=(); else SUDO=(sudo); fi
as_root() { "${SUDO[@]}" "$@"; }

KEEPALIVE_PID=""
cleanup() { [ -n "$KEEPALIVE_PID" ] && kill "$KEEPALIVE_PID" 2>/dev/null; return 0; }
trap cleanup EXIT

sudo_start() {
    [ "$(id -u)" -eq 0 ] || [ -n "$KEEPALIVE_PID" ] && return 0
    sudo -n true 2>/dev/null || sudo -v || die "this installer needs sudo rights"
    # Detached from stdout/stderr: its sleep would otherwise hold a pipe the
    # caller reads from (install.sh ... | tail) open for up to a minute.
    ( while kill -0 $$ 2>/dev/null; do sudo -n true 2>/dev/null; sleep 50; done ) </dev/null >/dev/null 2>&1 &
    KEEPALIVE_PID=$!
}

# Run a command as the instance's service user (inside $DIR for in_dir).
as_svc() {
    if [ "$SVC_USER" = "$(id -un)" ]; then
        "$@"
    elif [ "$(id -u)" -eq 0 ]; then
        runuser -u "$SVC_USER" -- "$@"
    else
        sudo -u "$SVC_USER" -H "$@"
    fi
}
in_dir() { as_svc bash -c 'cd "$1" || exit 1; shift; exec "$@"' _ "$DIR" "$@"; }
py() { in_dir "$DIR/venv/bin/python" "$@"; }

env_get() {
    py -c 'import sys
from dotenv import dotenv_values
print(dotenv_values(".env").get(sys.argv[1]) or "")' "$1" 2>/dev/null
}
env_set() {
    py -c 'import sys
from dotenv import set_key
set_key(".env", sys.argv[1], sys.argv[2], quote_mode="never")' "$1" "$2" >/dev/null &&
        in_dir chmod 600 .env
}
gen_secret() {   # $1 = maximum length
    python3 -c 'import secrets,sys; n=int(sys.argv[1]); print(secrets.token_urlsafe(n)[:n])' "$1"
}

# Longest CLI secret the device keeps, from the configuration schema. The
# schema's max_len is the size of the device's field, which includes the
# terminator: a device stores max_len - 1 characters, silently drops the rest,
# and then answers every remote CLI command with "auth failed" (the same rule
# streaming/server.py warns about at startup). Without a schema: 31, the
# field size of the reference device minus its terminator.
cli_secret_max() {
    local schema="$DIR/configgen/data/device_config.schema.json" n=""
    if [ -f "$schema" ]; then
        n=$(python3 - "$schema" <<'PY' 2>/dev/null
import json, sys
for k in json.load(open(sys.argv[1])).get("keys", []):
    if k.get("key") == "streaming_cli_secret" and k.get("max_len"):
        print(int(k["max_len"]) - 1)
PY
)
    fi
    echo "${n:-31}"
}

# =========================================================== dialogs =======
# Every dialog takes an answers-file key. In --unattended mode the answer (or
# the default) is used without showing anything; interactively the answer is
# the preset.

term_size() {
    local l c
    read -r l c < <(stty size 2>/dev/null || echo "24 80")
    H=$(( l - 2 )); [ "$H" -gt 40 ] && H=40; [ "$H" -lt 16 ] && H=16
    W=$(( c - 4 )); [ "$W" -gt 96 ] && W=96; [ "$W" -lt 60 ] && W=60
    LH=$(( H - 10 ))
}

ask_input() {   # KEY text default
    local key=$1 text=$2 def=${ANSWER[$1]:-$3}
    if $UNATTENDED; then echo "$def"; return 0; fi
    term_size
    whiptail --title "$TITLE" --inputbox "$text" "$H" "$W" "$def" 3>&1 1>&2 2>&3
}

ask_yesno() {   # KEY text default(yes|no)
    local key=$1 text=$2 def=${ANSWER[$1]:-$3}
    if $UNATTENDED; then [ "$def" = yes ]; return; fi
    term_size
    if [ "$def" = no ]; then
        whiptail --title "$TITLE" --defaultno --yesno "$text" "$H" "$W"
    else
        whiptail --title "$TITLE" --yesno "$text" "$H" "$W"
    fi
}

MENU_CANCEL="Cancel"   # label of the menu's second button; main menu: "Exit"

ask_menu() {    # KEY text default tag item [tag item ...]
    local key=$1 text=$2 def=${ANSWER[$1]:-$3}; shift 3
    if $UNATTENDED; then echo "$def"; return 0; fi
    term_size
    whiptail --title "$TITLE" --ok-button "Select" --cancel-button "$MENU_CANCEL" \
        --default-item "$def" --menu "$text" "$H" "$W" "$LH" "$@" 3>&1 1>&2 2>&3
}

ask_checklist() {   # KEY text tag item on/off ...  -> space separated tags
    local key=$1 text=$2; shift 2
    if $UNATTENDED; then
        if [ -n "${ANSWER[$key]+x}" ]; then echo "${ANSWER[$key]}"; return 0; fi
        local out=() ; while [ $# -gt 0 ]; do [ "$3" = ON ] && out+=("$1"); shift 3; done
        echo "${out[*]}"; return 0
    fi
    term_size
    whiptail --title "$TITLE" --separate-output --checklist "$text" "$H" "$W" "$LH" "$@" 3>&1 1>&2 2>&3 | tr '\n' ' ' | sed 's/ $//'
    return "${PIPESTATUS[0]}"
}

msg() {
    if $UNATTENDED; then printf '\n%s\n\n' "$1"; return 0; fi
    term_size
    whiptail --title "$TITLE" --msgbox "$1" "$H" "$W"
}

# Enter must always close what is shown. A plain message box does that: its
# <Ok> has the focus. A scrolling box (--scrolltext / --textbox) does not - the
# focus sits on the text, Enter does nothing and only Tab reaches <Ok>, which
# nobody guesses. So a text that fits goes into a message box, and a longer
# one is printed to the terminal instead (scrollback, copyable), then Enter.
show_file() {   # file [title]
    if $UNATTENDED; then cat "$1"; return 0; fi
    local n; term_size
    n=$(wc -l <"$1")
    if [ $((n + 8)) -le "$H" ]; then
        whiptail --title "${2:-$TITLE}" --msgbox "$(cat "$1")" $((n + 8)) "$W"
    else
        print_and_wait "$1" "${2:-}"
    fi
}

print_and_wait() {   # file [title]
    if $UNATTENDED; then cat "$1"; return 0; fi
    clear
    [ -n "${2:-}" ] && printf '=== %s ===\n\n' "$2"
    cat "$1"; echo
    read -rp "Press Enter to continue ... " _
}

# ============================================================ registry =====

# The install-time answers are kept too, so an interrupted install resumes with
# them instead of asking again.
INSTANCE_VARS=(NAME DIR SVC_USER COMP_STREAMING COMP_BATCH COMP_CASTER STREAM_PORT
               ADMIN_PORT CASTER_PORT BATCH_PORT FW_PORTS SSL_MODE LE_DOMAIN LE_EMAIL
               REPO_URL REPO_BRANCH PUBLIC_HOST BASE_STATIONS RAW_MAX_AGE_H FW_ENABLED
               FW_ACTIVATE FW_OPEN INSTALL_DONE)

reset_instance() {
    NAME="" DIR="" SVC_USER="$(id -un)" COMP_STREAMING=yes COMP_BATCH=yes COMP_CASTER=yes
    STREAM_PORT="" ADMIN_PORT="" CASTER_PORT="" BATCH_PORT="" FW_PORTS="" SSL_MODE=none
    LE_DOMAIN="" LE_EMAIL="" REPO_URL="$DEFAULT_REPO_URL" REPO_BRANCH="$DEFAULT_BRANCH"
    PUBLIC_HOST="" BASE_STATIONS="" RAW_MAX_AGE_H=168 FW_ENABLED=no FW_ACTIVATE=no FW_OPEN=""
    INSTALL_DONE=no
}

save_registry() {
    local v tmp
    tmp=$(mktemp)
    { echo "# wormhole instance - written by install.sh, read by install.sh"
      for v in "${INSTANCE_VARS[@]}"; do printf '%s=%q\n' "$v" "${!v}"; done; } >"$tmp"
    as_root mkdir -p "$REGISTRY_DIR"
    as_root install -m 644 "$tmp" "$REGISTRY_DIR/$NAME.conf"
    rm -f "$tmp"
}

load_registry() {   # NAME
    local f="$REGISTRY_DIR/$1.conf"
    [ -f "$f" ] || return 1
    reset_instance
    # shellcheck disable=SC1090
    source "$f"
}

list_instances() {
    local f
    for f in "$REGISTRY_DIR"/*.conf; do [ -f "$f" ] && basename "$f" .conf; done
}

# Unit names follow the deployment guide (server-deployment.md): instance wormhole_N (directory,
# registry) runs wormhole-N-stream / -batch / -caster, so an instance set up by
# hand from that guide and one set up here look the same.
unit() { echo "wormhole-${NAME#wormhole_}-$1"; }

units() {   # the instance's systemd units, start order
    [ "$COMP_CASTER" = yes ] && echo "$(unit caster)"
    [ "$COMP_STREAMING" = yes ] && echo "$(unit stream)"
    [ "$COMP_BATCH" = yes ] && echo "$(unit batch)"
    return 0
}

# ============================================================== ports ======

port_listening() { ss -ltnH "( sport = :$1 )" 2>/dev/null | grep -q .; }

# Ports another registered instance has claimed (listening or not).
reserved_ports() {
    local f
    for f in "$REGISTRY_DIR"/*.conf; do
        [ -f "$f" ] || continue
        [ "$(basename "$f" .conf)" = "$NAME" ] && continue
        # shellcheck disable=SC1090
        ( source "$f"; echo "$STREAM_PORT $ADMIN_PORT $CASTER_PORT $BATCH_PORT" )
    done
}

port_free() {   # port [exempt...]  - exempt = ports this instance already owns
    local p=$1; shift
    local e; for e in "$@"; do [ "$p" = "$e" ] && return 0; done
    port_listening "$p" && return 1
    reserved_ports | tr ' ' '\n' | grep -qx "$p" && return 1
    return 0
}

suggest_base() {
    local b
    for b in "${BASE_CANDIDATES[@]}"; do
        if port_free "$b" && port_free $((b+1)) && port_free $((b+2)) && port_free $((b+3)); then
            echo "$b"; return 0
        fi
    done
    echo 20000
}

selected_ports() {   # "label port" lines for the chosen components
    [ "$COMP_STREAMING" = yes ] && echo "stream $STREAM_PORT" && echo "admin $ADMIN_PORT"
    [ "$COMP_CASTER" = yes ] && echo "caster $CASTER_PORT"
    [ "$COMP_BATCH" = yes ] && echo "batch $BATCH_PORT"
    return 0
}

validate_ports() {   # exempt ports as args; prints the problem, returns 1
    local seen="" label p
    while read -r label p; do
        [ -z "$label" ] && continue
        if ! [[ "$p" =~ ^[0-9]+$ ]] || [ "$p" -lt 1024 ] || [ "$p" -gt 65535 ]; then
            echo "$label port '$p' is not a number between 1024 and 65535"; return 1
        fi
        if echo "$seen" | tr ' ' '\n' | grep -qx "$p"; then
            echo "port $p is used twice"; return 1
        fi
        seen="$seen $p"
        if ! port_free "$p" "$@"; then
            echo "$label port $p is already in use on this host (or reserved by another wormhole instance)"
            return 1
        fi
    done < <(selected_ports)
    return 0
}

dialog_ports() {   # [exempt ports...]  - sets *_PORT; returns 1 on cancel
    local b choice v problem
    if [ -z "$STREAM_PORT" ]; then
        b=$(suggest_base)
        b=$(ask_input BASE_PORT "Port block for this instance.

Enter the first port; the others follow it:
  first    streaming (devices connect here)
  first+1  admin API (local only, never opened)
  first+2  NTRIP caster
  first+3  batch uploads

You can change each one individually on the next screen." "$b") || return 1
        [[ "$b" =~ ^[0-9]+$ ]] || b=$(suggest_base)
        STREAM_PORT=${ANSWER[STREAM_PORT]:-$b}
        ADMIN_PORT=${ANSWER[ADMIN_PORT]:-$((b+1))}
        CASTER_PORT=${ANSWER[CASTER_PORT]:-$((b+2))}
        BATCH_PORT=${ANSWER[BATCH_PORT]:-$((b+3))}
    fi
    while true; do
        if $UNATTENDED; then
            problem=$(validate_ports "$@") || die "$problem"
            return 0
        fi
        local items=()
        [ "$COMP_STREAMING" = yes ] && items+=(stream "Streaming (devices)      $STREAM_PORT  public"
                                              admin  "Admin API / remote CLI   $ADMIN_PORT  local only")
        [ "$COMP_CASTER" = yes ] && items+=(caster "NTRIP caster (rovers)    $CASTER_PORT  public")
        [ "$COMP_BATCH" = yes ] && items+=(batch "Batch uploads (HTTP)     $BATCH_PORT  public")
        items+=(ok "Continue with these ports")
        choice=$(ask_menu PORT_MENU "Ports of this instance. Select one to change it." ok "${items[@]}") || return 1
        case "$choice" in
            stream) v=$(ask_input X "Streaming port (devices connect here):" "$STREAM_PORT") && STREAM_PORT=$v ;;
            admin)  v=$(ask_input X "Admin API port (bound to 127.0.0.1, never opened in the firewall):" "$ADMIN_PORT") && ADMIN_PORT=$v ;;
            caster) v=$(ask_input X "NTRIP caster port:" "$CASTER_PORT") && CASTER_PORT=$v ;;
            batch)  v=$(ask_input X "Batch upload port:" "$BATCH_PORT") && BATCH_PORT=$v ;;
            ok)
                if problem=$(validate_ports "$@"); then return 0; fi
                msg "Cannot use these ports:

$problem" ;;
        esac
    done
}

public_ports() {   # ports that devices / rovers reach from outside
    [ "$COMP_STREAMING" = yes ] && echo "$STREAM_PORT"
    [ "$COMP_CASTER" = yes ] && echo "$CASTER_PORT"
    [ "$COMP_BATCH" = yes ] && echo "$BATCH_PORT"
    return 0
}

port_role() {
    case "$1" in
        "$STREAM_PORT") echo "streaming" ;; "$CASTER_PORT") echo "ntrip-caster" ;;
        "$BATCH_PORT") echo "batch-upload" ;; 80) echo "letsencrypt" ;; *) echo "port" ;;
    esac
}

# =========================================================== preflight =====

PREFLIGHT_REPORT=""
pf() { PREFLIGHT_REPORT+="$1  $2"$'\n'; }

preflight() {   # returns 1 on a hard failure
    local fail=0 warn=0 arch avail_kb
    PREFLIGHT_REPORT=""
    if [ -r /etc/os-release ] && grep -qiE '^(ID|ID_LIKE)=.*(debian|ubuntu)' /etc/os-release; then
        pf "[ OK ]" "$(. /etc/os-release; echo "$PRETTY_NAME")"
    else
        pf "[FAIL]" "not a Debian/Ubuntu/Raspberry Pi OS system"; fail=1
    fi
    if [ -d /run/systemd/system ]; then pf "[ OK ]" "systemd"; else pf "[FAIL]" "systemd is not running"; fail=1; fi
    if command -v python3 >/dev/null && python3 -c "import sys; sys.exit(sys.version_info < tuple(map(int, '$MIN_PYTHON'.split('.'))))"; then
        pf "[ OK ]" "Python $(python3 -c 'import platform; print(platform.python_version())')"
    else
        pf "[FAIL]" "Python >= $MIN_PYTHON required (found: $(python3 -V 2>&1 || echo none))"; fail=1
    fi
    arch=$(uname -m)
    case "$arch" in
        armv6l|armv7l) pf "[WARN]" "$arch (32-bit): some Python packages may have to be compiled; a 64-bit OS is recommended"; warn=1 ;;
        *) pf "[ OK ]" "architecture $arch" ;;
    esac
    if curl -fsS --max-time 8 -o /dev/null https://github.com && curl -fsS --max-time 8 -o /dev/null https://pypi.org/simple/pip/; then
        pf "[ OK ]" "internet (github.com, pypi.org)"
    else
        pf "[WARN]" "github.com or pypi.org not reachable - cloning and pip will fail"; warn=1
    fi
    if [ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" = yes ]; then
        pf "[ OK ]" "clock synchronised (NTP)"
    else
        pf "[WARN]" "clock not NTP-synchronised - server-clock file names will be off"; warn=1
    fi
    avail_kb=$(df -Pk "${HOME:-/}" | awk 'NR==2 {print $4}')
    if [ "${avail_kb:-0}" -lt $((MIN_FREE_GB * 1024 * 1024)) ]; then
        pf "[WARN]" "only $((avail_kb / 1024 / 1024)) GB free - a station records roughly 1-2 GB per day"; warn=1
    else
        pf "[ OK ]" "$((avail_kb / 1024 / 1024)) GB free disk"
    fi
    if command -v ufw >/dev/null; then
        pf "[ OK ]" "firewall: ufw ($(as_root ufw status 2>/dev/null | head -1 | sed 's/Status: //'))"
    else
        pf "[INFO]" "no ufw installed - open the ports in your own firewall/router"
    fi
    [ $fail -eq 0 ] || return 1
    PREFLIGHT_WARN=$warn
    return 0
}

# ============================================================ steps ========

STEP_N=0
STEP_TOTAL=0

step() {   # title function
    local title=$1; shift
    STEP_N=$((STEP_N + 1))
    printf '[%2d/%d] %-52s ' "$STEP_N" "$STEP_TOTAL" "$title"
    log "=== step: $title ($NAME)"
    if "$@" >>"$LOG_FILE" 2>&1; then echo "ok"; return 0; fi
    echo "FAILED"; return 1
}

run_step() {   # title function - with retry / log / rollback on failure
    local choice
    while ! step "$@"; do
        if $UNATTENDED; then
            tail -n 30 "$LOG_FILE" >&2
            die "step failed: $1 (log: $LOG_FILE)"
        fi
        STEP_N=$((STEP_N - 1))
        choice=$(ask_menu X "Step failed: $1

The full output is in $LOG_FILE." retry \
            retry "Retry this step" log "Show the log" rollback "Roll back (remove units and firewall rules)" \
            abort "Abort, keep everything as it is") || choice=abort
        case "$choice" in
            retry) ;;
            log) tail -n 200 "$LOG_FILE" >"/tmp/wormhole-log.$$"; show_file "/tmp/wormhole-log.$$" "Log"; rm -f "/tmp/wormhole-log.$$" ;;
            rollback) remove_units; firewall_remove
                      msg "Units and firewall rules removed. The directory $DIR is kept; run the installer again to resume."; exit 1 ;;
            abort) exit 1 ;;
        esac
    done
}

st_packages() {
    local pkgs=(python3 python3-venv python3-pip git curl openssl iproute2 ca-certificates) missing=() p
    [ "$COMP_CASTER" = yes ] && pkgs+=(make gcc pkg-config libcyaml-dev libevent-dev libjson-c-dev libssl-dev)
    [ "$SSL_MODE" = letsencrypt ] && pkgs+=(certbot)
    for p in "${pkgs[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
    [ ${#missing[@]} -eq 0 ] && { echo "all packages present"; return 0; }
    as_root env DEBIAN_FRONTEND=noninteractive apt-get update &&
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}"
}

st_user() {
    [ "$SVC_USER" != "$SYSTEM_USER" ] && return 0
    id "$SYSTEM_USER" >/dev/null 2>&1 && return 0
    as_root useradd --system --create-home --home-dir "$SYSTEM_USER_HOME" \
        --shell /usr/sbin/nologin "$SYSTEM_USER"
}

st_clone() {
    if [ -d "$DIR/.git" ]; then
        echo "already cloned"
    else
        if [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
            echo "$DIR exists, is not empty and is not a git checkout"; return 1
        fi
        as_root mkdir -p "$DIR" && as_root chown "$SVC_USER:" "$DIR" &&
            as_svc git clone --branch "$REPO_BRANCH" "$REPO_URL" "$DIR" || return 1
    fi
    check_code
}

# The cloned code must be a version this installer knows how to set up. An
# older one installs half-way and then misbehaves (its caster/setup.sh, for
# one, overwrote a user unit of the same name that ran a different caster).
check_code() {
    local f missing=()
    for f in install.sh streaming_server.py server.py fake_device.py requirements.txt .env.example; do
        [ -e "$DIR/$f" ] || missing+=("$f")
    done
    [ "$COMP_CASTER" = yes ] && [ ! -e "$DIR/caster/generate_config.py" ] && missing+=(caster/generate_config.py)
    if [ ${#missing[@]} -gt 0 ]; then
        echo "The code from $REPO_URL ($REPO_BRANCH) is too old for this installer - missing: ${missing[*]}."
        echo "Choose a newer code source (Change the code source), or remove $DIR and start again."
        return 1
    fi
    if [ "$COMP_CASTER" = yes ] && [ -z "$BASE_STATIONS" ] &&
       ! grep -q STREAM_CASTER_AUTO_ENABLE "$DIR/caster/generate_config.py"; then
        echo "This code version cannot start the caster without base station IDs - enter at least one, or choose a newer code source."
        return 1
    fi
    echo "code: $(as_svc git -C "$DIR" log -1 --format='%h %cd' --date=short)"
}

st_venv() {
    local req=(-r requirements.txt)
    [ -f "$DIR/requirements-dev.txt" ] && req+=(-r requirements-dev.txt)
    if [ ! -x "$DIR/venv/bin/python" ]; then in_dir python3 -m venv venv || return 1; fi
    in_dir venv/bin/pip install --upgrade pip && in_dir venv/bin/pip install "${req[@]}"
}

st_env() {
    local max
    if in_dir test -f .env; then echo ".env exists - kept unchanged"; return 0; fi
    in_dir cp .env.example .env && in_dir chmod 600 .env || return 1
    max=$(cli_secret_max)
    env_set API_KEY "$(gen_secret 32)" &&
    env_set PUBLIC_HOST "$PUBLIC_HOST" || return 1
    if [ "$COMP_BATCH" = yes ]; then
        env_set HOST 0.0.0.0 && env_set PORT "$BATCH_PORT" || return 1
    fi
    if [ "$COMP_STREAMING" = yes ]; then
        env_set STREAM_HOST 0.0.0.0 &&
        env_set STREAM_PORT "$STREAM_PORT" &&
        env_set STREAM_ADMIN_HOST 127.0.0.1 &&
        env_set STREAM_ADMIN_PORT "$ADMIN_PORT" &&
        env_set STREAM_CLI_SECRET "$(gen_secret "$max")" &&
        env_set STREAM_RAW_CAPTURE true &&
        env_set STREAM_RAW_MAX_AGE_H "$RAW_MAX_AGE_H" &&
        env_set STREAM_ROVER_AUTO_BASE_STATIONS "$BASE_STATIONS" || return 1
    fi
    if [ "$COMP_CASTER" = yes ]; then
        env_set STREAM_CASTER_HOST 127.0.0.1 &&
        env_set STREAM_CASTER_PORT "$CASTER_PORT" &&
        env_set STREAM_CASTER_STATIONS "$BASE_STATIONS" &&
        env_set STREAM_CASTER_AUTO_ENABLE true &&
        env_set STREAM_CASTER_ETC_DIR "$DIR/caster/millipede-caster/etc" || return 1
    fi
    echo ".env written"
}

st_dirs() {
    in_dir mkdir -p data/incoming data/outgoing data/incoming_stream
}

le_hook_path() { echo "/etc/letsencrypt/renewal-hooks/deploy/$NAME.sh"; }

st_ssl() {
    case "$SSL_MODE" in
        none) return 0 ;;
        selfsigned)
            if ! in_dir test -f ssl/cert.pem; then
                in_dir mkdir -p ssl &&
                in_dir openssl req -x509 -newkey rsa:4096 -keyout ssl/key.pem -out ssl/cert.pem \
                    -days 3650 -nodes -subj "/CN=${PUBLIC_HOST:-wormhole}" || return 1
            fi ;;
        letsencrypt)
            local hook tmp
            hook=$(le_hook_path)
            if [ "${FW_ENABLED:-no}" = yes ]; then
                as_root ufw allow 80/tcp comment "$NAME letsencrypt" || return 1
                FW_PORTS="$(echo "$FW_PORTS 80" | xargs)"
            fi
            as_root certbot certonly --standalone -n --agree-tos --keep-until-expiring \
                -m "$LE_EMAIL" -d "$LE_DOMAIN" || return 1
            tmp=$(mktemp)
            cat >"$tmp" <<EOF
#!/bin/sh
# Installed by wormhole install.sh for $NAME: copy the renewed certificate
# where the service user can read it, then restart the batch server.
case " \$RENEWED_DOMAINS " in *" $LE_DOMAIN "*) ;; *) exit 0 ;; esac
install -d -o $SVC_USER -m 700 $DIR/ssl
install -o $SVC_USER -m 600 /etc/letsencrypt/live/$LE_DOMAIN/privkey.pem $DIR/ssl/key.pem
install -o $SVC_USER -m 644 /etc/letsencrypt/live/$LE_DOMAIN/fullchain.pem $DIR/ssl/cert.pem
systemctl try-restart $(unit batch).service
EOF
            as_root install -D -m 755 "$tmp" "$hook"; rm -f "$tmp"
            as_root env RENEWED_DOMAINS="$LE_DOMAIN" "$hook" || return 1 ;;
    esac
    env_set SSL_CERTFILE "$DIR/ssl/cert.pem" && env_set SSL_KEYFILE "$DIR/ssl/key.pem"
}

# Built here instead of through caster/setup.sh: older versions of that script
# always install a *user* unit called millipede-caster.service - on a host that
# already runs a caster that way, it replaces that unit. The caster of an
# instance runs as its own system unit (st_units) and needs none.
st_caster() {
    if ! in_dir test -x caster/millipede-caster/caster/caster; then
        in_dir test -d caster/millipede-caster/.git ||
            in_dir git clone "$MILLIPEDE_URL" caster/millipede-caster || return 1
    fi
    in_dir make -C caster/millipede-caster/caster || return 1
    py caster/generate_config.py
}

write_unit() {   # name description exec [extra]
    local tmp; tmp=$(mktemp)
    cat >"$tmp" <<EOF
# Written by wormhole install.sh for instance $NAME - rerun it to regenerate.
[Unit]
Description=$2 ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
WorkingDirectory=$DIR
ExecStart=$3
${4:-}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    as_root install -m 644 "$tmp" "/etc/systemd/system/$1.service"; rm -f "$tmp"
}

st_units() {
    local ssl_flag="" u
    [ "$SSL_MODE" = none ] && ssl_flag=" --no-ssl"
    # The streaming unit deliberately does not depend on the caster: the sink
    # reconnects on its own, and a caster that is down must never hold up the
    # receiver (server-deployment.md, step 7).
    [ "$COMP_CASTER" = yes ] && write_unit "$(unit caster)" "Wormhole NTRIP caster" \
        "$DIR/caster/millipede-caster/caster/caster -c $DIR/caster/millipede-caster/etc/caster.yaml" \
        'ExecReload=/bin/kill -HUP $MAINPID'
    [ "$COMP_STREAMING" = yes ] && write_unit "$(unit stream)" "Wormhole streaming server" \
        "$DIR/venv/bin/python streaming_server.py"
    [ "$COMP_BATCH" = yes ] && write_unit "$(unit batch)" "Wormhole batch file server" \
        "$DIR/venv/bin/python server.py$ssl_flag"
    as_root systemctl daemon-reload || return 1
    for u in $(units); do
        as_root systemctl enable "$u.service" && as_root systemctl restart "$u.service" || return 1
    done
    wait_listening 30
}

wait_listening() {   # seconds
    local t=0 label p ok
    while [ "$t" -lt "$1" ]; do
        ok=true
        while read -r label p; do port_listening "$p" || ok=false; done < <(selected_ports)
        $ok && return 0
        sleep 1; t=$((t + 1))
    done
    echo "not all ports are listening after $1 s:"; selected_ports
    return 1
}

remove_units() {
    local u
    for u in "$(unit batch)" "$(unit stream)" "$(unit caster)"; do
        if [ -f "/etc/systemd/system/$u.service" ]; then
            as_root systemctl disable --now "$u.service" >>"$LOG_FILE" 2>&1
            as_root rm -f "/etc/systemd/system/$u.service"
        fi
    done
    as_root systemctl daemon-reload
}

firewall_remove() {   # the rules recorded for this instance
    command -v ufw >/dev/null || return 0
    local p
    for p in $FW_PORTS; do as_root ufw delete allow "$p/tcp" >>"$LOG_FILE" 2>&1; done
    FW_PORTS=""
    return 0
}

st_firewall() {
    [ "${FW_ENABLED:-no}" = yes ] || { echo "firewall: not managed"; return 0; }
    command -v ufw >/dev/null || return 0
    local p keep_le=""
    echo " $FW_PORTS " | grep -q ' 80 ' && keep_le=80
    if [ "${FW_ACTIVATE:-no}" = yes ] && ! as_root ufw status | grep -q "Status: active"; then
        # SSH first, or enabling the firewall locks out the session doing it.
        as_root ufw allow OpenSSH >/dev/null 2>&1 || as_root ufw allow 22/tcp || return 1
        as_root ufw --force enable || return 1
    fi
    for p in $FW_PORTS; do
        [ "$p" = 80 ] && continue
        echo " $FW_OPEN " | grep -q " $p " || as_root ufw delete allow "$p/tcp"
    done
    for p in $FW_OPEN; do
        as_root ufw allow "$p/tcp" comment "$NAME $(port_role "$p")" || return 1
    done
    FW_PORTS="$(echo "$FW_OPEN $keep_le" | xargs)"
    save_registry
}

# Point wormhole-setup at an install.sh that will still exist once $DIR is gone.
relink_setup() {
    if [ -z "$(list_instances)" ]; then as_root rm -f "$SETUP_LINK"; return 0; fi
    [ "$(readlink "$SETUP_LINK" 2>/dev/null)" = "$DIR/install.sh" ] || return 0
    local n d=""
    for n in $(list_instances); do
        d=$( load_registry "$n" && [ -f "$DIR/install.sh" ] && echo "$DIR" ) && break
    done
    if [ -n "$d" ]; then
        as_root ln -sfn "$d/install.sh" "$SETUP_LINK"
    elif [ -n "$(list_instances)" ]; then
        as_root install -m 755 "$DIR/install.sh" "$SETUP_LINK.new" && as_root mv -f "$SETUP_LINK.new" "$SETUP_LINK"
    else
        as_root rm -f "$SETUP_LINK"
    fi
}

st_register() {
    save_registry && as_root ln -sfn "$DIR/install.sh" "$SETUP_LINK"
}

# ======================================================== server test ======

TEST_REPORT=""
TEST_FAILS=0
tr_ok()   { TEST_REPORT+="[ OK ] $1"$'\n'; log "test ok: $1"; }
tr_fail() { TEST_REPORT+="[FAIL] $1"$'\n'; TEST_FAILS=$((TEST_FAILS + 1)); log "test FAIL: $1"; }
tr_info() { TEST_REPORT+="[INFO] $1"$'\n'; log "test info: $1"; }

batch_url() {
    if [ "$SSL_MODE" = none ]; then echo "http://127.0.0.1:$BATCH_PORT"; else echo "https://127.0.0.1:$BATCH_PORT"; fi
}

station_json() {   # station id -> "connected bytes_rx garbage resync" or empty
    curl -fsS --max-time 5 -H "X-API-Key: $API_KEY" "http://127.0.0.1:$ADMIN_PORT/stream/stations" 2>/dev/null |
        python3 -c 'import json, sys
d = json.load(sys.stdin)
rows = d.get("stations", d) if isinstance(d, dict) else d
for s in rows if isinstance(rows, list) else []:
    if str(s.get("station_id")) == sys.argv[1]:
        print(s.get("connected"), s.get("bytes_rx", 0), s.get("garbage_bytes", 0), s.get("resync_events", 0))' "$1"
}

server_test() {   # [with_pytest yes|no] [install yes|no]
    local u label p addrs secret max name fpid st connected bytes garbage resync
    TEST_REPORT="" TEST_FAILS=0
    API_KEY=$(env_get API_KEY)

    for u in $(units); do
        if systemctl is-active --quiet "$u"; then tr_ok "$u is running"; else tr_fail "$u is not running"; fi
        if systemctl is-enabled --quiet "$u"; then tr_ok "$u starts at boot"; else tr_fail "$u is not enabled at boot"; fi
    done

    while read -r label p; do
        addrs=$(ss -ltnH "( sport = :$p )" | awk '{print $4}' | sed 's/:[0-9]*$//' | sort -u | xargs)
        if [ -z "$addrs" ]; then tr_fail "$label port $p is not listening"; continue; fi
        if [ "$label" = admin ]; then
            if [ "$addrs" = "127.0.0.1" ]; then tr_ok "admin port $p is local only (127.0.0.1)"
            else tr_fail "admin port $p listens on '$addrs' - it must be 127.0.0.1 only"; fi
        else
            tr_ok "$label port $p is listening ($addrs)"
        fi
    done < <(selected_ports)

    if [ "$COMP_STREAMING" = yes ]; then
        if curl -fsS --max-time 5 "http://127.0.0.1:$ADMIN_PORT/health" >/dev/null; then tr_ok "admin API /health"
        else tr_fail "admin API /health does not answer"; fi
        if curl -fsS --max-time 5 -o /dev/null -H "X-API-Key: $API_KEY" "http://127.0.0.1:$ADMIN_PORT/stream/stations"; then
            tr_ok "admin API accepts the API key"
        else tr_fail "admin API /stream/stations refused the API key"; fi

        secret=$(env_get STREAM_CLI_SECRET); max=$(cli_secret_max)
        if [ "${#secret}" -le "$max" ]; then tr_ok "CLI secret fits the device field (${#secret}/$max)"
        else tr_fail "CLI secret is ${#secret} chars, the device accepts $max"; fi

        st=$(station_json "$SELFTEST_STATION")
        if [ "${st%% *}" = True ]; then
            tr_info "streaming test skipped: a real station $SELFTEST_STATION is connected"
        else
            in_dir env FAKE_DEVICE_ENABLE=1 venv/bin/python fake_device.py --host 127.0.0.1 \
                --port "$STREAM_PORT" --station "$SELFTEST_STATION" --role stream \
                --secret "$secret" --duration 20 >>"$LOG_FILE" 2>&1 &
            fpid=$!
            connected="" bytes=0 garbage=0 resync=0
            for _ in $(seq 1 15); do
                sleep 1
                read -r connected bytes garbage resync < <(station_json "$SELFTEST_STATION"; echo)
                [ "$connected" = True ] && [ "${bytes:-0}" -gt 2000 ] && break
            done
            if [ "$connected" = True ] && [ "${bytes:-0}" -gt 0 ]; then
                tr_ok "test station $SELFTEST_STATION streamed $bytes bytes"
                if [ "$garbage" = 0 ] && [ "$resync" = 0 ]; then tr_ok "stream intact (garbage 0, resyncs 0)"
                else tr_fail "stream damaged: garbage_bytes=$garbage resync_events=$resync"; fi
            else
                tr_fail "test station $SELFTEST_STATION did not stream (see log)"
            fi
            wait "$fpid" 2>/dev/null
            in_dir bash -c "find data/incoming_stream -maxdepth 1 -name '*$SELFTEST_STATION' -exec rm -rf {} +"
        fi
    fi

    if [ "$COMP_BATCH" = yes ]; then
        if curl -fsSk --max-time 5 "$(batch_url)/health" >/dev/null; then tr_ok "batch server /health"
        else tr_fail "batch server /health does not answer"; fi
        name="wormhole-selftest-$$.bin"
        if echo selftest | curl -fsSk --max-time 10 -X POST -H "X-API-Key: $API_KEY" \
               -H "Content-Type: application/octet-stream" --data-binary @- \
               "$(batch_url)/modem/upload?device_id=selftest&filename=$name" >/dev/null &&
           curl -fsSk --max-time 5 -H "X-API-Key: $API_KEY" "$(batch_url)/uploads" | grep -q "$name"; then
            tr_ok "batch upload works"
        else
            tr_fail "batch test upload failed"
        fi
        in_dir rm -f "data/incoming/$name"
    fi

    if [ "$COMP_CASTER" = yes ]; then
        if curl -s --max-time 5 "http://127.0.0.1:$CASTER_PORT/" | grep -q ENDSOURCETABLE; then
            tr_ok "NTRIP caster answers with a sourcetable"
        else tr_fail "NTRIP caster on port $CASTER_PORT does not answer"; fi
    fi

    if in_dir test -w .env && in_dir test -w data; then tr_ok ".env and data/ writable by $SVC_USER"
    else tr_fail ".env or data/ not writable by $SVC_USER"; fi

    if [ "${1:-no}" = yes ]; then
        if in_dir venv/bin/python -m pytest -q >>"$LOG_FILE" 2>&1; then tr_ok "test suite (pytest) passed"
        else tr_fail "test suite (pytest) failed - see $LOG_FILE"; fi
    fi

    # The self-test station stays listed (disconnected) until a restart; on a
    # fresh install nothing real is connected yet, so clear it.
    if [ "${2:-no}" = yes ] && [ "$COMP_STREAMING" = yes ]; then
        as_root systemctl restart "$(unit stream)" && wait_listening 30 >/dev/null
    fi

    tr_info "reachability from outside cannot be tested from here - from another machine: nc -vz ${PUBLIC_HOST:-<host>} ${STREAM_PORT:-$BATCH_PORT}"
    [ "$TEST_FAILS" -eq 0 ]
}

show_test_result() {
    local f; f=$(mktemp)
    { if [ "$TEST_FAILS" -eq 0 ]; then echo "Server test passed."; else echo "Server test: $TEST_FAILS check(s) FAILED."; fi
      echo; printf '%s' "$TEST_REPORT"
      [ "$TEST_FAILS" -eq 0 ] || { echo; echo "Details: $LOG_FILE and 'Logs' in the manage menu."; }; } >"$f"
    show_file "$f" "Server test - $NAME"
    rm -f "$f"
}

# =========================================================== summary =======

write_summary() {
    local f secret host
    f=$(mktemp)
    API_KEY=$(env_get API_KEY); secret=$(env_get STREAM_CLI_SECRET)
    host=${PUBLIC_HOST:-<this server>}
    {
        echo "Wormhole instance $NAME"
        echo "Directory: $DIR   Service user: $SVC_USER"
        echo "Services:  $(units | xargs)"
        echo
        echo "Ports"
        [ "$COMP_STREAMING" = yes ] && echo "  $STREAM_PORT/tcp  streaming - devices connect here"
        [ "$COMP_STREAMING" = yes ] && echo "  $ADMIN_PORT/tcp  admin API - 127.0.0.1 only, never open it"
        [ "$COMP_CASTER" = yes ] && echo "  $CASTER_PORT/tcp  NTRIP caster - rovers pull corrections here"
        [ "$COMP_BATCH" = yes ] && echo "  $BATCH_PORT/tcp  batch uploads ($( [ "$SSL_MODE" = none ] && echo http || echo https))"
        if [ -n "$FW_PORTS" ]; then echo "  Opened in ufw: $FW_PORTS"
        else echo "  Not opened by this installer - open $(public_ports | xargs) in your firewall."; fi
        echo "  Behind a router/NAT: forward $(public_ports | xargs) to this machine."
        echo
        echo "Credentials (also in $DIR/.env)"
        echo "  API key (X-API-Key):     $API_KEY"
        [ "$COMP_STREAMING" = yes ] && echo "  CLI secret:              $secret"
        echo
        if [ "$COMP_STREAMING" = yes ]; then
            echo "On the device (CONFIG.TXT)"
            echo "  operation_mode = 1"
            echo "  streaming_server_ip = $host"
            echo "  streaming_server_port = $STREAM_PORT"
            echo "  streaming_station_id = <this station's id>"
            echo "  streaming_cli_secret = $secret"
            echo "  -> 'Generate device CONFIG.TXT' in wormhole-setup writes the whole file."
            echo
            echo "Admin API from your PC (SSH tunnel; the port is never public):"
            echo "  ssh -L $ADMIN_PORT:127.0.0.1:$ADMIN_PORT <user>@$host"
            echo "  then http://127.0.0.1:$ADMIN_PORT/config/  (device config generator)"
            echo "  Remote CLI on this server: cd $DIR && venv/bin/python stream_cli/stream_cli.py --list"
            echo
        fi
        [ "$COMP_BATCH" = yes ] && echo "Files for devices to download: $DIR/data/outgoing" && echo
        echo "Manage this instance later:  wormhole-setup"
        echo "Installer log:               $LOG_FILE"
    } >"$f"
    as_root install -o "$SVC_USER" -m 600 "$f" "$DIR/INSTALL_SUMMARY.txt"
    SUMMARY_TMP=$f
}

# ============================================================ install ======

do_install() {
    local comps b choice extra n
    reset_instance
    REPO_URL=${ANSWER[REPO_URL]:-$REPO_URL} REPO_BRANCH=${ANSWER[REPO_BRANCH]:-$REPO_BRANCH}
    PREFLIGHT_WARN=0

    # An install that was interrupted resumes with its recorded answers.
    for n in $(list_instances); do
        ( load_registry "$n" && [ "$INSTALL_DONE" != yes ] ) || continue
        if ask_yesno RESUME "The installation of $n was not finished.

Resume it now (recommended)? Choose No to start a different instance." yes; then
            load_registry "$n"; run_install_steps; return
        fi
    done

    if ! preflight; then
        msg "This machine cannot run wormhole:

$PREFLIGHT_REPORT"; exit 1
    fi
    if [ "$PREFLIGHT_WARN" = 1 ]; then
        ask_yesno CONTINUE_ON_WARN "System check - there are warnings:

$PREFLIGHT_REPORT
Continue anyway?" yes || exit 1
    elif ! $UNATTENDED; then
        msg "System check

$PREFLIGHT_REPORT"
    fi

    comps=$(ask_checklist COMPONENTS "What should this instance run?" \
        streaming "Streaming server (live TCP stream from devices)" ON \
        caster    "NTRIP caster (bases publish, rovers pull)" ON \
        batch     "Batch file server (devices upload finished files)" ON) || exit 0
    COMP_STREAMING=no COMP_CASTER=no COMP_BATCH=no
    [[ " $comps " == *" streaming "* ]] && COMP_STREAMING=yes
    [[ " $comps " == *" caster "* ]] && COMP_CASTER=yes
    [[ " $comps " == *" batch "* ]] && COMP_BATCH=yes
    if [ "$COMP_CASTER" = yes ] && [ "$COMP_STREAMING" = no ]; then
        msg "The NTRIP caster is fed by the streaming server - streaming is switched on as well."
        COMP_STREAMING=yes
    fi
    [ "$COMP_STREAMING$COMP_BATCH" = nono ] && die "nothing selected"

    dialog_ports || exit 0
    if [ "$COMP_STREAMING" = yes ]; then b=$STREAM_PORT; else b=$BATCH_PORT; fi
    NAME="wormhole_$b"
    if load_registry "$NAME"; then
        ask_yesno RESUME "$NAME is already installed in $DIR.

Run the install steps again to repair it? Existing settings and .env are kept." yes || exit 0
        run_install_steps; return
    fi

    choice=$(ask_menu SERVICE_USER "Which user should run the services?" current \
        current "$(id -un) - the current user (simple)" \
        system  "$SYSTEM_USER - a dedicated system user (separated from logins)") || exit 0
    if [ "$choice" = system ]; then SVC_USER=$SYSTEM_USER; DIR="/opt/$NAME"
    else SVC_USER=$(id -un); DIR="$HOME/$NAME"; fi
    [ "$SVC_USER" = root ] && ! $UNATTENDED && { ask_yesno X "The services would run as root. That works, but a normal user is safer. Continue?" no || exit 0; }
    DIR=$(ask_input INSTALL_DIR "Install directory:" "$DIR") || exit 0

    PUBLIC_HOST=$(ask_input PUBLIC_HOST "Address devices use to reach this server (public IP or DNS name).

Detected on this machine: $(hostname -I 2>/dev/null | awk '{print $1}')
Behind a router this is the router's public address or a DNS name." \
        "$(hostname -I 2>/dev/null | awk '{print $1}')") || exit 0

    if [ "$COMP_STREAMING" = yes ]; then
        BASE_STATIONS=$(ask_input BASE_STATIONS "Base station IDs (optional, comma separated, e.g. 1001,1002).

A base listed here gets a caster mountpoint right away and is trusted as a correction source for your rovers. Rovers need no entry.

Leave empty if you do not know them yet: a base that identifies itself still gets its mountpoint automatically; trusting it for rovers can be added later under Settings." "") || exit 0
        BASE_STATIONS=$(echo "$BASE_STATIONS" | tr -cd '0-9,' | sed 's/^,*//; s/,*$//; s/,,*/,/g')
        RAW_MAX_AGE_H=$(ask_input RAW_MAX_AGE_H "How long to keep the raw capture (hours)?

The raw capture records every byte a station sends - it is what makes damaged streams diagnosable, so it always stays on. It is the largest data item (roughly 0.5-1 GB per station and day) and is pruned at each start." 168) || exit 0
        [[ "$RAW_MAX_AGE_H" =~ ^[0-9]+$ ]] || RAW_MAX_AGE_H=168
    fi

    if [ "$COMP_BATCH" = yes ]; then
        SSL_MODE=$(ask_menu SSL_MODE "HTTPS for the batch upload server?" none \
            none        "No - plain HTTP (devices without TLS)" \
            selfsigned  "Self-signed certificate" \
            letsencrypt "Let's Encrypt (needs a DNS name and port 80)") || exit 0
        if [ "$SSL_MODE" = letsencrypt ]; then
            LE_DOMAIN=$(ask_input LE_DOMAIN "DNS name for the certificate:" "$PUBLIC_HOST") || exit 0
            LE_EMAIL=$(ask_input LE_EMAIL "E-mail for Let's Encrypt expiry notices:" "") || exit 0
        fi
    fi

    FW_OPEN=$(public_ports | xargs)
    if command -v ufw >/dev/null; then
        local items=() p
        for p in $(public_ports); do items+=("$p" "$(port_role "$p")" ON); done
        FW_OPEN=$(ask_checklist FW_OPEN "Open these ports in the firewall (ufw)?

The admin port $ADMIN_PORT is never opened." "${items[@]}") || exit 0
        FW_ENABLED=yes
        if ! as_root ufw status | grep -q "Status: active"; then
            ask_yesno FW_ACTIVATE "ufw is installed but not active.

Activate it now? SSH (22) is allowed first so this session is not locked out. Everything else that is not explicitly allowed will be blocked." no && FW_ACTIVATE=yes
        fi
    fi

    while true; do
        choice=$(ask_menu CONFIRM "Ready to install $NAME

  Directory:   $DIR
  User:        $SVC_USER
  Components:  $(units | sed "s/^wormhole-[0-9]*-//" | xargs)
  Ports:       $(selected_ports | tr '\n' ' ')
  Firewall:    $( [ "$FW_ENABLED" = yes ] && echo "open ${FW_OPEN:-nothing}" || echo "not managed (no ufw)")
  Public host: $PUBLIC_HOST
  Code:        $REPO_URL ($REPO_BRANCH)" install \
            install "Install now" source "Change the code source (advanced)" cancel "Cancel") || exit 0
        case "$choice" in
            install)
                # A dedicated service user cannot use the SSH key of the
                # person running this, so an SSH URL would fail at the clone.
                if ssh_url "$REPO_URL" && [ "$SVC_USER" != "$(id -un)" ]; then
                    msg "The code source $REPO_URL is an SSH URL. The service user $SVC_USER has no SSH key for it, so the clone would fail.

Change the code source to an https URL, or install with the current user."
                    continue
                fi
                break ;;
            source) extra=$(ask_input X "Git repository URL:" "$REPO_URL") && REPO_URL=$extra
                    extra=$(ask_input X "Branch:" "$REPO_BRANCH") && REPO_BRANCH=$extra ;;
            *) exit 0 ;;
        esac
    done

    if $UNATTENDED && ssh_url "$REPO_URL" && [ "$SVC_USER" != "$(id -un)" ]; then
        die "REPO_URL $REPO_URL is an SSH URL; the service user $SVC_USER has no key for it - use an https URL or SERVICE_USER=current"
    fi
    run_install_steps
}

run_install_steps() {
    $UNATTENDED || clear
    echo "Installing $NAME - log: $LOG_FILE"; echo
    STEP_N=0; STEP_TOTAL=$(( 9 + $( [ "$COMP_CASTER" = yes ] && echo 1 || echo 0 ) + $( [ "$SSL_MODE" != none ] && echo 1 || echo 0 ) ))
    run_step "System packages"               st_packages
    run_step "Service user"                  st_user
    run_step "Code ($REPO_BRANCH)"           st_clone
    save_registry   # from here on a rerun resumes this instance
    run_step "Python environment"            st_venv
    run_step "Configuration (.env)"          st_env
    run_step "Data directories"              st_dirs
    [ "$SSL_MODE" != none ] && run_step "HTTPS certificate" st_ssl
    [ "$COMP_CASTER" = yes ] && run_step "NTRIP caster (build + config)" st_caster
    run_step "Firewall"                      st_firewall
    run_step "systemd services"              st_units
    INSTALL_DONE=yes
    run_step "Register instance"             st_register
    echo; echo "Running the server test ..."
    local pytest=no
    ask_yesno RUN_PYTEST "Also run the full test suite (pytest)? It needs no hardware; on a Raspberry Pi it takes a minute or two." no && pytest=yes
    server_test "$pytest" yes
    show_test_result
    if [ "$COMP_STREAMING" = yes ] && [ -d "$DIR/configgen" ] &&
       ask_yesno GEN_CONFIG "Generate a device CONFIG.TXT for this server now?" no; then
        gen_device_config
    fi
    write_summary
    if $UNATTENDED; then
        cat "$SUMMARY_TMP"; rm -f "$SUMMARY_TMP"
        [ "$TEST_FAILS" -eq 0 ] || exit 3
        return 0
    fi
    local choice
    MENU_CANCEL="Exit"
    choice=$(ask_menu X "$NAME is installed$( [ "$TEST_FAILS" -eq 0 ] && echo " and the server test passed" || echo " - but $TEST_FAILS server test check(s) FAILED")." exit \
        exit   "Exit - print the summary (ports, keys, device settings) and quit" \
        manage "Open the manage menu of $NAME") || choice="exit"
    MENU_CANCEL="Cancel"
    if [ "$choice" = manage ]; then
        rm -f "$SUMMARY_TMP"; manage_menu "$NAME"; return 0
    fi
    finish_with_summary
}

# Leave the dialogs and print the summary as the last thing on the terminal, so
# the keys and the device settings stay in the scrollback to be copied.
finish_with_summary() {
    clear
    cat "$SUMMARY_TMP"; rm -f "$SUMMARY_TMP"
    echo
    echo "Setup finished. Manage this instance later with:  wormhole-setup"
    exit 0
}

# ======================================================= device config =====

gen_device_config() {
    local roles=() r role sid sname out f args=()
    [ -d "$DIR/configgen" ] || { msg "This version has no config generator."; return; }
    while read -r r _; do [ -n "$r" ] && roles+=("$r" ""); done < <(py -m configgen --list-roles 2>/dev/null)
    [ ${#roles[@]} -gt 0 ] || { msg "The config generator lists no roles."; return; }
    role=$(ask_menu DEVICE_ROLE "Role of the device:" "${roles[0]}" "${roles[@]}") || return
    args=(--role "$role")
    if [[ "$role" != batch* ]]; then
        sid=$(ask_input DEVICE_STATION_ID "Station ID of the device (number, unique per server):" "") || return
        [[ "$sid" =~ ^[0-9]+$ ]] || { msg "The station ID must be a number."; return; }
        args+=(--set "streaming_station_id=$sid")
    fi
    sname=$(ask_input DEVICE_NAME "Station name (optional, e.g. A001):" "") || return
    [ -n "$sname" ] && args+=(--set "station=$sname")
    out="$HOME/CONFIG_${sid:-$role}.TXT"
    out=$(ask_input DEVICE_OUT "Write the file to:" "$out") || return
    f=$(mktemp)
    if py -m configgen "${args[@]}" >"$out" 2>"$f"; then
        { echo "Written: $out"; echo "Copy it to the device's SD card as CONFIG.TXT."; echo; cat "$f"; } >"$f.2"
    else
        { echo "Written with ERRORS: $out"; echo "Fix the flagged keys before using it."; echo; cat "$f"; } >"$f.2"
    fi
    show_file "$f.2" "Device configuration"
    rm -f "$f" "$f.2"
}

# ============================================================== manage =====

pick_instance() {
    local items=() n
    for n in $(list_instances); do
        ( load_registry "$n" && echo "$n" "$DIR" )
    done >/tmp/wormhole-instances.$$
    while read -r n d; do items+=("$n" "$d"); done </tmp/wormhole-instances.$$
    rm -f /tmp/wormhole-instances.$$
    [ ${#items[@]} -gt 0 ] || { msg "No wormhole instance is registered on this host yet."; return 1; }
    ask_menu X "Which instance?" "${items[0]}" "${items[@]}"
}

status_report() {
    local f u key
    f=$(mktemp)
    key=$(env_get API_KEY)
    {
        echo "$NAME  ($DIR, user $SVC_USER)"; echo
        for u in $(units); do
            printf '%-26s %-9s since %s, restarts %s\n' "$u" "$(systemctl is-active "$u")" \
                "$(systemctl show "$u" -p ActiveEnterTimestamp --value)" "$(systemctl show "$u" -p NRestarts --value)"
        done
        echo
        if [ "$COMP_STREAMING" = yes ]; then
            echo "Stations:"
            curl -fsS --max-time 5 -H "X-API-Key: $key" "http://127.0.0.1:$ADMIN_PORT/stream/stations" 2>/dev/null |
                python3 -c 'import json, sys
d = json.load(sys.stdin)
rows = d.get("stations", d) if isinstance(d, dict) else d
if not rows: print("  none")
for s in rows:
    print("  %-8s %-12s role=%-6s rx=%-12s garbage=%s resyncs=%s" % (s.get("station_id"),
          "connected" if s.get("connected") else "offline", s.get("role"), s.get("bytes_rx"),
          s.get("garbage_bytes"), s.get("resync_events")))' || echo "  (admin API not reachable)"
            echo
        fi
        if [ "$COMP_CASTER" = yes ]; then
            echo "Caster mountpoints:"
            curl -fsS --max-time 5 -H "X-API-Key: $key" "http://127.0.0.1:$ADMIN_PORT/stream/caster" 2>/dev/null |
                python3 -c 'import json, sys
b = json.load(sys.stdin).get("bundled", {})
m = b.get("mountpoints", {})
print("  caster pid:", b.get("caster_pid"))
print("  none yet" if not m else "\n".join("  %s%s" % (k, " (auto)" if v.get("auto_provisioned") else "") for k, v in m.items()))' || echo "  (not reachable)"
            echo
        fi
        echo "Disk: $(in_dir du -sh data 2>/dev/null | cut -f1) in data/, $(df -h "$DIR" | awk 'NR==2 {print $4}') free"
        echo "Firewall ports opened: ${FW_PORTS:-none}"
        [ "$COMP_STREAMING" = yes ] && echo "Note: garbage/resyncs count per session - they start at 0 after every restart."
    } >"$f"
    show_file "$f" "Status - $NAME"
    rm -f "$f"
}

restart_warning() {
    [ "$COMP_STREAMING" = yes ] || return 0
    ask_yesno X "Restarting the streaming server disconnects all stations; they reconnect by themselves. Data sent in between is kept in the raw capture but not demuxed (replay.py can recover it). Continue?" yes
}

do_update() {
    local before after changed restart=() f
    f=$(mktemp)
    before=$(in_dir git rev-parse HEAD)
    if [ -n "$(in_dir git status --porcelain --untracked-files=no)" ]; then
        msg "$DIR has local changes to tracked files. Update refuses to overwrite them - commit or revert them first:

$(in_dir git status --short --untracked-files=no | head -20)"; return 1
    fi
    sudo_start
    echo "Updating $NAME ..."
    in_dir git pull --ff-only >>"$LOG_FILE" 2>&1 || { msg "git pull failed - see $LOG_FILE"; return 1; }
    after=$(in_dir git rev-parse HEAD)
    if [ "$before" = "$after" ]; then msg "$NAME is already up to date ($(echo "$after" | cut -c1-7))."; return 0; fi
    changed=$(in_dir git diff --name-only "$before" "$after")
    STEP_N=0; STEP_TOTAL=3
    run_step "Python packages" st_venv
    if ! step "Test suite" in_dir venv/bin/python -m pytest -q; then
        ask_yesno X "The test suite failed after the update. Restart the services anyway?" no || return 1
    fi
    if [ "$COMP_CASTER" = yes ] && echo "$changed" | grep -q '^caster/'; then
        run_step "NTRIP caster" st_caster; restart+=("$(unit caster)")
    fi
    echo "$changed" | grep -qE '^(streaming/|streaming_server\.py|downloads\.py|configgen/|requirements)' &&
        [ "$COMP_STREAMING" = yes ] && restart+=("$(unit stream)")
    echo "$changed" | grep -qE '^(server\.py|downloads\.py|requirements)' &&
        [ "$COMP_BATCH" = yes ] && restart+=("$(unit batch)")
    if [ ${#restart[@]} -gt 0 ]; then
        if restart_warning; then
            as_root systemctl restart "${restart[@]/%/.service}"; wait_listening 30 >>"$LOG_FILE" 2>&1
        fi
    fi
    server_test no no
    { echo "Updated $(echo "$before" | cut -c1-7) -> $(echo "$after" | cut -c1-7)"
      echo "Restarted: ${restart[*]:-nothing (no server code changed)}"; echo
      printf '%s' "$TEST_REPORT"; } >"$f"
    show_file "$f" "Update - $NAME"; rm -f "$f"
}

do_logs() {
    local items=() u choice f
    for u in $(units); do items+=("$u" "journal"); done
    [ "$COMP_STREAMING" = yes ] && items+=(streaming.log "streaming server log file")
    [ "$COMP_BATCH" = yes ] && items+=(server.log "batch server log file")
    items+=(install "installer log")
    choice=$(ask_menu X "Which log?" "${items[0]}" "${items[@]}") || return
    f=$(mktemp)
    case "$choice" in
        streaming.log|server.log) in_dir tail -n 300 "$choice" >"$f" 2>&1 ;;
        install) tail -n 300 "$LOG_FILE" >"$f" 2>&1 ;;
        *) as_root journalctl -u "$choice" -n 300 --no-pager >"$f" 2>&1 ;;
    esac
    show_file "$f" "$choice"; rm -f "$f"
}

do_uninstall() {   # [purge yes|no]
    local purge=${1:-ask} typed
    ask_yesno UNINSTALL "Remove $NAME?

Stops and removes its services, firewall rules and registration. Recorded data is only deleted if you confirm that separately." no || return 1
    sudo_start
    remove_units
    firewall_remove
    [ -f "$(le_hook_path)" ] && as_root rm -f "$(le_hook_path)"
    as_root rm -f "$REGISTRY_DIR/$NAME.conf"
    relink_setup
    if [ "$purge" = ask ]; then
        typed=$(ask_input X "Services removed. The directory $DIR (code, .env and ALL recorded data) is still there.

To delete it as well, type the instance name ($NAME). Leave empty to keep it." "") || typed=""
        [ "$typed" = "$NAME" ] && purge=yes || purge=no
    fi
    if [ "$purge" = yes ]; then as_root rm -rf "$DIR"; msg "$NAME removed, including $DIR."
    else msg "$NAME removed. $DIR was kept."; fi
}

settings_menu() {
    local choice v old_caster old ports_before
    while true; do
        choice=$(ask_menu X "Settings of $NAME - every change restarts what it affects and runs the server test." ports \
            ports    "Ports" \
            host     "Public address (PUBLIC_HOST): $(env_get PUBLIC_HOST)" \
            stations "Trusted base stations: $(env_get STREAM_ROVER_AUTO_BASE_STATIONS)" \
            secret   "Generate a new CLI secret" \
            raw      "Raw capture retention: $(env_get STREAM_RAW_MAX_AGE_H) h" \
            https    "HTTPS for batch uploads: $SSL_MODE" \
            firewall "Firewall rules (opened: ${FW_PORTS:-none})" \
            back     "Back") || return
        sudo_start
        case "$choice" in
            ports)
                ports_before="$STREAM_PORT $ADMIN_PORT $CASTER_PORT $BATCH_PORT"
                old_caster=$CASTER_PORT
                # shellcheck disable=SC2086
                dialog_ports $ports_before || continue
                [ "$ports_before" = "$STREAM_PORT $ADMIN_PORT $CASTER_PORT $BATCH_PORT" ] && continue
                restart_warning || { load_registry "$NAME"; continue; }
                [ "$COMP_STREAMING" = yes ] && { env_set STREAM_PORT "$STREAM_PORT"; env_set STREAM_ADMIN_PORT "$ADMIN_PORT"; }
                [ "$COMP_BATCH" = yes ] && env_set PORT "$BATCH_PORT"
                if [ "$COMP_CASTER" = yes ] && [ "$old_caster" != "$CASTER_PORT" ]; then
                    env_set STREAM_CASTER_PORT "$CASTER_PORT"; st_caster >>"$LOG_FILE" 2>&1
                fi
                save_registry
                if [ -n "$FW_PORTS" ]; then FW_ENABLED=yes FW_ACTIVATE=no FW_OPEN=$(public_ports | xargs); st_firewall >>"$LOG_FILE" 2>&1; fi
                apply_and_test "$(units)"
                [ "$COMP_STREAMING" = yes ] && msg "Devices must now use streaming port $STREAM_PORT - update their CONFIG.TXT (Generate device CONFIG.TXT)." ;;
            host)
                v=$(ask_input X "Address devices use to reach this server:" "$(env_get PUBLIC_HOST)") || continue
                env_set PUBLIC_HOST "$v"; PUBLIC_HOST=$v
                apply_and_test "$( [ "$COMP_STREAMING" = yes ] && echo "$(unit stream)")" ;;
            stations)
                v=$(ask_input X "Base station IDs trusted as correction sources for rovers (comma separated).

With a caster they also get a mountpoint. Existing mountpoints are never removed." "$(env_get STREAM_ROVER_AUTO_BASE_STATIONS)") || continue
                v=$(echo "$v" | tr -cd '0-9,' | sed 's/^,*//; s/,*$//; s/,,*/,/g')
                restart_warning || continue
                env_set STREAM_ROVER_AUTO_BASE_STATIONS "$v"
                if [ "$COMP_CASTER" = yes ]; then
                    old=$(env_get STREAM_CASTER_STATIONS)
                    env_set STREAM_CASTER_STATIONS "$(echo "$old,$v" | tr ',' '\n' | grep -E '^[0-9]+$' | sort -un | paste -sd,)"
                    st_caster >>"$LOG_FILE" 2>&1
                fi
                apply_and_test "$(units)" ;;
            secret)
                ask_yesno X "A new CLI secret locks out every device until its CONFIG.TXT (streaming_cli_secret) carries the new one. Continue?" no || continue
                v=$(gen_secret "$(cli_secret_max)")
                env_set STREAM_CLI_SECRET "$v"
                apply_and_test "$(unit stream)"
                msg "New CLI secret:

  $v

Set it as streaming_cli_secret on every device." ;;
            raw)
                v=$(ask_input X "Keep the raw capture for how many hours?" "$(env_get STREAM_RAW_MAX_AGE_H)") || continue
                [[ "$v" =~ ^[0-9]+$ ]] || continue
                env_set STREAM_RAW_MAX_AGE_H "$v"
                restart_warning && apply_and_test "$(unit stream)" ;;
            https)
                [ "$COMP_BATCH" = yes ] || { msg "This instance has no batch server."; continue; }
                v=$(ask_menu X "HTTPS for the batch upload server?" "$SSL_MODE" none "Plain HTTP" \
                    selfsigned "Self-signed certificate" letsencrypt "Let's Encrypt") || continue
                SSL_MODE=$v
                if [ "$v" = letsencrypt ]; then
                    LE_DOMAIN=$(ask_input X "DNS name:" "${LE_DOMAIN:-$(env_get PUBLIC_HOST)}") || continue
                    LE_EMAIL=$(ask_input X "E-mail for expiry notices:" "") || continue
                    st_packages >>"$LOG_FILE" 2>&1
                fi
                FW_ENABLED=no; [ -n "$FW_PORTS" ] && FW_ENABLED=yes
                PUBLIC_HOST=$(env_get PUBLIC_HOST)
                if ! st_ssl >>"$LOG_FILE" 2>&1; then msg "Setting up the certificate failed - see $LOG_FILE"; continue; fi
                save_registry
                st_units >>"$LOG_FILE" 2>&1
                apply_and_test "" ;;
            firewall)
                command -v ufw >/dev/null || { msg "ufw is not installed. Open $(public_ports | xargs) in your own firewall."; continue; }
                local items=() p
                for p in $(public_ports); do
                    if [ -z "$FW_PORTS" ] || echo " $FW_PORTS " | grep -q " $p "; then items+=("$p" "$(port_role "$p")" ON)
                    else items+=("$p" "$(port_role "$p")" OFF); fi
                done
                FW_OPEN=$(ask_checklist X "Ports to open (the admin port is never opened):" "${items[@]}") || continue
                FW_ENABLED=yes FW_ACTIVATE=no
                st_firewall >>"$LOG_FILE" 2>&1 && msg "Firewall rules applied: ${FW_PORTS:-none}" ;;
            back) return ;;
        esac
    done
}

apply_and_test() {   # units to restart
    local u
    for u in $1; do as_root systemctl restart "$u.service"; done
    wait_listening 30 >>"$LOG_FILE" 2>&1
    server_test no no
    show_test_result
}

manage_menu() {
    local choice
    load_registry "$1" || die "unknown instance $1"
    while true; do
        MENU_CANCEL="Back"
        choice=$(ask_menu X "$NAME  -  $DIR" status \
            status  "Status" \
            test    "Run the server test" \
            config  "Generate a device CONFIG.TXT" \
            update  "Update (git pull, test, restart what changed)" \
            restart "Restart services" \
            logs    "Logs" \
            settings "Settings (ports, address, stations, secret, HTTPS, firewall)" \
            summary "Show the install summary (ports, keys, device settings)" \
            uninstall "Uninstall" \
            back    "Back to the main menu" \
            exit    "Exit") || { MENU_CANCEL="Cancel"; return; }
        MENU_CANCEL="Cancel"
        case "$choice" in
            status)  status_report ;;
            test)    local pt=no; ask_yesno X "Include the full test suite (pytest)?" no && pt=yes
                     server_test "$pt" no; show_test_result ;;
            config)  gen_device_config ;;
            update)  do_update ;;
            restart) restart_warning && { sudo_start; apply_and_test "$(units)"; } ;;
            logs)    do_logs ;;
            settings) settings_menu ;;
            summary) write_summary; print_and_wait "$SUMMARY_TMP"; rm -f "$SUMMARY_TMP" ;;
            uninstall) do_uninstall ask && return ;;
            back)    return ;;
            exit)    write_summary; finish_with_summary ;;
        esac
    done
}

main_menu() {
    local choice n
    while true; do
        MENU_CANCEL="Exit"
        choice=$(ask_menu X "Wormhole - IoT file and streaming server

Install a new instance, or manage one that is installed on this host." install \
            install "Install a new wormhole instance" \
            manage  "Manage an installed instance ($(list_instances | wc -l) registered)" \
            exit    "Exit") || exit 0
        MENU_CANCEL="Cancel"
        case "$choice" in
            install) do_install ;;
            manage)  n=$(pick_instance) && manage_menu "$n" ;;
            exit)    exit 0 ;;
        esac
    done
}

# ================================================================ main =====

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; }

load_answers() {   # KEY=VALUE lines, # comments
    local line k v
    [ -f "$1" ] || die "answers file not found: $1"
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%%#*}; [[ "$line" == *=* ]] || continue
        k=$(echo "${line%%=*}" | xargs); v=$(echo "${line#*=}" | sed 's/^ *//; s/ *$//; s/^"\(.*\)"$/\1/')
        ANSWER[$k]=$v
    done <"$1"
}

bootstrap() {
    if ! command -v whiptail >/dev/null; then
        echo "Installing whiptail (dialog tool) ..."
        as_root apt-get install -y whiptail >/dev/null || die "cannot install whiptail"
    fi
    if [ "$(id -u)" -eq 0 ] && ! $UNATTENDED; then
        whiptail --title "$TITLE" --defaultno --yesno "You are running this as root.

Better: run it as the normal user that should own the installation; it asks for sudo where needed. Continue as root?" 12 70 || exit 1
    fi
}

ssh_url() { [[ "$1" == git@* || "$1" == ssh://* ]]; }

# Run from inside a git checkout, the default code source is that checkout's
# origin and branch: the installer then installs the code it came with. A
# downloaded copy keeps DEFAULT_REPO_URL.
detect_source() {
    local here url br
    here=$(dirname "$(readlink -f "$0")")
    git -C "$here" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 0
    url=$(git -C "$here" remote get-url origin 2>/dev/null) || return 0
    br=$(git -C "$here" symbolic-ref --short -q HEAD) || return 0
    DEFAULT_REPO_URL=$url DEFAULT_BRANCH=$br
}

main() {
    local cmd=${1:-menu}
    detect_source
    case "$cmd" in
        -h|--help) usage; exit 0 ;;
        --unattended)
            [ -n "${2:-}" ] || die "--unattended needs an answers file"
            UNATTENDED=true; load_answers "$2"
            sudo_start
            do_install ;;
        status|test|update|restart|uninstall)
            [ -n "${2:-}" ] || die "$cmd needs an instance name ($(list_instances | xargs))"
            load_registry "$2" || die "unknown instance $2"
            UNATTENDED=true
            sudo_start
            [[ " $* " == *" --yes "* ]] && { ANSWER[UNINSTALL]=yes; }
            case "$cmd" in
                status)  status_report ;;
                test)    server_test "$( [[ " $* " == *" --pytest "* ]] && echo yes || echo no)" no; show_test_result
                         [ "$TEST_FAILS" -eq 0 ] || exit 3 ;;
                update)  do_update ;;
                restart) sudo_start; apply_and_test "$(units)" ;;
                uninstall) do_uninstall "$( [[ " $* " == *" --purge "* ]] && echo yes || echo no)" ;;
            esac ;;
        menu)
            [ -t 0 ] && [ -t 1 ] || die "no terminal. Download the script and run it: bash install.sh"
            bootstrap
            sudo_start
            main_menu ;;
        *) usage; exit 2 ;;
    esac
}

main "$@"
