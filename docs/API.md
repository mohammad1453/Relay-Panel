# 3X-UI compatibility

Relay works with **every 3X-UI release from 2.x to the newest 3.x** ([MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui)). It does not check for one exact version. When it first connects, it looks at which API the panel offers and picks one of two modes. The admin dashboard and `status.sh` show the detected version and mode.

| 3X-UI version | Mode Relay uses | Differences for you |
|---|---|---|
| 3.1.0 and newer (3.2 … 3.11 and future) | **Client API** (`/panel/api/clients/*`) | None: several inbounds per customer, connection links, node support |
| 2.x and 3.0.x | **Classic API** (`/panel/api/inbounds/*`) | One inbound per customer (these panels reject the same email on two inbounds); single config links are not available, the subscription link still works; WireGuard, AmneziaWG, MTProto and TUIC inbounds cannot be sold. Moving a customer to another inbound resets its traffic counter in 3X-UI. |

Both API tokens and username/password work on 3.x. Classic panels (2.x / 3.0.x) have no API tokens: use the panel username and password.

## Tested against real panels

Before this release, Relay's service code was run against real 3X-UI builds (compiled from the official tags, throwaway databases). The run created inbounds, a reseller and customers. It then edited, moved between inbounds, edited a customer in 3X-UI and accepted the change, and finally deleted and force-removed customers:

| 3X-UI | Detected mode | Result |
|---|---|---|
| v2.9.4 | classic | passed |
| v3.0.2 | classic | passed |
| v3.1.0 | client API | passed |
| v3.11.0 (latest at the time) | client API | passed |

The source of v3.8.5 through v3.11.0 was also compared: the client endpoints Relay uses, their request bodies and the client record fields are unchanged; newer versions only add fields (for example `clientHostRuleIds`, `resetWeekday`), which Relay ignores.

## Future versions

Because Relay chooses by capability, a new 3X-UI release keeps working as long as it keeps these endpoints. If a future release removes or reshapes one, Relay stops with a clear message (*"3X-UI sent an answer Relay does not understand"*) instead of guessing, and nothing is changed in 3X-UI. If that happens, update Relay.

## Endpoints used

All paths are relative to the panel address including its web path.

Session login (when no API token is set): `GET csrf-token` (only on panels that have it), `POST login` with form `username`, `password`.

| Purpose | Client API (3.1+) | Classic API (2.x / 3.0.x) |
|---|---|---|
| Detect | `GET panel/api/clients/list` (and `server/status` for the version) | the same returns 404 → classic |
| Inbounds | `GET panel/api/inbounds/list` | `GET panel/api/inbounds/list` |
| Clients | `GET panel/api/clients/list` | built from `settings.clients` and `clientStats` in the inbound list |
| Create | `POST clients/add` `{client, inboundIds}` | `POST inbounds/addClient` form `id`, `settings` |
| Update | `POST clients/update/:email` | `POST inbounds/updateClient/:clientId` |
| Delete | `POST clients/del/:email` | `POST inbounds/:id/delClient/:clientId`; for an inbound's last client, `POST inbounds/update/:id` without it |
| Add / remove inbounds | `POST clients/:email/attach`, `/detach` | delete from the old inbound, add to the new one |
| Online | `POST clients/onlines` | `POST inbounds/onlines` |
| Links | `GET clients/links/:email` | not available |

`clientId` in classic paths is the UUID for VMess/VLESS, the password for Trojan, the email for Shadowsocks and `auth` for Hysteria.

## Rules Relay keeps on every version

- Responses must be `{success: true, obj: ...}`. HTTP 200 alone is not proof. A `success:false` or a timeout can still mean the change was partly applied, so Relay never blindly retries a change. It reads 3X-UI back and only then counts the sale.
- `totalGB` is in bytes; GB in Relay means GiB (1024³ bytes).
- Fixed expiry is milliseconds since 1970. A negative value is "days after first use", which 3X-UI turns into a date on the first connection.
- `obj.nodePending: true` (3.x with nodes) means the master saved the change but a node has not confirmed it; Relay waits and accepts it after a short settling time.
- 3X-UI's own `msg` text is never shown to resellers.
- Relay always sets the client `group` to the reseller's tag. Versions that do not report `group` (for example 3.1.0) are matched by email, subscription ID and UUID instead.
