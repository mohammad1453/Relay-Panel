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
    if not user:
        # Every login-page visit creates a pre-login session; cap them so
        # unauthenticated requests cannot grow the database without bound.
        db.execute("DELETE FROM sessions WHERE user_id IS NULL AND token_hash NOT IN "
                   "(SELECT token_hash FROM sessions WHERE user_id IS NULL ORDER BY created DESC LIMIT ?)",
                   (ANONYMOUS_SESSION_CAP,))
    return token, csrf


# Per-IP failures are tightly limited. The per-account bucket is much looser so
# that one client cannot lock a legitimate user (or the admin) out with a few
# wrong passwords, while distributed guessing against one account stays bounded.
IP_LIMIT = 8
ACCOUNT_LIMIT = 50
ANONYMOUS_SESSION_CAP = 5000


def authenticate(database, username, password, ip):
    buckets = [("ip:" + digest(ip), IP_LIMIT), ("user:" + digest(username), ACCOUNT_LIMIT)]
    with database.transaction() as db:
        blocked = False
        for bucket, limit in buckets:
            row = db.execute("SELECT * FROM attempts WHERE bucket=?", (bucket,)).fetchone()
            if row and row['until_time'] > now() and row['count'] >= limit:
                blocked = True
        if blocked:
            # Not audited: the audit table is append-only, and blocked attempts
            # would let an attacker grow it without bound.
            return None
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        good = verify(user['password_hash'] if user else DUMMY, password)
        if not good or not user or user['suspended']:
            for bucket, _ in buckets:
                db.execute("INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=CASE WHEN until_time<? THEN 1 ELSE count+1 END, until_time=CASE WHEN until_time<? THEN excluded.until_time ELSE until_time END",
                           (bucket, now()+900, now(), now()))
            audit(db, None, "login", outcome="denied")
            return None
        db.execute("DELETE FROM attempts WHERE bucket=?", (buckets[1][0],))
        db.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user['id']))
        audit(db, user['id'], "login")
        return dict(user)
