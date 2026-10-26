# Upgrading an existing Relay install

You do **not** need to uninstall or reinstall. The updater keeps your database, settings, resellers and customers. It upgrades the database by itself, and rolls back automatically if anything fails.

Run the `sudo` commands on the VPS. Run the other commands on your own computer.

## 1. Make a backup and copy it to your computer

On the VPS:

```bash
sudo bash /opt/3xui-reseller/backup.sh
sudo ls -lh /var/backups/3xui-reseller/
```

On your computer, copy the newest file (replace `VPS_IP` and the file name):

```bash
scp root@VPS_IP:/var/backups/3xui-reseller/relay-20261025T120000000000Z.tar.gz .
```

Keep this file safe. It contains your Relay database and settings, including the 3X-UI login.

## 2. Update

On the VPS, run the online installer. Relay is already installed, so it updates instead of installing:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/mohammad1453/Relay-Panel/main/install-online.sh)
```

It downloads the latest release, checks its SHA-256, makes another backup, stops only Relay, installs the new code, upgrades the database (to schema 5) and checks that Relay starts. 3X-UI is never stopped or changed. If anything fails, the previous version comes back automatically.

<details><summary>Without the online installer</summary>

Download `relay-panel-vX.Y.Z.tar.gz` and its `.sha256` from the [releases page](https://github.com/mohammad1453/Relay-Panel/releases) to `/root/`, then:

```bash
cd /root && sha256sum -c relay-panel-vX.Y.Z.tar.gz.sha256
sudo bash /opt/3xui-reseller/update.sh --archive /root/relay-panel-vX.Y.Z.tar.gz --sha256 "$(cut -d' ' -f1 relay-panel-vX.Y.Z.tar.gz.sha256)"
```

</details>

## 3. Settings to add

The easiest way to set the subscription address is in the panel: sign in as admin, open **Settings**, enter it (for example `https://sub.example.com/i/`) and save. The file setting below does the same thing.

Edit `/etc/3xui-reseller/app.env` and add or change:

```bash
# Your public subscription address; the customer's subscription ID is added after it.
XUI_SUB_URL="https://sub.example.com/i/"
# Sync with 3X-UI every 30 seconds (the new default).
SYNC_SECONDS="30"
```

Then restart Relay:

```bash
sudo systemctl restart xui-reseller.service
```

If you set up Nginx with `--domain`, also add `proxy_set_header X-Forwarded-For $remote_addr;` inside the `location /` block of `/etc/nginx/conf.d/3xui-reseller.conf`, then run `sudo nginx -t && sudo systemctl reload nginx`.

## 4. What happens to existing customers

- Within about 30 seconds of the first sync, inbounds you deleted in 3X-UI are removed from Relay. Customers that were only "unavailable" because of a deleted inbound go back to normal by themselves.
- Customers that were edited directly in 3X-UI show up under **Needs attention** with a table of what changed. Press **Accept 3X-UI values** to keep what 3X-UI has, or **Remove customer**.
- Customers stuck in "setting up" whose client no longer exists in 3X-UI are removed after about 1–2 minutes, and their data goes back to the reseller's plan.
- To delete a test reseller, open it, go to **Danger zone** and press **Delete reseller and customers** (press twice to confirm). This removes its customers from 3X-UI too.

## Telegram bot (1.1.0)

To get alerts in Telegram, follow [TELEGRAM.md](TELEGRAM.md). Nothing changes until you turn the bot on in **Settings**.

## If something goes wrong

The updater restores the previous version by itself when the health check fails. To go back to your step 1 backup manually:

```bash
scp relay-20261025T120000000000Z.tar.gz root@VPS_IP:/root/
sudo bash /opt/3xui-reseller/restore.sh --archive /root/relay-20261025T120000000000Z.tar.gz
```

After a restore, sales are locked until you check them; see "Backup and restore" in [INSTALL.en.md](INSTALL.en.md).
