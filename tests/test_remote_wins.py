"""'3X-UI wins' mode: changes made in 3X-UI are accepted automatically (free for the
reseller), can be undone, and the sales lock after a restore can be lifted in the panel."""
import json
import uuid
import pytest
from fastapi.testclient import TestClient
from reseller.domain import RuleError
from reseller.web import create_app
from conftest import form, sale
from test_web import login, csrf, error_of

GB = 1024**3


def customer(db, cid):
    return db.one('SELECT * FROM customers WHERE id=?', (cid,))


def sold(db, actor):
    return db.one('SELECT sold FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['sold']


def test_change_in_3xui_is_accepted_for_free_and_can_be_undone(setup):
    _, panel, db, service, admin, actor, _ = setup
    assert service.remote_changes_mode() == 'auto'
    op = sale(service, actor)
    panel.rows['customer-one'].update(totalGB=200 * GB, comment='gift from admin')
    service.refresh()
    c = customer(db, op['customer_id'])
    assert c['issue'] == '' and c['package'] == 200 * GB
    assert json.loads(c['payload'])['comment'] == 'gift from admin'
    assert sold(db, actor) == 100 * GB                       # a gift: the plan was not charged
    event = db.one("SELECT * FROM audit WHERE action='customer_auto_accepted'")
    data = json.loads(event['subject'])
    assert data['old'] == 100 * GB and data['new'] == 200 * GB and event['actor'] is None
    assert service.undo_info(c['id'])
    service.refresh()                                        # stays accepted, no new event
    assert db.one("SELECT count(*) n FROM audit WHERE action='customer_auto_accepted'")['n'] == 1
    with pytest.raises(RuleError):
        service.undo_remote(actor, c['id'])
    service.undo_remote(admin, c['id'])
    c = customer(db, c['id'])
    assert c['package'] == 100 * GB and panel.rows['customer-one']['totalGB'] == 100 * GB
    assert panel.rows['customer-one']['comment'] == json.loads(c['payload'])['comment'] != 'gift from admin'
    assert sold(db, actor) == 100 * GB and service.undo_info(c['id']) is None
    service.refresh()
    assert customer(db, c['id'])['issue'] == ''
    with pytest.raises(RuleError, match='undo_unavailable'):
        service.undo_remote(admin, c['id'])


def test_gifted_data_cannot_be_refunded_into_the_plan(setup):
    _, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor)
    panel.rows['customer-one']['totalGB'] = 300 * GB
    service.refresh()
    edit = service.reserve(actor, form(volume='20'), str(uuid.uuid4()), 'update', op['customer_id'])
    service.execute(edit['id'])
    # Only the 80 GB the reseller paid for above the new size comes back, not the gift.
    assert sold(db, actor) == 20 * GB


