import json
import uuid
from concurrent.futures import ThreadPoolExecutor
import pytest
from reseller.db import now
from reseller.domain import GIB, RuleError, volume
from conftest import form,sale


def grant(db,actor):
    return db.one('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC',(actor['id'],))


def test_create_delete_never_refunds(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    g=grant(db,actor)
    assert g['sold']==100*GIB and g['allowance']-g['sold']==400*GIB and g['reserved']==0
    assert panel.rows['customer-one']['group']=='sales-team'
    delete=service.reserve(actor,{},str(uuid.uuid4()),'delete',op['customer_id'])
    service.execute(delete['id'])
    assert grant(db,actor)['sold']==100*GIB
    assert panel.rows=={}
    assert db.one('SELECT status FROM customers')['status']=='deleted'


@pytest.mark.parametrize('new,expected', [('150',150),('70',70)])
def test_limit_changes(setup,new,expected):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor)
    changed=service.reserve(actor,{'volume':new},str(uuid.uuid4()),'update',op['customer_id'])
    service.execute(changed['id'])
    assert grant(db,actor)['sold']==expected*GIB


def test_decrease_refunds_unused_quota_and_reincrease_charges_difference(setup):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor)
    for limit in ['70','100']:
        update=service.reserve(actor,{'volume':limit},str(uuid.uuid4()),'update',op['customer_id'])
        service.execute(update['id'])
    assert grant(db,actor)['sold']==100*GIB
    assert db.one("SELECT sum(amount) n FROM ledger WHERE kind='refund'")['n']==30*GIB


def test_refund_is_capped_by_measured_customer_usage(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    panel.rows['customer-one']['traffic']={'up':40*GIB,'down':40*GIB}
    changed=service.reserve(actor,{'volume':'5'},str(uuid.uuid4()),'update',op['customer_id'])
    service.execute(changed['id'])
    assert grant(db,actor)['sold']==80*GIB
    assert db.one("SELECT amount FROM ledger WHERE kind='refund'")['amount']==20*GIB


def test_missing_master_customer_is_soft_deleted_and_audited(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    panel.rows.pop('customer-one')
    service.refresh()
    customer=db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
    assert customer['status']=='deleted'
    assert customer['issue']=='remote_missing'
    assert grant(db,actor)['sold']==100*GIB
    event=db.one("SELECT * FROM audit WHERE action='customer_remote_missing'")
    assert event['actor'] is None and event['subject']==str(op['customer_id'])


def test_non_quota_change_and_multi_inbound_aggregate_once(setup):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor,inbounds=[1,2])
    update=service.reserve(actor,{'comment':'hello'},str(uuid.uuid4()),'update',op['customer_id'])
    service.execute(update['id']);service.refresh()
    assert grant(db,actor)['sold']==100*GIB
    assert db.one('SELECT used FROM customers')['used']==10


def test_insufficient_before_external_write(setup):
    _,panel,db,service,_,actor,_=setup
    sale(service,actor,volume='420')
    with pytest.raises(RuleError,match='insufficient_quota'):
        sale(service,actor,email='second-customer')
    assert panel.count==1
    assert grant(db,actor)['sold']==420*GIB


def test_concurrency_no_oversell(setup):
    _,panel,db,service,_,actor,_=setup
    def work(i):
        try:
            sale(service,actor,email='client-'+str(i),volume='300')
            return True
        except RuleError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(work,[1,2]))
    assert sorted(results)==[False,True]
    assert grant(db,actor)['sold']==300*GIB
    assert panel.count==1


def test_timeout_reconciles_without_duplicate(setup):
    _,panel,db,service,_,actor,_=setup
    panel.mode='timeout'
    oid=str(uuid.uuid4())
    data=form()
    op=service.reserve(actor,data,oid)
    service.execute(oid)
    assert db.one('SELECT status FROM operations')['status']=='committed'
    service.reserve(actor,data,oid);service.execute(oid);service.reconcile(oid)
    assert panel.count==1 and grant(db,actor)['sold']==100*GIB


