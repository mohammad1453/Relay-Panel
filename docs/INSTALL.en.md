# Install Relay alongside your existing 3X-UI panel

## Requirements

Debian 12/13 or Ubuntu 22.04/24.04/26.04, x86_64 or aarch64, systemd, Python 3.10+, sudo/root, internet for package installation and at least 1 GiB free space. These are supported installer targets, **not a claim of live distro/VPS testing**. Any MHSanaei/3x-ui version from 2.x to the newest 3.x works; see [3X-UI compatibility](API.md). Have its actual API base URL including web path and an admin-scoped token (recommended), or username/password without interactive 2FA. These are backend credentials only.

Inspect your panel installation without changing it:

```bash
sudo systemctl list-unit-files | grep -Ei 'x-ui|3x-ui'
sudo systemctl show x-ui.service --property=FragmentPath
sudo systemctl status x-ui.service --no-pager
sudo ss -ltnp
```

For a differently named or containerized panel inspect the corresponding service/container configuration. Identify its real database location/backend from your protected installation configuration. Do not publish that configuration or passwords. You must supply the original SQLite paths, or `external` for PostgreSQL. Relay will refuse a database collision and also refuses any existing non-Relay SQLite schema.

## Transfer and install

The quickest way is the online installer. It downloads the latest release from [GitHub Releases](https://github.com/mohammad1453/Relay-Panel/releases), checks its SHA-256 and runs `install.sh`. Every option of `install.sh` can be added at the end. Add `--version vX.Y.Z` for a specific release.

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh)
```

To install by hand instead, download `relay-panel-vX.Y.Z.tar.gz` and its `.sha256` from the release page (or build them with `python tools/package_release.py`):

```bash
mkdir -p ~/relay-source && cd ~
sha256sum -c relay-panel-vX.Y.Z.tar.gz.sha256
tar -xzf relay-panel-vX.Y.Z.tar.gz -C ~/relay-source
cd ~/relay-source
sudo bash ./install.sh --dry-run
sudo bash ./install.sh
```

**3X-UI on the same VPS:** enter its address the way you open it in the browser. If that is the public IP of this VPS with plain `http://`, the installer switches to `http://127.0.0.1:PORT/WEB-PATH/` (same port and web path) and stores the public host as `XUI_LINK_HOST`, so customer links keep showing it. Plain HTTP to 3X-UI is only allowed on 127.0.0.1; a 3X-UI on another server needs its `https://` address.

The installer checks OS/architecture/systemd, existing services, conventional panel paths, listeners, application path ownership and space. It asks for the HTTPS application origin, master URL, original DB paths and master credentials before making installation changes. Passwords are hidden and never command-line arguments. It installs Python venv support, creates `xui-reseller`, installs the application, initializes its DB and prompts for the initial administrator username/password. Remember that password; it is not logged or saved in plaintext. First login requires changing it.

Separate locations:

| Item | Path |
|---|---|
| Program | `/opt/3xui-reseller/` |
| Protected configuration | `/etc/3xui-reseller/app.env` |
| Dedicated database | `/var/lib/3xui-reseller/app.db` |
| Backups | `/var/backups/3xui-reseller/` |
| Service | `xui-reseller.service` |

The original `x-ui.service`, database and configuration are not reused or changed. Rerunning a completed installation preserves all secrets/data/accounts. Interrupted setup can be rerun after correcting the reported issue. Installation may remain installed with a failed master connection; `status.sh` and UI operations expose the problem. Fix `/etc/3xui-reseller/app.env` with `sudoedit`, then restart **only** `xui-reseller.service`.

## Direct IP and web-path compatibility mode

For a 3X-UI-style address such as `http://203.0.113.10:8095/relay-secret/`, use the explicit public HTTP mode:

```bash
sudo bash ./install.sh --public-http \
  --origin http://YOUR_PUBLIC_VPS_IP:8095/CHANGE-THIS-LONG-WEB-PATH/ \
  --port 8095
```

The origin must contain a literal non-loopback IP, the same port, and a nonempty web path. Relay binds to all network interfaces in this mode. The installer does not modify the firewall; allow TCP 8095 in the VPS provider firewall and/or UFW if required. The web path reduces routine scanning but does not encrypt traffic: passwords, sessions and customer data cross the network as plaintext HTTP. Use a restrictive source-IP firewall rule or move to HTTPS for Internet-wide access.