def test_inbound_and_link_changes_are_accepted_and_undone(setup):
    _, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor, inbounds=[1, 2])
    old_sub = customer(db, op['customer_id'])['sub_id']
    row = panel.rows['customer-one']
    row['subId'] = 'brand-new-sub'
    service.remote.detach('customer-one', [2])
    service.remote.attach('customer-one', [3])
    service.refresh()
    c = customer(db, op['customer_id'])
    assert c['issue'] == '' and c['sub_id'] == 'brand-new-sub'
    assert sorted(r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?', (c['id'],))) == [1, 3]
    assert json.loads(db.one("SELECT subject FROM audit WHERE action='customer_auto_accepted'")['subject'])['identity'] is True
    service.undo_remote(admin, c['id'])
    c = customer(db, c['id'])
    assert c['sub_id'] == old_sub and panel.rows['customer-one']['subId'] == old_sub
    assert sorted(panel.rows['customer-one']['inboundIds']) == [1, 2]
    assert sorted(r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?', (c['id'],))) == [1, 2]


def test_conflicting_change_stays_under_attention(setup):
    _, panel, db, service, admin, actor, _ = setup
    first = sale(service, actor)
    second = sale(service, actor, email='customer-two', volume='10')
    panel.rows['customer-two']['subId'] = customer(db, first['customer_id'])['sub_id']
    service.refresh()
    assert customer(db, second['customer_id'])['issue'] == 'remote_identity'
    assert customer(db, first['customer_id'])['issue'] == ''


def test_ask_mode_keeps_the_old_behaviour_and_switching_back_accepts(setup):
    _, panel, db, service, admin, actor, _ = setup
    service.set_remote_changes_mode(admin, 'ask')
    op = sale(service, actor)
    panel.rows['customer-one']['comment'] = 'edited'
    service.refresh()
    assert customer(db, op['customer_id'])['issue'] == 'remote_changed'
    with pytest.raises(RuleError):
        service.set_remote_changes_mode(actor, 'auto')
    service.set_remote_changes_mode(admin, 'auto')           # pending changes are accepted right away
    assert customer(db, op['customer_id'])['issue'] == ''


def test_settings_customer_page_and_undo_in_the_panel(setup):
    config, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor)
    panel.rows['customer-one']['totalGB'] = 150 * GB
    service.refresh()
    with TestClient(create_app(config, service.remote, background=False)) as c:
        token = login(c, 'admin')
        page = c.get(f"/customers/{op['customer_id']}").text
        assert 'Changed in 3X-UI' in page and '100 GB' in page and '150 GB' in page and 'undo-remote' in page
        assert c.post(f"/customers/{op['customer_id']}/undo-remote", data={'csrf': token}, follow_redirects=False).status_code == 303
        assert panel.rows['customer-one']['totalGB'] == 100 * GB
        assert error_of(c.post(f"/customers/{op['customer_id']}/undo-remote", data={'csrf': token}, follow_redirects=False)) == 'undo_unavailable'
        assert 'value="auto" checked' in c.get('/settings').text
        c.post('/settings/remote-changes', data={'csrf': token, 'mode': 'ask'})
        assert service.remote_changes_mode() == 'ask'
        assert c.post('/settings/remote-changes', data={'csrf': token, 'mode': 'whatever'}, follow_redirects=False).status_code in (303, 400)
        assert service.remote_changes_mode() == 'ask'
    with TestClient(create_app(config, service.remote, background=False)) as c:
        token = login(c)
        assert 'Changed in 3X-UI' not in c.get(f"/customers/{op['customer_id']}").text
        feed = c.get('/activity').text
        assert 'was changed in 3X-UI; Relay now uses the new values' in feed and 'free for the reseller' in feed
        assert 'undid the 3X-UI change of' in feed
        assert c.post('/settings/remote-changes', data={'csrf': token, 'mode': 'auto'}, follow_redirects=False).status_code in (303, 403)
        assert service.remote_changes_mode() == 'ask'
    activity = [r['action'] for r in db.all('SELECT action FROM audit')]
    assert 'customer_auto_accepted' in activity and 'customer_undo_remote' in activity


def test_admin_unlocks_sales_after_a_restore(setup):
    config, panel, db, service, admin, actor, _ = setup
    with db.transaction() as conn:
        conn.execute("INSERT INTO state VALUES('write_lock','restored')")
    with pytest.raises(RuleError, match='restore_locked'):
        sale(service, actor)
    with TestClient(create_app(config, service.remote, background=False)) as c:
        token = login(c)
        assert 'Sales are paused after a backup restore' in c.get('/').text
        assert c.post('/sales/unlock', data={'csrf': token}, follow_redirects=False).status_code in (303, 403)
        assert service.sales_locked()
    with TestClient(create_app(config, service.remote, background=False)) as c:
        login(c, 'admin')
        assert 'unlock sales on the Needs attention page' in c.get('/').text
        page = c.get('/attention').text
        assert 'Unlock sales' in page and 'Sales are paused' in page
        token = csrf(c, '/attention')
        response = c.post('/sales/unlock', data={'csrf': token}, follow_redirects=False)
        assert response.status_code == 303
        assert not service.sales_locked()
        assert 'Unlock sales' not in c.get('/attention').text
    # Selling works again (reserve is the step the lock blocks).
    assert service.reserve(actor, form(email='after-unlock'), str(uuid.uuid4()))['status'] == 'reserved'
    assert db.one("SELECT actor FROM audit WHERE action='restore_unlock'")['actor'] == admin['id']


def test_second_automatic_accept_keeps_the_first_undo_copy(setup):
    _, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor)
    panel.rows['customer-one']['totalGB'] = 120 * GB
    service.refresh()
    first = service.undo_info(op['customer_id'])
    with pytest.raises(RuleError):
        service.accept_remote(admin, op['customer_id'], auto=True)
    assert service.undo_info(op['customer_id']) == first and first['package'] == 100 * GB
