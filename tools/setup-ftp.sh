#!/usr/bin/env bash
# setup-ftp.sh - FTP and FTPS (vsftpd) for a wormhole instance.
#
# Devices that upload over FTP/FTPS instead of HTTP get one FTP account per
# instance, chrooted to the instance's data/ tree: uploads go to incoming/ (the
# same folder the batch server writes), downloads come from outgoing/ (the same
# config/firmware files the batch server serves). FTPS is explicit (AUTH TLS on
# port 21) and optional - plain FTP keeps working unless --force-tls.
#
#   bash tools/setup-ftp.sh [options]            set up / repair (idempotent)
#   bash tools/setup-ftp.sh --test [--user U]    self test only
#   bash tools/setup-ftp.sh --remove [--user U]  remove the account from vsftpd
#
# Options:
#   --dir DIR            instance directory (default: the checkout this script is in)
#   --user NAME          FTP login (default: FTP_USER in .env, else <directory>_ftp,
#                        e.g. wormhole_9000_ftp; the installer passes <instance>_ftp)
#   --password-stdin     read the password from stdin (default: generate one)
#   --keep-user NAME     also allow this existing local account (home unchanged); repeatable
#   --public-host HOST   address in passive replies (default: PUBLIC_HOST from .env, else none)
#   --pasv MIN-MAX       passive data ports (default 50000-50100)
#   --cert FILE --key FILE  TLS certificate (default: generated, /etc/ssl/private/wormhole-vsftpd.pem)
#   --no-tls | --force-tls  plain FTP only | refuse plain FTP (default: TLS optional)
#   --firewall auto|ufw|none  (default auto: ufw if it is active, else only a hint)
#   --yes                take over a vsftpd.conf that this script did not write
#   --dry-run            print what would change, change nothing
#
# vsftpd is one server per host: every instance adds its account to the same
# vsftpd.conf (written by this script, previous file backed up). Only accounts in
# /etc/vsftpd/wormhole.userlist may log in. The password is stored in the
# instance .env (FTP_USER / FTP_PASSWORD, mode 600). Documentation:
# docs/ftp-ftps-server-2026-10-17.md.

set -uo pipefail

VSFTPD_CONF=/etc/vsftpd.conf
USERLIST=/etc/vsftpd/wormhole.userlist
USER_CONF_DIR=/etc/vsftpd/user_conf
PAM_SERVICE=vsftpd-wormhole
STATE_DIR=/etc/wormhole/ftp
CERT_DEFAULT=/etc/ssl/private/wormhole-vsftpd.pem
MARKER="# Managed by wormhole tools/setup-ftp.sh"

MODE=setup DIR="" FTP_USER_ARG="" PW_STDIN=false KEEP_USERS=() PUBLIC_HOST_ARG=""
PASV="50000-50100" CERT="" KEY="" TLS=optional FIREWALL=auto YES=false DRY=false

die()  { echo "error: $*" >&2; exit 1; }
info() { echo "  $*"; }
if [ "$(id -u)" -eq 0 ]; then SUDO=(); else SUDO=(sudo); fi
as_root() { "${SUDO[@]}" "$@"; }
run() {   # mutating command: printed in --dry-run, executed as root otherwise
    if $DRY; then echo "  + $*"; return 0; fi
    as_root "$@"
}
put_file() {   # path mode owner content - write a root-owned file (diff in --dry-run)
    local path=$1 mode=$2 owner=$3 content=$4 tmp
    tmp=$(mktemp)
    printf '%s\n' "$content" >"$tmp"
    if $DRY; then
        echo "  = $path ($mode $owner)"
        as_root diff -u "$path" "$tmp" 2>/dev/null | sed 's/^/    /' | head -60
        rm -f "$tmp"; return 0
    fi
    as_root install -D -m "$mode" -o "${owner%:*}" -g "${owner#*:}" "$tmp" "$path"
    rm -f "$tmp"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir) DIR=$2; shift ;;
        --user) FTP_USER_ARG=$2; shift ;;
        --password-stdin) PW_STDIN=true ;;
        --keep-user) KEEP_USERS+=("$2"); shift ;;
        --public-host) PUBLIC_HOST_ARG=$2; shift ;;
        --pasv) PASV=$2; shift ;;
        --cert) CERT=$2; shift ;;
        --key) KEY=$2; shift ;;
        --no-tls) TLS=off ;;
        --force-tls) TLS=forced ;;
        --firewall) FIREWALL=$2; shift ;;
        --yes) YES=true ;;
        --dry-run) DRY=true ;;
        --test) MODE="test" ;;
        --remove) MODE="remove" ;;
        -h|--help) sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option $1 (--help)" ;;
    esac
    shift
