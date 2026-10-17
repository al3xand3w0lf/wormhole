# FTP and FTPS for batch devices (`tools/setup-ftp.sh`) — 2026-10-17

Batch devices can upload over FTP instead of HTTP (`upload_protocol = 0` in the
reference firmware's `CONFIG.TXT`). `tools/setup-ftp.sh` sets up an FTP/FTPS
server for an instance, repeatably, and the installer offers it as the component
**ftp**. Result: one wormhole instance serves HTTP, HTTPS, FTP and FTPS from the
**same** file tree.

```bash
bash tools/setup-ftp.sh                    # in the instance checkout: set up / repair
bash tools/setup-ftp.sh --test             # self test only
bash tools/setup-ftp.sh --remove           # take the account out of vsftpd again
bash install.sh --unattended answers.env   # with COMPONENTS=... batch ftp
```

## What it builds

| | |
|---|---|
| Server | **vsftpd** (Debian package), one per host, standalone on port 21 |
| Account | one per instance, `<instance>_ftp` (e.g. `wormhole_8000_ftp`; the installer passes it, on its own the script takes `FTP_USER` from `.env`, else `<directory>_ftp`): system user, shell `/usr/sbin/nologin`, home `<dir>/data`, primary group = the group of the service user |
| View | chroot to `<dir>/data`: **uploads to `incoming/`** (where `server.py` puts HTTP uploads), **downloads from `outgoing/`** (what `/modem/download` serves). `incoming_stream/` and `*.partial` are denied |
| Who may log in | only accounts in `/etc/vsftpd/wormhole.userlist` (`userlist_deny=NO`); other local accounts with `--keep-user NAME` |
| TLS | explicit FTPS (`AUTH TLS` on 21), **optional** (plain FTP still works); `--force-tls` refuses plain FTP, `--no-tls` switches TLS off. Certificate `/etc/ssl/private/wormhole-vsftpd.pem` (self-signed, 10 years) unless `--cert/--key` |
| Passive ports | `50000-50100` (`--pasv MIN-MAX`); `pasv_address` from `PUBLIC_HOST` in `.env` or `--public-host` (DNS name → `pasv_addr_resolve=YES`) |
| Password | generated (24 characters, `A-Za-z0-9`) or `--password-stdin`; stored in the instance `.env` as `FTP_USER` / `FTP_PASSWORD` (600). At most 31 characters: the device field `passwd_ftp` of the reference firmware is 32 bytes incl. the terminator |
| Firewall | `--firewall auto` (default): ufw if active (`21/tcp`, `MIN:MAX/tcp`, comment `wormhole ftp`), otherwise only a hint |
| Files written | `/etc/vsftpd.conf` (old file backed up as `.bak_<date>`), `/etc/pam.d/vsftpd-wormhole`, `/etc/vsftpd/wormhole.userlist`, `/etc/vsftpd/user_conf/<account>`, `/etc/wormhole/ftp/<account>.conf` |

It ends with the self test, an overview (instance, account, ports 21 + passive
range, TLS mode) and the device settings:

```ini
upload_protocol = 0
server_ftp = <PUBLIC_HOST>
port_ftp = 21
ftp_tls = 1
folder_ftp = incoming
folder_ftp_dl = /outgoing
user_ftp = wormhole_8000_ftp
passwd_ftp = <FTP_PASSWORD from .env>
```

## Why it is built this way

- **vsftpd, not an FTP server in Python.** FTPS with passive data connections,
  chroot and PAM is what vsftpd has done securely for decades. The device
  specifics are three lines of configuration (below), not code.
- **One file tree for HTTP and FTP.** A device that switches protocols finds the
  same downloads, and the operator puts config and firmware into one folder,
  `data/outgoing`. A device with only one FTP folder for uploads **and**
  downloads (`folder_ftp`, `AT+QFTPCWD`) cannot reach both, though. Firmware with
  a separate download key (`folder_ftp_dl`) keeps the session in `incoming` and
  names the path in the download (`AT+QFTPGET="/outgoing/<name>"`). For firmware
  without it, see the next-but-one section.
- **Userlist instead of "every local user".** vsftpd's default lets every account
  with a valid shell log in — on an internet-facing test host the log showed
  about 500 attempts each for `admin`, `root`, `test`, `user`, … on port 21. With
  the userlist, unknown names are refused before the password is checked.
- **Own PAM service.** Debian's `/etc/pam.d/vsftpd` contains `pam_shells`, which
  refuses accounts with `/usr/sbin/nologin`. Instead of adding `nologin` to
  `/etc/shells` (that would open every nologin account system-wide), the script
  writes `vsftpd-wormhole` without `pam_shells`; the userlist decides.
- **Primary group = the service user's group.** Uploaded files are owned by the
  FTP account with the service group and mode 644: the batch server can read,
  rename and delete them (it owns the directory). `incoming/` gets `g+w`; `data/`
  itself must not be group-writable (chroot root).
- **Open the passive range explicitly.** A conntrack FTP helper can read the
  `PASV` reply of plain FTP and open the data port by itself, but not inside TLS.
  On the test host the helper did not even work for plain FTP (nftables, `policy
  drop`: no `helper=ftp`, no expectation) — hence the fixed range.

### The three lines for cellular modems (tested with a Quectel EC25)

```ini
require_ssl_reuse=NO     # the modem opens the data connection without resuming the TLS session
strict_ssl_read_eof=NO   # it closes the data connection without TLS close_notify
ssl_tlsv1=YES            # TLS 1.2 (vsftpd 3.0.3 allows TLS >= 1.0 with this switch, SSLv2/3 off)
```

Without `strict_ssl_read_eof=NO` every login works but every upload fails
(`+QFTPPUT: 627,426`, vsftpd: "DATA connection terminated without SSL shutdown").
With it, vsftpd accepts the file and only writes a `DEBUG` line per upload
("Buggy client! Integrity of upload cannot be asserted") — the device compares
size and checksum itself.

### Files of other owners in `incoming/`

HTTP uploads in `incoming/` belong to the service user (644). An FTP upload under
the same name is refused (`553`): overwriting needs write permission on the
**file**. Deleting needs it only on the **directory**, which the FTP account has
through the group. A device that gets `550/553` should therefore not treat it as
a transport failure: ask `SIZE`, delete the old file (`DELE`) if it is empty or
the same size (an earlier attempt), otherwise upload under a unique name.

## Older firmware, existing FTP accounts

- Devices without `folder_ftp_dl`: `folder_ftp = incoming`, and config/firmware
  files for them additionally into `data/incoming/` — or keep a separate account
  with its own home (`--keep-user`), which the script leaves untouched.
- Existing hand-made accounts: `--keep-user NAME` puts them on the userlist with
  their home directory unchanged.
- On a host that already ran vsftpd the script asks before replacing
  `vsftpd.conf` (`--yes` in scripts and the installer); the old file is kept as
  `.bak_<date>`.

## Self test

`--test`, the end of every setup, and the installer's server test (lines prefixed
`ftp:`):

