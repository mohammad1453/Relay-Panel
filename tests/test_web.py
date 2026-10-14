import re
import uuid
from dataclasses import replace
from fastapi.testclient import TestClient
import pytest
from reseller.web import create_app
from reseller.db import now
from conftest import sale


def csrf(client,path='/login'):
    response=client.get(path)
    assert response.status_code==200,response.text
    return re.search(r'name="csrf" value="([^"]+)"',response.text).group(1)


def login(client,username='reseller',password='A-long-test-password!'):
    token=csrf(client)
    response=client.post('/login',data={'csrf':token,'username':username,'password':password},follow_redirects=False)
    assert response.status_code==303,response.text
    return csrf(client,'/account')


@pytest.fixture
def client(setup):
    config,_,_,service,*_=setup
    with TestClient(create_app(config,service.remote,background=False)) as c:
        yield c


def test_unauthenticated_redirect_and_csrf(client):
    assert client.get('/customers',follow_redirects=False).status_code==303
    assert client.post('/login',data={'username':'admin','password':'x'}).status_code==403
    assert client.post('/customers/new').status_code==403


def test_session_rotation_logout_and_headers(client):
    csrf(client)
    old=client.cookies.get('relay_session')
    token=login(client)
    assert client.cookies.get('relay_session')!=old
    response=client.get('/')
    assert "frame-ancestors 'none'" in response.headers['content-security-policy']
    assert response.headers['cache-control']=='no-store'
    assert response.headers['referrer-policy']=='same-origin'
    assert client.post('/logout',data={'csrf':token},follow_redirects=False).status_code==303
    assert client.get('/',follow_redirects=False).status_code==303


def test_reseller_cannot_access_admin(client):
    token=login(client)
    for route in ['/resellers','/resellers/new','/operations','/inbounds']:
        assert client.get(route).status_code==403
    for route in ['/sync','/resellers/new','/resellers/1/renew']:
        assert client.post(route,data={'csrf':token}).status_code==403


def test_cross_account_read_and_mutation(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    token=login(client,'other','Another-test-password!')
    for suffix in ['', '/edit','/links','/qr/connection/0']:
        assert client.get('/customers/'+str(op['customer_id'])+suffix).status_code==404
    assert client.post('/customers/'+str(op['customer_id'])+'/delete',data={'csrf':token,'operation_id':str(uuid.uuid4())}).status_code==404


def test_origin_rejection(client):
    token=login(client)
    assert client.post('/logout',data={'csrf':token},headers={'Origin':'https://evil.invalid'}).status_code==403
    assert client.post('/logout',data={'csrf':token},headers={'Origin':'null'}).status_code==403


def test_public_http_path_prefix(setup):
    config,_,_,service,*_=setup
    config=replace(config,origin='http://203.0.113.10:8095',base_path='/relay-secret',public_http=True)
    with TestClient(create_app(config,service.remote,background=False),base_url=config.origin) as prefixed:
        prefixed.cookies.set('relay_session','obsolete-root-cookie',path='/')
        assert prefixed.get('/login').status_code==404
        login_page=prefixed.get('/relay-secret/login')
        assert login_page.status_code==200
        assert '<base href="/relay-secret/">' in login_page.text
        assert prefixed.get('/relay-secret/static/app.css').status_code==200
        token=re.search(r'name="csrf" value="([^"]+)"',login_page.text).group(1)
        response=prefixed.post('/relay-secret/login',data={'csrf':token,'username':'admin','password':'A-long-test-password!'},headers={'Origin':config.origin},follow_redirects=False)
        assert response.status_code==303
        assert response.headers['location']=='/relay-secret/'
        cookies=response.headers.get_list('set-cookie')
        assert any('Max-Age=0' in value and 'Path=/' in value for value in cookies)
        assert any('relay_session_' in value and 'Path=/relay-secret/' in value and 'Max-Age=43200' in value for value in cookies)


def test_oversized_and_too_many_fields(client):
    assert client.post('/login',content=b'x'*65537).status_code==413
    assert client.post('/login',content=b'k=v&'*101).status_code==403


def test_all_pages_render_and_persian(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    login(client,'admin')
    routes=['/','/resellers','/resellers/new','/resellers/'+str(actor['id']),'/customers','/customers/'+str(op['customer_id']),'/customers/'+str(op['customer_id'])+'/edit','/customers/'+str(op['customer_id'])+'/links','/operations','/inbounds','/account']
    for lang in ['en','fa']:
        client.get('/language?lang='+lang)
        for route in routes:
            response=client.get(route)
            assert response.status_code==200,(route,response.text)
            assert ('dir="rtl"' if lang=='fa' else 'dir="ltr"') in response.text
            assert 'fixture-server-secret' not in response.text


def test_create_workflow(client,setup):
    _,panel,db,_,_,actor,_=setup
    token=login(client)
    assert client.get('/customers/new').status_code==200
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'web-client','volume':'100','first_use':'on','days':'30','enable':'on','inbounds':'1','group':'wrong'},follow_redirects=False)
    assert response.status_code==303,response.text
    assert panel.rows['web-client']['group']=='sales-team'
    detail=client.get(response.headers['location'])
    assert detail.status_code==200
    assert 'Customer created successfully and is active' in detail.text
    assert db.one('SELECT package FROM customers')['package']==100*1024**3


