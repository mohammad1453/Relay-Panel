# Telegram bot

Relay can send alerts to Telegram and let you act on them with buttons: approve a reseller's request, accept a change made in 3X-UI, turn a customer on or off, and more. Every admin and reseller connects their own Telegram account and chooses what they want to hear about.

The bot is off until you turn it on. Everything it does can also be done in the panel.

## 1. Create the bot (admin, once)

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a name (for example `My Relay`) and a username ending in `bot` (for example `my_relay_bot`).
3. BotFather replies with a **token** like `1234567890:AAH...`. Keep it secret: anyone with the token controls the bot.

## 2. Turn it on in Relay

Sign in as admin, open **Settings → Telegram bot**:

| Field | What to enter |
|---|---|
| Enabled | On |
| Bot token | The token from BotFather. Leave empty later to keep the saved one |
| Proxy | Only if Telegram is blocked on your server, e.g. `socks5://127.0.0.1:10808`. See "Server in Iran" below |
| Telegram API address | Only when you use a Cloudflare Worker (see below). Leave empty otherwise |
| Daily report hour | Hour (server time, 0–23) for the daily report |

Press **Save**. The status shows **Connected as @your_bot** when the token works. If it shows an error, check the token and, on servers where Telegram is blocked, the proxy.

## 3. Connect your Telegram account (admin and each reseller)

1. Open **Account → Telegram** and press **Connect Telegram**.
2. Press the **Open in Telegram** link (or send the shown `/start CODE` to the bot). The code works for 15 minutes, once.
3. The bot answers *Connected to Relay as …*. Back on the Account page, tick the messages you want and save.

**Send test message** in Settings checks that delivery works. To stop messages, press **Disconnect** on the Account page or block the bot.

## What the admin gets

- Customers changed directly in 3X-UI: what changed, with an **Undo** button (or, in **Ask me first** mode, **Accept 3X-UI values** and **Remove**)
- Sales paused after a backup restore
- Changes that are stuck or failed, with **Check again**
- Requests for more data from resellers, with **Approve** and **Decline**
- 3X-UI unreachable for 5 minutes, and back again
- Reseller plans at 90% or ending within 3 days
- Customers or inbounds deleted in 3X-UI
- Bulk changes finished
- Optional: every sale and every admin sign-in
- Daily report

## What resellers get

- Plan almost used up, ending soon, ended, renewed
- Answers to their requests for more data
- Their customers changed by the admin in 3X-UI, with the new link when it changed
- Their customers deleted in 3X-UI
- Bulk changes finished
- Daily summary: customers expiring soon or out of data

## Commands

| Command | Who | What it does |
|---|---|---|
| `/status` | all | Plan and customer summary (admin: whole panel and 3X-UI connection) |
| `/find NAME` | all | Find customers; each comes with **On/Off**, **Links** and **Remove** buttons |
| `/customers` | reseller | Customers that are expiring, out of data or disabled |
| `/request GB [days] [note]` | reseller | Ask the admin for more data, e.g. `/request 100 30 new customers` |
| `/requests` | all | Pending requests (admin: with buttons) |
| `/attention` | admin | Everything under **Needs attention** |
| `/resellers` | admin | Resellers with plan usage |
| `/sync` | admin | Sync with 3X-UI now |
| `/report` | all | The daily report, now |
| `/help` | all | List of commands |

**Remove** always asks for a second press. Resellers can only see and change their own customers. The bot only answers private chats from connected accounts.

## Server in Iran or another blocked country

If Telegram is blocked on your server, the status in Settings shows a connection error. There are two ways around it. You only need one.

### Way A: through your foreign server, with 3X-UI (recommended if you have one)

The idea: you add a small proxy to 3X-UI that only this server can use, and tell Xray to send that proxy's traffic through your existing tunnel to the foreign server. Relay then talks to Telegram through that proxy.

```text
Relay ──► 127.0.0.1:10808 (proxy inbound) ──► Xray routing rule ──► tunnel outbound ──► foreign server ──► Telegram
```

**1. Find the tag of your tunnel.** In 3X-UI open **Xray Configs → Outbounds**. Besides `direct` and `blocked` you should see the tunnel to your foreign server. Write down its **tag** (for example `hetzner`). If you don't see one there, your tunnel works outside Xray; see "If your tunnel is not an Xray outbound" below.

**2. Add the proxy inbound.** Open **Inbounds → Add Inbound** and set:

| Field | Value |
|---|---|
| Remark | `relay-telegram` |
| Protocol | `mixed` (SOCKS and HTTP in one) |
| Listen IP | `127.0.0.1`. This keeps it private to the server. Never leave it empty here |
| Port | `10808` (any free port) |
| Password authentication | Off (only this server can reach it) |
| UDP | Off |

