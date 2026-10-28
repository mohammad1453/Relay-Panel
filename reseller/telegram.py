"""Telegram bot: alerts, approvals and quick commands for the admin and resellers.

How it fits together
* The admin saves a bot token on the Settings page. Each person (admin or
  reseller) links their own Telegram chat from the Account page with a
  one-time code; only linked chats can use the bot.
* Messages go through the ``outbox`` table and are sent with retries, so a
  slow or blocked Telegram never slows the panel down.
* A background thread long-polls Telegram for commands and button presses
  (no webhook or HTTPS needed) and, about once a minute, looks for things
  worth telling people: new audit events, customers that need attention,
  plans running out, 3X-UI going down, and the daily report.
* Every button runs the same checks as the panel (ownership, roles, plans).
"""
import hashlib
import html
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from . import activity
from .db import now, audit
from .domain import RuleError, GIB, DRIFT_ISSUES, display_name
from .links import label_links
from .xui import RemoteError

log = logging.getLogger('relay.telegram')

# What each person is told about, with defaults. Shown as switches on the Account page.
PREFS = {
    'admin': [
        ('attention', 'Things that need me (stuck or failed changes, sales paused)', True),
        ('changes', 'Customers changed in 3X-UI and accepted automatically, with Undo', True),
        ('requests', 'Requests from resellers for more data', True),
        ('sync', '3X-UI unreachable and back again', True),
        ('plans', 'Reseller plans almost used up or about to expire', True),
        ('removals', 'Customers or inbounds deleted in 3X-UI', True),
        ('sales', 'Every customer a reseller creates or deletes', False),
        ('jobs', 'Bulk changes finished', True),
        ('logins', 'Each admin sign-in', False),
        ('daily', 'Daily report', True),
    ],
    'reseller': [
        ('plans', 'My plan: almost used up, about to expire, renewed', True),
        ('requests', 'Answers to my requests for more data', True),
        ('removals', 'My customers deleted in 3X-UI', True),
        ('changes', 'My customers changed by the admin in 3X-UI', True),
        ('jobs', 'My bulk changes finished', True),
        ('daily', 'Daily summary: customers expiring or out of data', True),
    ],
}
SCAN_SECONDS = 60
STUCK_SECONDS = 180


def esc(value):
    return html.escape(str(value), quote=False)


def gb(value):
    return activity.gb(value)


class TelegramError(Exception):
    def __init__(self, description='error', code=None):
        self.code = code
        super().__init__(description)


DEFAULT_API = 'https://api.telegram.org'


class TelegramAPI:
    def __init__(self, token, proxy='', transport=None, base=DEFAULT_API):
        self.client = httpx.Client(base_url=f'{base}/bot{token}/', timeout=httpx.Timeout(40, connect=10),
                                   proxy=proxy or None, transport=transport, trust_env=False)

    def call(self, method, **params):
        try:
            response = self.client.post(method, json={k: v for k, v in params.items() if v is not None})
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError('network: ' + type(exc).__name__) from None
        if not isinstance(data, dict) or not data.get('ok'):
            raise TelegramError(str((data or {}).get('description', 'error'))[:200], (data or {}).get('error_code'))
        return data.get('result')

    def close(self):
        self.client.close()


