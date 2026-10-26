"""Turn raw audit and ledger rows into plain sentences people can read.

Each sentence is a list of (text, strong) parts so templates can bold names
while still autoescaping everything.
"""
import json
from .db import now
from .domain import GIB, DRIFT_ISSUES

# Internal steps nobody needs to read; the admin can still find them in technical details.
TECHNICAL = {'customer_create_reserved', 'customer_update_reserved', 'customer_delete_reserved',
             'operation_release', 'remote_operation', 'worker', 'request', 'reconcile', 'repair'}

# Groups for the activity filter.
CATEGORIES = {
    'customers': ('customer_%', 'bulk_%'),
    'plan': ('grant%', 'reseller_%'),
    'signin': ('login', 'logout', 'password_%', 'bootstrap'),
    'system': ('inbound_removed', 'sync', 'restore_unlock', 'customer_remote_missing', 'customer_removed_during_setup'),
}
CATEGORY_LABELS = {'customers': 'Customers', 'plan': 'Plans & resellers', 'signin': 'Sign-ins', 'system': 'System'}

# Events a reseller sees even when the system (not they) caused them, if it is about their customer.
SYSTEM_CUSTOMER_EVENTS = ('customer_remote_missing', 'customer_removed_during_setup', 'customer_force_removed',
                          'customer_accept_remote', 'customer_create', 'customer_update', 'customer_delete')


def gb(value):
    if value is None:
        return '—'
    text = f'{value / GIB:,.2f}'.rstrip('0').rstrip('.')
    return text + ' GB'


def parse(subject):
    if isinstance(subject, str) and subject.startswith('{'):
        try:
            return json.loads(subject)
        except ValueError:
            return {}
    return {}


def lookups(db, events):
    """Fetch names for every customer, user and operation the events mention, in a few queries."""
    customer_ids, op_ids = set(), set()
    for e in events:
        data = parse(e['subject'])
        if data.get('customer'):
            customer_ids.add(int(data['customer']))
        elif str(e['subject']).isdigit():
            customer_ids.add(int(e['subject']))
        elif len(str(e['subject'])) == 36:
            op_ids.add(e['subject'])
    ops = {}
    if op_ids:
        marks = ','.join('?' * len(op_ids))
        ops = {r['id']: r for r in db.all(f'SELECT id,customer_id,kind FROM operations WHERE id IN ({marks})', tuple(op_ids))}
        customer_ids |= {r['customer_id'] for r in ops.values()}
    customers = {}
    if customer_ids:
        marks = ','.join('?' * len(customer_ids))
        customers = {r['id']: r['email'] for r in db.all(f'SELECT id,email FROM customers WHERE id IN ({marks})', tuple(customer_ids))}
    users = {r['id']: r['username'] for r in db.all('SELECT id,username FROM users')}
    grants = {}
    grant_ids = {int(e['subject']) for e in events if e['action'] in ('grant', 'grant_adjust') and str(e['subject']).isdigit()}
    if grant_ids:
        marks = ','.join('?' * len(grant_ids))
        grants = {r['id']: r for r in db.all(f'SELECT id,user_id,allowance,expires FROM grants WHERE id IN ({marks})', tuple(grant_ids))}
    return {'customers': customers, 'users': users, 'ops': ops, 'grants': grants}


def _who(e, names):
    if not e['actor']:
        return ('Relay', True)
    if e['actor'] == names.get('viewer'):
        return ('You', True)
    return (names['users'].get(e['actor'], 'someone'), True)


def _customer(e, names):
    data = parse(e['subject'])
    if data.get('customer'):
        cid = int(data['customer'])
    elif str(e['subject']).isdigit():
        cid = int(e['subject'])
    else:
        op = names['ops'].get(e['subject'])
        cid = op['customer_id'] if op else None
    return names['customers'].get(cid, 'a customer'), cid