## Safe SSH tunnel evaluation (no public port)

If you do not yet have HTTPS, choose this explicit private mode at installation:

```bash
cd ~/relay-source
sudo bash ./install.sh --tunnel-http --origin http://localhost:8095 --port 8095
```

On your own computer:

```bash
ssh -L 8095:127.0.0.1:8095 YOUR_SSH_USER@YOUR_VPS
```

Open `http://localhost:8095` locally. Keep the SSH session open. If 8095 is occupied select another port consistently in install and tunnel commands. This explicit evaluation mode relaxes Secure cookies only for tunnel HTTP; never expose it publicly. To transition to production set `APP_ENV="production"` and `APP_ORIGIN="https://your-dedicated-domain"`, configure HTTPS and restart the reseller service.

## Dedicated domain and HTTPS

Use a dedicated hostname pointing to this VPS. Existing proxy configurations are preserved. If Nginx is already installed and you have a valid certificate/key for that hostname, automatic setup is available during installation:

```bash
sudo bash ./install.sh --origin https://reseller.example.com \
  --domain reseller.example.com \
  --tls-cert /etc/letsencrypt/live/reseller.example.com/fullchain.pem \
  --tls-key /etc/letsencrypt/live/reseller.example.com/privkey.pem
```

Replace the example domain and paths. The tool checks DNS resolution, certificate hostname/expiry, conflicts, creates `/etc/nginx/conf.d/3xui-reseller.conf`, runs `nginx -t`, and reloads only after validation. It never replaces existing certificates or sites. Automatic ACME issuance and installing a competing proxy are intentionally not supported; obtain the certificate using your existing TLS management process. For Caddy/Apache or an already completed installation, add a dedicated HTTPS vhost manually forwarding to `127.0.0.1:8095`, preserve Host, set `X-Forwarded-For` to the client address (overwrite it, do not append), set 64 KiB body limit and 120-second upstream timeout. Without the forwarded address every visitor shares one login rate-limit bucket. If you installed an earlier release with `--domain`, add `proxy_set_header X-Forwarded-For $remote_addr;` to `/etc/nginx/conf.d/3xui-reseller.conf` yourself and reload Nginx; the installer will not overwrite an existing site file. Do not expose the internal port or change firewall rules for it.

## First use

1. Log in and change the initial password.
2. Open **Inbounds → Refresh from master**. Check names, IDs, protocol, ports and availability.
3. Create a reseller with unique username/tag, password, GB grant, expiration and explicitly permitted inbounds.
4. Sign in as that reseller and change its password.
5. Use a **dedicated test inbound** to create a small customer. Verify native group, byte allowance and expiry on the master, then test its returned subscription/protocol URL with an actual client. Do not use production customers for destructive smoke tests.
6. Delete the test customer through Relay and confirm its sold quota remains charged. Review the operation and ledger.

## Status, logs, API and password recovery

```bash
sudo bash /opt/3xui-reseller/status.sh
sudo systemctl status xui-reseller.service --no-pager
sudo journalctl -u xui-reseller.service -n 100 --no-pager
curl --fail http://127.0.0.1:8095/healthz
cd /opt/3xui-reseller
sudo -u xui-reseller .venv/bin/python -m reseller.manage check --env /etc/3xui-reseller/app.env
sudo -u xui-reseller .venv/bin/python -m reseller.manage sync --env /etc/3xui-reseller/app.env
sudo -u xui-reseller .venv/bin/python -m reseller.manage reset-password --env /etc/3xui-reseller/app.env
```

`healthz` checks the application database; `check` separately authenticates and reads master inbounds. Recovery prompts for the username/password, invalidates sessions, and requires password change. Rotate the master token in its own panel, update the protected file, and restart only Relay. Never paste tokens into shell history.

## Backups and restore

```bash
sudo bash /opt/3xui-reseller/backup.sh
sudo ls -lh /var/backups/3xui-reseller/
sudo bash /opt/3xui-reseller/restore.sh --archive /var/backups/3xui-reseller/REPLACE_WITH_BACKUP.tar.gz
```