class Telegram:
    def __init__(self, db, service, config, subscription_link_for, transport=None):
        self.db, self.service, self.config = db, service, config
        self.subscription_link_for = subscription_link_for
        self.transport = transport
        self.zone = ZoneInfo(config.timezone)
        self._api, self._api_key = None, None
        self._last_scan = 0
        self.stop = threading.Event()

    # ----- Settings ----------------------------------------------------------------

    def setting(self, key, default=''):
        row = self.db.one('SELECT value FROM state WHERE key=?', ('setting:tg_'+key,))
        return row['value'] if row else default

    def save_settings(self, actor, **values):
        with self.db.transaction() as db:
            for key, value in values.items():
                if value is None:
                    continue
                db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('setting:tg_'+key, str(value)))
            audit(db, actor['id'], 'settings_update', 'Telegram')
        self._api_key = None   # rebuild the client with the new token / proxy / API address

    def enabled(self):
        return self.setting('enabled') == '1' and bool(self.setting('token'))

    def api(self):
        key = (self.setting('token'), self.setting('proxy'), self.setting('api').rstrip('/'))
        if not key[0]:
            return None
        if key != self._api_key:
            if self._api:
                self._api.close()
            self._api, self._api_key = TelegramAPI(key[0], key[1], self.transport, key[2] or DEFAULT_API), key
        return self._api

    def _state(self, key, default=''):
        row = self.db.one('SELECT value FROM state WHERE key=?', (key,))
        return row['value'] if row else default

    def _set_state(self, key, value):
        with self.db.transaction() as db:
            db.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))

    def status(self):
        return {'enabled': self.enabled(), 'username': self._state('tg:username'), 'error': self._state('tg:error'),
                'last_ok': int(self._state('tg:last_ok', '0') or 0)}

    def check(self):
        """Ask Telegram who the bot is; remembers the bot username for the connect link."""
        api = self.api()
        if not api:
            raise TelegramError('No bot token saved.')
        me = api.call('getMe')
        self._set_state('tg:username', me.get('username', ''))
        return me

    # ----- Linking chats to Relay users -------------------------------------------

    def new_code(self, user):
        code = secrets.token_hex(4).upper()
        with self.db.transaction() as db:
            db.execute('DELETE FROM telegram_codes WHERE user_id=? OR expires<?', (user['id'], now()))
            db.execute('INSERT INTO telegram_codes VALUES(?,?,?)', (code, user['id'], now()+900))
        return code

    def link(self, chat_id, name, code):
        with self.db.transaction() as db:
            row = db.execute('SELECT * FROM telegram_codes WHERE code=? AND expires>?', (code.strip().upper(), now())).fetchone()
            if not row:
                return None
            db.execute('DELETE FROM telegram_codes WHERE code=?', (row['code'],))
            db.execute('DELETE FROM telegram_links WHERE chat_id=? OR user_id=?', (chat_id, row['user_id']))
            db.execute("INSERT INTO telegram_links(user_id,chat_id,name,prefs,linked) VALUES(?,?,?,'{}',?)", (row['user_id'], chat_id, name[:100], now()))
            audit(db, row['user_id'], 'telegram_linked', name[:100])
        return self.db.one('SELECT * FROM users WHERE id=?', (row['user_id'],))

    def unlink(self, user):
        with self.db.transaction() as db:
            if db.execute('DELETE FROM telegram_links WHERE user_id=?', (user['id'],)).rowcount:
                audit(db, user['id'], 'telegram_unlinked')

    def linked(self, user_id):
        return self.db.one('SELECT * FROM telegram_links WHERE user_id=?', (user_id,))

    def chat_user(self, chat_id):
        return self.db.one('''SELECT u.* FROM telegram_links t JOIN users u ON u.id=t.user_id
                              WHERE t.chat_id=? AND u.suspended=0 AND u.deleted=0''', (chat_id,))

    def prefs(self, user, link=None):
        link = link or self.linked(user['id'])
        stored = json.loads(link['prefs']) if link and link['prefs'] else {}
        return {key: bool(stored.get(key, default)) for key, _, default in PREFS[user['role']]}

    def set_prefs(self, user, values):
        prefs = {key: key in values for key, _, _ in PREFS[user['role']]}
        with self.db.transaction() as db:
            db.execute('UPDATE telegram_links SET prefs=? WHERE user_id=?', (json.dumps(prefs), user['id']))

    # ----- Outbox ------------------------------------------------------------------

    def enqueue(self, chat_id, text, buttons=None, dedupe=None):
        with self.db.transaction() as db:
            db.execute('INSERT OR IGNORE INTO outbox(chat_id,text,buttons,dedupe,created) VALUES(?,?,?,?,?)',
                       (chat_id, text[:4000], json.dumps(buttons or []), dedupe, now()))

    def _targets(self, role=None, user_id=None, pref=None):
        sql = 'SELECT t.*,u.role,u.id uid FROM telegram_links t JOIN users u ON u.id=t.user_id WHERE u.suspended=0 AND u.deleted=0'
        args = []
        if role:
            sql += ' AND u.role=?'
            args.append(role)
        if user_id is not None:
            sql += ' AND u.id=?'
            args.append(user_id)
        result = []
        for row in self.db.all(sql, args):
            if pref and not self.prefs({'id': row['uid'], 'role': row['role']}, row).get(pref, False):
                continue
            result.append(row)
        return result

    def notify(self, text, pref, role=None, user_id=None, buttons=None, dedupe=None):
        """Queue a message for everyone of a role (or one user) who wants this kind of news."""
        if not self.enabled():
            return
        for target in self._targets(role, user_id, pref):
            self.enqueue(target['chat_id'], text, buttons, f'{dedupe}:{target["chat_id"]}' if dedupe else None)

    def flush(self, limit=20):
        api = self.api()
        if not api:
            return 0
        sent = 0
        for row in self.db.all('SELECT * FROM outbox WHERE sent IS NULL AND attempts<5 ORDER BY id LIMIT ?', (limit,)):
            try:
                api.call('sendMessage', chat_id=row['chat_id'], text=row['text'], parse_mode='HTML', disable_web_page_preview=True,
                         reply_markup=self._markup(json.loads(row['buttons'])))
                with self.db.transaction() as db:
                    db.execute('UPDATE outbox SET sent=?,attempts=attempts+1,error=? WHERE id=?', (now(), '', row['id']))
                sent += 1
            except TelegramError as exc:
                # 403: the person blocked the bot; 400: a bad message. Neither gets better by retrying.
                give_up = exc.code in (400, 403)
                with self.db.transaction() as db:
                    db.execute('UPDATE outbox SET attempts=?,error=? WHERE id=?', (5 if give_up else row['attempts']+1, str(exc), row['id']))
                if not give_up:
                    raise
        with self.db.transaction() as db:
            db.execute('DELETE FROM outbox WHERE created<?', (now()-30*86400,))
        return sent

    @staticmethod
    def _markup(buttons):
        if not buttons:
            return None
        rows = []
        for row in buttons:
            rows.append([{'text': b[0], 'url': b[1][4:]} if b[1].startswith('url:') else {'text': b[0], 'callback_data': b[1]} for b in row])
        return {'inline_keyboard': rows}

    def panel_url(self, path='/'):
        origin = self.config.origin.rstrip('/')
        host = origin.split('://', 1)[-1].split(':', 1)[0].split('/', 1)[0]
        if host in ('localhost', '127.0.0.1', '[::1]'):
            return ''   # Telegram cannot open the user's own computer
        return origin + self.config.base_path + path

    def _open(self, label, path):
        url = self.panel_url(path)
        return [label, 'url:'+url] if url else None

    # ----- Main loop ---------------------------------------------------------------

    def run_forever(self):
        backoff = 5
        while not self.stop.is_set():
            if not self.enabled():
                self.stop.wait(10)
                continue
            try:
                self.step(poll_timeout=25)
                backoff = 5
                self._set_state('tg:last_ok', now())
                if self._state('tg:error'):
                    self._set_state('tg:error', '')
            except TelegramError as exc:
                self._set_state('tg:error', str(exc))
                self.stop.wait(backoff)
                backoff = min(300, backoff * 2)
            except Exception:
                log.exception('telegram loop error')
                self.stop.wait(backoff)

    def step(self, poll_timeout=0):
        """One round: read commands and button presses, run the checks, send queued messages."""
        api = self.api()
        if not api:
            return
        if not self._state('tg:username'):
            self.check()
            api.call('deleteWebhook')
        offset = int(self._state('tg:offset', '0') or 0)
        updates = api.call('getUpdates', offset=offset or None, timeout=poll_timeout,
                           allowed_updates=['message', 'callback_query']) or []
        for update in updates:
            self._set_state('tg:offset', update['update_id'] + 1)
            try:
                self.handle(update)
            except (TelegramError, RemoteError, RuleError) as exc:
                log.warning('telegram update failed: %s', exc)
        if time.time() - self._last_scan >= SCAN_SECONDS:
            self._last_scan = time.time()
            self.scan()
        self.flush()

    # ----- Incoming messages ---------------------------------------------------------

    def reply(self, chat_id, text, buttons=None):
        self.api().call('sendMessage', chat_id=chat_id, text=text[:4000], parse_mode='HTML', disable_web_page_preview=True,
                        reply_markup=self._markup(buttons))

    def handle(self, update):
        if 'callback_query' in update:
            return self.handle_button(update['callback_query'])
        message = update.get('message') or {}
        chat = message.get('chat') or {}
        text = (message.get('text') or '').strip()
        if chat.get('type') != 'private' or not text.startswith('/'):
            return
        chat_id = chat['id']
        command, _, rest = text.partition(' ')
        command = command.split('@', 1)[0].lower()
        rest = rest.strip()
        if command == '/start' and rest:
            name = ' '.join(x for x in (chat.get('first_name'), chat.get('last_name')) if x) or chat.get('username') or ''
            user = self.link(chat_id, name, rest)
            if not user:
                return self.reply(chat_id, '❌ This code is wrong or expired. Make a new one on the Account page in Relay.')
            return self.reply(chat_id, f'✅ Connected to Relay as <b>{esc(user["username"])}</b>.\n\n' + self.help_text(user))
        user = self.chat_user(chat_id)
        if not user:
            return self.reply(chat_id, '👋 This is the Relay bot. To connect, open Relay → <b>Account</b> → <b>Connect Telegram</b>, then press the link it shows.')
        handlers = {'/help': self.cmd_help, '/start': self.cmd_help, '/status': self.cmd_status, '/find': self.cmd_find,
                    '/requests': self.cmd_requests, '/report': self.cmd_report, '/sync': self.cmd_sync}
        if user['role'] == 'admin':
            handlers.update({'/attention': self.cmd_attention, '/resellers': self.cmd_resellers})
        else:
            handlers.update({'/customers': self.cmd_customers, '/request': self.cmd_request})
        handler = handlers.get(command)
        if not handler:
            return self.reply(chat_id, 'I don\'t know that command.\n\n' + self.help_text(user))
        try:
            handler(chat_id, user, rest)
        except RuleError as exc:
            self.reply(chat_id, '❌ ' + esc(self.t(exc.code)))
        except RemoteError as exc:
            self.reply(chat_id, '❌ ' + esc(self.t(exc.code)))

    @staticmethod
    def t(code):
        from .i18n import translator
        return translator()(code)

    def help_text(self, user):
        if user['role'] == 'admin':
            return ('<b>Commands</b>\n/status – panel and 3X-UI at a glance\n/attention – what needs you now\n'
                    '/requests – reseller requests waiting for you\n/find <i>name</i> – find a customer\n'
                    '/resellers – plans of all resellers\n/report – today\'s report\n/sync – sync with 3X-UI now')
        return ('<b>Commands</b>\n/status – your plan\n/customers – customers expiring or out of data\n'
                '/find <i>name</i> – find a customer and get their link\n'
                '/request <i>GB</i> [<i>days</i>] [<i>note</i>] – ask the admin for more data\n/requests – your requests\n'
                '/report – your summary')

    def cmd_help(self, chat_id, user, rest):
        self.reply(chat_id, self.help_text(user))

    def _plan(self, uid):
        return self.db.one('SELECT * FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>? ORDER BY id DESC LIMIT 1',
                           (uid, now(), now()))

    def _date(self, ts):
        return datetime.fromtimestamp(ts, self.zone).strftime('%Y-%m-%d %H:%M') if ts else '—'

    def _days_left(self, ts):
        days = int((ts - now()) // 86400)
        return 'today' if days < 1 else f'{days} day' + ('s' if days != 1 else '')

    def cmd_status(self, chat_id, user, rest):
        if user['role'] == 'admin':
            info = json.loads(self._state('xui_info', '{}') or '{}')
            ok = int(self._state('sync_ok', '0') or 0)
            failing = bool(self._state('sync_error')) and int(self._state('sync_error').split(':')[-1] or 0) >= ok
            counts = self.db.one(f"""SELECT (SELECT count(*) FROM users WHERE role='reseller' AND deleted=0) resellers,
                (SELECT count(*) FROM customers WHERE status='active') customers,
                (SELECT count(*) FROM customers WHERE status='active' AND online=1 AND telemetry_at>{now()-600}) online,
                (SELECT count(*) FROM requests WHERE status='pending') requests""")
            lines = [('🔴 3X-UI unreachable' if failing else '🟢 3X-UI connected') +
                     (f" · v{esc(info['version'])}" if info.get('version') else '') +
                     (f" · synced {activity.relative(ok)}" if ok else ''),
                     f"👥 {counts['resellers']} resellers · {counts['customers']} customers ({counts['online']} online)",
                     f"⚠️ Needs attention: {len(self.attention_items())}", f"📨 Requests waiting: {counts['requests']}"]
            buttons = [x for x in [self._open('Open dashboard', '/')] if x]
            return self.reply(chat_id, '\n'.join(lines), [buttons] if buttons else None)
        plan = self._plan(user['id'])
        counts = self.db.one(f"""SELECT count(*) n, coalesce(sum(CASE WHEN online=1 AND telemetry_at>{now()-600} THEN 1 ELSE 0 END),0) online
                                FROM customers WHERE user_id=? AND status='active'""", (user['id'],))
        if plan:
            left = max(0, plan['allowance']-plan['sold']-plan['reserved'])
            text = (f"📦 <b>Your plan</b>\nLeft to sell: <b>{gb(left)}</b> of {gb(plan['allowance'])}\n"
                    f"Expires: {self._date(plan['expires'])} ({self._days_left(plan['expires'])})")
        else:
            text = '📦 You have no active plan. Use /request to ask the admin for one.'
        text += f"\n👥 Customers: {counts['n']} ({counts['online']} online)"
        self.reply(chat_id, text)

    # ----- Customers ----------------------------------------------------------------

    def customer_card(self, c, viewer):
        payload = json.loads(c['payload'])
        state, _ = activity.customer_state(c, payload)
        ms = c.get('remote_expiry') or payload.get('expiryTime') or 0
        if ms > 0:
            expiry = f'{self._date(ms/1000)} ({self._days_left(ms/1000)})' if ms/1000 > now() else 'expired'
        elif ms < 0:
            expiry = f'{-ms//86400000} days after first use'
        else:
            expiry = 'never'
        names = [r['name'] for r in self.db.all('SELECT i.name FROM placements p JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=?', (c['id'],))]
        lines = [f"👤 <b>{esc(display_name(c['email']))}</b> · {esc(self.t(state))}",
                 f"📊 {gb(c['used'] or 0)} used of {gb(c['package'])}", f"⏳ Expires: {expiry}",
                 f"🔌 {esc(', '.join(names) or 'no inbound')}"]
        if viewer['role'] == 'admin':
            owner = self.db.one('SELECT username FROM users WHERE id=?', (c['user_id'],))
            lines.append(f"🧑‍💼 {esc(owner['username'] if owner else '?')}")
        if payload.get('comment'):
            lines.append(f"📝 {esc(payload['comment'])}")
        sub = self.subscription_link_for(c['sub_id']) if c['status'] == 'active' else ''
        if sub:
            lines.append(f"🔗 <code>{esc(sub)}</code>")
        buttons = []
        if c['status'] == 'active':
            buttons.append(['🔗 Links', f"lnk:{c['id']}"])
            buttons.append(['⏸ Turn off', f"off:{c['id']}"] if payload.get('enable', True) else ['▶️ Turn on', f"on:{c['id']}"])
        rows = [buttons] if buttons else []
        if viewer['role'] == 'admin' and c['issue'] in DRIFT_ISSUES:
            rows.append([['✅ Accept 3X-UI values', f"acc:{c['id']}"], ['🗑 Remove', f"rm:{c['id']}"]])
        opener = self._open('Open in Relay', f"/customers/{c['id']}")
        if opener:
            rows.append([opener])
        return '\n'.join(lines), rows

    def _customer(self, user, cid):
        row = self.db.one('SELECT * FROM customers WHERE id=?', (cid,))
        if not row or (user['role'] != 'admin' and row['user_id'] != user['id']):
            raise RuleError('not_found', 404)
        return row

    def cmd_find(self, chat_id, user, rest):
        if not rest:
            return self.reply(chat_id, 'Write a name after /find, for example <code>/find ali</code>.')
        sql = "SELECT * FROM customers WHERE status!='deleted' AND (email LIKE ? OR json_extract(payload,'$.comment') LIKE ?)"
        args = ['%'+rest[:60]+'%']*2
        if user['role'] != 'admin':
            sql += ' AND user_id=?'
            args.append(user['id'])
        rows = self.db.all(sql + ' ORDER BY id DESC LIMIT 6', args)
        if not rows:
            return self.reply(chat_id, f'No customer matches “{esc(rest)}”.')
        for c in rows[:5]:
            text, buttons = self.customer_card(c, user)
            self.reply(chat_id, text, buttons)
        if len(rows) > 5:
            self.reply(chat_id, 'More customers match; type more of the name.')

    def cmd_customers(self, chat_id, user, rest):
        rows = self.db.all("SELECT * FROM customers WHERE user_id=? AND status='active' ORDER BY id", (user['id'],))
        soon, empty = [], []
        for c in rows:
            p = json.loads(c['payload'])
            ms = c['remote_expiry'] or p.get('expiryTime') or 0
            if 0 < ms <= (now()+3*86400)*1000:
                soon.append(f"• {esc(display_name(c['email']))} – " + ('expired' if ms <= now()*1000 else self._days_left(ms/1000)))
            if c['used'] is not None and c['used'] >= c['package']:
                empty.append(f"• {esc(display_name(c['email']))}")
        text = f"👥 <b>{len(rows)} customers</b>\n\n⏳ <b>Expiring within 3 days</b>\n" + ('\n'.join(soon) or 'none') + \
               "\n\n📭 <b>Out of data</b>\n" + ('\n'.join(empty) or 'none')
        self.reply(chat_id, text)

    def send_links(self, chat_id, user, cid):
        c = self._customer(user, cid)
        remote = self.service.remote
        record = next((r for r in remote.clients() if r['email'] == c['email']), None)
        if c['status'] != 'active' or not record:
            raise RuleError('remote_missing', 409)
        inbounds = self.db.all('SELECT i.id,i.name,i.protocol,i.port,i.metadata FROM placements p JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=? ORDER BY i.id', (cid,))
        items = label_links([u for u in remote.links(c['email']) if not u.startswith(('http://', 'https://'))], inbounds, display_name(c['email']))
        parts = [f"🔗 <b>{esc(display_name(c['email']))}</b>"]
        sub = self.subscription_link_for(c['sub_id'])
        if sub:
            parts.append(f"Subscription:\n<code>{esc(sub)}</code>")
        for item in items:
            parts.append(f"<b>{esc(item['title'])}</b>\n<code>{esc(item['uri'])}</code>")
        if len(parts) == 1:
            parts.append('No links yet.')
        chunk = ''
        for part in parts:
            if len(chunk) + len(part) > 3800:
                self.reply(chat_id, chunk)
                chunk = ''
            chunk += part + '\n\n'
        self.reply(chat_id, chunk)

    # ----- Requests ----------------------------------------------------------------------

    def request_text(self, req):
        owner = self.db.one('SELECT username FROM users WHERE id=?', (req['user_id'],))
        plan = self._plan(req['user_id'])
        text = (f"📨 <b>{esc(owner['username'] if owner else '?')}</b> asks for <b>{gb(req['volume'])}</b>"
                + (f" and <b>{req['days']} more days</b>" if req['days'] else '') + '.')
        if req['note']:
            text += f"\n📝 {esc(req['note'])}"
        if plan:
            text += f"\nPlan now: {gb(plan['sold']+plan['reserved'])} used of {gb(plan['allowance'])}, ends {self._date(plan['expires'])}."
        else:
            text += '\nNo active plan now; approving starts a new one.'
        return text

    def request_buttons(self, req):
        rows = [[['✅ Approve', f"rqy:{req['id']}"], ['❌ Decline', f"rqn:{req['id']}"]]]
        opener = self._open('Change amount in Relay', '/attention')
        if opener:
            rows.append([opener])
        return rows

    def cmd_requests(self, chat_id, user, rest):
        if user['role'] == 'admin':
            rows = self.db.all("SELECT * FROM requests WHERE status='pending' ORDER BY id")
            if not rows:
                return self.reply(chat_id, '📭 No requests are waiting.')
            for req in rows[:10]:
                self.reply(chat_id, self.request_text(req), self.request_buttons(req))
            return
        rows = self.db.all('SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 5', (user['id'],))
        if not rows:
            return self.reply(chat_id, 'You have no requests. Ask with <code>/request 100 30</code> (100 GB, 30 more days).')
        icon = {'pending': '⏳', 'approved': '✅', 'declined': '❌'}
        self.reply(chat_id, '\n'.join(f"{icon.get(r['status'], '•')} {gb(r['volume'])}" + (f" +{r['days']} days" if r['days'] else '')
                                      + f" – {r['status']}" + (f" ({esc(r['answer'])})" if r['answer'] else '') for r in rows))

    def cmd_request(self, chat_id, user, rest):
        parts = rest.split()
        if not parts:
            return self.reply(chat_id, 'Write the amount: <code>/request 100</code> for 100 GB, or <code>/request 100 30 busy month</code> for 100 GB and 30 more days.')
        amount, rest_parts = parts[0], parts[1:]
        days = int(rest_parts.pop(0)) if rest_parts and rest_parts[0].isdigit() else 0
        self.service.create_request(user, amount, days, ' '.join(rest_parts))
        self.reply(chat_id, '✅ Request sent. You will get a message when the admin answers.')

    # ----- Admin overviews ---------------------------------------------------------------

    def attention_items(self):
        items = []
        for c in self.db.all("""SELECT c.*,u.username owner FROM customers c JOIN users u ON u.id=c.user_id
                                WHERE c.status='active' AND c.issue IN ('remote_changed','remote_identity','remote_drift')"""):
            snapshot = self.db.one('SELECT value FROM state WHERE key=?', (f"drift:{c['id']}",))
            items.append(('drift', c, snapshot['value'] if snapshot else ''))
        for c in self.db.all("SELECT c.*,u.username owner FROM customers c JOIN users u ON u.id=c.user_id WHERE c.status='failed'"):
            items.append(('failed', c, ''))
        for o in self.db.all("""SELECT o.*,c.email,c.user_id,u.username owner FROM operations o JOIN customers c ON c.id=o.customer_id
                                JOIN users u ON u.id=c.user_id WHERE o.status IN ('sent','uncertain') AND o.created<?""", (now()-STUCK_SECONDS,)):
            items.append(('stuck', o, ''))
        return items

    def attention_message(self, kind, row, snapshot):
        owner = esc(row['owner'])
        if kind == 'drift':
            names = {r['id']: r['name'] for r in self.db.all('SELECT id,name FROM inbounds')}
            c = dict(row)
            c['placement_ids'] = [r['inbound_id'] for r in self.db.all('SELECT inbound_id FROM placements WHERE customer_id=?', (c['id'],))]
            diff = activity.drift_diff(c, json.loads(c['payload']), json.loads(snapshot) if snapshot else None, names, self._date)
            text = f"⚠️ <b>{esc(display_name(c['email']))}</b> ({owner}) was changed directly in 3X-UI."
            if diff:
                text += '\n' + '\n'.join(f"• {esc(f)}: {esc(a)} → <b>{esc(b)}</b>" for f, a, b in diff)
            text += '\nRelay paused it until you decide.'
            buttons = [[['✅ Accept 3X-UI values', f"acc:{c['id']}"], ['🗑 Remove', f"rm:{c['id']}"]]]
            path = f"/customers/{c['id']}"
        elif kind == 'failed':
            text = f"❌ Creating <b>{esc(display_name(row['email']))}</b> ({owner}) failed before it reached 3X-UI. No data was used."
            buttons = [[['🗑 Remove', f"rm:{row['id']}"]]]
            path = f"/customers/{row['id']}"
        else:
            text = (f"⏳ A change to <b>{esc(display_name(row['email']))}</b> ({owner}) is not confirmed by 3X-UI after "
                    f"{max(1, (now()-row['created'])//60)} min." + (' A node has not confirmed it yet.' if row['diagnostic'] == 'node_pending' else ''))
            buttons = [[['🔄 Check again', f"chk:{row['id']}"]]]
            path = '/attention'
        opener = self._open('Open in Relay', path)
        if opener:
            buttons.append([opener])
        return text, buttons

    def cmd_attention(self, chat_id, user, rest):
        items = self.attention_items()
        pending = self.db.all("SELECT * FROM requests WHERE status='pending' ORDER BY id")
        locked = self.service.sales_locked()
        if locked:
            text, buttons = self.lock_message()
            self.reply(chat_id, text, buttons)
        if not items and not pending and not locked:
            return self.reply(chat_id, '✅ All clear. Nothing needs you.')
        for kind, row, snapshot in items[:10]:
            self.reply(chat_id, *self.attention_message(kind, row, snapshot))
        for req in pending[:5]:
            self.reply(chat_id, self.request_text(req), self.request_buttons(req))

    def cmd_resellers(self, chat_id, user, rest):
        rows = self.db.all(f"""SELECT u.id,u.username,u.suspended,(SELECT count(*) FROM customers c WHERE c.user_id=u.id AND c.status='active') customers,
                              g.allowance,g.sold,g.reserved,g.expires FROM users u
                              LEFT JOIN grants g ON g.id=(SELECT id FROM grants WHERE user_id=u.id AND closed=0 AND starts<={now()} AND expires>{now()} ORDER BY id DESC LIMIT 1)
                              WHERE u.role='reseller' AND u.deleted=0 ORDER BY u.username""")
        if not rows:
            return self.reply(chat_id, 'No resellers yet.')
        lines = []
        for r in rows:
            if r['suspended']:
                plan = 'suspended'
            elif r['allowance']:
                pct = round((r['sold']+r['reserved'])*100/r['allowance'])
                plan = f"{gb(r['sold']+r['reserved'])}/{gb(r['allowance'])} ({pct}%), {self._days_left(r['expires'])} left"
            else:
                plan = 'no active plan'
            lines.append(f"• <b>{esc(r['username'])}</b> – {plan} · {r['customers']} customers")
        self.reply(chat_id, '🧑‍💼 <b>Resellers</b>\n' + '\n'.join(lines))

    def cmd_sync(self, chat_id, user, rest):
        self.service.refresh(wait=15)
        self.reply(chat_id, '🔄 Synced with 3X-UI.')

    def cmd_report(self, chat_id, user, rest):
        self.reply(chat_id, self.report_text(user))

    def report_text(self, user):
        day = now() - 86400
        if user['role'] == 'admin':
            r = self.db.one(f"""SELECT (SELECT count(*) FROM customers WHERE status='active') customers,
                (SELECT count(*) FROM customers WHERE created>{day} AND status!='failed') new_customers,
                (SELECT coalesce(sum(amount),0) FROM ledger WHERE kind='sale' AND created>{day}) sold,
                (SELECT count(*) FROM requests WHERE status='pending') requests,
                (SELECT count(*) FROM grants g JOIN users u ON u.id=g.user_id WHERE u.deleted=0 AND g.closed=0 AND g.expires BETWEEN {now()} AND {now()+3*86400}) ending""")
            return (f"📊 <b>Relay report</b> · {self._date(now())[:10]}\n"
                    f"👥 Customers: {r['customers']} (+{r['new_customers']} in 24 h)\n💾 Sold in 24 h: {gb(r['sold'])}\n"
                    f"⚠️ Needs attention: {len(self.attention_items())}\n📨 Requests waiting: {r['requests']}\n"
                    f"⏳ Reseller plans ending within 3 days: {r['ending']}")
        plan = self._plan(user['id'])
        rows = self.db.all("SELECT * FROM customers WHERE user_id=? AND status='active'", (user['id'],))
        soon = [c for c in rows if 0 < (c['remote_expiry'] or json.loads(c['payload']).get('expiryTime') or 0) <= (now()+3*86400)*1000]
        empty = [c for c in rows if c['used'] is not None and c['used'] >= c['package']]
        text = f"📊 <b>Your summary</b> · {self._date(now())[:10]}\n"
        if plan:
            text += f"📦 Left to sell: {gb(max(0, plan['allowance']-plan['sold']-plan['reserved']))}, plan ends {self._date(plan['expires'])}\n"
        else:
            text += '📦 No active plan.\n'
        text += f"👥 Customers: {len(rows)}\n⏳ Expiring within 3 days: " + (', '.join(esc(display_name(c['email'])) for c in soon[:15]) or 'none')
        text += '\n📭 Out of data: ' + (', '.join(esc(display_name(c['email'])) for c in empty[:15]) or 'none')
        return text

    # ----- Buttons -------------------------------------------------------------------------

    def handle_button(self, query):
        chat_id = (query.get('message') or {}).get('chat', {}).get('id')
        message_id = (query.get('message') or {}).get('message_id')
        data = str(query.get('data', ''))
        user = self.chat_user(chat_id) if chat_id else None
        api = self.api()
        if not user:
            return api.call('answerCallbackQuery', callback_query_id=query['id'], text='Connect this chat to Relay first.', show_alert=True)
        action, _, arg = data.partition(':')
        try:
            note = self.run_button(chat_id, user, action, arg, message_id)
        except (RuleError, RemoteError) as exc:
            return api.call('answerCallbackQuery', callback_query_id=query['id'], text='❌ ' + self.t(exc.code)[:180], show_alert=True)
        api.call('answerCallbackQuery', callback_query_id=query['id'], text=(note or 'Done')[:180])

    def _done(self, chat_id, message_id, note):
        """Replace the buttons under a handled alert with the outcome, so nobody presses twice."""
        if message_id:
            try:
                self.api().call('editMessageReplyMarkup', chat_id=chat_id, message_id=message_id,
                                reply_markup={'inline_keyboard': [[{'text': note, 'callback_data': 'noop:'}]]})
            except TelegramError:
                pass

    def run_button(self, chat_id, user, action, arg, message_id):
        admin = user['role'] == 'admin'
        if action == 'noop':
            return ''
        if not arg.isdigit() and action != 'chk':
            raise RuleError('invalid_input')
        if action == 'acc':
            if not admin:
                raise RuleError('forbidden', 403)
            self.service.accept_remote(user, int(arg))
            self._done(chat_id, message_id, '✅ Accepted')
            return 'Relay now uses the 3X-UI values.'
        if action == 'rm':
            self._customer(user, int(arg))
            if message_id:
                self.api().call('editMessageReplyMarkup', chat_id=chat_id, message_id=message_id, reply_markup=self._markup(
                    [[['⚠️ Yes, remove it', f'rmy:{arg}'], ['Cancel', f'rmn:{arg}']]]))
            return 'Press "Yes, remove it" to confirm.'
        if action == 'rmn':
            self._done(chat_id, message_id, 'Kept')
            return 'Nothing was removed.'
        if action == 'rmy':
            if admin:
                self.service.force_remove(user, int(arg))
            else:
                op = self.service.reserve(user, {}, str(uuid.uuid4()), 'delete', int(arg))
                self.service.execute(op['id'])
            self._done(chat_id, message_id, '🗑 Removed')
            return 'Customer removed.'
        if action in ('on', 'off'):
            self._customer(user, int(arg))
            op = self.service.reserve(user, {'enable': action == 'on'}, str(uuid.uuid4()), 'update', int(arg))
            self.service.execute(op['id'])
            self._done(chat_id, message_id, '▶️ Turned on' if action == 'on' else '⏸ Turned off')
            return 'Turned on.' if action == 'on' else 'Turned off.'
        if action == 'lnk':
            self.send_links(chat_id, user, int(arg))
            return ''
        if action == 'und':
            if not admin:
                raise RuleError('forbidden', 403)
            self.service.undo_remote(user, int(arg))
            self._done(chat_id, message_id, '↩️ Undone')
            return 'The old values are back in 3X-UI.'
        if action in ('rqy', 'rqn'):
            if not admin:
                raise RuleError('forbidden', 403)
            self.service.decide_request(user, int(arg), action == 'rqy')
            self._done(chat_id, message_id, '✅ Approved' if action == 'rqy' else '❌ Declined')
            return 'Approved; the plan was topped up.' if action == 'rqy' else 'Declined.'
        if action == 'chk':
            if not admin:
                raise RuleError('forbidden', 403)
            self.service.reconcile(arg)
            status = (self.db.one('SELECT status FROM operations WHERE id=?', (arg,)) or {}).get('status')
            if status == 'committed':
                self._done(chat_id, message_id, '✅ Confirmed')
                return 'Confirmed by 3X-UI.'
            return 'Still not confirmed. Relay keeps checking.'
        raise RuleError('invalid_input')

    # ----- Watching for news ----------------------------------------------------------------

    def scan(self):
        """Turn what happened since the last look into messages."""
        self.scan_audit()
        self.scan_attention()
        self.scan_plans()
        self.scan_sync()
        self.scan_daily()

    def scan_audit(self):
        cursor = self._state('tg:audit')
        if not cursor:
            # First run: start from now instead of replaying the whole history.
            last = self.db.one('SELECT coalesce(max(id),0) n FROM audit')['n']
            return self._set_state('tg:audit', last)
        rows = self.db.all('SELECT a.*,u.username,u.role FROM audit a LEFT JOIN users u ON u.id=a.actor WHERE a.id>? ORDER BY a.id LIMIT 200', (int(cursor),))
        for e in rows:
            try:
                self.audit_event(e)
            except Exception:
                log.exception('telegram audit event %s', e['id'])
            self._set_state('tg:audit', e['id'])

    def _customer_owner(self, cid):
        return self.db.one('SELECT c.*,u.username owner,u.role owner_role FROM customers c JOIN users u ON u.id=c.user_id WHERE c.id=?', (cid,))

    def audit_event(self, e):
        action, subject = e['action'], e['subject']
        data = activity.parse(subject)
        key = f"audit:{e['id']}"
        if action in ('customer_remote_missing', 'customer_removed_during_setup') and str(subject).isdigit():
            c = self._customer_owner(int(subject))
            if not c:
                return
            name = esc(display_name(c['email']))
            why = 'was deleted in 3X-UI, so Relay removed it too' if action == 'customer_remote_missing' else \
                  'was not found in 3X-UI while being set up; Relay removed it and returned the data to the plan'
            self.notify(f"🗑 Customer <b>{name}</b> ({esc(c['owner'])}) {why}.", 'removals', role='admin', dedupe=key)
            if c['owner_role'] == 'reseller':
                self.notify(f"🗑 Your customer <b>{name}</b> {why}.", 'removals', user_id=c['user_id'], dedupe=key)
        elif action == 'customer_auto_accepted' and data.get('customer'):
            c = self._customer_owner(int(data['customer']))
            if not c or c['status'] != 'active':
                return
            name = esc(display_name(c['email']))
            names = {r['id']: r['name'] for r in self.db.all('SELECT id,name FROM inbounds')}
            rows = activity.change_rows(data, names, self._date)
            lines = ''.join(f"\n• {esc(f)}: {esc(a)} → <b>{esc(b)}</b>" for f, a, b in rows)
            owner = f" ({esc(c['owner'])})" if c['owner_role'] == 'reseller' else ''
            self.notify(f"🔄 Customer <b>{name}</b>{owner} was changed in 3X-UI. Relay now uses the new values (free for the reseller).{lines}",
                        'changes', role='admin', dedupe=key,
                        buttons=[[['↩️ Undo', f"und:{c['id']}"]] + [x for x in [self._open('Open', f"/customers/{c['id']}")] if x]])
            if c['owner_role'] == 'reseller':
                text = f"🔄 The admin changed your customer <b>{name}</b>.{lines}\nThis did not use any data from your plan."
                buttons = None
                if data.get('identity'):
                    text += '\n🔗 Their link changed: send the customer the new link.'
                    buttons = [[['🔗 New links', f"lnk:{c['id']}"]]]
                self.notify(text, 'changes', user_id=c['user_id'], buttons=buttons, dedupe=key)
        elif action == 'customer_undo_remote' and data.get('customer'):
            c = self._customer_owner(int(data['customer']))
            if c and c['owner_role'] == 'reseller':
                self.notify(f"↩️ The admin put back the previous values of your customer <b>{esc(display_name(c['email']))}</b>.",
                            'changes', user_id=c['user_id'], dedupe=key)
        elif action == 'inbound_removed':
            text = f"🔌 Inbound <b>{esc(data.get('name') or '#'+str(data.get('id')))}</b> was deleted in 3X-UI and removed from Relay"
            text += f"; {data.get('customers')} customers lost it." if data.get('customers') else '.'
            self.notify(text, 'removals', role='admin', dedupe=key)
            for uid in data.get('resellers') or []:
                self.notify(text, 'removals', user_id=uid, dedupe=key)
        elif action in ('customer_create', 'customer_delete') and e['role'] == 'reseller' and str(subject).isdigit():
            c = self._customer_owner(int(subject))
            if c:
                verb = 'created' if action == 'customer_create' else 'deleted'
                extra = f" ({gb(c['package'])})" if action == 'customer_create' else ''
                self.notify(f"🛒 <b>{esc(e['username'])}</b> {verb} customer <b>{esc(display_name(c['email']))}</b>{extra}.", 'sales', role='admin', dedupe=key)
        elif action == 'request_created':
            req = self.db.one('SELECT * FROM requests WHERE id=?', (data.get('request'),))
            if req and req['status'] == 'pending':
                self.notify(self.request_text(req), 'requests', role='admin', buttons=self.request_buttons(req), dedupe=key)
        elif action in ('request_approved', 'request_declined'):
            req = self.db.one('SELECT * FROM requests WHERE id=?', (data.get('request'),))
            if req:
                if action == 'request_approved':
                    plan = self._plan(req['user_id'])
                    text = f"✅ Your request was approved: <b>{gb(req['volume'])}</b> added" + (f" and {req['days']} more days" if req['days'] else '') + '.'
                    if plan:
                        text += f"\nLeft to sell now: {gb(max(0, plan['allowance']-plan['sold']-plan['reserved']))}, plan ends {self._date(plan['expires'])}."
                else:
                    text = f"❌ Your request for {gb(req['volume'])} was declined."
                if req['answer']:
                    text += f"\n💬 {esc(req['answer'])}"
                self.notify(text, 'requests', user_id=req['user_id'], dedupe=key)
        elif action == 'grant' and str(subject).isdigit():
            g = self.db.one('SELECT * FROM grants WHERE id=?', (int(subject),))
            if g:
                self.notify(f"📦 New plan: <b>{gb(g['allowance'])}</b> until {self._date(g['expires'])}.", 'plans', user_id=g['user_id'], dedupe=key)
        elif action == 'grant_adjust' and str(subject).isdigit():
            g = self.db.one('SELECT * FROM grants WHERE id=?', (int(subject),))
            if g:
                self.notify(f"📦 Your plan was changed: <b>{gb(g['allowance'])}</b> until {self._date(g['expires'])}.", 'plans', user_id=g['user_id'], dedupe=key)
        elif action == 'bulk_inbounds' and e['actor']:
            text = (f"🧰 Bulk change finished: <b>{esc(data.get('summary', ''))}</b>\n"
                    f"{data.get('done', 0)} changed · {data.get('skipped', 0)} skipped · {data.get('failed', 0)} failed")
            opener = self._open('Show details', f"/jobs/{data.get('job')}")
            self.notify(text, 'jobs', user_id=e['actor'], buttons=[[opener]] if opener else None, dedupe=key)
        elif action == 'login' and e['outcome'] == 'ok' and e['role'] == 'admin':
            self.notify(f"🔐 <b>{esc(e['username'])}</b> signed in to Relay ({self._date(e['created'])}).", 'logins', role='admin', dedupe=key)

    def lock_message(self):
        return ("🔒 <b>Sales are paused</b> after a backup restore. Nobody can create or change customers. "
                "Check customers and plans, then unlock sales on the Needs attention page.",
                [[button]] if (button := self._open('Open Needs attention', '/attention')) else None)

    def scan_attention(self):
        if self.service.sales_locked():
            unlocks = self.db.one("SELECT count(*) n FROM audit WHERE action='restore_unlock'")['n']
            text, buttons = self.lock_message()
            self.notify(text, 'attention', role='admin', buttons=buttons, dedupe=f'lock:{unlocks}')
        for kind, row, snapshot in self.attention_items():
            if kind == 'drift':
                key = f"att:drift:{row['id']}:{hashlib.sha1(snapshot.encode()).hexdigest()[:12]}"
            elif kind == 'failed':
                key = f"att:failed:{row['id']}"
            else:
                key = f"att:stuck:{row['id']}"
            text, buttons = self.attention_message(kind, row, snapshot)
            self.notify(text, 'attention', role='admin', buttons=buttons, dedupe=key)

    def scan_plans(self):
        rows = self.db.all('''SELECT g.*,u.username FROM grants g JOIN users u ON u.id=g.user_id
                              WHERE u.role='reseller' AND u.deleted=0 AND g.closed=0 AND g.expires>?''', (now()-86400,))
        for g in rows:
            used = g['sold'] + g['reserved']
            who = esc(g['username'])
            if g['expires'] <= now():
                if not self._plan(g['user_id']):
                    self.notify(f"⛔ Your plan ended on {self._date(g['expires'])}. New sales are paused. Use /request to ask for more.", 'plans', user_id=g['user_id'], dedupe=f"planend:{g['id']}")
                    self.notify(f"⛔ The plan of <b>{who}</b> has ended.", 'plans', role='admin', dedupe=f"planend:{g['id']}")
                continue
            if g['allowance'] and used >= 0.9 * g['allowance']:
                left = gb(max(0, g['allowance'] - used))
                self.notify(f"📉 Your plan is almost used up: <b>{left}</b> left of {gb(g['allowance'])}.", 'plans', user_id=g['user_id'], dedupe=f"plan90:{g['id']}")
                self.notify(f"📉 <b>{who}</b> has used 90% of their plan ({left} left).", 'plans', role='admin', dedupe=f"plan90:{g['id']}")
            if g['expires'] <= now() + 3*86400:
                self.notify(f"⏳ Your plan ends {self._date(g['expires'])} ({self._days_left(g['expires'])}).", 'plans', user_id=g['user_id'], dedupe=f"plan3d:{g['id']}")
                self.notify(f"⏳ The plan of <b>{who}</b> ends {self._date(g['expires'])}.", 'plans', role='admin', dedupe=f"plan3d:{g['id']}")

    def scan_sync(self):
        error = self._state('sync_error')
        ok = int(self._state('sync_ok', '0') or 0)
        code, _, when = error.partition(':')
        failing = bool(error) and int(when or 0) >= ok
        outage = self._state('tg:outage')
        if failing and not outage:
            self._set_state('tg:outage', now())
        elif failing and outage and now() - int(outage) >= 300:
            self.notify(f"🔴 Relay cannot reach 3X-UI since {self._date(int(outage))}: {esc(self.t(code))}", 'sync', role='admin', dedupe=f"syncdown:{outage}")
        elif not failing and outage:
            if now() - int(outage) >= 300:
                self.notify(f"🟢 3X-UI is reachable again (was down for {max(1, (now()-int(outage))//60)} min).", 'sync', role='admin', dedupe=f"syncup:{outage}")
            self._set_state('tg:outage', '')

    def scan_daily(self):
        try:
            hour = int(self.setting('report_hour', '9'))
        except ValueError:
            hour = 9
        local = datetime.fromtimestamp(now(), self.zone)
        if local.hour < hour:
            return
        day = local.strftime('%Y-%m-%d')
        for target in self._targets(pref='daily'):
            user = self.db.one('SELECT * FROM users WHERE id=?', (target['uid'],))
            self.enqueue(target['chat_id'], self.report_text(user), None, f"daily:{day}:{target['chat_id']}")
