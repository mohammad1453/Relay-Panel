"""Application CLI. Secrets are read from protected configuration or a terminal."""
import argparse
import getpass
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from .config import Config
from .db import Database, audit
from .domain import Service, identifier
from .security import password_hash
from .xui import XUI, RemoteError


def load_env(path):
    if not path:
        return
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        line=line.strip()
        if not line or line.startswith('#'):
            continue
        key,sep,value=line.partition('=')
        if not sep or not key.replace('_','').isalnum():
            raise ValueError('Invalid environment file')
        # JSON values written by installer preserve punctuation safely; no shell evaluation.
        if value.startswith('"'):
            value=json.loads(value)
        os.environ[key]=value


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['migrate','bootstrap','reset-password','check','sync','serve','backup-db','unlock-restored'])
    parser.add_argument('--env')
    parser.add_argument('--output')
    args=parser.parse_args()
    load_env(args.env)
    config=Config.env().validate()
    db=Database(config.db)
    db.migrate()
    if args.command=='migrate':
        print('Database schema 2 ready.')
    elif args.command=='bootstrap':
        if db.one("SELECT id FROM users WHERE role='admin'"):
            print('Administrator already exists; password unchanged.')
            return
        username=os.getenv('ADMIN_INITIAL_USERNAME') or input('Administrator username: ')
        password=os.getenv('ADMIN_INITIAL_PASSWORD') or getpass.getpass('Initial password (12+ characters): ')
        Service(db,None).bootstrap(username,password)
        print('Administrator created. Password change required at first login.')
    elif args.command=='reset-password':
        username=identifier(input('Account username: '))
        encoded=password_hash(getpass.getpass('New password (12+ characters): '))
        with db.transaction() as conn:
            user=conn.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()
            if not user:
                raise SystemExit('Account not found')
            conn.execute('UPDATE users SET password_hash=?,must_change=1 WHERE id=?',(encoded,user['id']))
            conn.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            audit(conn,None,'password_recovery',user['id'])
        print('Password reset; all sessions revoked.')
    elif args.command=='unlock-restored':
        print('Restored backups may omit sales made after the backup. Compare master clients, invoices, reservations and grants first. Correct grant allowances before unlocking.')
        if input('Type I RECONCILED ALL SALES to unlock: ')!='I RECONCILED ALL SALES':
            raise SystemExit('Cancelled; sales remain locked')
        with db.transaction() as conn:
            conn.execute("DELETE FROM state WHERE key='write_lock'")
            audit(conn,None,'restore_unlock')
        print('Sales unlocked by explicit operator acknowledgement.')
    elif args.command in ('check','sync'):
        remote=XUI(config)
        try:
            if args.command=='check':
                count=len(remote.inbounds())
                remote.clients()
                info=remote.info()
                mode={'clients':'client API','classic':'classic API (one inbound per customer)'}.get(info['mode'],info['mode'])
                print(f"Database OK. 3X-UI {('v'+info['version']) if info['version'] else '(version not reported)'} via {mode}: {count} inbounds (read only).")
            else:
                Service(db,remote).refresh()
                print('Reconciliation completed. Review unresolved operations in the web console.')
        except RemoteError as exc:
            raise SystemExit('Master check failed: '+exc.code) from None
        finally:
            remote.close()
    elif args.command=='backup-db':
        if not args.output:
            parser.error('--output is required')
        destination=Path(args.output).resolve()
        if destination.exists() or destination==Path(config.db).resolve():
            raise SystemExit('Backup destination must be a new file')
        with db.connect() as source,closing(sqlite3.connect(destination)) as target:
            source.backup(target)
        destination.chmod(0o600)
        print('Consistent SQLite backup created.')
    elif args.command=='serve':
        import uvicorn
        from .web import create_app
        # Behind the local Nginx proxy every request arrives from 127.0.0.1; trust
        # the forwarded client address from loopback only so login rate limits
        # apply per visitor. Public HTTP mode is reached directly and trusts nothing.
        uvicorn.run(create_app(config),host='0.0.0.0' if config.public_http else '127.0.0.1',port=int(os.getenv('APP_PORT','8095')),
                    proxy_headers=not config.public_http,forwarded_allow_ips='127.0.0.1',access_log=False,workers=1)


if __name__=='__main__':
    main()
