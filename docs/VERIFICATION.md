# Verification and remaining limits

This is a local implementation, not a verified production deployment. No live panel credentials or VPS were supplied. No master installation/service/database was changed.

## Evidence

- Read official v3.8.5 source, controllers, database models, central-client creation/update, protocol defaulting, native groups, first-use expiry, global traffic overlay, link generation and partial-node behavior.
- Automated tests exercise authentication/session isolation, CSRF, rate limiting, bilingual rendering, QR routes, accounting A–J, concurrency, timeout reconciliation, partial fanout, global traffic aggregation and all nine client-capable protocol request shapes.
- Lifecycle tests use temporary application-owned paths and mocked system commands. SQLite backup/restore integrity and archive validation execute against actual local test databases.
- Bash syntax checks execute with Git for Windows Bash. Linux systemd/apt/proxy effects are mocked, not run on a Linux VM or real VPS.
- Browser inspection (Playwright, 1366px and 390px) covered every administrator and reseller page, light and dark themes, inline form errors and mobile card tables. Screenshots in `docs/screenshots` contain fixture data only. The embedded browser sends `Origin:null`; only the disposable preview harness normalizes it. The real application continues rejecting it and automated tests exercise origin rejection.

Latest local run (2026-10-13, Windows, Python 3.14.8): **88 tests passed** in 17.35 seconds. One upstream Starlette TestClient/httpx deprecation warning remains. Python compilation, JavaScript syntax, dependency consistency (`pip check`) and `bash -n` for all six lifecycle wrappers passed. ShellCheck was unavailable. Passing mocks is not proof of working VPN connectivity or Linux distro compatibility.

The direct-IP compatibility mode is covered for prefix isolation, static assets, login redirects, CSRF origin enforcement and path-scoped session cookies. It is deliberately explicit (`--public-http`) because it exposes plaintext HTTP on all interfaces; the installer leaves firewall policy to the operator.

Desktop RTL DOM measurements confirmed no horizontal overflow (1425px client/scroll width at a 1440px viewport); the sidebar remained within the viewport. The embedded browser's full-page RTL capture shifted fixed-position content, so the Persian desktop preview uses a viewport capture.

User-supplied VPS installation output subsequently showed Ubuntu 24.04 package setup succeeding but pip failing DNS resolution for `files.pythonhosted.org`. It also exposed Ubuntu's automatic `needrestart` hook restarting unrelated services. The installer now sets `NEEDRESTART_SUSPEND=1` and list-only mode for its apt subprocesses. The updated lifecycle suite passed all **18 tests**, including a regression test verifying the override even when the parent environment requests automatic restarts. This patch has not yet been run on the VPS; the reported installation did not reach application startup.

## Material limitations to resolve during staging

1. Run a fresh installation, rerun, upgrade, failure rollback and uninstall on an isolated supported Linux VM before production. Test the optional Nginx branch with your real proxy topology and certificate permissions. ShellCheck may not be installed on this development host.
2. Confirm deployed panel version/schema and actual link/traffic behavior. Test every protocol/cipher you intend to sell with a real client; fixtures do not emulate Xray, kernel WireGuard/AmneziaWG or TUIC/MTProto daemons.
3. Same-protocol multi-inbound packages are supported. Mixed-protocol packages are deliberately rejected. Cipher/key defaults remain panel-generated. Manual tunnel keys/addresses and MTProto secrets are not exposed for editing. Returned link support depends on the configured upstream link provider.
4. An absent remote client after an ambiguous send cannot be safely retried or refunded without authoritative completion evidence. It stays reserved. A guarded admin repair exists for verified existing identities and incomplete attachments; absent ambiguous operations require supervised recovery. Pending backing nodes remain unresolved until a safe retry reports no pending node and readback matches.
5. A master administrator can race the application's preflight checks because the upstream API has no remote compare-and-swap ownership lock. Avoid simultaneous manual edits to managed clients.
6. Large panels with responses above 16 MiB require a source-verified pagination extension. Lists here paginate locally after server-side ownership filtering; the upstream adapter uses bounded full lists.
7. Expiration is displayed as localized digits and Gregorian dates in the configured timezone. Persian RTL and translated interface labels are included; a Jalali calendar is not implemented.
8. Automatic ACME issuance is not implemented. Existing Nginx plus existing valid TLS files is supported; Apache/Caddy use documented manual vhosts. No public port/firewall changes occur automatically.
9. Backup restore activates a persistent sales lock because the master is independent. Reconcile historical sales before using the explicit CLI unlock. Audit records are append-only against application writes, not against root database administrators.

Do not label this deployment production-ready until the live staging acceptance checks above pass.
