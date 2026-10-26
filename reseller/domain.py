import base64
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import uuid
from decimal import Decimal, InvalidOperation
from .db import now, audit
from .security import password_hash
from .xui import RemoteError

GIB = 1024**3
from .domain_constants import PROTOCOLS  # noqa: E402  (re-exported for templates and tests)
# Issues that mean the 3X-UI copy of a customer no longer matches Relay.
DRIFT_ISSUES = ('remote_changed', 'remote_identity', 'remote_drift')
# 3X-UI client group for customers the admin creates directly (they have no reseller plan).
ADMIN_GROUP = 'RelayPanel'
# A sent create/update whose client is missing on this many full syncs is treated as gone.
ABSENT_SYNCS = 3


class RuleError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def volume(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > 1_000_000:
            raise ValueError()
        size = int(amount * GIB)
        if size <= 0:
            raise ValueError()
        return size
    except (InvalidOperation, ValueError, OverflowError):
        raise RuleError('invalid_volume') from None


def identifier(value):
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.@-]{1,63}', value):
        raise RuleError('invalid_identifier')
    return value


def active_grant(db, user_id):
    row = db.execute('SELECT * FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>? ORDER BY id DESC LIMIT 1', (user_id, now(), now())).fetchone()
    if not row:
        raise RuleError('plan_expired', 403)
    return row


DELETED_MARK = '~deleted-'


def display_name(email):
    """Customer name without the suffix added when it was deleted."""
    return str(email).split(DELETED_MARK)[0]


def free_customer_name(db, customer_id):
    """After a delete, release the name and credentials so they can be used again."""
    db.execute("UPDATE customers SET email=email||?||id,sub_id=sub_id||?||id,uuid=NULL,remote_id=NULL WHERE id=? AND instr(email,?)=0",
               (DELETED_MARK,DELETED_MARK,customer_id,DELETED_MARK))
    db.execute('DELETE FROM identities WHERE customer_id=?',(customer_id,))


def owned(db, actor, customer_id):
    row = db.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    if not row or (actor['role'] != 'admin' and row['user_id'] != actor['id']):
        raise RuleError('not_found', 404)
    return row


