import base64
import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from decimal import Decimal, InvalidOperation
from .db import now, audit
from .security import password_hash
from .xui import RemoteError

GIB = 1024**3
PROTOCOLS = {'vmess', 'vless', 'trojan', 'shadowsocks', 'hysteria', 'wireguard', 'amneziawg', 'mtproto', 'tuic'}


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


def owned(db, actor, customer_id):
    row = db.execute('SELECT * FROM customers WHERE id=?', (customer_id,)).fetchone()
    if not row or (actor['role'] != 'admin' and row['user_id'] != actor['id']):
        raise RuleError('not_found', 404)
    return row


class Service:
    def __init__(self, database, remote):
        self.db, self.remote = database, remote

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

    def _valid_inbounds(self, db, ids):
        ids = sorted(set(int(x) for x in ids))
        if not ids or len(ids) > 32:
            raise RuleError('inbound_denied')
        for iid in ids:
            ib = db.execute('SELECT * FROM inbounds WHERE id=?', (iid,)).fetchone()
            if not ib or not ib['available'] or ib['protocol'] not in PROTOCOLS or ib['synced'] < now()-600:
                raise RuleError('inbound_unavailable')
        return ids

    def sync_inbounds(self):
        rows = self.remote.inbounds()
        with self.db.transaction() as db:
            db.execute('UPDATE inbounds SET available=0')
            for ib in rows:
                db.execute('INSERT INTO inbounds VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,port=excluded.port,available=excluded.available,metadata=excluded.metadata,synced=excluded.synced',
                           (ib['id'], str(ib.get('remark','')), ib['protocol'], ib.get('port',0), int(ib.get('enable',False)), dumps(ib), now()))
            db.execute("INSERT INTO state VALUES('connection',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now()),))
        return rows

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
            if not customer and actor['role'] != 'reseller':
                raise RuleError('forbidden',403)
            owner = db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
            # An admin must be able to remove customers of an expired or suspended
            # reseller. A delete charges nothing, so it needs no active plan.
            admin_delete = kind == 'delete' and actor['role'] == 'admin'
            if admin_delete:
                grant = db.execute('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
                if not grant:
                    raise RuleError('plan_expired',403)
            else:
                grant = active_grant(db, uid)  # Admin uses the same sales invariant.
                if owner['suspended']:
                    raise RuleError('forbidden',403)
            if customer and (customer['status'] != 'active' or customer['issue']):
                raise RuleError('reconcile_required',409)
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
                if not admin_delete and not db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?',(uid,iid)).fetchone():
                    raise RuleError('inbound_denied',403)
            if kind != 'delete':
                self._valid_inbounds(db, ids)
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
                               enable=bool(form.get('enable',payload.get('enable',True))),group=owner['tag'],
                               reset=0,resetDay=0,resetMax=0,trafficReset='never',trafficResetDay=1)
                charge = max(0,amount - (customer['package'] if customer else 0))
            else:
                amount, charge = customer['package'], 0
            if grant['sold']+grant['reserved']+charge > grant['allowance']:
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
                       (operation_id,actor['id'],customer_id,grant['id'],kind,charge,request_hash,desired,now(),now()))
            db.execute('UPDATE grants SET reserved=reserved+? WHERE id=?',(charge,grant['id']))
            self._entry(db, uid, grant['id'], customer_id, operation_id,'reserve',charge,actor['id'])
            audit(db,actor['id'], 'customer_'+kind+'_reserved',customer_id)
            return dict(db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone())

    @staticmethod
    def _entry(db,uid,gid,cid,oid,kind,amount,actor):
        db.execute('INSERT INTO ledger(user_id,grant_id,customer_id,operation_id,kind,amount,actor,created) VALUES(?,?,?,?,?,?,?,?)', (uid,gid,cid,oid,kind,amount,actor,now()))

    def _identity(self, local, remote):
        payload = json.loads(local['payload'])
        return (remote.get('email') == local['email'] and remote.get('subId') == local['sub_id']
                and (not local['remote_id'] or remote.get('id') == local['remote_id'])
                and (not local['uuid'] or remote.get('uuid') == local['uuid'])
                and remote.get('group') == payload['group'])

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
            elif not remote or not self._identity(local,remote) or not self._matches(remote,json.loads(local['payload'])) or set(remote['inboundIds']) != set(desired.get('previousInboundIds',desired['inboundIds'])):
                raise RuleError('remote_drift')
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
            can_send=bool(original and owner) and (admin_delete or not owner['suspended'] and not original['closed'] and original['expires']>now() and permitted)
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
                result = self.remote.delete(local['email'])
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
                if added:
                    result=self.remote.attach(local['email'],added)
                    if isinstance(result,dict) and result.get('nodePending'):
                        self.uncertain(operation_id,'node_pending')
                        return
                if removed:
                    result=self.remote.detach(local['email'],removed)
                    if isinstance(result,dict) and result.get('nodePending'):
                        self.uncertain(operation_id,'node_pending')
                        return
            with self.db.transaction() as db:
                db.execute("UPDATE operations SET diagnostic='remote_ack' WHERE id=? AND status IN ('sent','uncertain')",(operation_id,))
        except RemoteError as exc:
            self.uncertain(operation_id,exc.code)
        self.reconcile(operation_id)

    def uncertain(self, oid, code):
        with self.db.transaction() as db:
            db.execute("UPDATE operations SET status='uncertain',diagnostic=?,updated=? WHERE id=? AND status IN ('sent','uncertain')",(code,now(),oid))
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
        if any(actual.get(k) != desired.get(k) for k in keys):
            return False
        for field in ('id','password','auth'):
            remote_value=actual.get('uuid') if field=='id' and 'uuid' in actual else actual.get(field)
            if desired.get(field) and desired[field]!=remote_value:
                return False
        # First-use duration can legitimately have become an absolute timestamp.
        wanted, got = desired['expiryTime'], actual.get('expiryTime',0)
        return got == wanted or wanted < 0 and got > 0

    def reconcile(self, oid):
        op = self.db.one('SELECT * FROM operations WHERE id=?',(oid,))
        if not op or op['status'] not in ('sent','uncertain'):
            return
        local = self.db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
        desired = json.loads(op['desired'])
        try:
            records, inbounds = self.remote.clients(), self.remote.inbounds()
            remote = next((r for r in records if r['email']==local['email']),None)
            by_id = {i['id']:i for i in inbounds}
            outcomes = {}
            affected_ids=set(desired['inboundIds']) | set(desired.get('previousInboundIds',[]))
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
                complete = remote is None and all(v=='verified' for v in outcomes.values())
            else:
                complete = bool(remote and remote.get('email')==local['email'] and (not local['remote_id'] or remote.get('id')==local['remote_id']) and self._matches(remote,desired['client'])
                                and set(remote['inboundIds'])==set(desired['inboundIds']) and all(v=='verified' for v in outcomes.values()))
            # Offline backing nodes cannot be proven applied just from master's stored configuration.
            if any(by_id.get(i,{}).get('nodeId') for i in affected_ids) and op['diagnostic']!='remote_ack':
                complete = False
            with self.db.transaction() as db:
                current = db.execute('SELECT status FROM operations WHERE id=?',(oid,)).fetchone()[0]
                if current not in ('sent','uncertain'):
                    return
                for iid,status in outcomes.items():
                    db.execute('UPDATE placements SET status=? WHERE customer_id=? AND inbound_id=?',(status,local['id'],iid))
                if not complete:
                    db.execute("UPDATE operations SET status='uncertain',diagnostic=CASE WHEN diagnostic='node_pending' THEN diagnostic ELSE 'reconcile_required' END,updated=? WHERE id=?",(now(),oid))
                    return
                refund = int(desired.get('refund',0)) if op['kind']=='update' else 0
                changed = db.execute('UPDATE grants SET reserved=reserved-?,sold=sold+?-? WHERE id=? AND sold>=?',
                                     (op['charge'],op['charge'],refund,op['grant_id'],refund)).rowcount
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
                           ('deleted' if op['kind']=='delete' else 'active',dumps(payload),payload['totalGB'],remote['id'] if remote else local['remote_id'],payload.get('id'),payload['subId'],'',local['id']))
                db.execute("UPDATE operations SET status='committed',diagnostic='',updated=? WHERE id=?",(now(),oid))
                audit(db,op['actor'],'customer_'+op['kind'],local['id'])
        except RemoteError as exc:
            self.uncertain(oid,exc.code)

    def refresh(self):
        self.sync_inbounds()
        remote_records = self.remote.clients()
        remote_by_email = {r['email']: r for r in remote_records}
        with self.db.transaction() as db:
            for customer in db.execute("SELECT c.id,c.email FROM customers c JOIN users u ON u.id=c.user_id WHERE c.status!='deleted' AND u.suspended=1").fetchall():
                remote = remote_by_email.get(customer['email'])
                if remote and remote.get('enable') is not False:
                    db.execute("INSERT INTO state(key,value) VALUES(?, '0') ON CONFLICT(key) DO NOTHING", (f"customer_enable:{customer['id']}",))
        self.sync_customer_enable(remote_records)
        records = {r['email']:r for r in remote_records}
        try:
            online = set(self.remote.online())
        except RemoteError:
            online = None
        with self.db.transaction() as db:
            for local in db.execute("SELECT c.*,u.suspended owner_suspended FROM customers c JOIN users u ON u.id=c.user_id WHERE c.status='active'").fetchall():
                remote = records.get(local['email'])
                ids = {r[0] for r in db.execute('SELECT inbound_id FROM placements WHERE customer_id=?',(local['id'],))}
                issue = ''
                if not remote:
                    issue='remote_missing'
                else:
                    expected = json.loads(local['payload'])
                    if local['owner_suspended']:
                        expected['enable'] = False
                    if not self._identity(local,remote) or set(remote['inboundIds'])!=ids or not self._matches(remote,expected):
                        issue='remote_drift'
                    if db.execute('SELECT 1 FROM state WHERE key=?',(f"customer_enable:{local['id']}",)).fetchone():
                        issue='suspension_pending'
                traffic = remote.get('traffic') if remote and not issue else None
                used = int(traffic.get('up',0))+int(traffic.get('down',0)) if isinstance(traffic,dict) else None
                pending = db.execute("SELECT 1 FROM operations WHERE customer_id=? AND status IN ('reserved','sent','uncertain')",(local['id'],)).fetchone()
                if not remote and not pending:
                    db.execute("UPDATE customers SET status='deleted',issue='remote_missing',online=NULL,telemetry_at=?,remote_expiry=NULL WHERE id=?",(now(),local['id']))
                    audit(db,None,'customer_remote_missing',local['id'],'remote_missing')
                else:
                    db.execute('UPDATE customers SET issue=?,used=?,online=?,telemetry_at=?,remote_expiry=? WHERE id=?',
                               (issue,used,int(local['email'] in online) if online is not None and not issue else None,now(),remote.get('expiryTime') if remote else None,local['id']))
        for op in self.db.all("SELECT id,status FROM operations WHERE status IN ('reserved','sent','uncertain')"):
            if op['status']=='reserved':
                # Crash before send: release, rather than running an old action after plan expiry.
                row=self.db.one('SELECT created FROM operations WHERE id=?',(op['id'],))
                if row['created']<now()-300:
                    self.release(op['id'],'unsent_expired',unsent=True)
            else:
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
            grant=active_grant(db,local['user_id'])
            desired=json.loads(old['desired'])
            ids=self._valid_inbounds(db,desired['inboundIds'])
            if any(not db.execute('SELECT 1 FROM permissions WHERE user_id=? AND inbound_id=?',(local['user_id'],iid)).fetchone() for iid in ids):
                raise RuleError('inbound_denied',403)
            expiry=desired['client']['expiryTime']
            if expiry>0 and expiry<=now()*1000:
                raise RuleError('invalid_expiry')
            charge=desired['client']['totalGB']
            if grant['sold']+grant['reserved']+charge>grant['allowance']:
                raise RuleError('insufficient_quota')
            db.execute("INSERT INTO operations(id,actor,customer_id,grant_id,kind,charge,request_hash,desired,status,created,updated) VALUES(?,?,?,?,'create',?,?,?,'reserved',?,?)",(new_id,actor['id'],local['id'],grant['id'],charge,fingerprint,old['desired'],now(),now()))
            db.execute('UPDATE grants SET reserved=reserved+? WHERE id=?',(charge,grant['id']))
            self._entry(db,local['user_id'],grant['id'],local['id'],new_id,'reserve',charge,actor['id'])
            db.execute("UPDATE customers SET status='pending',issue='' WHERE id=?",(local['id'],))
            audit(db,actor['id'],'retry_unsent',local['id'])
            return dict(db.execute('SELECT * FROM operations WHERE id=?',(new_id,)).fetchone())