def describe(e, names, date):
    """Return dict(parts, tone, icon, technical, link) for one audit row."""
    action, outcome = e['action'], e['outcome']
    who = _who(e, names)
    tone, icon, link, parts = 'info', 'dot', None, None
    data = parse(e['subject'])
    name, cid = _customer(e, names)
    if action.startswith('customer_') or action in ('retry_unsent', 'remote_operation', 'reconcile', 'repair'):
        link = f'/customers/{cid}' if cid else None
    if action == 'login':
        if outcome == 'ok':
            parts, tone, icon = [who, (' signed in', False)], 'info', 'login'
            if who[0] == 'You':
                parts = [('You signed in', True)]
        else:
            parts, tone, icon = [('Failed sign-in attempt', False)], 'warning', 'lock'
    elif action == 'logout':
        parts, icon = [who, (' signed out', False)], 'login'
    elif action == 'password_change':
        parts, icon = [who, (' changed their password', False)], 'lock'
    elif action == 'password_recovery':
        parts, icon, tone = [('Password reset from the server for ', False), (names['users'].get(int(e['subject']) if str(e['subject']).isdigit() else 0, 'a user'), True)], 'lock', 'warning'
    elif action == 'bootstrap':
        parts, icon = [('Admin account created', False)], 'user'
    elif action == 'reseller_create':
        parts, tone, icon = [who, (' added reseller ', False), (names['users'].get(int(e['subject']), 'a reseller'), True)], 'success', 'user'
        link = f"/resellers/{e['subject']}"
    elif action == 'reseller_update':
        parts, icon = [who, (' updated reseller ', False), (names['users'].get(int(e['subject']), 'a reseller'), True)], 'user'
        link = f"/resellers/{e['subject']}"
    elif action == 'reseller_delete':
        parts, tone, icon = [who, (' deleted reseller ', False), (str(e['subject']), True), (' and their customers', False)], 'danger', 'trash'
    elif action in ('grant', 'grant_adjust'):
        g = names['grants'].get(int(e['subject'])) if str(e['subject']).isdigit() else None
        owner = names['users'].get(g['user_id'], 'a reseller') if g else 'a reseller'
        verb = ' started a new plan for ' if action == 'grant' else ' changed the plan of '
        parts = [who, (verb, False), (owner, True)]
        if g:
            parts += [(': ', False), (gb(g['allowance']), True), (' until ' + date(g['expires']), False)]
        tone, icon = 'success', 'plan'
    elif action == 'customer_create':
        parts, tone, icon = [who, (' created customer ', False), (name, True)], 'success', 'plus'
    elif action == 'customer_update':
        parts, icon = [who, (' edited customer ', False), (name, True)], 'edit'
    elif action == 'customer_delete':
        parts, tone, icon = [who, (' deleted customer ', False), (name, True)], 'danger', 'trash'
    elif action == 'customer_force_removed':
        parts, tone, icon = [who, (' removed customer ', False), (name, True)], 'danger', 'trash'
        if outcome == 'already_missing':
            parts.append((' (it was already gone from 3X-UI)', False))
    elif action == 'customer_accept_remote':
        parts, tone, icon = [who, (' accepted the 3X-UI values for ', False), (name, True)], 'success', 'check'
        if data.get('old') and data.get('new') and data['old'] != data['new']:
            parts += [(f" (data limit {gb(data['old'])} → {gb(data['new'])})", False)]
    elif action == 'customer_remote_missing':
        parts, tone, icon = [('Customer ', False), (name, True), (' was deleted in 3X-UI, so Relay removed it too', False)], 'warning', 'trash'
    elif action == 'customer_removed_during_setup':
        parts, tone, icon = [('Customer ', False), (name, True), (' was not found in 3X-UI while being set up. Relay removed it and returned the data to the plan', False)], 'warning', 'trash'
    elif action == 'inbound_removed':
        count = data.get('customers', 0)
        parts = [('Inbound ', False), (data.get('name') or f"#{data.get('id', '?')}", True), (' was deleted in 3X-UI and removed here', False)]
        if count:
            parts.append((f"; {count} customer{'s' if count != 1 else ''} lost this inbound", False))
        tone, icon = 'warning', 'server'
    elif action == 'bulk_inbounds':
        parts, icon = [who, (' changed many customers: ', False), (str(data.get('summary', '')), True),
                       (f" — {data.get('done', 0)} changed, {data.get('skipped', 0)} skipped, {data.get('failed', 0)} failed", False)], 'server'
        tone = 'danger' if data.get('failed') else 'success'
        link = f"/jobs/{data.get('job')}" if data.get('job') else None
    elif action == 'settings_update':
        parts, icon = [who, (' changed the ', False), (str(e['subject']), True), (' setting', False)], 'key'
    elif action == 'sync':
        parts, icon = [who, (' synced with 3X-UI', False)], 'sync'
    elif action == 'restore_unlock':
        parts, icon, tone = [('Sales unlocked after a backup restore', False)], 'lock', 'warning'
    elif action == 'retry_unsent':
        parts, icon = [who, (' retried creating ', False), (name, True)], 'sync'
    elif action.endswith('_reserved'):
        kind = action.split('_')[1]
        parts, icon = [who, ({'create': ' started creating ', 'update': ' started editing ', 'delete': ' started deleting '}.get(kind, ' changed '), False), (name, True)], 'dot'
    elif action == 'remote_operation':
        parts, tone, icon = [('3X-UI did not confirm a change for ', False), (name, True), (f" ({outcome.replace('_', ' ')})", False)], 'warning', 'sync'
    elif action == 'operation_release':
        parts, icon = [('Cancelled an unsent change for ', False), (name, True)], 'dot'
    elif action in ('reconcile', 'repair'):
        parts, icon = [who, (' re-checked a change for ', False) if action == 'reconcile' else (' re-sent a change for ', False), (name, True)], 'sync'
    elif action == 'worker':
        parts, tone, icon = [('Background sync hit an error', False)], 'danger', 'sync'
    elif action == 'request':
        parts, tone, icon = [who, (f" hit an error: {outcome.replace('_', ' ')}", False)], 'warning', 'dot'
    else:
        parts = [who, (' ' + action.replace('_', ' '), False)]
    if outcome not in ('ok', 'already_missing') and action not in ('login', 'remote_operation', 'request', 'worker', 'customer_remote_missing', 'customer_removed_during_setup', 'operation_release'):
        tone = 'danger'
        parts = parts + [(' — failed', False)]
    return {'parts': parts, 'tone': tone, 'icon': icon, 'link': link, 'created': e['created'],
            'technical': action in TECHNICAL, 'raw': f"{action} · {e['subject']} · {outcome}"}


