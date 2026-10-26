"""Adding and removing inbounds for many customers at once."""
import re
import uuid
import pytest
from fastapi.testclient import TestClient
from reseller.domain import RuleError
from reseller.web import create_app
from conftest import sale
from test_web import login, csrf, client  # noqa: F401  (client is a fixture)


def placements(db, cid):
    return sorted(r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?', (cid,)))


def test_plan_explains_every_skip(setup):
    _, panel, db, service, admin, actor, _ = setup
    a = sale(service, actor, email='a-vless', inbounds=[1])
    b = sale(service, actor, email='b-vless', inbounds=[1, 2])
    c = sale(service, actor, email='c-trojan', inbounds=[3])
    plan = {i['email']: i for i in service.plan_bulk(actor, [a['customer_id'], b['customer_id'], c['customer_id']], 'add', [2])}
    assert plan['a-vless']['after'] == [1, 2] and not plan['a-vless']['skip']
    assert plan['b-vless']['skip'] == 'bulk_already'
    assert plan['c-trojan']['skip'] == 'bulk_different_protocol'
    plan = {i['email']: i for i in service.plan_bulk(actor, [a['customer_id'], b['customer_id']], 'remove', [1])}
    assert plan['a-vless']['skip'] == 'bulk_last_inbound' and plan['b-vless']['after'] == [2]
    with pytest.raises(RuleError):
        service.plan_bulk(actor, [a['customer_id']], 'move', [1, 3])     # mixed protocols
    # A reseller never sees another reseller's customers.
    other = db.one("SELECT * FROM users WHERE username='other'")
    assert service.plan_bulk(other, [a['customer_id']], 'add', [1]) == []


def test_run_job_applies_changes(setup):
    _, panel, db, service, admin, actor, _ = setup
    ops = [sale(service, actor, email=f'bulk-{n}', inbounds=[1]) for n in range(3)]
    ids = [o['customer_id'] for o in ops]
    plan = service.plan_bulk(actor, ids, 'add', [2])
    job = service.create_bulk_job(actor, 'add', [2], plan, 'Add Inbound 2')
    service.run_bulk_job(job)
    row = db.one('SELECT * FROM jobs WHERE id=?', (job,))
    assert row['status'] == 'finished' and row['done'] == 3 and row['failed'] == 0
    assert all(placements(db, cid) == [1, 2] for cid in ids)
    assert all(panel.rows[f'bulk-{n}']['inboundIds'] == [1, 2] for n in range(3))
    # Customers are not charged for inbound changes.
    assert db.one('SELECT sold FROM grants WHERE user_id=? AND closed=0', (actor['id'],))['sold'] == 300 * 1024**3
    plan = service.plan_bulk(actor, ids, 'disable', [])
    service.run_bulk_job(service.create_bulk_job(actor, 'disable', [], plan, 'Turn off'))
    assert all(panel.rows[f'bulk-{n}']['enable'] is False for n in range(3))
    plan = service.plan_bulk(actor, ids, 'move', [2])
    service.run_bulk_job(service.create_bulk_job(actor, 'move', [2], plan, 'Move'))
    assert all(placements(db, cid) == [2] for cid in ids)


def test_reseller_only_gets_permitted_inbounds(setup):
    _, panel, db, service, admin, actor, other = setup
    other_row = db.one('SELECT * FROM users WHERE id=?', (other,))
    op = sale(service, other_row, email='other-one', inbounds=[1])
    plan = service.plan_bulk(admin, [op['customer_id']], 'add', [2])
    assert plan[0]['skip'] == 'bulk_inbound_denied'


def test_interrupted_job_is_marked(setup):
    _, panel, db, service, admin, actor, _ = setup
    op = sale(service, actor, email='half', inbounds=[1])
    job = service.create_bulk_job(actor, 'add', [2], service.plan_bulk(actor, [op['customer_id']], 'add', [2]), 'Add')
    service.interrupt_jobs()
    row = db.one('SELECT * FROM jobs WHERE id=?', (job,))
    assert row['status'] == 'interrupted' and row['skipped'] == 1 and placements(db, op['customer_id']) == [1]


def test_web_flow_select_preview_apply(client, setup):
    _, panel, db, service, admin, actor, _ = setup
    ops = [sale(service, actor, email=f'web-{n}', inbounds=[1]) for n in range(2)]
    token = login(client)
    listing = client.get('/customers').text
    assert 'id="bulk-form"' in listing and 'data-bulk-item' in listing
    ids = [str(o['customer_id']) for o in ops]
    page = client.post('/bulk', data={'csrf': token, 'ids': ids, 'action': 'add'})
    assert page.status_code == 200 and 'What should happen?' in page.text
    no_pick = client.post('/bulk', data={'csrf': token, 'ids': ids, 'action': 'add', 'stage': 'preview'})
    assert no_pick.status_code == 400 and 'Pick at least one inbound' in no_pick.text
    preview = client.post('/bulk', data={'csrf': token, 'ids': ids, 'action': 'add', 'pick': '2', 'stage': 'preview'})
    assert 'Apply to 2 customers' in preview.text and 'Inbound 2' in preview.text
    assert placements(db, ops[0]['customer_id']) == [1]          # preview changes nothing
    done = client.post('/bulk', data={'csrf': token, 'ids': ids, 'action': 'add', 'pick': '2', 'stage': 'apply'}, follow_redirects=False)
    assert done.status_code == 303 and done.headers['location'].startswith('/jobs/')
    job = client.get(done.headers['location']).text
    assert 'Finished' in job and 'data-auto-refresh' not in job
    assert placements(db, ops[1]['customer_id']) == [1, 2]
    assert 'changed many customers' in client.get('/activity').text


def test_change_all_matching_filter(client, setup):
    _, panel, db, service, admin, actor, _ = setup
    keep = sale(service, actor, email='keep-me', inbounds=[1])
    target = sale(service, actor, email='target-me', inbounds=[1])
    token = login(client)
    page = client.get('/bulk?q=target&action=add&pick=2')
    assert '1 customers' in page.text and 'Picked by the list filters' in page.text
    client.post('/bulk', data={'csrf': token, 'q': 'target', 'action': 'add', 'pick': '2', 'stage': 'apply'})
    assert placements(db, target['customer_id']) == [1, 2] and placements(db, keep['customer_id']) == [1]


def test_admin_inbounds_page_shortcuts(client, setup):
    _, panel, db, service, admin, actor, _ = setup
    sale(service, actor, email='on-one', inbounds=[1])
    login(client, 'admin')
    page = client.get('/inbounds').text
    assert '/bulk?action=add&amp;pick=1' in page and 'action=remove&amp;pick=1&amp;inbound=1' in page
    assert client.get('/bulk?action=remove&pick=1&inbound=1').status_code == 200
