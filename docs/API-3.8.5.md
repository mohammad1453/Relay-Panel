# Verified 3X-UI v3.8.5 contract

Inspected official tag `v3.8.5`, annotated tag object `648d0fc71511a679f241179cbe06014809c5b0d6`, checked-out commit `7ef22f94c950ff09f0870e2295fa65ad5968742c`. The reference checkout is under `.reference/3x-ui` and excluded from distribution. No source, database or service of the user's installed panel was modified.

Primary source: [official v3.8.5 tree](https://github.com/MHSanaei/3x-ui/tree/v3.8.5). Paths below are relative to that tag.

## Authentication and envelopes

`internal/web/controller/api.go`: base-path-relative `/panel/api`, accepts `Authorization: Bearer` admin tokens or panel login sessions. Monitor and node-sync scopes do not permit the complete reseller workflow. The application's own users use sessions exclusively; master tokens never reach the browser.

`internal/web/controller/index.go` and `internal/web/session/csrf.go`: session login requires GET `/csrf-token`, then POST `/login` with form `username`, `password` and `X-CSRF-Token`. Fetch a fresh token after login. Master sessions are held only in the backend. Automated login does not support a changing 2FA code; use a master admin token for a 2FA-enabled panel.

Envelope: `{success: boolean, msg: string, obj: ...}`. HTTP 200 is not proof of success. HTTP or application errors after submission can mean partial application. The adapter never blindly retries mutations, never forwards remote `msg` to users, and bounds decoded responses to 16 MiB. Larger installations must introduce verified paging before raising this bound.

## Endpoints used

| Method | Path after `/panel/api/` | Body / result |
|---|---|---|
| GET | `server/status` | `panelVersion` must be `3.8.5` (optional leading `v`); adapter refuses other versions before any client operation |
| GET | `inbounds/list` | Inbound array; settings may be an object or serialized JSON |
| GET | `clients/list` | Central client records with `id`, `uuid`, `inboundIds`, optional global `traffic` |
| POST | `clients/add` | `{client: {...}, inboundIds: [1,2]}` |
| POST | `clients/update/:email` | Client object directly; not an inbound update |
| POST | `clients/del/:email` | Removes the central identity across its attachments |
| POST | `clients/:email/attach` | `{inboundIds:[...]}`; only used for explicit admin repair of an existing verified managed identity |
| POST | `clients/onlines` | Array of online identifiers; no fabricated speed |
| GET | `clients/links/:email` | Generated protocol link strings |
| GET | `clients/subLinks/:subId` | Actual subscription URL strings |

Sources: [`internal/web/controller/client.go`](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/client.go), `internal/web/service/client.go`, `client_crud.go`, `inbound_sublink.go`.

The old inbound `addClient` examples are deliberately not used. Links come from the panel's registered link provider. When connecting via loopback, set `XUI_LINK_HOST` to the intended trusted public hostname if the installation relies on request-host-based links. This sends a Host header only on the two link requests, never fetches URLs returned by the panel. Prefer the panel's explicit share address configuration. Validate a generated link with a real client before selling packages.

## Clients, groups and protocols

`internal/database/model/model.go`: `Client` uses `id` for a protocol UUID, `ClientRecord` uses numeric `id` for the stable record key and `uuid` for that UUID. Do not confuse them.

Native `group` is supported in both models; it is always overwritten server-side with the owner's immutable reseller tag. The adapter confirms group, subId, UUID where applicable, record ID, package fields, exact attachment set and per-inbound client configuration. Database ownership is authoritative.

Client-capable types: VMess, VLESS, Trojan, Shadowsocks, Hysteria (version 2 is still protocol `hysteria`), WireGuard, AmneziaWG, MTProto, TUIC. HTTP, mixed, tunnel/tun are not this managed-client model and remain visible but unassignable. Sources: `client_crud.go: fillProtocolDefaults`, `client_inbound_apply.go`, `client_wireguard.go`, `client_amneziawg.go`.

UUID applies to VMess/VLESS/TUIC; password to Trojan/Shadowsocks/TUIC; `auth` to Hysteria; `secret` to MTProto; tunnel keypair/address fields to WireGuard/AmneziaWG. The panel generates cipher-compatible Shadowsocks passwords, tunnel keys/addresses and MTProto secrets. No UUID is fabricated for non-UUID protocols. UUID, subscription ID and applicable password/auth may be edited; blank values preserve credentials. Tunnel/MTProto credential editing is intentionally not exposed. Same-protocol multiple attachments are supported; mixed-protocol packages are rejected explicitly. The panel preserves per-inbound tunnel peers on multi-tunnel updates when no `allowedIPsByInbound` override is supplied.

## Traffic, expiry and partial application

`totalGB` is a misleading field name: its wire value is **bytes**. UI GB means GiB (`1024^3` bytes), stated visibly. Zero/unlimited, negative and non-finite allowances are forbidden. All quota money-like amounts use integer bytes and decimal parsing.

`internal/web/service/inbound_traffic.go`: per-email statistics are overlaid with global traffic; `GetClientTrafficByEmail` returns a single aggregate. `ClientWithAttachments` exposes that aggregate under `traffic`. **Never sum per-inbound clientStats again.** Usage is `up+down`; remaining is `max(0, total-used)`.

Fixed expiry is epoch milliseconds. First-use expiry is a negative millisecond duration. On the first traffic tick the panel converts it to `now+duration` consistently across attachments. There is no invented boolean API property. Renewal of the reseller does not alter customer expiry. Editing a first-use customer preserves the activated timestamp discovered remotely.

Create/update fanout may commit some inbounds and still return `success:false`. `obj.nodePending=true` means the master saved a change while a backing node is pending. Such operations remain visibly unresolved; a read of master configuration alone is not treated as node acknowledgement. Explicit admin retry revalidates identity and preserves the original reservation. An absent client after a timeout is not sufficient evidence to retry creation or refund a reservation.

The target's deployed schema was not available. Fixtures mirror the inspected source, not a live installation. Before production, confirm your actual tag and read-only API response shape, then run a write test only on a dedicated test inbound.
