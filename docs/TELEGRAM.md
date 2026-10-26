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
| Proxy | Only if Telegram is blocked on your server, e.g. `socks5://127.0.0.1:1080` or `http://127.0.0.1:8080`. You can point it at a local Xray/3X-UI SOCKS inbound |
| Daily report hour | Hour (server time, 0–23) for the daily report |

Press **Save**. The status shows **Connected as @your_bot** when the token works. If it shows an error, check the token and, on servers where Telegram is blocked, the proxy.

## 3. Connect your Telegram account (admin and each reseller)

1. Open **Account → Telegram** and press **Connect Telegram**.
2. Press the **Open in Telegram** link (or send the shown `/start CODE` to the bot). The code works for 15 minutes, once.
3. The bot answers *Connected to Relay as …*. Back on the Account page, tick the messages you want and save.

**Send test message** in Settings checks that delivery works. To stop messages, press **Disconnect** on the Account page or block the bot.

## What the admin gets

- Customers changed directly in 3X-UI, with **Accept 3X-UI values** and **Remove** buttons
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

## Troubleshooting

- **Status shows "Unauthorized"**: the token is wrong or was revoked in BotFather. Paste a new one.
- **Status shows a connection error**: Telegram is blocked from the server. Set a proxy.
- **No messages**: check the ticks on your Account page, and that you didn't block the bot. Messages that can't be delivered are retried a few times, then dropped.
- Logs: `sudo journalctl -u xui-reseller -n 100 | grep -i telegram`