def feed(db, user, date, limit=15, offset=0, category='', reseller=None, include_technical=False):
    """Readable activity rows visible to this user, newest first."""
    where, args = [], []
    if user['role'] != 'admin':
        # Own actions, plus system events about the reseller's own customers.
        marks = ','.join('?' * len(SYSTEM_CUSTOMER_EVENTS))
        where.append(f"""(a.actor=? OR (a.action IN ({marks}) AND EXISTS(SELECT 1 FROM customers c WHERE c.user_id=? AND
                     c.id=CAST(CASE WHEN json_valid(a.subject) THEN json_extract(a.subject,'$.customer') ELSE a.subject END AS INTEGER)))
                     OR (a.action='inbound_removed' AND json_valid(a.subject) AND EXISTS(SELECT 1 FROM json_each(a.subject,'$.resellers') WHERE value=?)))""")
        args += [user['id'], *SYSTEM_CUSTOMER_EVENTS, user['id'], user['id']]
    elif reseller:
        where.append("(a.actor=? OR EXISTS(SELECT 1 FROM customers c WHERE c.user_id=? AND c.id=CAST(a.subject AS INTEGER)) OR (a.action IN ('grant','grant_adjust') AND EXISTS(SELECT 1 FROM grants g WHERE g.user_id=? AND g.id=CAST(a.subject AS INTEGER))) OR (a.action IN ('reseller_create','reseller_update') AND a.subject=?))")
        args += [reseller, reseller, reseller, str(reseller)]
    if not include_technical:
        marks = ','.join('?' * len(TECHNICAL))
        where.append(f'a.action NOT IN ({marks})')
        args += sorted(TECHNICAL)
        # Hide noisy failed-login and request-error rows from resellers.
        if user['role'] != 'admin':
            where.append("NOT (a.action='login' AND a.outcome!='ok')")
    if category in CATEGORIES:
        where.append('(' + ' OR '.join('a.action LIKE ?' for _ in CATEGORIES[category]) + ')')
        args += list(CATEGORIES[category])
    sql = 'SELECT a.* FROM audit a' + (' WHERE ' + ' AND '.join(where) if where else '') + ' ORDER BY a.id DESC LIMIT ? OFFSET ?'
    rows = db.all(sql, tuple(args) + (limit, offset))
    names = lookups(db, rows)
    names['viewer'] = user['id']
    return [describe(e, names, date) for e in rows]


LEDGER_TEXT = {
    'grant': ('New plan', 'success'), 'adjust': ('Plan changed', 'info'), 'reserve': ('Put on hold', 'muted'),
    'release': ('Hold returned', 'muted'), 'sale': ('Sold', 'info'), 'refund': ('Returned to plan', 'success'),
}


