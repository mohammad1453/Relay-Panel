# Data, transactions and operational recovery

A single Uvicorn process serves FastAPI/Jinja routes and a background reconciliation loop. SQLite WAL with foreign keys, 30-second busy timeout, `BEGIN IMMEDIATE` transactions and integer byte counters serializes quota decisions across threads/processes. No database lock is held across remote requests. The database has an application ID and refuses unrelated schemas. Migrations are explicit SQL with `user_version` checks; a future schema is refused.

## Sale protocol

1. Re-read actor, ownership, suspension, active grant, inbound permission/freshness, identifiers and positive volume inside a write transaction.
2. Atomically reserve `max(0,new_limit-old_limit)`. Bind the UUID idempotency key to actor, customer, action and request digest. One pending operation per customer; credential claims prevent concurrent local collisions.
3. Read the master before sending: reject existing identities on create; verify stable record, credentials, native group and exact attachment ownership on update/delete. Unsent failures can release reservations safely.
4. Recheck plan/suspension/permissions and compare-and-swap reserved → sent before network I/O. Record desired state durably. There are no automatic mutation retries.
5. Read back central records and each inbound placement. Only complete verified application commits reservation → cumulative sale. A timeout, application error, missing/mismatched attachment or pending node retains the reservation.
6. Background reconciliation performs readbacks. An administrator can request a guarded repair of a verified existing identity. It never claims unrelated clients or recreates an absent ambiguous client.

Delete, disable, and expiry never create refunds. On a verified customer limit reduction, Relay returns the decrease that remains unused: `max(0, old allowance - max(new allowance, measured usage))`. Refunds are capped at the customer's net sales on the active grant and the grant's sold balance. For example, reducing 100 GB to 70 GB with 10 GB used returns 30 GB; reducing to 5 GB with 80 GB used returns 20 GB. An increase is charged by its positive difference. Plan renewals close the previous grant and create a fresh one; outstanding operations retain their original grant attribution. The immutable ledger and audit history survive deletion and renewal.

During synchronization, an active local customer absent from the master client's list is soft-deleted and logged as a system audit event. Relay hides it from the default customer list while retaining its record, operations, and accounting history, and frees its name, subscription ID and credentials for reuse. Missing customers do not trigger quota refunds.

Inbounds deleted in the master are deleted from Relay in the same sync, together with reseller permissions and customer placements that pointed at them; pending operations drop the removed inbound from their desired state. An empty inbound list is only trusted when two syncs in a row return it.

## Remote failures and drift

Operations expose reserved/sent/uncertain/committed/failed statuses plus per-inbound results. Production exceptions expose short translated codes only. A disappeared or changed remote record blocks unsafe writes. The master administrator must avoid editing a managed identity during an application mutation: the upstream API offers no compare-and-swap transaction spanning a remote read and write. The application cannot eliminate that external administrator race; it checks before and after.

A sent create or update whose client is absent from the master on three full syncs at least 20 seconds apart is finished as failed: its reservation is released, the customer is marked deleted and a system event explains it. A request still in flight would normally land within that window; if a late request does create the client afterwards, it appears in 3X-UI as an unmanaged client.

Customers edited directly in the master are marked `remote_changed` (values or inbounds) or `remote_identity` (record, subscription ID, UUID or group). Relay stores a snapshot of the master copy so the admin sees a field-by-field comparison. "Accept 3X-UI values" copies the master copy into Relay without charging or refunding the reseller (a zero-amount ledger note is written). The admin's delete removes the client from the master if present and cancels pending operations, returning held quota. Deleting a reseller deletes all of its customers first, then retires the account (renamed, suspended, permissions and sessions removed) because ledger and audit rows are permanent.

Changes the master accepted with `nodePending` are committed once the master copy matches and at least two sync intervals (minimum 60 seconds) have passed; the admin can also mark an operation as done after checking it.

## Backup and restore

The lifecycle tool uses SQLite's online backup API, not a live file copy. Backups contain only application DB, protected configuration and a checksummed manifest. They never include the master's database. Archives are root-readable. Restore accepts an exact member allowlist, validates hashes, application ID, schema and integrity, stops only the reseller service, revokes sessions and enables a persistent sales lock.

**Restoring an old application backup cannot restore the independent master.** Sales made after the backup may still exist remotely. Compare master clients, invoices and grant history; correct missing accounting conservatively. Only then run `unlock-restored`, which requires a typed acknowledgement and records an audit event. Do not unlock just because the application starts.

## Security boundary

All browser access is session-authenticated. Random session tokens are stored as SHA-256 digests server-side; Argon2id password hashes use 64 MiB, three iterations and two lanes. Login, password change and permission changes invalidate/rotate sessions. Initial/reset passwords require change. Sessions have absolute and idle expiry. CSRF tokens and origin checks cover POSTs, including login. Rate limits persist in SQLite for both account/IP login buckets and authenticated writes. The service ignores client forwarding headers by default; behind Nginx the IP bucket is shared and account buckets still apply. Configure a trusted-proxy strategy only after assessing deployment topology.

Templates autoescape. CSP forbids inline script, third-party resources, embedding and objects. Secrets never enter audit text, logs or client assets. DB and backups contain customer connection credentials because they are needed for management: host and backup security remain critical. Audit is append-only through application database triggers, not tamper-proof against the host administrator.

The installer runs under root but the server uses an unprivileged system user and a restricted filesystem. It installs only needed Python/system dependencies and never stops or restarts the original 3X-UI service. Optional Nginx setup creates one new file, validates the complete configuration and rolls that file back on failure; it does not manage ACME or unrelated sites.
