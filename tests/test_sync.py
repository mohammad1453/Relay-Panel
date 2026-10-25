"""Sync with 3X-UI: removed inbounds, clients removed during setup, edits made in 3X-UI."""
import json
import threading
import uuid
import pytest
from reseller.db import now
from reseller.domain import RuleError
from conftest import form, sale


def customer(db, cid):
    return db.one('SELECT * FROM customers WHERE id=?', (cid,))


def test_removed_inbound_is_deleted_with_links_and_customer_stays_clean(setup):
    _, panel, db, service, _, actor, _ = setup
    op = sale(service, actor, inbounds=[1, 2])
    panel.delete_inbound(2)
    service.refresh()
    assert not db.one('SELECT 1 x FROM inbounds WHERE id=2')
    assert not db.one('SELECT 1 x FROM permissions WHERE inbound_id=2')
    assert [r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?', (op['customer_id'],))] == [1]
    c = customer(db, op['customer_id'])
    assert c['status'] == 'active' and c['issue'] == ''
    event = db.one("SELECT subject FROM audit WHERE action='inbound_removed'")
    assert json.loads(event['subject'])['customers'] == 1
    # The customer can still be edited and deleted.
    update = service.reserve(actor, form(volume='50', inbounds=[1]), str(uuid.uuid4()), 'update', op['customer_id'])
    service.execute(update['id'])
    assert db.one('SELECT status FROM operations WHERE id=?', (update['id'],))['status'] == 'committed'


def test_customer_whose_only_inbound_was_removed_can_be_deleted(setup):
    _, panel, db, service, _, actor, _ = setup
    op = sale(service, actor, inbounds=[1])
    panel.delete_inbound(1)
    service.refresh()
    assert customer(db, op['customer_id'])['issue'] == ''
    delete = service.reserve(actor, {}, str(uuid.uuid4()), 'delete', op['customer_id'])
    service.execute(delete['id'])
    assert customer(db, op['customer_id'])['status'] == 'deleted'
    assert 'customer-one' not in panel.rows


def test_empty_inbound_list_is_ignored_once(setup):
    _, panel, db, service, *_ = setup
    saved = panel.inbounds
    panel.inbounds = []
    service.sync_inbounds()
    assert db.one('SELECT count(*) n FROM inbounds')['n'] == len(saved)
    service.sync_inbounds()
    assert db.one('SELECT count(*) n FROM inbounds')['n'] == 0


def test_client_removed_in_3xui_during_setup_is_released(setup, monkeypatch):
    _, panel, db, service, _, actor, _ = setup
    panel.mode = 'drop'
    op = sale(service, actor)
    panel.mode = 'ok'
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'uncertain'
    clock = [now()]
    monkeypatch.setattr('reseller.domain.now', lambda: clock[0])
    for _ in range(3):
        service.refresh()
        clock[0] += 30
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'failed'
    assert customer(db, op['customer_id'])['status'] == 'deleted'
    grant = db.one('SELECT * FROM grants WHERE user_id=? AND closed=0', (actor['id'],))
    assert grant['reserved'] == 0 and grant['sold'] == 0


def test_absent_count_needs_spaced_syncs(setup):
    _, panel, db, service, _, actor, _ = setup
    panel.mode = 'drop'
    op = sale(service, actor)
    panel.mode = 'ok'
    for _ in range(5):
        service.refresh()
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'uncertain'


def test_edit_in_3xui_flags_customer_and_accept_copies_values(setup):
    _, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor)
    panel.rows['customer-one']['totalGB'] = 200 * 1024**3
    panel.rows['customer-one']['comment'] = 'changed in 3x-ui'
    service.refresh()
    c = customer(db, op['customer_id'])
    assert c['issue'] == 'remote_changed'
    assert db.one('SELECT value FROM state WHERE key=?', (f"drift:{c['id']}",))
    with pytest.raises(RuleError):
        service.reserve(actor, form(volume='10'), str(uuid.uuid4()), 'update', c['id'])
    with pytest.raises(RuleError):
        service.accept_remote(actor, c['id'])
    service.accept_remote(admin, c['id'])
    c = customer(db, c['id'])
    assert c['issue'] == '' and c['package'] == 200 * 1024**3
    assert json.loads(c['payload'])['comment'] == 'changed in 3x-ui'
    service.refresh()
    assert customer(db, c['id'])['issue'] == ''
    # The reseller was not charged for the change made in 3X-UI.
    assert db.one('SELECT sold FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['sold'] == 100 * 1024**3


def test_admin_force_remove_works_for_drifted_and_missing(setup):
    _, panel, db, service, admin, actor, _ = setup
    first = sale(service, actor)
    second = sale(service, actor, email='customer-two')
    panel.rows['customer-one']['id'] = 999   # recreated in 3X-UI
    panel.rows.pop('customer-two')           # removed in 3X-UI
    service.force_remove(admin, first['customer_id'])
    service.force_remove(admin, second['customer_id'])
    assert 'customer-one' not in panel.rows
    assert customer(db, first['customer_id'])['status'] == 'deleted'
    assert customer(db, second['customer_id'])['status'] == 'deleted'


def test_force_remove_cancels_stuck_setup_and_returns_quota(setup):
    _, panel, db, service, admin, actor, _ = setup
    panel.mode = 'drop'
    op = sale(service, actor)
    panel.mode = 'ok'
    service.force_remove(admin, op['customer_id'])
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'failed'
    assert db.one('SELECT reserved FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['reserved'] == 0


def test_delete_reseller_removes_customers_and_frees_name(setup):
    _, panel, db, service, admin, actor, _ = setup
    sale(service, actor)
    sale(service, actor, email='customer-two')
    panel.rows.pop('customer-two')
    assert service.delete_reseller(admin, actor['id']) == 0
    assert panel.rows == {}
    row = db.one('SELECT * FROM users WHERE id=?', (actor['id'],))
    assert row['deleted'] == 1 and row['username'].startswith('deleted-')
    assert not db.one("SELECT 1 x FROM customers WHERE user_id=? AND status!='deleted'", (actor['id'],))
    service.reseller(admin, 'reseller', 'A-long-test-password!', 'sales-team', 10, now() + 86400, [1])


def test_delete_reseller_reports_failures(setup):
    _, panel, db, service, admin, actor, _ = setup
    sale(service, actor)
    panel.mode = 'unavailable'
    assert service.delete_reseller(admin, actor['id']) == 1
    assert db.one('SELECT deleted FROM users WHERE id=?', (actor['id'],))['deleted'] == 0


def test_node_pending_settles(setup, monkeypatch):
    _, panel, db, service, _, actor, _ = setup
    for ib in panel.inbounds:
        ib['nodeId'] = 7
    panel.mode = 'node'
    op = sale(service, actor)
    panel.mode = 'ok'
    assert db.one('SELECT diagnostic FROM operations WHERE id=?', (op['id'],))['diagnostic'] == 'node_pending'
    service.reconcile(op['id'])
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'uncertain'
    monkeypatch.setattr('reseller.domain.now', lambda: now() + 120)
    service.reconcile(op['id'])
    assert db.one('SELECT status FROM operations WHERE id=?', (op['id'],))['status'] == 'committed'


def test_sync_lock_prevents_overlap(setup):
    service = setup[3]
    service.sync_lock.acquire()
    try:
        with pytest.raises(RuleError) as error:
            service.refresh(wait=0.05)
        assert error.value.code == 'sync_busy'
    finally:
        service.sync_lock.release()
    service.refresh()
    assert setup[2].one("SELECT value FROM state WHERE key='sync_ok'")