def ledger_rows(db, rows, reseller_view=False):
    """Readable data movements. Resellers don't see the internal hold/return pairs."""
    customers = {r['id']: r['email'] for r in db.all('SELECT id,email FROM customers')}
    result = []
    for r in rows:
        if reseller_view and r['kind'] in ('reserve', 'release'):
            continue
        label, tone = LEDGER_TEXT.get(r['kind'], (r['kind'], 'info'))
        if r['kind'] == 'adjust' and not r['amount'] and r.get('customer_id'):
            label, tone = 'Accepted 3X-UI values (no charge)', 'muted'
        sign = '+' if r['kind'] in ('grant', 'refund', 'release') or r['kind'] == 'adjust' and r['amount'] > 0 else '−' if r['kind'] in ('sale', 'reserve') or r['amount'] < 0 else ''
        amount = abs(r['amount'])
        result.append({'label': label, 'tone': tone, 'amount': (sign + gb(amount)) if amount else '—',
                       'customer': customers.get(r.get('customer_id')), 'customer_id': r.get('customer_id'),
                       'created': r['created'], 'by': r.get('actor_name') or 'Relay', 'status': r.get('action_status', 'ok')})
    return result


def customer_state(c, p, current=None):
    """One status for a customer: (key, tone). Key is a message key."""
    current = current or now()
    expiry = c.get('remote_expiry') or p.get('expiryTime') or 0
    if c['status'] == 'deleted':
        return 'deleted', 'muted'
    if c['status'] == 'pending':
        return 'setting_up', 'warning'
    if c['status'] == 'failed':
        return 'failed', 'danger'
    if c.get('issue') and c['issue'] != 'suspension_pending':
        return 'needs_attention', 'danger'
    if not p.get('enable', True):
        return 'disabled', 'muted'
    if expiry > 0 and expiry <= current * 1000:
        return 'expired', 'danger'
    if c.get('used') is not None and c.get('package') and c['used'] >= c['package']:
        return 'out_of_data', 'danger'
    if expiry > 0 and expiry <= (current + 3 * 86400) * 1000:
        return 'expiring', 'warning'
    return 'active', 'success'


def drift_diff(c, p, snapshot, inbound_names, date):
    """Rows of (field, relay value, 3X-UI value) for fields that differ."""
    if not snapshot:
        return []
    rows = []
    def fmt_expiry(v):
        if not v:
            return 'Never'
        if v < 0:
            return f'{-v // 86400000} days from first use'
        return date(v / 1000)
    checks = [
        ('Data limit', gb(p.get('totalGB')), gb(snapshot.get('totalGB')) if snapshot.get('totalGB') else 'Unlimited'),
        ('Expires', fmt_expiry(p.get('expiryTime')), fmt_expiry(snapshot.get('expiryTime'))),
        ('Enabled', 'Yes' if p.get('enable', True) else 'No', 'Yes' if snapshot.get('enable', True) else 'No'),
        ('Note', p.get('comment') or '—', snapshot.get('comment') or '—'),
        ('Subscription ID', c.get('sub_id') or '—', snapshot.get('subId') or '—'),
        ('UUID', c.get('uuid') or '—', snapshot.get('uuid') or '—'),
        ('Group', p.get('group') or '—', snapshot.get('group') or '—'),
    ]
    first_use_started = (p.get('expiryTime') or 0) < 0 and (snapshot.get('expiryTime') or 0) > 0
    for field, relay, remote in checks:
        if field == 'Expires' and first_use_started:
            continue
        if field == 'UUID' and not c.get('uuid'):
            continue
        if relay != remote:
            rows.append((field, relay, remote))
    local_ids = sorted(c.get('placement_ids') or [])
    remote_ids = sorted(snapshot.get('inboundIds') or [])
    if local_ids != remote_ids:
        def names(ids):
            return ', '.join(inbound_names.get(i, f'#{i}') for i in ids) or 'None'
        rows.append(('Inbounds', names(local_ids), names(remote_ids)))
    return rows


def relative(ts, current=None):
    current = current or now()
    delta = current - int(ts or 0)
    if not ts:
        return 'never'
    if delta < 0:
        delta = -delta
        future = True
    else:
        future = False
    for limit, unit, size in ((60, 'second', 1), (3600, 'minute', 60), (86400, 'hour', 3600), (86400 * 60, 'day', 86400), (10**12, 'month', 86400 * 30)):
        if delta < limit:
            n = max(1, delta // size) if unit != 'second' else delta
            if unit == 'second' and n < 10:
                return 'just now'
            text = f"{n} {unit}{'s' if n != 1 else ''}"
            return ('in ' + text) if future else (text + ' ago')
    return ''


def is_drift(issue):
    return issue in DRIFT_ISSUES