def test_over_quota_customer_create_shows_inline_message(client):
    token=login(client)
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'over-quota','volume':'501','first_use':'on','days':'30','enable':'on','inbounds':'1'},follow_redirects=False)
    assert response.status_code==200
    assert 'This amount is greater than your remaining reseller quota' in response.text
    assert '501' in response.text


def test_password_change_revokes_other_sessions(client,setup):
    config,_,db,service,_,_,_=setup
    token=login(client)
    other=TestClient(create_app(config,service.remote,background=False))
    login(other)
    response=client.post('/account',data={'csrf':token,'current_password':'A-long-test-password!','new_password':'A-different-test-password!'},follow_redirects=False)
    assert response.status_code==303
    assert other.get('/',follow_redirects=False).status_code==303
    assert client.get('/').status_code==200


def test_expired_can_read_not_write(client,setup):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor)
    with db.transaction() as conn:conn.execute('UPDATE grants SET starts=?,expires=? WHERE user_id=?',(now()-100,now()-1,actor['id']))
    token=login(client)
    assert client.get('/').status_code==200
    assert client.get('/customers/'+str(op['customer_id'])+'/links').status_code==200
    assert client.get('/customers/new').status_code==403
    assert client.post('/customers/'+str(op['customer_id'])+'/delete',data={'csrf':token,'operation_id':str(uuid.uuid4())}).status_code==403


def test_login_rate_limit_and_no_enumeration(client):
    token=csrf(client)
    for _ in range(9):
        assert client.post('/login',data={'csrf':token,'username':'reseller','password':'wrong'}).status_code==401
    assert client.post('/login',data={'csrf':token,'username':'reseller','password':'A-long-test-password!'}).status_code==401


def test_connection_links_show_separate_subscription_and_uri_sections_without_qr(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor);login(client)
    response=client.get('/customers/'+str(op['customer_id'])+'/links')
    assert response.status_code==200
    assert 'Subscription link' in response.text and 'Configuration URIs' in response.text
    assert '<img' not in response.text.lower() and '/qr/' not in response.text.lower()


def test_admin_edits_reseller_revokes_sessions(client,setup):
    _,_,db,_,_,actor,_=setup
    token=login(client,'admin')
    response=client.post('/resellers/'+str(actor['id']),data={'csrf':token,'username':'renamed-reseller','inbounds':'1','notes':'Reviewed'},follow_redirects=False)
    assert response.status_code==303
    row=db.one('SELECT * FROM users WHERE id=?',(actor['id'],))
    assert row['username']=='renamed-reseller' and row['notes']=='Reviewed'
    assert row['tag']=='sales-team'


def test_admin_suspension_disables_and_restores_reseller_customers(client,setup):
    _,panel,db,service,_,actor,_=setup
    operation=sale(service,actor)
    customer=db.one('SELECT email FROM customers WHERE id=?',(operation['customer_id'],))
    token=login(client,'admin')
    response=client.post('/resellers/'+str(actor['id']),data={'csrf':token,'username':actor['username'],'suspended':'on','inbounds':['1']},follow_redirects=False)
    assert response.status_code==303
    assert db.one('SELECT status FROM customers WHERE id=?',(operation['customer_id'],))['status']=='active'
    assert panel.rows[customer['email']]['enable'] is False
    token=csrf(client,'/resellers/'+str(actor['id']))
    response=client.post('/resellers/'+str(actor['id']),data={'csrf':token,'username':actor['username'],'inbounds':['1']},follow_redirects=False)
    assert response.status_code==303
    assert panel.rows[customer['email']]['enable'] is True