Save it. In the inbound list, open its details and note its **tag**. With a listen IP it is usually `inbound-127.0.0.1:10808`. Relay ignores this inbound; it is never offered to resellers.

**3. Add the routing rule.** Open **Xray Configs → Routing Rules → Add Rule** and set:

| Field | Value |
|---|---|
| Inbound Tags | the tag from step 2, e.g. `inbound-127.0.0.1:10808` |
| Outbound Tag | the tunnel tag from step 1, e.g. `hetzner` |

Leave everything else empty. Rules are checked from top to bottom and the first match wins, so drag this rule **near the top**, below the `api` rule. Press **Save**, then **Restart Xray**.

**4. Test on the server:**

```bash
# Without the proxy: should fail or hang (this shows Telegram is blocked)
curl -m 10 -sS -o /dev/null -w '%{http_code}\n' https://api.telegram.org
# Through the proxy: 302 or 200 means it works
curl -m 15 -sS -o /dev/null -w '%{http_code}\n' -x socks5h://127.0.0.1:10808 https://api.telegram.org
# Which country does the proxy come out in? It should be your foreign server's
curl -m 15 -sS -x socks5h://127.0.0.1:10808 https://ipinfo.io/country
```

`000` means no connection: check the rule order, the tags and that Xray was restarted.

**5. Use it in Relay.** In **Settings → Telegram bot** set **Proxy** to `socks5://127.0.0.1:10808`, leave **Telegram API address** empty, and save. The status should become **Connected as @your_bot**.

**If your tunnel is not an Xray outbound** (for example a port forward or a tunnel tool like Rathole/Backhaul/GRE): the easiest way is to add the foreign server as an outbound. Take one of your working config links that goes to the foreign server and add it under **Xray Configs → Outbounds → Add Outbound**. Newer 3X-UI versions let you paste the link; otherwise copy the address, port, ID and transport settings from it. Give it the tag `foreign`, then continue with step 2 using `foreign` as the outbound tag.

### Way B: a Cloudflare Worker (no foreign server needed)

A Worker is a tiny program that runs on Cloudflare. Relay sends its Telegram calls to your Worker, and the Worker passes them on to Telegram. This works as long as your server can reach Cloudflare.

**1. Create the Worker** (free account at [dash.cloudflare.com](https://dash.cloudflare.com); do this from a computer that can open Cloudflare):
1. **Workers & Pages → Create → Create Worker**, give it a name (e.g. `relay-tg`) and press **Deploy**.
2. Press **Edit code**, replace everything with the code from [`deploy/telegram-worker.js`](../deploy/telegram-worker.js), and change `BOT_ID` to the number before `:` in your bot token. This makes sure only your bot can use the Worker.
3. Press **Deploy**. Your Worker's address looks like `https://relay-tg.YOUR-NAME.workers.dev`.

**2. Test it from your computer.** Open `https://relay-tg.YOUR-NAME.workers.dev/botYOUR_TOKEN/getMe` in a browser. You should see `{"ok":true,...}` with your bot's name.

**3. Test it from the Iran server.** First without the token:

```bash
curl -m 15 -sS -o /dev/null -w '%{http_code}\n' https://relay-tg.YOUR-NAME.workers.dev/
```

- `404`: the server reaches the Worker (404 is the Worker's own answer). Good.
- `000` or a timeout: `workers.dev` is blocked from this server. Go to step 4.

Then the full test, typing the token so it doesn't end up in your shell history:

```bash
read -rsp 'Bot token: ' T; echo; curl -m 15 -sS "https://relay-tg.YOUR-NAME.workers.dev/bot$T/getMe"; echo; unset T
```

`{"ok":true,...}` means everything works.

**4. If `workers.dev` is blocked: use your own domain.** If your domain is on Cloudflare (for example the one you use for subscriptions), open the Worker → **Settings → Domains & Routes → Add → Custom domain** and enter a new subdomain such as `tg.example.com`. Wait a minute, then repeat step 3 with `https://tg.example.com`. Your own domain is much less likely to be blocked.

**5. Use it in Relay.** In **Settings → Telegram bot** set **Telegram API address** to your Worker address (e.g. `https://tg.example.com`), leave **Proxy** empty, and save.

Notes: your bot token passes through your own Cloudflare account. Relay makes about 3,500 calls a day, well within the free plan's 100,000.

## Troubleshooting

- **Status shows "Unauthorized"**: the token is wrong or was revoked in BotFather. Paste a new one.
- **Status shows a connection error**: Telegram is blocked from the server. See "Server in Iran or another blocked country" above.
- **No messages**: check the ticks on your Account page, and that you didn't block the bot. Messages that can't be delivered are retried a few times, then dropped.
- Logs: `sudo journalctl -u xui-reseller -n 100 | grep -i telegram`