def test_partial_holds_reservation(setup):
    _,panel,db,service,_,actor,_=setup
    panel.mode='partial'
    op=sale(service,actor,inbounds=[1,2])
    assert db.one('SELECT status FROM operations')['status']=='uncertain'
    assert grant(db,actor)['reserved']==100*GIB and grant(db,actor)['sold']==0
    service.execute(op['id'])
    service.release(op['id'],'timeout',unsent=True)
    assert panel.count==1 and grant(db,actor)['reserved']==100*GIB
    assert db.all('SELECT status FROM placements ORDER BY inbound_id')==[{'status':'verified'},{'status':'mismatch'}]


def test_preflight_error_releases_unsent(setup):
    _,panel,db,service,_,actor,_=setup
    panel.mode='unavailable'
    sale(service,actor)
    assert grant(db,actor)['sold']==0 and grant(db,actor)['reserved']==0
    assert db.one('SELECT status FROM operations')['status']=='failed'


@pytest.mark.parametrize('kind',['create','update','delete'])
def test_expired_read_only(setup,kind):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor)
    with db.transaction() as conn:
        conn.execute('UPDATE grants SET starts=?,expires=? WHERE user_id=?',(now()-100,now()-1,actor['id']))
    with pytest.raises(RuleError,match='plan_expired'):
        service.reserve(actor,form(email='second'),str(uuid.uuid4()),kind,None if kind=='create' else op['customer_id'])
    assert db.one('SELECT status FROM customers')['status']=='active'


def test_renew_preserves_history(setup):
    _,_,db,service,admin,actor,_=setup
    sale(service,actor)
    service.renew(admin,actor['id'],'200',now()+100000)
    assert len(db.all('SELECT * FROM grants WHERE user_id=?',(actor['id'],)))==2
    assert grant(db,actor)['sold']==0 and grant(db,actor)['allowance']==200*GIB
    assert db.one('SELECT sum(amount) n FROM ledger WHERE user_id=? AND kind=\'sale\'',(actor['id'],))['n']==100*GIB


@pytest.mark.parametrize('value',['0','-1','NaN','Infinity','-Infinity','1e999','abc','','1,2','0.00000000001'])
def test_bad_volume(value):
    with pytest.raises(RuleError):volume(value)


def test_decimal_conversion():
    assert volume('0.5')==536870912


def test_ledger_immutable(setup):
    import sqlite3
    _,_,db,_,_,_,_=setup
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:conn.execute('DELETE FROM ledger')


def test_idempotency_payload_binding(setup):
    _,_,_,service,_,actor,other=setup
    oid=str(uuid.uuid4());service.reserve(actor,form(),oid)
    with pytest.raises(RuleError,match='operation_conflict'):
        service.reserve(actor,form(volume='150'),oid)


def test_ownership_permissions_and_group(setup):
    _,panel,db,service,_,actor,other=setup
    op=sale(service,actor,group='attacker')
    assert panel.rows['customer-one']['group']==actor['tag']
    outsider=db.one('SELECT * FROM users WHERE id=?',(other,))
    with pytest.raises(RuleError,match='not_found'):
        service.reserve(outsider,{},str(uuid.uuid4()),'delete',op['customer_id'])
    with pytest.raises(RuleError,match='inbound_denied'):
        service.reserve(outsider,form(email='other-customer',inbounds=[2]),str(uuid.uuid4()))


def test_no_adoption_of_existing_client(setup):
    _,panel,db,service,_,actor,_=setup
    panel.rows['customer-one']={'id':99,'email':'customer-one','subId':'unrelated','uuid':'','group':'foreign','inboundIds':[1]}
    sale(service,actor)
    assert panel.rows['customer-one']['group']=='foreign' and panel.count==0
    assert grant(db,actor)['sold']==0


