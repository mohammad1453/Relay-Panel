"""Resellers ask for more data; the admin approves or declines."""
import pytest
from reseller.db import now
from reseller.domain import RuleError

GIB = 1024**3


def plan(db, uid):
    return db.one('SELECT * FROM grants WHERE user_id=? AND closed=0 ORDER BY id DESC LIMIT 1', (uid,))


def test_request_approve_tops_up_active_plan(setup):
    _, _, db, service, admin, actor, _ = setup
    before = plan(db, actor['id'])
    rid = service.create_request(actor, '100', 10, 'busy month')
    with pytest.raises(RuleError) as error:
        service.create_request(actor, '50', 0)
    assert error.value.code == 'request_pending'
    with pytest.raises(RuleError):
        service.decide_request(actor, rid, True)            # resellers cannot approve
    service.decide_request(admin, rid, True, volume_gb='150')
    after = plan(db, actor['id'])
    assert after['allowance'] == before['allowance'] + 150 * GIB
    assert after['expires'] == before['expires'] + 10 * 86400
    row = db.one('SELECT * FROM requests WHERE id=?', (rid,))
    assert row['status'] == 'approved' and row['volume'] == 150 * GIB
    with pytest.raises(RuleError):
        service.decide_request(admin, rid, False)


def test_request_starts_new_plan_when_expired(setup):
    _, _, db, service, admin, actor, _ = setup
    with db.transaction() as conn:
        conn.execute('UPDATE grants SET starts=?,expires=? WHERE user_id=?', (now()-100, now()-1, actor['id']))
    rid = service.create_request(actor, '20', 30)
    service.decide_request(admin, rid, True)
    new = plan(db, actor['id'])
    assert new['allowance'] == 20 * GIB and new['expires'] > now() + 29 * 86400


def test_request_decline(setup):
    _, _, db, service, admin, actor, _ = setup
    before = plan(db, actor['id'])['allowance']
    rid = service.create_request(actor, '20', 0)
    service.decide_request(admin, rid, False, answer='not this month')
    assert db.one('SELECT status,answer FROM requests WHERE id=?', (rid,))['answer'] == 'not this month'
    assert plan(db, actor['id'])['allowance'] == before
    service.create_request(actor, '10', 0)   # a new request is allowed after a decision
