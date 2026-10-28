# Changelog

All notable changes to Relay are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and Relay uses
[Semantic Versioning](https://semver.org/): see [docs/RELEASING.md](docs/RELEASING.md).

## [Unreleased]

## [1.2.0] - 2026-10-28

### Added
- **3X-UI wins** (new default, Settings → Changes made in 3X-UI). When you
  change a customer directly in 3X-UI, Relay copies the new values right away
  instead of pausing the customer. It is free for the reseller: extra data is
  a gift and is never refunded into their plan, even if they shrink the
  customer later. You see what changed on the customer page and in Telegram,
  with an **Undo** button for 7 days. The reseller gets a message too, with
  the new link when it changed. **Ask me first** keeps the old behaviour.
- **Unlock sales** button on Needs attention after a backup restore, a banner
  on every page while sales are paused, and a Telegram alert.

### Fixed
- An update that failed and rolled back by itself paused all sales ("Sales
  are paused after a backup restore") and only a command-line tool could
  unlock them. The rollback now restores a copy taken with Relay stopped, so
  nothing can be missing and sales stay open. The same applies when a
  restore fails and rolls back.

## [1.1.2] - 2026-10-28

### Fixed
- Updating failed on servers that cannot reach pypi.org (for example in Iran)
  with "Temporary failure in name resolution" for `files.pythonhosted.org`.
  Releases now include the Python packages Relay needs, and updates install
  them without internet when nothing compiled has changed.
- The health check after an update no longer goes through a download proxy
  set in the environment.

### Added
- `--proxy http://HOST:PORT` option for the online installer, to download
  through a 3X-UI `mixed`/`http` inbound. A clear hint is shown when packages
  cannot be downloaded.

## [1.1.1] - 2026-10-27

### Added
- **Telegram API address** setting for servers where Telegram is blocked:
  point the bot at your own relay, such as a Cloudflare Worker. The Worker
  code is in `deploy/telegram-worker.js`.
- Guide for servers in Iran and other blocked countries in
  `docs/TELEGRAM.md`: a private proxy inbound in 3X-UI routed through your
  foreign server, or a Cloudflare Worker, with test commands for each.

## [1.1.0] - 2026-10-26

Update in place with the online installer; the database is upgraded to schema 5
automatically. The bot is off until you turn it on in **Settings**.

### Added
- **Telegram bot.** Admins and resellers connect their own Telegram account
  from **Account → Connect Telegram** and choose which messages they get.
  - Admin alerts with buttons: customers changed in 3X-UI (**Accept 3X-UI
    values** / **Remove**), stuck or failed changes (**Check again**), requests
    for more data (**Approve** / **Decline**), 3X-UI unreachable and back,
    reseller plans at 90% or ending, customers and inbounds deleted in 3X-UI,
    bulk changes finished, optional sales and sign-in alerts, daily report.
  - Reseller alerts: plan almost used up, ending, renewed; answers to requests;
    customers deleted in 3X-UI; daily summary of customers expiring or out of data.
  - Commands: `/status`, `/attention`, `/requests`, `/find`, `/customers`,
    `/resellers`, `/request`, `/report`, `/sync`, `/help`. `/find` shows each
    customer with **Turn on/off**, **Links** and **Remove** (asks first) buttons.
  - Messages that fail to send are retried; repeated alerts are sent only once.
  - Optional proxy (`socks5://`, `http://`) for servers where Telegram is blocked.
- **Requests for more data.** Resellers press **Request more data** on their
  dashboard (or `/request 50 30` in the bot). The admin sees it under **Needs
  attention** and in Telegram, can change the amount and days, and approves or
  declines with an optional answer. Approving adds to the current plan, or
  starts a new one when the old plan has ended.

## [1.0.0] - 2026-10-26

First stable release. Update an existing 0.1.0 install in place with the online
installer or `update.sh`; data and settings are kept and the database is upgraded
to schema 3 automatically.

### Added
- Works with every 3X-UI version from 2.x to the newest 3.x instead of only
  3.8.5. Relay detects the panel's API: the client API on 3.1 and newer, the
  classic inbound API on 2.x and 3.0 (one inbound per customer there). Tested
  against real v2.9.4, v3.0.2, v3.1.0 and v3.11.0 panels. The dashboard and
  `status.sh` show the detected version and mode.
- The admin can create customers directly. They have no plan limit, can use
  every inbound, and appear in 3X-UI with the group `RelayPanel`.
- Change many customers at once: select customers on the list (or take
  every customer matching the filters) and add, remove or move inbounds, or
  turn them on or off. A preview shows who changes and who is skipped and
  why; the changes then run in the background with a progress page. The
  admin's Inbounds page has "Add to customers" / "Remove from customers"
  shortcuts. Available to the admin and to resellers (their own customers
  and permitted inbounds only).
- Settings page for the admin: set the subscription address (for example
  `https://sub.example.com/i/`, or with `{subId}` inside). Without it, no
  subscription link is shown.
- Connection links are titled "inbound name · customer name" (with the
  server address underneath) instead of the raw encoded remark, and the
  subscription link is always shown first.
- One-line online installer and updater (`install-online.sh`) that downloads a
  published GitHub release and checks its SHA-256.
- Version number shown in the panel, `/healthz` and `status.sh`; GitHub Actions
  build and publish releases from version tags.
- **Needs attention** page for the admin: plain explanations and safe actions
  (Check again, Send again, Mark as done, Accept 3X-UI values, Remove customer)
  with a field-by-field comparison of changes made directly in 3X-UI.
- Delete a reseller together with all of its customers.
- **Activity** page with readable sentences, filters and per-reseller view.
- **Sync now** button and a sync status indicator for admins and resellers.
- `XUI_SUB_URL` setting for the public subscription link.

### Changed
- New interface in the style of 3X-UI: dark and light themes, mobile layout,
  usage bars, filter chips and quick-pick buttons.
- English only; the Persian interface and language switch were removed.
- Errors are shown on the same page, next to the field that caused them, and
  forms keep what was typed. Inline "click again to confirm" replaces pop-ups.
- Sync with 3X-UI every 30 seconds (was 120), quick re-checks every 10 seconds
  for customers being set up, and back-off while 3X-UI is unreachable.
- The installer accepts the 3X-UI address as you open it in the browser. When
  3X-UI runs on the same VPS over plain HTTP, Relay automatically connects to it
  on `127.0.0.1` and keeps the public host for customer links.

### Fixed
- Inbounds deleted in 3X-UI were never removed from Relay.
- Customers showed as unavailable after one of their inbounds was deleted in 3X-UI,
  and then could not be edited or deleted.
- A customer deleted in 3X-UI while still being set up stayed in Relay forever.
- Changes waiting on a 3X-UI node could stay pending forever.
- Deleted customers blocked their name, subscription ID and UUID from reuse.
- Filter, paging and tab links broke when Relay runs under a web path.
- The background sync and a manual sync could run at the same time.

### Security
- Logout now ends the server session when a web path is configured.
- Login rate limits apply per visitor behind the Nginx proxy, and a few wrong
  passwords can no longer lock the admin out for everyone.
- Public HTTP mode keeps all production configuration checks.
- Anonymous requests can no longer grow the database without limit.
- Removed a hard-coded third-party subscription domain.

## [0.1.0] - 2026-10-14

Initial public version.

[Unreleased]: https://github.com/mohammad1453/Relay-Panel/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/mohammad1453/Relay-Panel/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/mohammad1453/Relay-Panel/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/mohammad1453/Relay-Panel/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/mohammad1453/Relay-Panel/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/mohammad1453/Relay-Panel/releases/tag/v1.0.0
[0.1.0]: https://github.com/mohammad1453/Relay-Panel/commit/3ce308d