- vsftpd active and enabled, port 21 listening, `vsftpd.conf` written by the script;
- account on the userlist, chrooted to `<dir>/data`, can write `incoming/`;
- **FTP and FTPS**: 70 kB random upload to `incoming/`, compared byte by byte on
  disk; 30 kB random file in `outgoing/`, downloaded and compared; both deleted;
- a wrong password is refused.

The login tests need the password (`.env`, or `--password-stdin`); without it
they are skipped with an `[INFO]` line. Reachability from outside cannot be tested
on the machine itself; the report prints the `curl` line for another machine.

## Verified (2026-10-17, Debian, vsftpd 3.0.3)

| Test | Result |
|---|---|
| Dry run against a hand-made config | diff shown, nothing changed |
| Setup with `--keep-user` for an existing account | 12 checks OK (first run: a password file written on Windows ended in `\r` → `530`; stdin is now stripped of `\r`) |
| Cellular device over FTPS | uploads to `/incoming` (`FTP 226`), owner FTP account; config download from `/outgoing`, CRC-32 equal on device and server |
| Kept account | still logs in, home unchanged |
| Installer, `COMPONENTS=batch ftp`, throw-away instance (unattended) | 10/10 steps, server test 18/18 incl. 12 `ftp:` checks; `uninstall --purge --yes` removed account, user config, state file, units and directory; the other instance's account still 12/12 |

## Open

- Let's Encrypt: the renewal hook restarts the batch server, not vsftpd; vsftpd
  uses its own self-signed certificate (devices do not check it).
- An FTP upload is visible under its final name while it is still being written
  (vsftpd has no temp-name-and-rename). The batch server ignores `incoming/`
  contents it did not write; an evaluation script should look at the size/mtime.
- Several instances on one host share port 21, the passive range, the
  certificate and `pasv_address` (the last run's `PUBLIC_HOST` wins); each run
  adds its account.
