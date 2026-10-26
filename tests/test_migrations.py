from pathlib import Path

from reseller.db import APPLICATION_ID, Database


def test_schema_one_upgrade_preserves_ledger_and_adds_refunds(tmp_path):
    path = tmp_path / 'legacy.db'
    db = Database(path)
    schema = Path(__file__).parents[1] / 'reseller' / 'migrations' / '001.sql'
    with db.connect() as conn:
        conn.executescript(f'PRAGMA application_id={APPLICATION_ID};\n' + schema.read_text() + '\nPRAGMA user_version=1;')
        conn.execute("INSERT INTO users(id,username,password_hash,role,created) VALUES(1,'reseller','hash','reseller',1)")
        conn.execute('INSERT INTO grants(id,user_id,allowance,sold,starts,expires) VALUES(1,1,100,50,1,100)')
        conn.execute("INSERT INTO ledger(id,user_id,grant_id,kind,amount,actor,created) VALUES(1,1,1,'sale',50,1,2)")

    db.migrate()

    with db.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 4
        assert tuple(conn.execute('SELECT kind,amount FROM ledger WHERE id=1').fetchone()) == ('sale', 50)
        conn.execute("INSERT INTO ledger(user_id,grant_id,kind,amount,actor,created) VALUES(1,1,'refund',10,1,3)")
        assert conn.execute("SELECT amount FROM ledger WHERE kind='refund'").fetchone()[0] == 10