class Service:
    def __init__(self, database, remote):
        self.db, self.remote = database, remote
        # Only one full sync at a time (background worker and "Sync now").
        self.sync_lock = threading.Lock()
        # Node-backed changes the master accepted are trusted after this long.
        self.settle_seconds = 60

    def bootstrap(self, username, password):
        with self.db.transaction() as db:
            if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
                return False
            db.execute("INSERT INTO users(username,password_hash,role,created) VALUES(?,?,'admin',?)", (identifier(username), password_hash(password), now()))
            audit(db, None, 'bootstrap')
            return True

    def reseller(self, actor, username, password, tag, allowance, expires, inbound_ids):
        if actor['role'] != 'admin':
            raise RuleError('forbidden', 403)
        tag = identifier(tag)
        if tag == ADMIN_GROUP.lower():
            raise RuleError('identity_conflict')
        with self.db.transaction() as db:
            ids = self._valid_inbounds(db, inbound_ids)
            try:
                uid = db.execute("INSERT INTO users(username,password_hash,role,tag,created) VALUES(?,?,'reseller',?,?)", (identifier(username), password_hash(password), tag, now())).lastrowid
            except sqlite3.IntegrityError:
                raise RuleError('identity_conflict') from None
            for iid in ids:
                db.execute('INSERT INTO permissions VALUES(?,?)', (uid, iid))
            self._grant(db, actor['id'], uid, allowance, expires)
            audit(db, actor['id'], 'reseller_create', uid)
            return uid

    def _grant(self, db, actor_id, uid, allowance, expires):
        amount = volume(allowance)
        if expires <= now():
            raise RuleError('invalid_expiry')
        db.execute('UPDATE grants SET closed=1 WHERE user_id=?', (uid,))
        gid = db.execute('INSERT INTO grants(user_id,allowance,starts,expires) VALUES(?,?,?,?)', (uid, amount, now(), expires)).lastrowid
        db.execute("INSERT INTO ledger(user_id,grant_id,kind,amount,actor,created) VALUES(?,?,'grant',?,?,?)", (uid,gid,amount,actor_id,now()))
        audit(db, actor_id, 'grant', gid)
        return gid

    def renew(self, actor, uid, allowance, expires):
        if actor['role'] != 'admin':
            raise RuleError('forbidden',403)
        with self.db.transaction() as db:
            if not db.execute("SELECT 1 FROM users WHERE id=? AND role='reseller'", (uid,)).fetchone():
                raise RuleError('not_found',404)
            return self._grant(db, actor['id'], uid, allowance, expires)

    def set_reseller_customer_enabled(self, uid, enabled):
        """Queue and apply a reseller suspension override without changing customer records."""
        customers = self.db.all("SELECT id,payload FROM customers WHERE user_id=? AND status!='deleted'", (uid,))
        with self.db.transaction() as db:
            for customer in customers:
                customer_enabled=bool(json.loads(customer['payload']).get('enable',True)) if enabled else False
                db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           (f'customer_enable:{customer["id"]}', '1' if customer_enabled else '0'))
        try:
            return self.sync_customer_enable()
        except RemoteError:
            return len(customers)

    def sync_customer_enable(self, records=None):
        """Retry acknowledged enable/disable changes after master or node outages."""
        pending = self.db.all("SELECT key,value FROM state WHERE key LIKE 'customer_enable:%'")
        if not pending:
            return 0
        records = records if records is not None else self.remote.clients()
        by_email = {row['email']: row for row in records}
        remaining = 0
        for item in pending:
            try:
                customer_id = int(item['key'].split(':', 1)[1])
                enabled = item['value'] == '1'
                customer = self.db.one('SELECT * FROM customers WHERE id=? AND status!=\'deleted\'', (customer_id,))
                if not customer:
                    with self.db.transaction() as db:
                        db.execute('DELETE FROM state WHERE key=?', (item['key'],))
                    continue
                remote = by_email.get(customer['email'])
                payload = json.loads(customer['payload'])
                if not remote:
                    with self.db.transaction() as db:
                        db.execute('DELETE FROM state WHERE key=?', (item['key'],))
                    continue
                if not self._identity(customer, remote):
                    remaining += 1
                    continue
                desired = {**payload, 'enable': enabled}
                # Retry even if the master's copy already changed: a prior response may
                # have reported that the backing node still needs to apply the update.
                result = self.remote.update(customer['email'], desired)
                if isinstance(result, dict) and result.get('nodePending'):
                    remaining += 1
                    continue
                remote['enable'] = enabled
                with self.db.transaction() as db:
                    db.execute('DELETE FROM state WHERE key=?', (item['key'],))
            except (RemoteError, ValueError, TypeError):
                remaining += 1
        return remaining

    @property
    def protocols(self):
        """Protocols the connected 3X-UI can manage clients for."""
        return getattr(self.remote, 'protocols', PROTOCOLS) if self.remote else PROTOCOLS

    def _valid_inbounds(self, db, ids):
        ids = sorted(set(int(x) for x in ids))
        if not ids or len(ids) > 32:
            raise RuleError('inbound_denied')
        for iid in ids:
            ib = db.execute('SELECT * FROM inbounds WHERE id=?', (iid,)).fetchone()
            if not ib or not ib['available'] or ib['protocol'] not in self.protocols or ib['synced'] < now()-600:
                raise RuleError('inbound_unavailable')
        return ids

    def sync_inbounds(self):
        rows = self.remote.inbounds()
        remote_ids = {ib['id'] for ib in rows}
        with self.db.transaction() as db:
            local = {r['id']: r['name'] for r in db.execute('SELECT id,name FROM inbounds')}
            missing = sorted(i for i in local if i not in remote_ids)
            if missing and not rows:
                # An empty list may be a glitch: only trust it on two syncs in a row.
                if not db.execute("SELECT 1 FROM state WHERE key='inbounds_empty_once'").fetchone():
                    db.execute("INSERT INTO state VALUES('inbounds_empty_once',?)",(str(now()),))
                    missing = []
            else:
                db.execute("DELETE FROM state WHERE key='inbounds_empty_once'")
            for iid in missing:
                self._remove_inbound(db, iid, local[iid])
            db.execute('UPDATE inbounds SET available=0')
            for ib in rows:
                db.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,port=excluded.port,available=excluded.available,metadata=excluded.metadata,synced=excluded.synced',
                           (ib['id'], str(ib.get('remark','')), ib['protocol'], ib.get('port',0), int(bool(ib.get('enable',False)) and ib['protocol'] in self.protocols), dumps(ib), now()))
            db.execute("INSERT INTO state VALUES('connection',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now()),))
            if hasattr(self.remote, 'info'):
                db.execute("INSERT INTO state VALUES('xui_info',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (dumps(self.remote.info()),))
        return rows

    @staticmethod
    def _remove_inbound(db, iid, name):
        """Forget an inbound that was deleted in 3X-UI, and every link to it."""
        customers = [r[0] for r in db.execute('SELECT customer_id FROM placements WHERE inbound_id=?',(iid,))]
        resellers = [r[0] for r in db.execute('SELECT user_id FROM permissions WHERE inbound_id=?',(iid,))]
        db.execute('DELETE FROM placements WHERE inbound_id=?',(iid,))
        db.execute('DELETE FROM permissions WHERE inbound_id=?',(iid,))
        db.execute('DELETE FROM inbounds WHERE id=?',(iid,))
        for op in db.execute("SELECT id,desired FROM operations WHERE status IN ('reserved','sent','uncertain')").fetchall():
            desired = json.loads(op['desired'])
            before = dumps(desired)
            for key in ('inboundIds','previousInboundIds'):
                if key in desired:
                    desired[key] = [i for i in desired[key] if i != iid]
            if dumps(desired) != before:
                db.execute('UPDATE operations SET desired=? WHERE id=?',(dumps(desired),op['id']))
        audit(db, None, 'inbound_removed', dumps({'id':iid,'name':name,'customers':len(customers),'resellers':resellers}))

    def credentials(self, protocols, form):
        result = {}
        if protocols & {'vmess', 'vless', 'tuic'}:
            # 3X-UI treats client identifiers as protocol data; preserve custom values
            # exactly as entered instead of enforcing UUID syntax in Relay.
            result['id'] = form.get('uuid') or str(uuid.uuid4())
        if protocols & {'trojan','tuic','shadowsocks'}:
            # Empty Shadowsocks password is filled by the panel for the exact cipher/key length.
            if protocols == {'shadowsocks'}:
                result['password'] = ''
            else:
                result['password'] = form.get('credential') or secrets.token_urlsafe(24)
        if 'hysteria' in protocols:
            result['auth'] = form.get('credential') or secrets.token_urlsafe(24)
        # Tunnel keys/addresses and MTProto FakeTLS secrets are allocated by the verified panel code.
        result['security'] = 'auto'
        return result

    def reserve(self, actor, form, operation_id, kind='create', customer_id=None):
        credential=str(form.get('credential',''))
        uuid_value=str(form.get('uuid',''))
        sub_id_value=str(form.get('sub_id',''))
        if any(any(ord(c)<32 for c in value) for value in (credential,uuid_value,sub_id_value)):
            raise RuleError('invalid_input')
        try:
            operation_id = str(uuid.UUID(operation_id))
        except (ValueError, AttributeError):
            raise RuleError('invalid_operation') from None
        request_hash = hashlib.sha256(dumps({'form':form,'kind':kind,'customer':customer_id}).encode()).hexdigest()
        with self.db.transaction() as db:
            if db.execute("SELECT 1 FROM state WHERE key='write_lock'").fetchone():
                raise RuleError('restore_locked',403)
            current = db.execute('SELECT * FROM users WHERE id=?', (actor['id'],)).fetchone()
            if not current or current['suspended']:
                raise RuleError('forbidden',403)
            previous = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if previous:
                if previous['actor'] != actor['id'] or previous['request_hash'] != request_hash:
                    raise RuleError('operation_conflict',409)
                return dict(previous)
            customer = owned(db, actor, customer_id) if customer_id else None
            uid = customer['user_id'] if customer else actor['id']
            owner = db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            # Customers the admin creates belong to the admin: no plan, no quota, all inbounds.
            admin_owned = owner['role'] == 'admin'
            # An admin must be able to remove customers of an expired or suspended
            # reseller. A delete charges nothing, so it needs no active plan.
            admin_delete = kind == 'delete' and actor['role'] == 'admin'
            if admin_owned:
                grant = None
            elif admin_delete:
                grant = db.execute('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
                if not grant:
                    raise RuleError('plan_expired',403)
            else:
                grant = active_grant(db, uid)  # Admin uses the same sales invariant.
                if owner['suspended']:
                    raise RuleError('forbidden',403)
            if customer and kind == 'delete':
                # Deleting must always be possible, also for customers that need attention.
                if customer['status'] == 'deleted':
                    raise RuleError('not_found',404)
            elif customer and customer['status'] != 'active':
                raise RuleError('reconcile_required',409)
            elif customer and customer['issue']:
                raise RuleError('remote_changed' if customer['issue'] in DRIFT_ISSUES else customer['issue'] if customer['issue'] in ('remote_missing','suspension_pending') else 'reconcile_required',409)
            if customer:
                if db.execute("SELECT 1 FROM operations WHERE customer_id=? AND status IN ('reserved','sent','uncertain')",(customer_id,)).fetchone():
                    raise RuleError('reconcile_required',409)
                previous_ids = [r[0] for r in db.execute('SELECT inbound_id FROM placements WHERE customer_id=?',(customer_id,))]
                ids = self._valid_inbounds(db, form.get('inbounds', previous_ids)) if kind == 'update' else previous_ids
                payload = json.loads(customer['payload'])
            else:
                ids = self._valid_inbounds(db, form.get('inbounds',[]))
                previous_ids = []
                payload = {}
            for iid in ids:
                if not admin_delete and not admin_owned and not db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?',(uid,iid)).fetchone():
                    raise RuleError('inbound_denied',403)
            if kind != 'delete':
                self._valid_inbounds(db, ids)
                if len(ids) > 1 and getattr(self.remote, 'single_inbound', False):
                    # Classic 3X-UI panels allow an email on one inbound only.
                    raise RuleError('single_inbound')
                amount = volume(form.get('volume', str(Decimal(customer['package'])/GIB) if customer else ''))
                expiry = int(form.get('expiry', payload.get('expiryTime',0)))
                preserved_expiry=bool(customer and expiry in (payload.get('expiryTime'),customer['remote_expiry']))
                if expiry == 0 or expiry > 0 and expiry <= now()*1000 and not preserved_expiry or expiry < -3650*86400000:
                    raise RuleError('invalid_expiry')
                if not customer:
                    protocols = {db.execute('SELECT protocol FROM inbounds WHERE id=?',(i,)).fetchone()[0] for i in ids}
                    # Mixed ciphers can require incompatible credential lengths; use homogeneous packages.
                    if len(protocols) != 1:
                        raise RuleError('mixed_protocols')
                    payload.update(self.credentials(protocols, form))
                    payload.update(email=identifier(form['email']),subId=form.get('sub_id') or secrets.token_urlsafe(24))
                elif kind=='update':
                    protocols={db.execute('SELECT protocol FROM inbounds WHERE id=?',(i,)).fetchone()[0] for i in ids}
                    previous_protocols={db.execute('SELECT protocol FROM inbounds WHERE id=?',(i,)).fetchone()[0] for i in previous_ids}
                    if len(protocols) != 1 or previous_protocols and protocols != previous_protocols:
                        raise RuleError('mixed_protocols')
                    if form.get('uuid') and protocols & {'vmess','vless','tuic'}:
                        payload['id']=form['uuid']
                    if form.get('sub_id'):
                        payload['subId']=form['sub_id']
                    if form.get('credential'):
                        if protocols & {'trojan','tuic'}:
                            payload['password']=form['credential']
                        elif 'hysteria' in protocols:
                            payload['auth']=form['credential']
                comment = str(form.get('comment',payload.get('comment','')))
                if len(comment) > 1000:
                    raise RuleError('invalid_input')
                payload.update(totalGB=amount, expiryTime=expiry, comment=comment,
                               enable=bool(form.get('enable',payload.get('enable',True))),group=ADMIN_GROUP if admin_owned else owner['tag'],
                               reset=0,resetDay=0,resetMax=0,trafficReset='never',trafficResetDay=1)
                charge = 0 if admin_owned else max(0,amount - (customer['package'] if customer else 0))
            else:
                amount, charge = customer['package'], 0
            if grant and grant['sold']+grant['reserved']+charge > grant['allowance']:
                raise RuleError('insufficient_quota')
            if not customer:
                try:
                    customer_id = db.execute("INSERT INTO customers(user_id,email,uuid,sub_id,payload,package,status,created) VALUES(?,?,?,?,?,?,'pending',?)",
                                             (uid,payload['email'],payload.get('id'),payload['subId'],dumps(payload),amount,now())).lastrowid
                    for iid in ids:
                        db.execute('INSERT INTO placements(customer_id,inbound_id) VALUES(?,?)',(customer_id,iid))
                except sqlite3.IntegrityError:
                    raise RuleError('identity_conflict') from None
            desired_data={'client':payload,'inboundIds':ids}
            if kind == 'update':
                desired_data['previousInboundIds']=previous_ids
            desired = dumps(desired_data)
            for field in ('id','subId','password','auth'):
                value=payload.get(field)
                if not value:
                    continue
                fingerprint=hashlib.sha256(str(value).encode()).hexdigest()
                claim=db.execute('SELECT customer_id FROM identities WHERE kind=? AND value=?',(field,fingerprint)).fetchone()
                if claim and claim[0]!=customer_id:
                    raise RuleError('identity_conflict')
                db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?)',(field,fingerprint,customer_id))
            db.execute("INSERT INTO operations(id,actor,customer_id,grant_id,kind,charge,request_hash,desired,status,created,updated) VALUES(?,?,?,?,?,?,?,?,'reserved',?,?)",
                       (operation_id,actor['id'],customer_id,grant['id'] if grant else None,kind,charge,request_hash,desired,now(),now()))
            if grant:
                db.execute('UPDATE grants SET reserved=reserved+? WHERE id=?',(charge,grant['id']))
                self._entry(db, uid, grant['id'], customer_id, operation_id,'reserve',charge,actor['id'])
            audit(db,actor['id'], 'customer_'+kind+'_reserved',customer_id)
            return dict(db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone())

    @staticmethod
    def _entry(db,uid,gid,cid,oid,kind,amount,actor):
        if gid is None:
            return  # admin-owned customers have no plan, so nothing to book
        db.execute('INSERT INTO ledger(user_id,grant_id,customer_id,operation_id,kind,amount,actor,created) VALUES(?,?,?,?,?,?,?,?)', (uid,gid,cid,oid,kind,amount,actor,now()))

    def _identity(self, local, remote):
        payload = json.loads(local['payload'])
        return (remote.get('email') == local['email'] and remote.get('subId') == local['sub_id']
                and (not local['remote_id'] or remote.get('id') == local['remote_id'])
                and (not local['uuid'] or remote.get('uuid') == local['uuid'])
                and remote.get('group', payload['group']) == payload['group'])

    @staticmethod
    def _refund_amount(db, user_id, grant_id, customer_id, old_amount, new_amount, used):
        """Return only this customer's unused, previously sold quota on this grant."""
        if used is None:
            return 0
        desired = max(0, old_amount - max(new_amount, used))
        if desired <= 0:
            return 0
        sold = db.execute("SELECT coalesce(sum(amount),0) FROM ledger WHERE user_id=? AND grant_id=? AND customer_id=? AND kind='sale'",
                          (user_id, grant_id, customer_id)).fetchone()[0]
        refunded = db.execute("SELECT coalesce(sum(amount),0) FROM ledger WHERE user_id=? AND grant_id=? AND customer_id=? AND kind='refund'",
                              (user_id, grant_id, customer_id)).fetchone()[0]
        grant = db.execute('SELECT sold FROM grants WHERE id=?',(grant_id,)).fetchone()
        attributable = max(0, sold - refunded)
        return min(desired, attributable, grant['sold'] if grant else 0)

    def execute(self, operation_id):
        op = self.db.one('SELECT * FROM operations WHERE id=?',(operation_id,))
        if not op or op['status'] != 'reserved':
            return
        local = self.db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
        desired = json.loads(op['desired'])
        # Read-only preflight, BEFORE marking sent. Failures here are definitely unsent.
        try:
            records = self.remote.clients()
            remote = next((r for r in records if r['email'].lower() == local['email']),None)
            if op['kind'] == 'create':
                if remote or any(r.get('subId') == local['sub_id'] or local['uuid'] and r.get('uuid') == local['uuid'] for r in records):
                    raise RuleError('identity_conflict')
            elif op['kind'] == 'delete':
                # A delete only needs to know it removes the right client; missing is fine.
                if remote and (remote.get('subId') != local['sub_id'] or local['remote_id'] and remote.get('id') != local['remote_id']):
                    raise RuleError('remote_identity')
            elif not remote:
                raise RuleError('remote_missing')
            elif not self._identity(local,remote):
                raise RuleError('remote_identity')
            elif not self._matches(remote,json.loads(local['payload'])) or set(remote['inboundIds']) != set(desired.get('previousInboundIds',desired['inboundIds'])):
                raise RuleError('remote_changed')
            for record in records:
                if record['email']==local['email']:
                    continue
                for field,remote_field in [('id','uuid'),('subId','subId'),('password','password'),('auth','auth')]:
                    if desired['client'].get(field) and desired['client'][field]==record.get(remote_field):
                        raise RuleError('identity_conflict')
            if op['kind']=='update' and desired['client']['expiryTime']<0 and remote and remote.get('expiryTime',0)>0:
                # Preserve the remote activation timestamp; never restart a first-use package during an edit.
                desired['client']['expiryTime']=remote['expiryTime']
            remote_traffic = remote.get('traffic') if remote else None
            if isinstance(remote_traffic,dict) and all(isinstance(remote_traffic.get(k),int) and remote_traffic[k]>=0 for k in ('up','down')):
                used_bytes = remote_traffic['up'] + remote_traffic['down']
            else:
                used_bytes = local['used']
        except (RemoteError,RuleError) as exc:
            self.release(operation_id, exc.code, unsent=True)
            return
        with self.db.transaction() as db:
            owner=db.execute('SELECT suspended FROM users WHERE id=?',(local['user_id'],)).fetchone()
            original=db.execute('SELECT * FROM grants WHERE id=?',(op['grant_id'],)).fetchone()
            permitted=all(db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?',(local['user_id'],iid)).fetchone() for iid in desired['inboundIds'])
            actor_row=db.execute('SELECT role FROM users WHERE id=?',(op['actor'],)).fetchone()
            admin_delete=op['kind']=='delete' and actor_row and actor_row['role']=='admin'
            admin_owned=op['grant_id'] is None
            can_send=bool(owner) and (admin_owned or admin_delete or bool(original) and not owner['suspended'] and not original['closed'] and original['expires']>now() and permitted)
            if can_send and op['kind']=='update':
                desired['refund'] = self._refund_amount(db,local['user_id'],op['grant_id'],local['id'],
                                                        local['package'],desired['client']['totalGB'],used_bytes)
            changed = db.execute("UPDATE operations SET status='sent',desired=?,updated=? WHERE id=? AND status='reserved'",(dumps(desired),now(),operation_id)).rowcount if can_send else 0
        if not can_send:
            self.release(operation_id,'plan_expired',unsent=True)
            return
        if not changed:
            return
        try:
            if op['kind'] == 'create':
                result = self.remote.create(desired['client'],desired['inboundIds'])
            elif op['kind'] == 'delete':
                result = self.remote.delete(local['email']) if remote else None
            else:
                result = self.remote.update(local['email'],desired['client'])
            if isinstance(result,dict) and result.get('nodePending'):
                self.uncertain(operation_id,'node_pending')
                return
            if op['kind'] == 'update':
                old_ids=set(desired.get('previousInboundIds',desired['inboundIds']))
                new_ids=set(desired['inboundIds'])
                added=sorted(new_ids-old_ids)
                removed=sorted(old_ids-new_ids)
                steps=[('attach',added),('detach',removed)]
                if getattr(self.remote,'detach_first',False):
                    steps.reverse()
                for step,inbound_ids in steps:
                    if not inbound_ids:
                        continue
                    result=getattr(self.remote,step)(local['email'],inbound_ids)
                    if isinstance(result,dict) and result.get('nodePending'):
                        self.uncertain(operation_id,'node_pending')
                        return
            with self.db.transaction() as db:
                db.execute("UPDATE operations SET diagnostic='remote_ack' WHERE id=? AND status IN ('sent','uncertain')",(operation_id,))
        except RemoteError as exc:
            self.uncertain(operation_id,exc.code)
        self.reconcile(operation_id)

    def uncertain(self, oid, code, keep_ack=False):
        with self.db.transaction() as db:
            # A read failure during verification must not erase proof that 3X-UI accepted the change.
            keep = "CASE WHEN diagnostic IN ('remote_ack','node_pending') THEN diagnostic ELSE ? END" if keep_ack else '?'
            db.execute("UPDATE operations SET status='uncertain',diagnostic="+keep+",updated=? WHERE id=? AND status IN ('sent','uncertain')",(code,now(),oid))
            audit(db,None,'remote_operation',oid,code)

    def release(self, oid, reason, unsent=False):
        # Post-send absence is never enough evidence to release: a timed-out request may still complete.
        with self.db.transaction() as db:
            op = db.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            if not unsent or op['status'] != 'reserved':
                return
            c = db.execute('SELECT * FROM customers WHERE id=?',(op['customer_id'],)).fetchone()
            db.execute('UPDATE grants SET reserved=reserved-? WHERE id=?',(op['charge'],op['grant_id']))
            self._entry(db,c['user_id'],op['grant_id'],c['id'],oid,'release',op['charge'],op['actor'])
            db.execute("UPDATE operations SET status='failed',diagnostic=?,updated=? WHERE id=?",(reason,now(),oid))
            if op['kind']=='create':
                db.execute("UPDATE customers SET status='failed',issue=? WHERE id=?",(reason,c['id']))
            audit(db,op['actor'],'operation_release',oid,reason)

    @staticmethod
    def _matches(actual, desired):
        keys = ('email','group','totalGB','enable','comment','subId')
        # A panel that does not report a field (e.g. group on old versions) is not a difference.
        if any(k in actual and actual.get(k) != desired.get(k) for k in keys if k != 'email') or actual.get('email') != desired.get('email'):
            return False
        for field in ('id','password','auth'):
            remote_value=actual.get('uuid') if field=='id' and 'uuid' in actual else actual.get(field)
            if desired.get(field) and desired[field]!=remote_value:
                return False
        # First-use duration can legitimately have become an absolute timestamp.
        wanted, got = desired['expiryTime'], actual.get('expiryTime',0)
        return got == wanted or wanted < 0 and got > 0

    def reconcile(self, oid, force=False):
        """Read 3X-UI back and commit the operation once its result is verified.

        force=True is the admin's "Mark as done": it skips per-inbound and node
        checks but still requires the client to exist (or be gone for a delete).
        """
        op = self.db.one('SELECT * FROM operations WHERE id=?',(oid,))
        if not op or op['status'] not in ('sent','uncertain'):
            return
        local = self.db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
        desired = json.loads(op['desired'])
        try:
            records, inbounds = self.remote.clients(), self.remote.inbounds()
            remote = next((r for r in records if r['email']==local['email']),None)
            by_id = {i['id']:i for i in inbounds}
            # Inbounds deleted in 3X-UI can no longer be verified or placed; ignore them.
            wanted = [i for i in desired['inboundIds'] if i in by_id]
            desired['inboundIds'] = wanted
            outcomes = {}
            affected_ids={i for i in set(wanted) | set(desired.get('previousInboundIds',[])) if i in by_id}
            for iid in affected_ids:
                ib = by_id.get(iid)
                clients = (ib or {}).get('settings',{}).get('clients',[])
                item = next((c for c in clients if c.get('email')==local['email']),None)
                expected_present=op['kind']!='delete' and iid in desired['inboundIds']
                if not ib:
                    outcomes[iid]='mismatch'
                elif expected_present:
                    outcomes[iid]='verified' if item and self._matches(item,desired['client']) else 'mismatch'
                else:
                    outcomes[iid]='verified' if not item else 'mismatch'
            if op['kind']=='delete':
                complete = remote is None and (force or all(v=='verified' for v in outcomes.values()))
            elif force:
                complete = bool(remote and remote.get('email')==local['email'])
            else:
                complete = bool(remote and remote.get('email')==local['email'] and (not local['remote_id'] or remote.get('id')==local['remote_id']) and self._matches(remote,desired['client'])
                                and set(remote['inboundIds'])==set(desired['inboundIds']) and all(v=='verified' for v in outcomes.values()))
            # Offline backing nodes cannot be proven applied just from master's stored configuration.
            # A change the master accepted with "node pending" is trusted once it has settled.
            settled = op['diagnostic']=='node_pending' and op['created'] < now()-self.settle_seconds
            if not force and any(by_id.get(i,{}).get('nodeId') for i in affected_ids) and op['diagnostic']!='remote_ack' and not settled:
                complete = False
            with self.db.transaction() as db:
                current = db.execute('SELECT status FROM operations WHERE id=?',(oid,)).fetchone()[0]
                if current not in ('sent','uncertain'):
                    return
                for iid,status in outcomes.items():
                    db.execute('UPDATE placements SET status=? WHERE customer_id=? AND inbound_id=?',(status,local['id'],iid))
                if not complete:
                    db.execute("UPDATE operations SET status='uncertain',diagnostic=CASE WHEN diagnostic IN ('node_pending','remote_ack') THEN diagnostic ELSE 'reconcile_required' END,updated=? WHERE id=?",(now(),oid))
                    return
                refund = int(desired.get('refund',0)) if op['kind']=='update' else 0
                changed = db.execute('UPDATE grants SET reserved=reserved-?,sold=sold+?-? WHERE id=? AND sold>=?',
                                     (op['charge'],op['charge'],refund,op['grant_id'],refund)).rowcount if op['grant_id'] is not None else 1
                if changed != 1:
                    db.execute("UPDATE operations SET status='uncertain',diagnostic='reconcile_required',updated=? WHERE id=?",(now(),oid))
                    return
                if op['charge']:
                    self._entry(db,local['user_id'],op['grant_id'],local['id'],oid,'sale',op['charge'],op['actor'])
                if refund:
                    self._entry(db,local['user_id'],op['grant_id'],local['id'],oid,'refund',refund,op['actor'])
                if op['kind']=='update':
                    for iid in desired['inboundIds']:
                        db.execute("INSERT INTO placements(customer_id,inbound_id,status) VALUES(?,?,'verified') ON CONFLICT(customer_id,inbound_id) DO UPDATE SET status='verified'",(local['id'],iid))
                    if desired.get('previousInboundIds'):
                        removed=sorted(set(desired['previousInboundIds'])-set(desired['inboundIds']))
                        if removed:
                            marks=','.join('?' for _ in removed)
                            db.execute(f'DELETE FROM placements WHERE customer_id=? AND inbound_id IN ({marks})',(local['id'],*removed))
                payload = desired['client']
                if remote:
                    # Persist generated protocol credentials so a later full client update preserves them.
                    for field in ('password','auth','privateKey','publicKey','preSharedKey','keepAlive','secret','adTag','flow','security','forwardedPorts'):
                        if field in remote:
                            payload[field]=remote[field]
                    if remote.get('allowedIPs'):
                        payload['allowedIPs'] = json.loads(remote['allowedIPs']) if isinstance(remote['allowedIPs'],str) else remote['allowedIPs']
                    payload['expiryTime'] = remote['expiryTime']
                db.execute('UPDATE customers SET status=?,payload=?,package=?,remote_id=?,uuid=?,sub_id=?,issue=? WHERE id=?',
                           ('deleted' if op['kind']=='delete' else 'active',dumps(payload),payload['totalGB'] or local['package'],remote['id'] if remote else local['remote_id'],payload.get('id'),payload['subId'],'',local['id']))
                db.execute("DELETE FROM state WHERE key IN (?,?)",(f"drift:{local['id']}",f"absent:{oid}"))
                if op['kind']=='delete':
                    free_customer_name(db, local['id'])
                db.execute("UPDATE operations SET status='committed',diagnostic='',updated=? WHERE id=?",(now(),oid))
                audit(db,op['actor'],'customer_'+op['kind'],local['id'])
        except RemoteError as exc:
            self.uncertain(oid,exc.code,keep_ack=True)

    def refresh(self, wait=None):
        """Full sync with 3X-UI. Only one runs at a time."""
        if not self.sync_lock.acquire(timeout=-1 if wait is None else wait):
            raise RuleError('sync_busy',409)
        try:
            self._refresh()
        except RemoteError as exc:
            self._set_state('sync_error',f'{exc.code}:{now()}')
            raise
        else:
            self._set_state('sync_ok',str(now()))
            with self.db.transaction() as db:
                db.execute("DELETE FROM state WHERE key='sync_error'")
        finally:
            self.sync_lock.release()

    def reconcile_pending(self):
        """Quick check of customers still being set up; skipped while a full sync runs."""
        if not self.sync_lock.acquire(blocking=False):
            return
        try:
            for op in self.db.all("SELECT id FROM operations WHERE status IN ('sent','uncertain')"):
                self.reconcile(op['id'])
        finally:
            self.sync_lock.release()

    def _set_state(self, key, value):
        with self.db.transaction() as db:
            db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

    def _bump_absent(self, oid):
        """Count full syncs (at least 20s apart) on which an operation's client was missing."""
        key=f'absent:{oid}'
        with self.db.transaction() as db:
            row=db.execute('SELECT value FROM state WHERE key=?',(key,)).fetchone()
            count,stamp=(int(x) for x in row[0].split(':')) if row else (0,0)
            if now()-stamp>=20:
                count+=1
                db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,f'{count}:{now()}'))
            return count

    def abandon(self, oid, reason):
        """Finish a sent operation whose client is gone from 3X-UI: return the held quota."""
        with self.db.transaction() as db:
            op = db.execute('SELECT * FROM operations WHERE id=?',(oid,)).fetchone()
            if not op or op['status'] not in ('sent','uncertain'):
                return
            c = db.execute('SELECT * FROM customers WHERE id=?',(op['customer_id'],)).fetchone()
            db.execute('UPDATE grants SET reserved=reserved-? WHERE id=?',(op['charge'],op['grant_id']))
            self._entry(db,c['user_id'],op['grant_id'],c['id'],oid,'release',op['charge'],op['actor'])
            db.execute("UPDATE operations SET status='failed',diagnostic=?,updated=? WHERE id=?",(reason,now(),oid))
            db.execute("UPDATE customers SET status='deleted',issue='remote_missing',online=NULL WHERE id=?",(c['id'],))
            db.execute("DELETE FROM state WHERE key IN (?,?,?)",(f'absent:{oid}',f"drift:{c['id']}",f"customer_enable:{c['id']}"))
            free_customer_name(db, c['id'])
            audit(db,None,'customer_removed_during_setup',c['id'],reason)

    def _refresh(self):
        self.sync_inbounds()
        remote_records = self.remote.clients()
        remote_by_email = {r['email']: r for r in remote_records}
        with self.db.transaction() as db:
            for customer in db.execute("SELECT c.id,c.email FROM customers c JOIN users u ON u.id=c.user_id WHERE c.status!='deleted' AND u.suspended=1").fetchall():
                remote = remote_by_email.get(customer['email'])
                if remote and remote.get('enable') is not False:
                    db.execute("INSERT INTO state(key,value) VALUES(?, '0') ON CONFLICT(key) DO NOTHING", (f"customer_enable:{customer['id']}",))
        self.sync_customer_enable(remote_records)
        records = remote_by_email
        try:
            online = set(self.remote.online())
        except RemoteError:
            online = None
        with self.db.transaction() as db:
            for local in db.execute("SELECT c.*,u.suspended owner_suspended FROM customers c JOIN users u ON u.id=c.user_id WHERE c.status='active'").fetchall():
                remote = records.get(local['email'])
                ids = {r[0] for r in db.execute('SELECT inbound_id FROM placements WHERE customer_id=?',(local['id'],))}
                pending = db.execute("SELECT 1 FROM operations WHERE customer_id=? AND status IN ('reserved','sent','uncertain')",(local['id'],)).fetchone()
                issue = ''
                if not remote:
                    issue='remote_missing'
                elif not pending:
                    # While an edit is in flight the remote copy is expected to differ.
                    expected = json.loads(local['payload'])
                    if local['owner_suspended']:
                        expected['enable'] = False
                    if not self._identity(local,remote):
                        issue='remote_identity'
                    elif set(remote['inboundIds'])!=ids or not self._matches(remote,expected):
                        issue='remote_changed'
                    if db.execute('SELECT 1 FROM state WHERE key=?',(f"customer_enable:{local['id']}",)).fetchone():
                        issue='suspension_pending'
                if issue in DRIFT_ISSUES:
                    # Snapshot of the 3X-UI copy so the admin can see exactly what changed.
                    db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(f"drift:{local['id']}",dumps(remote)))
                else:
                    db.execute('DELETE FROM state WHERE key=?',(f"drift:{local['id']}",))
                traffic = remote.get('traffic') if remote else None
                used = int(traffic.get('up',0))+int(traffic.get('down',0)) if isinstance(traffic,dict) else None
                if not remote and not pending:
                    db.execute("UPDATE customers SET status='deleted',issue='remote_missing',online=NULL,telemetry_at=?,remote_expiry=NULL WHERE id=?",(now(),local['id']))
                    db.execute('DELETE FROM state WHERE key=?',(f"customer_enable:{local['id']}",))
                    free_customer_name(db, local['id'])
                    audit(db,None,'customer_remote_missing',local['id'],'remote_missing')
                else:
                    db.execute('UPDATE customers SET issue=?,used=coalesce(?,used),online=?,telemetry_at=?,remote_expiry=? WHERE id=?',
                               (issue,used,int(local['email'] in online) if online is not None and remote else None,now(),remote.get('expiryTime') if remote else None,local['id']))
        for op in self.db.all("SELECT o.id,o.status,o.kind,o.created,c.email FROM operations o JOIN customers c ON c.id=o.customer_id WHERE o.status IN ('reserved','sent','uncertain')"):
            if op['status']=='reserved':
                # Crash before send: release, rather than running an old action after plan expiry.
                if op['created']<now()-300:
                    self.release(op['id'],'unsent_expired',unsent=True)
                continue
            if op['kind'] in ('create','update') and op['email'] not in remote_by_email:
                # Removed in 3X-UI while still being set up (or never applied): after a few
                # syncs, stop waiting, return the held quota and remove it from Relay.
                if self._bump_absent(op['id']) >= ABSENT_SYNCS:
                    self.abandon(op['id'],'remote_missing')
                    continue
            else:
                with self.db.transaction() as db:
                    db.execute('DELETE FROM state WHERE key=?',(f"absent:{op['id']}",))
            self.reconcile(op['id'])

    def repair(self, actor, oid):
        """Explicit admin retry of a verified existing identity; never re-creates an absent client."""
        if actor['role']!='admin':
            raise RuleError('forbidden',403)
        op=self.db.one('SELECT * FROM operations WHERE id=?',(oid,))
        if not op or op['status']!='uncertain':
            raise RuleError('invalid_operation',409)
        local=self.db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
        desired=json.loads(op['desired'])
        records=self.remote.clients()
        remote=next((r for r in records if r['email']==local['email']),None)
        allowed_ids=set(desired['inboundIds']) | set(desired.get('previousInboundIds',[]))
        if not remote or not set(remote['inboundIds']).issubset(allowed_ids):
            raise RuleError('remote_drift',409)
        identity_ok=self._identity(local,remote)
        if op['kind']=='update' and not identity_ok:
            identity_ok=(remote.get('email')==local['email'] and (not local['remote_id'] or remote.get('id')==local['remote_id'])
                         and self._matches(remote,desired['client']))
        if not identity_ok:
            raise RuleError('remote_drift',409)
        if op['kind']=='create' and not self._matches(remote,desired['client']):
            raise RuleError('remote_drift',409)
        if op['kind']=='update' and not (self._matches(remote,json.loads(local['payload'])) or self._matches(remote,desired['client'])):
            raise RuleError('remote_drift',409)
        with self.db.transaction() as db:
            changed=db.execute("UPDATE operations SET status='sent',updated=? WHERE id=? AND status='uncertain'",(now(),oid)).rowcount
            if not changed:
                raise RuleError('operation_conflict',409)
            audit(db,actor['id'],'repair',oid)
        try:
            missing=sorted(set(desired['inboundIds'])-set(remote['inboundIds']))
            extra=sorted(set(remote['inboundIds'])-set(desired['inboundIds']))
            if op['kind']=='delete':
                result=self.remote.delete(local['email'])
            elif op['kind']=='create' and missing:
                result=self.remote.attach(local['email'],missing)
            elif op['kind']=='update':
                result=self.remote.update(local['email'],desired['client'])
            else:
                result=self.remote.update(local['email'],desired['client'])
            if isinstance(result,dict) and result.get('nodePending'):
                self.uncertain(oid,'node_pending')
                return
            if op['kind']=='update' and missing:
                result=self.remote.attach(local['email'],missing)
                if isinstance(result,dict) and result.get('nodePending'):
                    self.uncertain(oid,'node_pending')
                    return
            if op['kind']=='update' and extra:
                result=self.remote.detach(local['email'],extra)
                if isinstance(result,dict) and result.get('nodePending'):
                    self.uncertain(oid,'node_pending')
                    return
            with self.db.transaction() as db:
                db.execute("UPDATE operations SET diagnostic='remote_ack' WHERE id=?",(oid,))
        except RemoteError as exc:
            self.uncertain(oid,exc.code)
        self.reconcile(oid)

    def retry_unsent(self, actor, old_id, new_id):
        """New atomic reservation for a creation proven to have failed before sending."""
        try:
            new_id=str(uuid.UUID(new_id))
        except (ValueError,AttributeError):
            raise RuleError('invalid_operation') from None
        fingerprint=hashlib.sha256(('retry:'+old_id).encode()).hexdigest()
        with self.db.transaction() as db:
            previous=db.execute('SELECT * FROM operations WHERE id=?',(new_id,)).fetchone()
            if previous:
                if previous['actor']!=actor['id'] or previous['request_hash']!=fingerprint:
                    raise RuleError('operation_conflict',409)
                return dict(previous)
            old=db.execute('SELECT * FROM operations WHERE id=?',(old_id,)).fetchone()
            if not old:
                raise RuleError('not_found',404)
            local=owned(db,actor,old['customer_id'])
            owner=db.execute('SELECT * FROM users WHERE id=?',(local['user_id'],)).fetchone()
            current=db.execute('SELECT * FROM users WHERE id=?',(actor['id'],)).fetchone()
            if owner['suspended'] or not current or current['suspended'] or db.execute("SELECT 1 FROM state WHERE key='write_lock'").fetchone():
                raise RuleError('forbidden',403)
            if old['status']!='failed' or old['kind']!='create' or local['status']!='failed':
                raise RuleError('reconcile_required',409)
            admin_owned=owner['role']=='admin'
            grant=None if admin_owned else active_grant(db,local['user_id'])
            desired=json.loads(old['desired'])
            ids=self._valid_inbounds(db,desired['inboundIds'])
            if not admin_owned and any(not db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?',(local['user_id'],iid)).fetchone() for iid in ids):
                raise RuleError('inbound_denied',403)
            expiry=desired['client']['expiryTime']
            if expiry>0 and expiry<=now()*1000:
                raise RuleError('invalid_expiry')
            charge=0 if admin_owned else desired['client']['totalGB']
            if grant and grant['sold']+grant['reserved']+charge>grant['allowance']:
                raise RuleError('insufficient_quota')
            gid=grant['id'] if grant else None
            db.execute("INSERT INTO operations(id,actor,customer_id,grant_id,kind,charge,request_hash,desired,status,created,updated) VALUES(?,?,?,?,'create',?,?,?,'reserved',?,?)",(new_id,actor['id'],local['id'],gid,charge,fingerprint,old['desired'],now(),now()))
            db.execute('UPDATE grants SET reserved=reserved+? WHERE id=?',(charge,gid))
            self._entry(db,local['user_id'],gid,local['id'],new_id,'reserve',charge,actor['id'])
            db.execute("UPDATE customers SET status='pending',issue='' WHERE id=?",(local['id'],))
            audit(db,actor['id'],'retry_unsent',local['id'])
            return dict(db.execute('SELECT * FROM operations WHERE id=?',(new_id,)).fetchone())

    # ----- Admin tools for customers that need attention -------------------

    # Fields copied from 3X-UI when the admin accepts its values.
    ACCEPT_FIELDS = ('totalGB','expiryTime','enable','comment','subId','group','password','auth','flow','security',
                     'secret','privateKey','publicKey','preSharedKey','keepAlive','adTag','forwardedPorts')

    def accept_remote(self, actor, customer_id):
        """Make Relay match a customer that was edited directly in 3X-UI."""
        if actor['role'] != 'admin':
            raise RuleError('forbidden',403)
        records = self.remote.clients()
        with self.db.transaction() as db:
            local = db.execute('SELECT * FROM customers WHERE id=?',(customer_id,)).fetchone()
            if not local or local['status'] != 'active':
                raise RuleError('not_found',404)
            if db.execute("SELECT 1 FROM operations WHERE customer_id=? AND status IN ('reserved','sent','uncertain')",(customer_id,)).fetchone():
                raise RuleError('reconcile_required',409)
            remote = next((r for r in records if r['email'] == local['email']),None)
            if not remote:
                raise RuleError('remote_missing',409)
            payload = json.loads(local['payload'])
            for field in self.ACCEPT_FIELDS:
                if field in remote:
                    payload[field] = remote[field]
            if remote.get('uuid'):
                payload['id'] = remote['uuid']
            if remote.get('allowedIPs'):
                payload['allowedIPs'] = json.loads(remote['allowedIPs']) if isinstance(remote['allowedIPs'],str) else remote['allowedIPs']
            known = {r[0] for r in db.execute('SELECT id FROM inbounds')}
            ids = sorted({i for i in remote['inboundIds'] if i in known})
            for field in ('id','subId','password','auth'):
                value = payload.get(field)
                if not value:
                    continue
                fingerprint = hashlib.sha256(str(value).encode()).hexdigest()
                claim = db.execute('SELECT customer_id FROM identities WHERE kind=? AND value=?',(field,fingerprint)).fetchone()
                if claim and claim[0] != customer_id:
                    raise RuleError('identity_conflict')
                db.execute('INSERT OR IGNORE INTO identities VALUES(?,?,?)',(field,fingerprint,customer_id))
            # Unlimited (0) cannot be stored as a package; keep the sold size then.
            package = payload.get('totalGB') if isinstance(payload.get('totalGB'),int) and payload['totalGB'] > 0 else local['package']
            db.execute('DELETE FROM placements WHERE customer_id=?',(customer_id,))
            db.executemany("INSERT INTO placements(customer_id,inbound_id,status) VALUES(?,?,'verified')",[(customer_id,i) for i in ids])
            db.execute("UPDATE customers SET payload=?,package=?,remote_id=?,uuid=?,sub_id=?,issue='',remote_expiry=? WHERE id=?",
                       (dumps(payload),package,remote['id'],payload.get('id'),payload['subId'],remote.get('expiryTime'),customer_id))
            db.execute('DELETE FROM state WHERE key=?',(f'drift:{customer_id}',))
            grant = db.execute('SELECT id FROM grants WHERE user_id=? ORDER BY id DESC LIMIT 1',(local['user_id'],)).fetchone()
            if grant:
                # Zero-amount note: a change made in 3X-UI is not charged to the reseller.
                db.execute("INSERT INTO ledger(user_id,grant_id,customer_id,kind,amount,actor,created) VALUES(?,?,?,'adjust',0,?,?)",
                           (local['user_id'],grant['id'],customer_id,actor['id'],now()))
            audit(db,actor['id'],'customer_accept_remote',dumps({'customer':customer_id,'old':local['package'],'new':package}))

    def force_remove(self, actor, customer_id):
        """Admin delete that works in every state: removes the client from 3X-UI if it
        is still there, cancels anything in progress and returns held quota."""
        if actor['role'] != 'admin':
            raise RuleError('forbidden',403)
        local = self.db.one('SELECT * FROM customers WHERE id=?',(customer_id,))
        if not local or local['status'] == 'deleted':
            raise RuleError('not_found',404)
        records = self.remote.clients()
        remote = next((r for r in records if r['email'] == local['email']),None)
        if remote:
            self.remote.delete(local['email'])
            if any(r['email'] == local['email'] for r in self.remote.clients()):
                raise RuleError('remote_rejected',502)
        with self.db.transaction() as db:
            for op in db.execute("SELECT * FROM operations WHERE customer_id=? AND status IN ('reserved','sent','uncertain')",(customer_id,)).fetchall():
                db.execute('UPDATE grants SET reserved=reserved-? WHERE id=?',(op['charge'],op['grant_id']))
                self._entry(db,local['user_id'],op['grant_id'],customer_id,op['id'],'release',op['charge'],actor['id'])
                db.execute("UPDATE operations SET status='failed',diagnostic='cancelled',updated=? WHERE id=?",(now(),op['id']))
            db.execute("UPDATE customers SET status='deleted',issue='',online=NULL WHERE id=?",(customer_id,))
            free_customer_name(db, customer_id)
            db.execute('DELETE FROM state WHERE key IN (?,?)',(f'drift:{customer_id}',f'customer_enable:{customer_id}'))
            audit(db,actor['id'],'customer_force_removed',customer_id,'ok' if remote else 'already_missing')

    def delete_reseller(self, actor, uid):
        """Delete a reseller and all of its customers. Returns how many customers failed."""
        if actor['role'] != 'admin':
            raise RuleError('forbidden',403)
        user = self.db.one("SELECT * FROM users WHERE id=? AND role='reseller' AND deleted=0",(uid,))
        if not user:
            raise RuleError('not_found',404)
        failed = 0
        for customer in self.db.all("SELECT id FROM customers WHERE user_id=? AND status!='deleted'",(uid,)):
            try:
                self.force_remove(actor,customer['id'])
            except (RemoteError,RuleError):
                failed += 1
        if failed:
            return failed
        with self.db.transaction() as db:
            # Ledger and audit rows are permanent, so the account is retired, not erased.
            # Renaming frees the username and tag for reuse.
            db.execute("UPDATE users SET deleted=1,suspended=1,username=?,tag=? WHERE id=?",
                       (f"deleted-{uid}-{user['username']}",f"deleted-{uid}-{user['tag']}",uid))
            db.execute('DELETE FROM permissions WHERE user_id=?',(uid,))
            db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            db.execute('UPDATE grants SET closed=1 WHERE user_id=?',(uid,))
            audit(db,actor['id'],'reseller_delete',user['username'])
        return 0

    # ----- Bulk inbound changes -------------------------------------------------

    BULK_ACTIONS = ('add', 'remove', 'move', 'enable', 'disable')

    def plan_bulk(self, actor, customer_ids, action, inbound_ids):
        """Work out, for each customer, the inbounds after the change, or why it is skipped.

        Nothing is changed here; this is the preview shown before applying.
        Returns a list of dicts: cid, email, owner, before, after, enable, skip (reason or '').
        """
        if action not in self.BULK_ACTIONS:
            raise RuleError('invalid_input')
        chosen = sorted({int(i) for i in inbound_ids})
        inbounds = {r['id']: r for r in self.db.all('SELECT id,name,protocol,available FROM inbounds')}
        if action in ('add', 'remove', 'move'):
            if not chosen or any(i not in inbounds for i in chosen):
                raise RuleError('no_inbound')
            if action == 'move' and len({inbounds[i]['protocol'] for i in chosen}) != 1:
                raise RuleError('mixed_protocols')
            if action == 'move' and len(chosen) > 1 and getattr(self.remote, 'single_inbound', False):
                raise RuleError('single_inbound')
        plan = []
        with self.db.transaction() as db:
            users = {r['id']: r for r in db.execute('SELECT id,username,role,suspended FROM users').fetchall()}
            pending = {r[0] for r in db.execute("SELECT customer_id FROM operations WHERE status IN ('reserved','sent','uncertain')")}
            for cid in dict.fromkeys(int(c) for c in customer_ids):
                row = db.execute('SELECT * FROM customers WHERE id=?', (cid,)).fetchone()
                if not row or (actor['role'] != 'admin' and row['user_id'] != actor['id']):
                    continue
                owner = users.get(row['user_id'])
                current = sorted(r[0] for r in db.execute('SELECT inbound_id FROM placements WHERE customer_id=?', (cid,)))
                payload = json.loads(row['payload'])
                item = {'cid': cid, 'email': row['email'], 'owner': owner['username'] if owner else '', 'before': current,
                        'after': current, 'enable': None, 'skip': ''}
                plan.append(item)
                protocols = {inbounds[i]['protocol'] for i in current if i in inbounds}
                admin_owned = owner and owner['role'] == 'admin'
                if row['status'] != 'active':
                    item['skip'] = 'bulk_not_active'
                elif row['issue']:
                    item['skip'] = 'bulk_attention'
                elif cid in pending:
                    item['skip'] = 'bulk_in_progress'
                elif owner and owner['suspended']:
                    item['skip'] = 'bulk_owner_suspended'
                elif not admin_owned and not db.execute('SELECT 1 FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>?',
                                                        (row['user_id'], now(), now())).fetchone():
                    item['skip'] = 'bulk_plan_expired'
                if item['skip']:
                    continue
                if action in ('enable', 'disable'):
                    item['enable'] = action == 'enable'
                    if bool(payload.get('enable', True)) == item['enable']:
                        item['skip'] = 'bulk_already'
                    continue
                if action == 'add':
                    usable = [i for i in chosen if not protocols or inbounds[i]['protocol'] in protocols]
                    if not protocols and len({inbounds[i]['protocol'] for i in usable}) > 1:
                        item['skip'] = 'mixed_protocols'
                        continue
                    if not usable:
                        item['skip'] = 'bulk_different_protocol'
                        continue
                    after = sorted(set(current) | set(usable))
                elif action == 'remove':
                    after = sorted(set(current) - set(chosen))
                    if after == current:
                        item['skip'] = 'bulk_not_on_inbound'
                        continue
                    if not after:
                        item['skip'] = 'bulk_last_inbound'
                        continue
                else:
                    if protocols and inbounds[chosen[0]]['protocol'] not in protocols:
                        item['skip'] = 'bulk_different_protocol'
                        continue
                    after = chosen
                if after == current:
                    item['skip'] = 'bulk_already'
                    continue
                if len(after) > 1 and getattr(self.remote, 'single_inbound', False):
                    item['skip'] = 'single_inbound'
                    continue
                if any(not inbounds[i]['available'] for i in set(after) - set(current)):
                    item['skip'] = 'inbound_unavailable'
                    continue
                if not admin_owned and any(not db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?', (row['user_id'], i)).fetchone()
                                           for i in set(after) - set(current)):
                    item['skip'] = 'bulk_inbound_denied'
                    continue
                item['after'] = after
        return plan

    def create_bulk_job(self, actor, action, inbound_ids, plan, summary):
        job_id = str(uuid.uuid4())
        items = [{**item, 'state': 'skipped' if item['skip'] else 'waiting', 'reason': item['skip']} for item in plan]
        skipped = sum(1 for i in items if i['state'] == 'skipped')
        with self.db.transaction() as db:
            db.execute("INSERT INTO jobs(id,actor,kind,summary,total,skipped,status,items,created,updated) VALUES(?,?,?,?,?,?,'running',?,?,?)",
                       (job_id, actor['id'], action, summary, len(items), skipped, dumps(items), now(), now()))
        return job_id

    def run_bulk_job(self, job_id):
        """Apply a bulk job customer by customer. Each change is a normal, verified edit."""
        job = self.db.one('SELECT * FROM jobs WHERE id=?', (job_id,))
        if not job or job['status'] != 'running':
            return
        actor = self.db.one('SELECT * FROM users WHERE id=?', (job['actor'],))
        items = json.loads(job['items'])
        for item in items:
            if item['state'] != 'waiting':
                continue
            form = {'inbounds': item['after']}
            if item.get('enable') is not None:
                form['enable'] = item['enable']
            try:
                op = self.reserve(actor, form, str(uuid.uuid4()), 'update', item['cid'])
                self.execute(op['id'])
                result = self.db.one('SELECT status,diagnostic FROM operations WHERE id=?', (op['id'],))
                if result['status'] == 'committed':
                    item['state'] = 'done'
                elif result['status'] in ('sent', 'uncertain'):
                    item['state'], item['reason'] = 'done', 'bulk_confirming'
                else:
                    item['state'], item['reason'] = 'failed', result['diagnostic'] or 'error'
            except (RuleError, RemoteError) as exc:
                item['state'], item['reason'] = 'failed', exc.code
            except Exception:
                item['state'], item['reason'] = 'failed', 'error'
            self._save_job(job_id, items, 'running')
        self._save_job(job_id, items, 'finished')
        done = sum(1 for i in items if i['state'] == 'done')
        failed = sum(1 for i in items if i['state'] == 'failed')
        with self.db.transaction() as db:
            audit(db, actor['id'], 'bulk_inbounds', dumps({'job': job_id, 'kind': job['kind'], 'summary': job['summary'],
                                                          'done': done, 'failed': failed, 'skipped': job['skipped']}))

    def _save_job(self, job_id, items, status):
        counts = {s: sum(1 for i in items if i['state'] == s) for s in ('done', 'skipped', 'failed')}
        with self.db.transaction() as db:
            db.execute('UPDATE jobs SET items=?,done=?,skipped=?,failed=?,status=?,updated=? WHERE id=?',
                       (dumps(items), counts['done'], counts['skipped'], counts['failed'], status, now(), job_id))

    def interrupt_jobs(self):
        """After a restart, mark jobs that were cut off; their remaining customers were not changed."""
        for job in self.db.all("SELECT id,items FROM jobs WHERE status='running'"):
            items = json.loads(job['items'])
            for item in items:
                if item['state'] == 'waiting':
                    item['state'], item['reason'] = 'skipped', 'bulk_interrupted'
            self._save_job(job['id'], items, 'interrupted')