Backups use SQLite's consistent online backup API. Keep encrypted off-host copies; root-readable archives contain recovery secrets. Suggested retention is seven daily and four weekly backups; retention deletion is operator-managed, never automatic. Restore requires typing `RESTORE`, validates checksums/schema, creates a pre-restore backup, revokes sessions and locks sales. The independent master is not restored. Reconcile post-backup sales and correct grant accounting before explicitly unlocking:

```bash
cd /opt/3xui-reseller
sudo -u xui-reseller .venv/bin/python -m reseller.manage unlock-restored --env /etc/3xui-reseller/app.env
```

## Installing behind a proxy

Some servers (for example in Iran) cannot reach `pypi.org` or GitHub directly. You see errors like *"Failed to establish a new connection: Temporary failure in name resolution"* for `files.pythonhosted.org`. A failed update rolls back by itself, so Relay keeps running the previous version.

From version 1.1.2, releases include the Python packages Relay needs, so **updates** normally work without reaching pypi.org. A fresh install, or an update that needs a new compiled package, still downloads from the internet.

To download through a proxy, use a 3X-UI `mixed` (or `http`) inbound that listens on `127.0.0.1` and is routed through a server abroad. The steps are in [TELEGRAM.md](TELEGRAM.md#way-a-through-your-foreign-server-with-3x-ui-recommended-if-you-have-one). Then:

```bash
bash <(curl -Ls -x http://127.0.0.1:10808 https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh) --proxy http://127.0.0.1:10808
```

`--proxy` is used only for downloading (curl and pip). Relay's own checks on `127.0.0.1` never go through it. Use `http://`, not `socks5://`, because pip cannot use SOCKS proxies.

## Updates and rollback

Run the online installer again; on an installed system it updates to the latest release (or the one given with `--version`):

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh)
```

Or update from a release archive you downloaded or built with `python tools/package_release.py`:

```bash
sudo bash /opt/3xui-reseller/update.sh --archive /root/relay-panel-vX.Y.Z.tar.gz --sha256 SHA256_FROM_THE_RELEASE_PAGE
```

The updater validates archive traversal and checksum, backs up DB/config, stops only Relay, installs code/dependencies, migrates and health-checks. On failure it restores prior code and DB/config automatically and restarts Relay. A restored DB is sales-locked pending operator review. Keep the previous release archive and pre-update backup for manual recovery: reinstall the old release into the dedicated code directory, restore its compatible backup, check health/reconcile, then unlock. Never run an older application against a newer schema.

## Uninstall

```bash
sudo bash /opt/3xui-reseller/uninstall.sh
```

Removes only Relay code and service. Configuration, database, backups, existing proxies and all shared packages remain. For intentional permanent removal:

```bash
sudo bash /opt/3xui-reseller/uninstall.sh --purge-data
```

This lists the exact paths and requires typing `PURGE 3XUI-RESELLER`. Save needed backups off-host first. If code was already uninstalled, use the retained source's `uninstall.sh`. Remove any obsolete **Relay-specific** proxy configuration manually after review; the script does not remove proxy files or certificates.

## Troubleshooting and isolation verification

Port conflict: choose a free `--port`; never stop the occupying service automatically. API 401/403: verify admin token scope or credentials. Wrong path/HTML: include the actual master web base path. TLS error: correct certificate/trust; TLS verification cannot be disabled. Pending operation or changed customer: open **Needs attention**. Each item explains the problem and offers the safe actions (Check again, Send again, Mark as done, Accept 3X-UI values, Remove customer). A sent change whose client stays missing from 3X-UI for 3 syncs is cancelled automatically and its held data returned.

Compare original panel service status/start time, unit-file hash and protected configuration hashes before/after installation. Do not expect a live panel database file hash to remain constant while traffic is being recorded.

```bash
sudo systemctl show x-ui.service --property=ActiveEnterTimestamp --property=FragmentPath
sudo systemctl status x-ui.service --no-pager
sudo systemctl status xui-reseller.service --no-pager
sudo stat /var/lib/3xui-reseller/app.db
```

All commands in this guide are for you to run on the VPS. Local application tests and mocked lifecycle tests are documented separately; **none of these VPS installation commands has been executed on your server**.
