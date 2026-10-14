import hashlib
import secrets
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, InvalidHashError
from .db import now, audit

hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY = hasher.hash(secrets.token_urlsafe(32))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password):
    if not 12 <= len(password) <= 256:
        raise ValueError("password_policy")
    return hasher.hash(password)


def verify(encoded, password):
    try:
        return hasher.verify(encoded, password[:257])
    except (VerificationError, InvalidHashError):
        return False


def new_session(db, config, user=None, old=None):
    token, csrf, stamp = secrets.token_urlsafe(48), secrets.token_urlsafe(32), now()
    if old:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(old),))
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (digest(token), user, csrf, stamp, stamp,
               stamp + (config.session_seconds if user else 900)))
    db.execute("DELETE FROM sessions WHERE expires<?", (stamp,))
    return token, csrf


def authenticate(database, username, password, ip):
    # Separate IP and account buckets prevent username spraying and distributed guessing.
    buckets = ["ip:" + digest(ip), "user:" + digest(username)]
    with database.transaction() as db:
        blocked = False
        for bucket in buckets:
            row = db.execute("SELECT * FROM attempts WHERE bucket=?", (bucket,)).fetchone()
            if row and row['until_time'] > now() and row['count'] >= 8:
                blocked = True
        if blocked:
            audit(db, None, "login", outcome="limited")
            return None
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        good = verify(user['password_hash'] if user else DUMMY, password)
        if not good or not user or user['suspended']:
            for bucket in buckets:
                db.execute("INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=CASE WHEN until_time<? THEN 1 ELSE count+1 END, until_time=CASE WHEN until_time<? THEN excluded.until_time ELSE until_time END",
                           (bucket, now()+900, now(), now()))
            audit(db, None, "login", outcome="denied")
            return None
        db.execute("DELETE FROM attempts WHERE bucket=?", (buckets[1],))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user['id']))
        audit(db, user['id'], "login")
        return dict(user)