def test_remote_identity_drift_blocks_delete(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    panel.rows['customer-one']['id']=999
    delete=service.reserve(actor,{},str(uuid.uuid4()),'delete',op['customer_id'])
    service.execute(delete['id'])
    assert 'customer-one' in panel.rows
    assert db.one('SELECT status FROM operations WHERE id=?',(delete['id'],))['status']=='failed'


@pytest.mark.parametrize('iid,key',[(1,'id'),(3,'password'),(4,'id'),(5,'password'),(6,'auth'),(7,'publicKey'),(8,'publicKey'),(9,'secret'),(10,'id')])
def test_all_client_protocols(setup,iid,key):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor,inbounds=[iid])
    c=db.one('SELECT * FROM customers WHERE id=?',(op['customer_id'],))
    assert c['status']=='active'
    assert key in json.loads(c['payload'])
    if iid not in (1,4,10):
        assert c['uuid'] is None


def test_first_use_expiration_is_negative_duration(setup):
    _,panel,db,service,_,actor,_=setup
    sale(service,actor,expiry=-30*86400000)
    assert panel.rows['customer-one']['expiryTime']==-30*86400000
    for ib in panel.inbounds:
        for c in ib['settings']['clients']:c['expiryTime']=(now()+86400)*1000
    panel.rows['customer-one']['expiryTime']=(now()+86400)*1000
    service.refresh()
    assert db.one('SELECT issue FROM customers')['issue']==''


