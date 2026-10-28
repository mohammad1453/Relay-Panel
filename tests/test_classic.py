"""Relay against a classic 3X-UI (2.x / 3.0.x) without the client API."""
import uuid
import pytest
from reseller.domain import RuleError
from conftest import form, sale

GIB = 1024**3


def op_status(db, op):
    return db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status']


def test_create_update_and_usage(classic):
    _, panel, db, service, _, actor = classic
    op = sale(service, actor, email='classic-one', volume='20')
    assert op_status(db, op) == 'committed'
    client = panel.find(1)['settings']['clients'][0]
    assert client['email'] == 'classic-one' and client['totalGB'] == 20 * GIB and client['group'] == 'sales-team'
    panel.online = {'classic-one'}
    service.refresh()
    c = db.one('SELECT * FROM customers WHERE id=?', (op['customer_id'],))
    assert c['issue'] == '' and c['used'] == 3 and c['online'] == 1
    update = service.reserve(actor, form(email='classic-one', volume='30'), str(uuid.uuid4()), 'update', op['customer_id'])
    service.execute(update['id'])
    assert op_status(db, update) == 'committed'
    assert panel.find(1)['settings']['clients'][0]['totalGB'] == 30 * GIB


def test_only_one_inbound_per_customer(classic):
    _, panel, db, service, _, actor = classic
    with pytest.raises(RuleError) as error:
        sale(service, actor, email='two-inbounds', inbounds=[1, 2])
    assert error.value.code == 'single_inbound'


def test_move_to_another_inbound_keeps_credentials(classic):
    _, panel, db, service, _, actor = classic
    sale(service, actor, email='neighbour')          # keeps inbound 1 non-empty
    op = sale(service, actor, email='mover')
    old = next(c for c in panel.find(1)['settings']['clients'] if c['email'] == 'mover')
    update = service.reserve(actor, form(email='mover', inbounds=[2]), str(uuid.uuid4()), 'update', op['customer_id'])
    service.execute(update['id'])
    assert op_status(db, update) == 'committed'
    assert all(c['email'] != 'mover' for c in panel.find(1)['settings']['clients'])
    moved = panel.find(2)['settings']['clients'][0]
    assert moved['id'] == old['id'] and moved['subId'] == old['subId']


def test_delete_last_client_of_an_inbound(classic):
    _, panel, db, service, admin, actor = classic
    op = sale(service, actor, email='only-one')
    delete = service.reserve(actor, {}, str(uuid.uuid4()), 'delete', op['customer_id'])
    service.execute(delete['id'])
    assert op_status(db, delete) == 'committed'
    assert panel.find(1)['settings']['clients'] == []


def test_delete_with_other_clients_and_admin_remove(classic):
    _, panel, db, service, admin, actor = classic
    first = sale(service, actor, email='first')
    second = sale(service, actor, email='second')
    service.force_remove(admin, first['customer_id'])
    assert [c['email'] for c in panel.find(1)['settings']['clients']] == ['second']
    delete = service.reserve(actor, {}, str(uuid.uuid4()), 'delete', second['customer_id'])
    service.execute(delete['id'])
    assert panel.find(1)['settings']['clients'] == []


@pytest.mark.parametrize('inbound,field', [(3, 'password'), (4, 'password')])
def test_trojan_and_shadowsocks(classic, inbound, field):
    _, panel, db, service, _, actor = classic
    op = sale(service, actor, email=f'proto-{inbound}', inbounds=[inbound])
    assert op_status(db, op) == 'committed'
    client = panel.find(inbound)['settings']['clients'][0]
    assert client[field]
    if inbound == 4:
        import base64
        assert len(base64.b64decode(client['password'])) == 32   # fits 2022-blake3-aes-256-gcm


def test_edit_in_classic_panel_is_flagged_and_accepted(classic):
    _, panel, db, service, admin, actor = classic
    op = sale(service, actor, email='edited')
    service.set_remote_changes_mode(db.one("SELECT * FROM users WHERE role='admin'"), 'ask')
    panel.find(1)['settings']['clients'][0]['comment'] = 'changed in 3x-ui'
    service.refresh()
    assert db.one('SELECT issue FROM customers WHERE id=?', (op['customer_id'],))['issue'] == 'remote_changed'
    service.accept_remote(admin, op['customer_id'])
    service.refresh()
    assert db.one('SELECT issue FROM customers WHERE id=?', (op['customer_id'],))['issue'] == ''


def test_links_page_works_without_link_api(classic):
    from fastapi.testclient import TestClient
    from reseller.web import create_app
    from test_web import login
    config, panel, db, service, _, actor = classic
    op = sale(service, actor, email='linked')
    with TestClient(create_app(config, service.remote, background=False)) as client:
        login(client)
        page = client.get(f"/customers/{op['customer_id']}/links")
        assert page.status_code == 200 and 'https://sub.example.invalid/i/' in page.text


def test_form_and_dashboard_show_classic_mode(classic):
    from fastapi.testclient import TestClient
    from reseller.web import create_app
    from test_web import login
    config, panel, db, service, _, actor = classic
    with TestClient(create_app(config, service.remote, background=False)) as client:
        login(client)
        form_page = client.get('/customers/new').text
        assert 'type="radio" name="inbounds"' in form_page and 'one inbound per customer' in form_page
    with TestClient(create_app(config, service.remote, background=False)) as client:
        login(client, 'admin')
        assert 'classic API' in client.get('/').text


def test_change_in_classic_panel_is_accepted_and_undone(classic):
    _, panel, db, service, admin, actor = classic
    op = sale(service, actor, email='classic-gift')
    client = panel.find(1)['settings']['clients'][0]
    client['totalGB'] = 300 * 1024**3
    client['comment'] = 'gift'
    service.refresh()
    c = db.one('SELECT * FROM customers WHERE id=?', (op['customer_id'],))
    assert c['issue'] == '' and c['package'] == 300 * 1024**3
    service.undo_remote(admin, op['customer_id'])
    client = panel.find(1)['settings']['clients'][0]
    assert client['totalGB'] == 100 * 1024**3 and client['comment'] != 'gift'
    service.refresh()
    assert db.one('SELECT issue,package FROM customers WHERE id=?', (op['customer_id'],)) == {'issue': '', 'package': 100 * 1024**3}
