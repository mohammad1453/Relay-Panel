import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

APPLICATION_ID = 0x58525253


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


def now():
    return int(time.time())


class Database:
    def __init__(self, path):
        self.path = str(Path(path).resolve())

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def migrate(self):
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError("Database symlinks are forbidden")
        with self.connect() as db:
            app_id = db.execute("PRAGMA application_id").fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if (tables or app_id) and app_id != APPLICATION_ID:
                raise ValueError("Refusing a database not owned by this application")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 3:
                raise ValueError("Database requires a newer application")
            if version == 0:
                sql = Path(__file__).with_name("migrations").joinpath("001.sql").read_text()
                db.executescript(f"BEGIN IMMEDIATE; PRAGMA application_id={APPLICATION_ID};\n" + sql + "\nPRAGMA user_version=1; COMMIT;")
                version = 1
            if version == 1:
                sql = Path(__file__).with_name("migrations").joinpath("002.sql").read_text()
                db.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version=2; COMMIT;")
                version = 2
            if version == 2:
                sql = Path(__file__).with_name("migrations").joinpath("003.sql").read_text()
                db.executescript("BEGIN IMMEDIATE;\n" + sql + "\nPRAGMA user_version=3; COMMIT;")
            db.execute("PRAGMA journal_mode=WAL")
        path.chmod(0o600)

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def all(self, sql, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.all(sql, args)
        return rows[0] if rows else None


def audit(db, actor, action, subject="", outcome="ok"):
    db.execute("INSERT INTO audit(actor,action,subject,outcome,created) VALUES(?,?,?,?,?)",
               (actor, action, str(subject), outcome, now()))
