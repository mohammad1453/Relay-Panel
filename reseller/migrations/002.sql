CREATE TABLE ledger_v2 (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), grant_id INTEGER NOT NULL REFERENCES grants(id),
 customer_id INTEGER REFERENCES customers(id), operation_id TEXT REFERENCES operations(id),
 kind TEXT NOT NULL CHECK(kind IN ('grant','adjust','reserve','release','sale','refund')),
 amount INTEGER NOT NULL, actor INTEGER NOT NULL REFERENCES users(id), created INTEGER NOT NULL
);
INSERT INTO ledger_v2(id,user_id,grant_id,customer_id,operation_id,kind,amount,actor,created)
 SELECT id,user_id,grant_id,customer_id,operation_id,kind,amount,actor,created FROM ledger;
DROP TABLE ledger;
ALTER TABLE ledger_v2 RENAME TO ledger;
CREATE UNIQUE INDEX ledger_operation_kind ON ledger(operation_id,kind);
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