def test_credential_edit_preserves_sales(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    new_uuid=str(uuid.uuid4())
    update=service.reserve(actor,{'uuid':new_uuid,'sub_id':'new-subscription'},str(uuid.uuid4()),'update',op['customer_id'])
    service.execute(update['id'])
    c=db.one('SELECT * FROM customers')
    assert c['uuid']==new_uuid and c['sub_id']=='new-subscription'
    assert grant(db,actor)['sold']==100*GIB


def test_admin_repairs_partial_without_double_charge(setup):
    _,panel,db,service,admin,actor,_=setup
    panel.mode='partial'
    op=sale(service,actor,inbounds=[1,2])
    panel.mode='ok'
    service.repair(admin,op['id'])
    assert db.one('SELECT status FROM operations')['status']=='committed'
    assert grant(db,actor)['sold']==100*GIB and panel.count==1


def test_restored_sales_lock(setup):
    _,_,db,service,_,actor,_=setup
    with db.transaction() as conn:conn.execute("INSERT INTO state VALUES('write_lock','restored')")
    with pytest.raises(RuleError,match='restore_locked'):sale(service,actor)


def test_remote_node_timeout_requires_acknowledgement(setup):
    _,panel,db,service,admin,actor,_=setup
    panel.inbounds[0]['nodeId']=1
    panel.mode='timeout'
    op=sale(service,actor)
    assert db.one('SELECT status FROM operations')['status']=='uncertain'
    assert grant(db,actor)['reserved']==100*GIB
    panel.mode='ok'
    service.repair(admin,op['id'])
    assert grant(db,actor)['sold']==100*GIB


def test_activation_not_reset_by_edit(setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor,expiry=-30*86400000)
    activated=(now()+10000)*1000
    panel.rows['customer-one']['expiryTime']=activated
    for ib in panel.inbounds:
        for c in ib['settings']['clients']:c['expiryTime']=activated
    update=service.reserve(actor,{'comment':'changed'},str(uuid.uuid4()),'update',op['customer_id'])
    service.execute(update['id'])
    assert panel.rows['customer-one']['expiryTime']==activated


def test_suspend_between_reservation_and_send(setup):
    _,panel,db,service,_,actor,_=setup
    op=service.reserve(actor,form(),str(uuid.uuid4()))
    with db.transaction() as conn:conn.execute('UPDATE users SET suspended=1 WHERE id=?',(actor['id'],))
    service.execute(op['id'])
    assert panel.count==0 and grant(db,actor)['reserved']==0


def test_reseller_suspension_disables_existing_customers_without_deleting_and_restores_setting(setup):
    _,panel,db,service,_,actor,_=setup
    created=sale(service,actor)
    customer=db.one('SELECT * FROM customers WHERE id=?',(created['customer_id'],))
    assert panel.rows[customer['email']]['enable'] is True
    with db.transaction() as conn:conn.execute('UPDATE users SET suspended=1 WHERE id=?',(actor['id'],))
    assert service.set_reseller_customer_enabled(actor['id'],False)==0
    assert panel.rows[customer['email']]['enable'] is False
    assert db.one('SELECT status FROM customers WHERE id=?',(customer['id'],))['status']=='active'
    assert json.loads(db.one('SELECT payload FROM customers WHERE id=?',(customer['id'],))['payload'])['enable'] is True
    service.refresh()
    assert db.one('SELECT issue FROM customers WHERE id=?',(customer['id'],))['issue']==''
    with db.transaction() as conn:conn.execute('UPDATE users SET suspended=0 WHERE id=?',(actor['id'],))
    assert service.set_reseller_customer_enabled(actor['id'],True)==0
    assert panel.rows[customer['email']]['enable'] is True


def test_reseller_plan_expiry_does_not_disable_existing_customer(setup):
    _,panel,db,service,_,actor,_=setup
    created=sale(service,actor)
    with db.transaction() as conn:
        conn.execute('UPDATE grants SET starts=?,expires=? WHERE user_id=?',(now()-100,now()-1,actor['id']))
    service.refresh()
    customer=db.one('SELECT status FROM customers WHERE id=?',(created['customer_id'],))
    assert customer['status']=='active'
    assert panel.rows['customer-one']['enable'] is True


def test_definitely_unsent_retry_uses_new_reservation_once(setup):
    _,panel,db,service,_,actor,_=setup
    panel.mode='unavailable'
    old=sale(service,actor)
    panel.mode='ok'
    new_id=str(uuid.uuid4())
    retried=service.retry_unsent(actor,old['id'],new_id)
    service.execute(retried['id'])
    service.retry_unsent(actor,old['id'],new_id)
    service.execute(new_id)
    assert panel.count==1
    assert grant(db,actor)['sold']==100*GIB and grant(db,actor)['reserved']==0
    assert len(db.all('SELECT * FROM customers'))==1


def test_uncertain_create_cannot_use_unsent_retry(setup):
    _,panel,db,service,_,actor,_=setup
    panel.mode='partial'
    old=sale(service,actor,inbounds=[1,2])
    with pytest.raises(RuleError,match='reconcile_required'):
        service.retry_unsent(actor,old['id'],str(uuid.uuid4()))


def test_failed_logins_from_one_ip_do_not_lock_out_others(setup):
    from reseller.security import authenticate
    _,_,db,*_=setup
    for _ in range(9):
        assert authenticate(db,'admin','wrong-password','198.51.100.1') is None
    assert authenticate(db,'admin','A-long-test-password!','198.51.100.1') is None
    assert authenticate(db,'admin','A-long-test-password!','198.51.100.2')['username']=='admin'
    assert not db.one("SELECT 1 x FROM audit WHERE outcome='limited'")


def test_anonymous_sessions_are_capped(setup, monkeypatch):
    from reseller import security
    config,_,db,*_=setup
    monkeypatch.setattr(security,'ANONYMOUS_SESSION_CAP',5)
    for _ in range(12):
        with db.transaction() as conn:
            security.new_session(conn,config)
    assert db.one('SELECT count(*) n FROM sessions WHERE user_id IS NULL')['n']==5


def test_admin_can_delete_customer_of_expired_suspended_reseller(setup):
    _,panel,db,service,admin,actor,_=setup
    op=sale(service,actor)
    with db.transaction() as conn:
        conn.execute('UPDATE grants SET expires=starts+1 WHERE user_id=?',(actor['id'],))
        conn.execute('UPDATE users SET suspended=1 WHERE id=?',(actor['id'],))
        conn.execute('DELETE FROM permissions WHERE user_id=?',(actor['id'],))
    with pytest.raises(RuleError):
        service.reserve(dict(actor,suspended=0),{},str(uuid.uuid4()),'delete',op['customer_id'])
    delete=service.reserve(admin,{},str(uuid.uuid4()),'delete',op['customer_id'])
    service.execute(delete['id'])
    assert db.one('SELECT status FROM operations WHERE id=?',(delete['id'],))['status']=='committed'
    assert db.one('SELECT status FROM customers WHERE id=?',(op['customer_id'],))['status']=='deleted'
    assert 'customer-one' not in panel.rows
