# Acceptance record

Local development date: 2026-10-13. No actual VPS or deployed master panel was available.

| Requirement area | Implementation / evidence |
|---|---|
| Initial inspection | Empty workspace; official v3.8.5 source cloned separately and inspected. See API contract. |
| Independent accounts | Argon2id, server sessions, rotation, idle/absolute expiry, CSRF/origin checks, persistent rate limits, initial-password change. |
| Role and tenant isolation | Backend ownership/role checks for list/detail/link/QR/edit/delete; cross-account and admin-route denial tests. |
| Reseller management | Create/edit username, suspend, reset password, immutable native group tag, notes, inbound permissions, grant renewal/adjustment and history. |
| Customer management | Protocol-aware create/edit, enable/disable through edit, usage-capped quota refunds after verified reductions, nonrefunding deletion, missing-master soft deletion with audit, generated links and QR, search/filter/sort/pagination. |
| Protocols | VMess, VLESS, Trojan, Shadowsocks, Hysteria, WireGuard, AmneziaWG, MTProto, TUIC; source-derived mock tests. Real protocol connectivity remains unverified. |
| Accounting A–J | Integer-byte cumulative sales, once per multi-inbound package, atomic reservations, concurrent no-oversell, positive-difference charging, unused-traffic refunds capped by measured use and grant-attributed sales, immutable ledger, separate renewals. |
| Partial/ambiguous effects | Per-placement readback, persistent reservation, safe reconciliation and guarded admin repair; absent ambiguous creation stays reserved. |
| Retry before send | Proven-unsent creation can be retried with a fresh reservation and idempotency key. No blind retry after transmission. |
| Expiration | Reseller read-only state, preserved customers, independent customer expiry, source-verified negative first-use duration and activation preservation. |
| Telemetry | Global aggregate used once; stale/unavailable state; no invented speed. |
| Bilingual UI | Persian RTL / English LTR; templates and backend errors translated, localized digits and timezone; browser desktop/mobile inspection. Gregorian calendar. |
| Deployment | Real installer, dedicated user/service/paths, loopback binding, private config, preflight-only mode, ownership/path checks and idempotent completed installation. |
| Lifecycle | Consistent online backup, archive validation, restore lock/session invalidation, verified local update archive, automatic rollback, nonpurging uninstall and deliberate purge. |
| Original-panel safety | No original service mutations/database writes; temporary sentinel and command-target tests. Actual VPS unchanged because it was never accessed. |
| Proxy | Optional separate existing-Nginx configuration with existing certificate, full validation and failure removal. No automatic ACME or competing proxy installation. |
| Handover | English and Persian installation/access/status/update/backup/restore/uninstall/recovery guides; source release archive and SHA-256. |

**Acceptance still outstanding:** real isolated Linux VM installation lifecycle, live version/schema connection, test-inbound write/readback and real client connectivity for every sold protocol. The project must not be represented as production-validated before these pass. See VERIFICATION.md for material operational limits.
