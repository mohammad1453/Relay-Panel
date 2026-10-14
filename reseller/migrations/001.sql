CREATE TABLE users (
 id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK(role IN ('admin','reseller')), tag TEXT UNIQUE,
 suspended INTEGER NOT NULL DEFAULT 0, must_change INTEGER NOT NULL DEFAULT 1,
 notes TEXT NOT NULL DEFAULT '', created INTEGER NOT NULL, last_login INTEGER
);
CREATE TABLE sessions (
 token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id), csrf TEXT NOT NULL,
 created INTEGER NOT NULL, seen INTEGER NOT NULL, expires INTEGER NOT NULL
);
CREATE INDEX sessions_user ON sessions(user_id);
CREATE TABLE attempts (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, until_time INTEGER NOT NULL);
CREATE TABLE inbounds (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL, port INTEGER NOT NULL,
 available INTEGER NOT NULL, metadata TEXT NOT NULL, synced INTEGER NOT NULL
);
CREATE TABLE permissions (
 user_id INTEGER NOT NULL REFERENCES users(id), inbound_id INTEGER NOT NULL REFERENCES inbounds(id),
 PRIMARY KEY(user_id,inbound_id)
);
CREATE TABLE grants (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), allowance INTEGER NOT NULL CHECK(allowance>0),
 sold INTEGER NOT NULL DEFAULT 0 CHECK(sold>=0), reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0),
 starts INTEGER NOT NULL, expires INTEGER NOT NULL, closed INTEGER NOT NULL DEFAULT 0,
 CHECK(sold+reserved<=allowance), CHECK(expires>starts)
);
CREATE INDEX grants_user ON grants(user_id);
CREATE TABLE customers (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), email TEXT NOT NULL UNIQUE,
 uuid TEXT UNIQUE, sub_id TEXT NOT NULL UNIQUE, remote_id INTEGER UNIQUE, payload TEXT NOT NULL,
 package INTEGER NOT NULL CHECK(package>0), status TEXT NOT NULL, created INTEGER NOT NULL,
 used INTEGER, online INTEGER, telemetry_at INTEGER, remote_expiry INTEGER, issue TEXT NOT NULL DEFAULT ''
);
CREATE INDEX customers_owner ON customers(user_id);
CREATE TABLE identities (
 kind TEXT NOT NULL, value TEXT NOT NULL, customer_id INTEGER NOT NULL REFERENCES customers(id),
 PRIMARY KEY(kind,value)
);
CREATE TABLE placements (
 customer_id INTEGER NOT NULL REFERENCES customers(id), inbound_id INTEGER NOT NULL REFERENCES inbounds(id),
 status TEXT NOT NULL DEFAULT 'pending', PRIMARY KEY(customer_id,inbound_id)
);
CREATE TABLE operations (
 id TEXT PRIMARY KEY, actor INTEGER NOT NULL REFERENCES users(id), customer_id INTEGER NOT NULL REFERENCES customers(id),
 grant_id INTEGER REFERENCES grants(id), kind TEXT NOT NULL, charge INTEGER NOT NULL CHECK(charge>=0),
 request_hash TEXT NOT NULL, desired TEXT NOT NULL, status TEXT NOT NULL, diagnostic TEXT NOT NULL DEFAULT '',
 created INTEGER NOT NULL, updated INTEGER NOT NULL
);
CREATE UNIQUE INDEX customer_pending ON operations(customer_id) WHERE status IN ('reserved','sent','uncertain');
CREATE TABLE ledger (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), grant_id INTEGER NOT NULL REFERENCES grants(id),
 customer_id INTEGER REFERENCES customers(id), operation_id TEXT REFERENCES operations(id),
 kind TEXT NOT NULL CHECK(kind IN ('grant','adjust','reserve','release','sale')),
 amount INTEGER NOT NULL, actor INTEGER NOT NULL REFERENCES users(id), created INTEGER NOT NULL
);
CREATE UNIQUE INDEX ledger_operation_kind ON ledger(operation_id,kind);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TABLE audit (
 id INTEGER PRIMARY KEY, actor INTEGER REFERENCES users(id), action TEXT NOT NULL,
 subject TEXT NOT NULL DEFAULT '', outcome TEXT NOT NULL, created INTEGER NOT NULL
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
