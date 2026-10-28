"""Telegram bot: linking, alerts, approvals and commands, against a fake Telegram API."""
import json
import re
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from reseller.db import now
from reseller.web import create_app
from reseller import telegram as tgmod
from conftest import sale
from test_web import login, csrf


class FakeTelegram:
    def __init__(self):
        self.sent, self.calls, self.updates, self.next_id = [], [], [], 1
        self.fail = None
        self.hosts = []

    def push_text(self, chat_id, text):
        self.updates.append({'update_id': self.next_id, 'message': {'chat': {'id': chat_id, 'type': 'private', 'first_name': 'Ali'}, 'text': text}})
        self.next_id += 1

    def push_button(self, chat_id, data, message_id=7):
        self.updates.append({'update_id': self.next_id, 'callback_query': {'id': f'q{self.next_id}', 'data': data,
                             'message': {'message_id': message_id, 'chat': {'id': chat_id}}}})
        self.next_id += 1

    def __call__(self, request):
        method = request.url.path.rsplit('/', 1)[-1]
        body = json.loads(request.content or b'{}')
        self.calls.append((method, body))
        self.hosts.append(request.url.host)
        if self.fail:
            return httpx.Response(200, json={'ok': False, 'error_code': self.fail, 'description': 'Forbidden: bot was blocked by the user'})
        if method == 'getMe':
            return httpx.Response(200, json={'ok': True, 'result': {'id': 1, 'username': 'relay_test_bot'}})
        if method == 'getUpdates':
            offset = body.get('offset') or 0
            result = [u for u in self.updates if u['update_id'] >= offset]
            return httpx.Response(200, json={'ok': True, 'result': result})
        if method == 'sendMessage':
            self.sent.append(body)
        return httpx.Response(200, json={'ok': True, 'result': True})

    def texts(self, chat_id=None):
        return [m['text'] for m in self.sent if chat_id is None or m['chat_id'] == chat_id]


@pytest.fixture
def bot(setup, monkeypatch):
    config, panel, db, service, admin, actor, other = setup
    fake = FakeTelegram()
    monkeypatch.setattr(tgmod, 'SCAN_SECONDS', 0)
    app = create_app(config, service.remote, background=False, telegram_transport=httpx.MockTransport(fake))
    client = TestClient(app)
    client.__enter__()
    tg = app.app.state.telegram if hasattr(app, 'app') else app.state.telegram
    yield fake, tg, client
    client.__exit__(None, None, None)


def enable(client, fake):
    token = login(client, 'admin')
    response = client.post('/settings/telegram', data={'csrf': token, 'enabled': 'on', 'token': '123456:ABCdef', 'report_hour': '23'}, follow_redirects=False)
    assert response.status_code == 303, response.text


def link(tg, fake, user, chat_id):
    code = tg.new_code(user)
    fake.push_text(chat_id, f'/start {code}')
    tg.step()


def test_settings_validate_and_connect_account(bot, setup):
    fake, tg, client = bot
    token = login(client, 'admin')
    bad = client.post('/settings/telegram', data={'csrf': token, 'enabled': 'on', 'token': 'nope', 'report_hour': '9'})
    assert bad.status_code == 400 and 'does not look like a bot token' in bad.text
    enable(client, fake)
    assert tg.enabled() and tg.status()['username'] == 'relay_test_bot'
    token = csrf(client, '/account')
    page = client.post('/account/telegram/connect', data={'csrf': token})
    code = re.search(r'start=([0-9A-F]+)', page.text).group(1)
    assert 'https://t.me/relay_test_bot?start=' in page.text
    fake.push_text(555, f'/start {code}')
    tg.step()
    assert any('Connected to Relay as <b>admin</b>' in t for t in fake.texts(555))
    assert 'Connected' in client.get('/account').text
    token = csrf(client, '/settings')
    assert client.post('/settings/telegram/test', data={'csrf': token}, follow_redirects=False).status_code == 303
    assert any('Test message from Relay' in t for t in fake.texts(555))


def test_unknown_chat_and_wrong_code(bot):
    fake, tg, client = bot
    enable(client, fake)
    fake.push_text(999, '/status')
    fake.push_text(999, '/start WRONG')
    tg.step()
    assert 'To connect' in fake.texts(999)[0] and 'wrong or expired' in fake.texts(999)[1]