done

# ---------------------------------------------------------------- instance --
[ -n "$DIR" ] || DIR=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
DIR=$(readlink -f "$DIR")
[ -d "$DIR/data" ] || die "$DIR has no data/ directory - is it a wormhole instance?"
NAME=$(basename "$DIR")
SVC_USER=$(stat -c %U "$DIR/data")
SVC_GROUP=$(stat -c %G "$DIR/data")
env_get() { as_root sed -n "s/^$1=//p" "$DIR/.env" 2>/dev/null | tail -1 | sed 's/^"\(.*\)"$/\1/'; }
# account: --user, else the one this instance already has (.env), else <directory>_ftp
FTP_USER=${FTP_USER_ARG:-$(env_get FTP_USER)}
FTP_USER=${FTP_USER:-${NAME}_ftp}
[[ "$FTP_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || die "invalid user name '$FTP_USER'"
[[ "$PASV" =~ ^([0-9]+)-([0-9]+)$ ]] || die "--pasv needs MIN-MAX"
PASV_MIN=${BASH_REMATCH[1]} PASV_MAX=${BASH_REMATCH[2]}
[ "$PASV_MIN" -ge 1024 ] && [ "$PASV_MAX" -gt "$PASV_MIN" ] && [ "$PASV_MAX" -le 65535 ] || die "bad passive range $PASV"

env_set() {   # KEY VALUE - in the instance .env, kept 600 and owned by the service user
    if $DRY; then echo "  + .env: $1=<${#2} chars>"; return 0; fi
    as_root touch "$DIR/.env"
    if as_root grep -q "^$1=" "$DIR/.env"; then
        as_root sed -i "s|^$1=.*|$1=$2|" "$DIR/.env"
    else
        printf '%s=%s\n' "$1" "$2" | as_root tee -a "$DIR/.env" >/dev/null
    fi
    as_root chown "$SVC_USER:$SVC_GROUP" "$DIR/.env"; as_root chmod 600 "$DIR/.env"
}

# ------------------------------------------------------------- self test ---
TEST_FAILS=0
ok()   { echo "[ OK ] $*"; }
fail() { echo "[FAIL] $*"; TEST_FAILS=$((TEST_FAILS + 1)); }
note() { echo "[INFO] $*"; }

ftp_roundtrip() {   # label curl-tls-args... - upload to incoming/, download from /outgoing
    local label=$1; shift
    local tag up dl got
    tag="wormhole-ftptest-$$-${label// /_}"
    up=$(mktemp); dl=$(mktemp); got=$(mktemp)
    head -c 70000 /dev/urandom >"$up"; head -c 30000 /dev/urandom >"$dl"
    as_root install -o "$SVC_USER" -g "$SVC_GROUP" -m 644 "$dl" "$DIR/data/outgoing/$tag.dl"
    if curl -sS --max-time 30 --netrc-file "$NETRC" --ftp-skip-pasv-ip "$@" -T "$up" \
            "ftp://127.0.0.1/incoming/$tag.up" 2>>"$ERRLOG" &&
       as_root cmp -s "$up" "$DIR/data/incoming/$tag.up"; then
        ok "$label upload to incoming/ ($(stat -c %s "$up") B, bytes equal)"
    else
        fail "$label upload to incoming/ (see $ERRLOG)"
    fi
    if curl -sS --max-time 30 --netrc-file "$NETRC" --ftp-skip-pasv-ip "$@" -o "$got" \
            "ftp://127.0.0.1/outgoing/$tag.dl" 2>>"$ERRLOG" && cmp -s "$dl" "$got"; then
        ok "$label download from /outgoing ($(stat -c %s "$dl") B, bytes equal)"
    else
        fail "$label download from /outgoing (see $ERRLOG)"
    fi
    as_root rm -f "$DIR/data/incoming/$tag.up" "$DIR/data/outgoing/$tag.dl"
    rm -f "$up" "$dl" "$got"
}

self_test() {   # password in $FTP_PW (may be empty)
    local u=$FTP_USER
    ERRLOG=$(mktemp /tmp/wormhole-ftptest.XXXXXX)
    if systemctl is-active --quiet vsftpd; then ok "vsftpd is running"; else fail "vsftpd is not running"; fi
    if systemctl is-enabled --quiet vsftpd; then ok "vsftpd starts at boot"; else fail "vsftpd is not enabled at boot"; fi
    if ss -ltnH '( sport = :21 )' | grep -q .; then ok "port 21 is listening"; else fail "port 21 is not listening"; fi
    if as_root head -1 "$VSFTPD_CONF" 2>/dev/null | grep -q "^$MARKER"; then
        ok "$VSFTPD_CONF written by setup-ftp.sh"
    else fail "$VSFTPD_CONF was not written by setup-ftp.sh"; fi
    if as_root grep -qx "$u" "$USERLIST" 2>/dev/null; then ok "$u may log in ($USERLIST)"; else fail "$u is not in $USERLIST"; fi
    if as_root grep -qx "local_root=$DIR/data" "$USER_CONF_DIR/$u" 2>/dev/null; then ok "$u is chrooted to $DIR/data"
    else fail "$u has no local_root=$DIR/data"; fi
    if as_root runuser -u "$u" -- test -w "$DIR/data/incoming" 2>/dev/null; then ok "$u can write data/incoming"
    else fail "$u cannot write data/incoming"; fi
    if [ -z "${FTP_PW:-}" ]; then
        note "no password known for $u - login tests skipped (FTP_PASSWORD in .env or --password-stdin)"
    else
        NETRC=$(mktemp); chmod 600 "$NETRC"
        printf 'machine 127.0.0.1 login %s password %s\n' "$u" "$FTP_PW" >"$NETRC"
        if [ "$TLS" != forced ]; then ftp_roundtrip "FTP"; else
            if curl -sS --max-time 10 --netrc-file "$NETRC" "ftp://127.0.0.1/" >/dev/null 2>&1; then
                fail "plain FTP login accepted although --force-tls"
            else ok "plain FTP refused (--force-tls)"; fi
        fi
        if [ "$TLS" != off ]; then ftp_roundtrip "FTPS" --ssl-reqd -k; fi
        if curl -sS --max-time 10 --user "$u:wrong-$$" "ftp://127.0.0.1/" >/dev/null 2>&1; then
            fail "a wrong password was accepted"
        else ok "a wrong password is refused"; fi
        rm -f "$NETRC"
    fi
    note "from outside test with: curl --ssl-reqd -k --user $u ftp://<host>/incoming/ (ports 21 and $PASV_MIN-$PASV_MAX must be open)"
    [ -s "$ERRLOG" ] || rm -f "$ERRLOG"
    [ "$TEST_FAILS" -eq 0 ]
}

if [ "$MODE" = test ]; then
    TLS=$( as_root grep -q '^ssl_enable=YES' "$VSFTPD_CONF" 2>/dev/null && { as_root grep -q '^force_local_logins_ssl=YES' "$VSFTPD_CONF" && echo forced || echo optional; } || echo off )
    FTP_PW=""
    if $PW_STDIN; then IFS= read -r FTP_PW; FTP_PW=${FTP_PW%$'\r'}
    elif [ "$(env_get FTP_USER)" = "$FTP_USER" ]; then FTP_PW=$(env_get FTP_PASSWORD); fi
    self_test; exit $(( TEST_FAILS ? 3 : 0 ))
fi

# ---------------------------------------------------------------- remove ---
if [ "$MODE" = remove ]; then
    echo "Removing FTP account $FTP_USER of $NAME (files in $DIR/data are kept)"
    run sed -i "/^$FTP_USER\$/d" "$USERLIST"
    run rm -f "$USER_CONF_DIR/$FTP_USER"
    if as_root grep -q '^CREATED_USER=yes' "$STATE_DIR/$FTP_USER.conf" 2>/dev/null; then run userdel "$FTP_USER"; fi
    run rm -f "$STATE_DIR/$FTP_USER.conf"
    if [ -z "$(as_root ls "$STATE_DIR" 2>/dev/null)" ] && command -v ufw >/dev/null; then
        run ufw delete allow 21/tcp >/dev/null 2>&1
        run ufw delete allow "$PASV_MIN:$PASV_MAX/tcp" >/dev/null 2>&1
        info "last wormhole FTP account removed - ufw rules for 21 and $PASV_MIN:$PASV_MAX deleted (if they existed)"
    fi
    if [ "$(env_get FTP_USER)" = "$FTP_USER" ]; then
        $DRY || as_root sed -i '/^FTP_USER=/d; /^FTP_PASSWORD=/d' "$DIR/.env"
    fi
    run systemctl restart vsftpd
    echo "done. vsftpd itself stays installed."
    exit 0
fi

# ----------------------------------------------------------------- setup ---
command -v apt-get >/dev/null || die "needs a Debian/Ubuntu system (apt-get)"
id "$SVC_USER" >/dev/null 2>&1 || die "owner of $DIR/data ($SVC_USER) is not a user"
$DRY && echo "dry run - nothing is changed"
echo "FTP/FTPS for $NAME: account $FTP_USER, root $DIR/data, TLS $TLS, passive $PASV_MIN-$PASV_MAX"

# 1. packages
missing=()
for p in vsftpd openssl curl; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if [ ${#missing[@]} -gt 0 ]; then
    run env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" || die "apt-get failed"
fi

# 2. take over vsftpd.conf (backup)
if as_root test -f "$VSFTPD_CONF" && ! as_root head -1 "$VSFTPD_CONF" | grep -q "^$MARKER"; then
    if ! $YES && ! $DRY; then
        echo "$VSFTPD_CONF was not written by this script. It will be backed up and replaced;"
        echo "only accounts in $USERLIST may log in afterwards (add others with --keep-user)."
        read -r -p "Continue? [y/N] " a; [[ "$a" =~ ^[yY] ]] || exit 1
    fi
    run cp -p "$VSFTPD_CONF" "$VSFTPD_CONF.bak_$(date +%Y%m%d_%H%M%S)"
fi

# 3. password
FTP_PW=""
if $PW_STDIN; then IFS= read -r FTP_PW; FTP_PW=${FTP_PW%$'\r'}; [ -n "$FTP_PW" ] || die "empty password on stdin"
elif [ "$(env_get FTP_USER)" = "$FTP_USER" ] && [ -n "$(env_get FTP_PASSWORD)" ]; then FTP_PW=$(env_get FTP_PASSWORD)
else FTP_PW=$(head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 24); fi
# the device field passwd_ftp of the reference firmware is 32 bytes incl. the terminator
[ ${#FTP_PW} -le 31 ] || die "password longer than 31 characters (device field)"

# 4. account: no shell, home = data/, primary group = the service user's group
CREATED=no
if id "$FTP_USER" >/dev/null 2>&1; then
    as_root grep -q '^CREATED_USER=yes' "$STATE_DIR/$FTP_USER.conf" 2>/dev/null && CREATED=yes
    run usermod -d "$DIR/data" -g "$SVC_GROUP" "$FTP_USER"
else
    run useradd --system --no-create-home --home-dir "$DIR/data" --shell /usr/sbin/nologin \
        --gid "$SVC_GROUP" --comment "wormhole $NAME FTP" "$FTP_USER" || die "useradd failed"
    CREATED=yes
fi
if $DRY; then echo "  + chpasswd $FTP_USER"; else printf '%s:%s\n' "$FTP_USER" "$FTP_PW" | as_root chpasswd || die "chpasswd failed"; fi
run chmod g+w "$DIR/data/incoming"
run chmod g-w "$DIR/data"

# 5. certificate
if [ "$TLS" != off ]; then
    if [ -z "$CERT" ]; then
        CERT=$CERT_DEFAULT KEY=$CERT_DEFAULT
        if ! as_root test -s "$CERT_DEFAULT"; then
            cn=${PUBLIC_HOST_ARG:-$(env_get PUBLIC_HOST)}; cn=${cn:-$(hostname -f)}
            run openssl req -x509 -nodes -days 3650 -newkey rsa:2048 -subj "/CN=$cn" \
                -keyout "$CERT_DEFAULT" -out "$CERT_DEFAULT" || die "openssl failed"
            run chmod 600 "$CERT_DEFAULT"
        fi
    fi
    [ -n "$KEY" ] || KEY=$CERT
fi

# 6. PAM: Debian's vsftpd service without pam_shells (the account has no shell);
#    the userlist decides who may log in
put_file "/etc/pam.d/$PAM_SERVICE" 644 root:root "$MARKER
# Debian's /etc/pam.d/vsftpd without pam_shells: wormhole FTP accounts have the shell
# /usr/sbin/nologin. Who may log in is decided by vsftpd's userlist ($USERLIST).
auth	required	pam_listfile.so item=user sense=deny file=/etc/ftpusers onerr=succeed
@include common-account
@include common-session
@include common-auth"

# 7. userlist, per-user root
users=$( { as_root cat "$USERLIST" 2>/dev/null; echo "$FTP_USER"; printf '%s\n' "${KEEP_USERS[@]}"; } |
         grep -E '^[a-z_][a-z0-9_-]*$' | sort -u )
put_file "$USERLIST" 644 root:root "$users"
put_file "$USER_CONF_DIR/$FTP_USER" 644 root:root "$MARKER
local_root=$DIR/data
deny_file={incoming_stream,*.partial}"

# 8. vsftpd.conf
host=${PUBLIC_HOST_ARG:-$(env_get PUBLIC_HOST)}
pasv_lines=""
if [ -n "$host" ]; then
    pasv_lines="pasv_address=$host"
    [[ "$host" =~ ^[0-9.]+$ ]] || pasv_lines+=$'\n'"pasv_addr_resolve=YES"
fi
if [ "$TLS" = off ]; then
    tls_lines="ssl_enable=NO"
else
    force=NO; [ "$TLS" = forced ] && force=YES
    tls_lines="ssl_enable=YES
rsa_cert_file=$CERT
rsa_private_key_file=$KEY
allow_anon_ssl=NO
force_local_logins_ssl=$force
force_local_data_ssl=$force
ssl_tlsv1=YES
ssl_sslv2=NO
ssl_sslv3=NO
ssl_ciphers=HIGH
# The Quectel EC25 opens the data connection without resuming the TLS session of the
# control connection, and closes it without a TLS close_notify. With vsftpd's defaults
# every login works but every transfer fails (QFTPPUT 627/426).
require_ssl_reuse=NO
strict_ssl_read_eof=NO"
fi
put_file "$VSFTPD_CONF" 644 root:root "$MARKER - rerun it instead of editing.
# Instances and accounts: $STATE_DIR, $USERLIST, $USER_CONF_DIR.
listen=YES
listen_ipv6=NO
anonymous_enable=NO
local_enable=YES
write_enable=YES
local_umask=022
chroot_local_user=YES
allow_writeable_chroot=YES
secure_chroot_dir=/var/run/vsftpd/empty
pam_service_name=$PAM_SERVICE
userlist_enable=YES
userlist_deny=NO
userlist_file=$USERLIST
user_config_dir=$USER_CONF_DIR
dirmessage_enable=NO
xferlog_enable=YES
connect_from_port_20=YES
pasv_enable=YES
pasv_min_port=$PASV_MIN
pasv_max_port=$PASV_MAX
$pasv_lines
$tls_lines"

# 9. instance .env + state
env_set FTP_USER "$FTP_USER"
env_set FTP_PASSWORD "$FTP_PW"
put_file "$STATE_DIR/$FTP_USER.conf" 644 root:root "# wormhole FTP account - written by tools/setup-ftp.sh
INSTANCE_DIR=$DIR
CREATED_USER=$CREATED
PASV=$PASV_MIN-$PASV_MAX
TLS=$TLS"

# 10. firewall
case "$FIREWALL" in
    auto) if command -v ufw >/dev/null && as_root ufw status | grep -q "Status: active"; then FIREWALL=ufw; else FIREWALL=none; fi ;;
esac
if [ "$FIREWALL" = ufw ]; then
    run ufw allow 21/tcp comment "wormhole ftp" >/dev/null
    run ufw allow "$PASV_MIN:$PASV_MAX/tcp" comment "wormhole ftp passive" >/dev/null
    info "ufw: 21/tcp and $PASV_MIN:$PASV_MAX/tcp open"
else
    info "firewall not changed - open 21/tcp and $PASV_MIN-$PASV_MAX/tcp (FTPS: a conntrack FTP helper cannot read the encrypted PASV reply, so the passive range must be open explicitly)"
fi

# 11. start
run systemctl enable vsftpd >/dev/null 2>&1
run systemctl restart vsftpd || die "vsftpd did not start - journalctl -u vsftpd"
$DRY && exit 0
sleep 1
echo
self_test
rc=$?
echo
echo "Overview"
echo "  instance      $DIR (service user $SVC_USER)"
echo "  FTP account   $FTP_USER - chroot $DIR/data: uploads incoming/, downloads /outgoing"
echo "  ports         21/tcp + passive $PASV_MIN-$PASV_MAX/tcp, public; shared by all instances on this host"
echo "  TLS           $( [ "$TLS" = off ] && echo "off (plain FTP)" || echo "explicit FTPS on 21$( [ "$TLS" = forced ] && echo ", plain FTP refused" || echo ", plain FTP still allowed")")"
echo
echo "Device CONFIG.TXT (folder_ftp_dl needs firmware with that key; older firmware: see the documentation):"
echo "  upload_protocol = 0"
echo "  server_ftp = ${host:-<this server>}"
echo "  port_ftp = 21"
echo "  ftp_tls = $( [ "$TLS" = off ] && echo 0 || echo 1)"
echo "  folder_ftp = incoming"
echo "  folder_ftp_dl = /outgoing"
echo "  user_ftp = $FTP_USER"
echo "  passwd_ftp = (FTP_PASSWORD in $DIR/.env)"
exit $(( rc ? 3 : 0 ))