def test_drift_alert_and_accept_button(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    service.set_remote_changes_mode(db.one("SELECT * FROM users WHERE role='admin'"), 'ask')
    enable(client, fake)
    link(tg, fake, admin, 100)
    op = sale(service, actor, email='drifty')
    panel.rows['drifty']['comment'] = 'changed in panel'
    service.refresh()
    tg.step()
    alert = next(m for m in fake.sent if 'was changed directly in 3X-UI' in m['text'])
    assert 'changed in panel' in alert['text']
    buttons = [b['callback_data'] for row in alert['reply_markup']['inline_keyboard'] for b in row if 'callback_data' in b]
    assert f"acc:{op['customer_id']}" in buttons
    tg.step()   # the same problem is not announced twice
    assert sum('was changed directly' in t for t in fake.texts(100)) == 1
    fake.push_button(100, f"acc:{op['customer_id']}")
    tg.step()
    assert db.one('SELECT issue FROM customers WHERE id=?', (op['customer_id'],))['issue'] == ''
    assert any(c[0] == 'answerCallbackQuery' and 'Relay now uses' in c[1].get('text', '') for c in fake.calls)


def test_reseller_request_flow_through_bot(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, admin, 100)
    link(tg, fake, actor, 200)
    before = db.one('SELECT allowance FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['allowance']
    fake.push_text(200, '/request 50 7 more customers')
    tg.step()
    tg.step()
    assert 'Request sent' in fake.texts(200)[-1]
    alert = next(m for m in fake.sent if m['chat_id'] == 100 and 'asks for' in m['text'])
    assert '50 GB' in alert['text'] and '7 more days' in alert['text'] and 'more customers' in alert['text']
    rid = db.one("SELECT id FROM requests WHERE status='pending'")['id']
    fake.push_button(200, f'rqy:{rid}')     # a reseller cannot approve
    tg.step()
    assert db.one('SELECT status FROM requests WHERE id=?', (rid,))['status'] == 'pending'
    fake.push_button(100, f'rqy:{rid}')
    tg.step()
    tg.step()
    assert db.one('SELECT allowance FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['allowance'] == before + 50 * 1024**3
    assert any('was approved' in t for t in fake.texts(200))


def test_commands_find_status_and_toggle(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, actor, 200)
    op = sale(service, actor, email='find-me')
    for command in ('/status', '/find find', '/customers', '/report', '/help', '/nonsense'):
        fake.push_text(200, command)
    tg.step()
    texts = fake.texts(200)
    assert any('Your plan' in t for t in texts) and any('<b>find-me</b>' in t for t in texts)
    assert any('https://sub.example.invalid/i/' in t for t in texts)
    assert any("don't know that command" in t for t in texts)
    fake.push_button(200, f"off:{op['customer_id']}")
    tg.step()
    assert panel.rows['find-me']['enable'] is False
    fake.push_button(200, f"lnk:{op['customer_id']}")
    tg.step()
    assert 'vless://' in fake.texts(200)[-1] and 'Inbound 1 · find-me' in fake.texts(200)[-1]
    # Another reseller cannot touch it.
    other = db.one("SELECT * FROM users WHERE username='other'")
    link(tg, fake, other, 300)
    fake.push_button(300, f"on:{op['customer_id']}")
    tg.step()
    assert panel.rows['find-me']['enable'] is False


def test_remove_needs_confirmation(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, admin, 100)
    op = sale(service, actor, email='to-remove')
    fake.push_button(100, f"rm:{op['customer_id']}")
    tg.step()
    assert 'to-remove' in panel.rows
    fake.push_button(100, f"rmy:{op['customer_id']}")
    tg.step()
    assert 'to-remove' not in panel.rows


def test_plan_and_removal_alerts_and_preferences(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, admin, 100)
    link(tg, fake, actor, 200)
    sale(service, actor, email='big', volume='450')
    op = sale(service, actor, email='gone', volume='10')
    panel.rows.pop('gone')
    service.refresh()
    tg.step()
    assert any('almost used up' in t for t in fake.texts(200))
    assert any('used 90%' in t for t in fake.texts(100))
    assert any('<b>gone</b> was deleted in 3X-UI' in t for t in fake.texts(200))
    # Turning a kind of message off stops it.
    tg.set_prefs(actor, {'requests'})
    count = len(fake.texts(200))
    with db.transaction() as conn:
        conn.execute('UPDATE grants SET expires=? WHERE user_id=?', (now() + 86400, actor['id']))
    tg.step()
    assert len(fake.texts(200)) == count


def test_blocked_chat_is_not_retried_forever(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    tg.enqueue(100, 'hello')
    fake.fail = 403
    tg.flush()
    fake.fail = None
    row = db.one("SELECT * FROM outbox WHERE text='hello'")
    assert row['attempts'] == 5 and 'blocked' in row['error'] and row['sent'] is None


def test_request_from_panel_and_admin_decides_on_attention_page(setup):
    config, _, db, service, admin, actor, _ = setup
    with TestClient(create_app(config, service.remote, background=False)) as c:
        token = login(c)
        page = c.post('/requests/new', data={'csrf': token, 'volume': '0', 'days': '0'})
        assert page.status_code == 400 and 'above 0 GB' in page.text
        token = csrf(c, '/requests/new')
        assert c.post('/requests/new', data={'csrf': token, 'volume': '40', 'days': '5', 'note': 'pls'}, follow_redirects=False).status_code == 303
        assert 'is waiting for the admin' in c.get('/').text
    with TestClient(create_app(config, service.remote, background=False)) as c:
        token = login(c, 'admin')
        page = c.get('/attention').text
        assert 'asks for more data' in page and 'pls' in page
        rid = db.one("SELECT id FROM requests WHERE status='pending'")['id']
        token = csrf(c, '/attention')
        c.post(f'/requests/{rid}/approve', data={'csrf': token, 'volume': '25', 'days': '0', 'answer': 'half now'})
        assert db.one('SELECT status,volume FROM requests WHERE id=?', (rid,))['volume'] == 25 * 1024**3
    with TestClient(create_app(config, service.remote, background=False)) as c:
        login(c)
        assert 'was approved' in c.get('/').text and 'half now' in c.get('/').text


def test_custom_api_address(bot):
    fake, tg, client = bot
    token = login(client, 'admin')
    for bad in ('http://tg.example.com', 'https://tg.example.com/?x=1', 'nope'):
        page = client.post('/settings/telegram', data={'csrf': token, 'enabled': 'on', 'token': '123456:ABCdef', 'api_url': bad, 'report_hour': '9'})
        assert page.status_code == 400 and 'must be an https:// address' in page.text
        token = csrf(client, '/settings')
    response = client.post('/settings/telegram', data={'csrf': token, 'enabled': 'on', 'token': '123456:ABCdef', 'api_url': 'https://tg.example.com/relay/', 'report_hour': '9'}, follow_redirects=False)
    assert response.status_code == 303
    assert fake.hosts[-1] == 'tg.example.com' and fake.calls[-1][0] == 'getMe'
    assert 'value="https://tg.example.com/relay"' in client.get('/settings').text
    token = csrf(client, '/settings')
    client.post('/settings/telegram', data={'csrf': token, 'enabled': 'on', 'api_url': '', 'report_hour': '9'})
    assert fake.hosts[-1] == 'api.telegram.org'


def test_auto_accepted_change_alerts_with_undo(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, admin, 100)
    link(tg, fake, actor, 200)
    op = sale(service, actor, email='gifted')
    tg.step()
    panel.rows['gifted']['totalGB'] = 250 * 1024**3
    panel.rows['gifted']['subId'] = 'new-sub-id'
    service.refresh()
    tg.step()
    alert = next(m for m in fake.sent if m['chat_id'] == 100 and 'was changed in 3X-UI' in m['text'])
    assert '100 GB → <b>250 GB</b>' in alert['text']
    buttons = [b.get('callback_data') for row in alert['reply_markup']['inline_keyboard'] for b in row]
    assert f"und:{op['customer_id']}" in buttons
    note = next(m for m in fake.sent if m['chat_id'] == 200 and 'The admin changed your customer' in m['text'])
    assert 'did not use any data' in note['text'] and 'send the customer the new link' in note['text']
    fake.push_button(200, f"und:{op['customer_id']}")     # resellers cannot undo
    tg.step()
    assert panel.rows['gifted']['totalGB'] == 250 * 1024**3
    fake.push_button(100, f"und:{op['customer_id']}")
    tg.step()
    tg.step()
    assert panel.rows['gifted']['totalGB'] == 100 * 1024**3
    assert any('put back the previous values' in t for t in fake.texts(200))


def test_paused_sales_alert_and_attention_command(bot, setup):
    fake, tg, client = bot
    _, panel, db, service, admin, actor, _ = setup
    enable(client, fake)
    link(tg, fake, admin, 100)
    with db.transaction() as conn:
        conn.execute("INSERT INTO state VALUES('write_lock','restored')")
    tg.step()
    tg.step()
    assert sum('Sales are paused' in t for t in fake.texts(100)) == 1
    fake.push_text(100, '/attention')
    tg.step()
    assert 'Sales are paused' in fake.texts(100)[-1]
    service.unlock_sales(admin)
    with db.transaction() as conn:
        conn.execute("INSERT INTO state VALUES('write_lock','restored')")
    tg.step()
    assert sum('Sales are paused' in t for t in fake.texts(100)) == 3   # a new lock is announced again
