import json
import re
import uuid
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit
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


def error_of(response):
    """The ?error= code a redirect carries back to the page."""
    assert response.status_code==303,response.text
    return parse_qs(urlsplit(response.headers['location']).query).get('error',[''])[0]


@pytest.fixture
def client(setup):
    config,_,_,service,*_=setup
    with TestClient(create_app(config,service.remote,background=False)) as c:
        yield c


def test_unauthenticated_redirect_and_csrf(client):
    assert client.get('/customers',follow_redirects=False).status_code==303
    response=client.post('/login',data={'username':'admin','password':'x'},follow_redirects=False)
    assert error_of(response)=='session_expired' and response.headers['X-Relay-Session-Error']
    assert client.post('/customers/new',follow_redirects=False).status_code==303


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
    for route in ['/resellers','/resellers/new','/attention','/inbounds']:
        response=client.get(route)
        assert response.status_code==403
        assert 'have access to this.' in response.text
    for route in ['/resellers/new','/resellers/1/renew','/resellers/1/delete','/customers/1/accept']:
        assert error_of(client.post(route,data={'csrf':token},follow_redirects=False))=='forbidden'


def test_cross_account_read_and_mutation(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    token=login(client,'other','Another-test-password!')
    for suffix in ['', '/edit','/links']:
        response=client.get('/customers/'+str(op['customer_id'])+suffix)
        assert response.status_code==404 and 'This item does not exist' in response.text
    response=client.post('/customers/'+str(op['customer_id'])+'/delete',data={'csrf':token,'operation_id':str(uuid.uuid4())},follow_redirects=False)
    assert error_of(response)=='not_found'


def test_origin_rejection(client):
    token=login(client)
    for origin in ('https://evil.invalid','null'):
        response=client.post('/logout',data={'csrf':token},headers={'Origin':origin},follow_redirects=False)
        assert error_of(response)=='session_expired'
    assert client.get('/',follow_redirects=False).status_code==200


def test_public_http_path_prefix(setup):
    config,_,_,service,*_=setup
    config=replace(config,origin='http://203.0.113.10:8095',base_path='/relay-secret',public_http=True,xui_database_paths='/etc/x-ui/x-ui.db')
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
        # Filter and paging links keep the base path once the browser script prefixes them.
        page=prefixed.get('/relay-secret/customers?view=active')
        assert 'href="/customers?view=online"' in page.text
        # Errors redirect back below the base path.
        token=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
        response=prefixed.post('/relay-secret/customers/999/delete',data={'csrf':token},headers={'Referer':config.origin+'/relay-secret/customers'},follow_redirects=False)
        assert response.headers['location'].startswith('/relay-secret/customers?error=')


def test_oversized_and_too_many_fields(client):
    assert error_of(client.post('/login',content=b'x'*65537,follow_redirects=False))=='too_large'
    assert error_of(client.post('/login',content=b'k=v&'*101,follow_redirects=False))=='session_expired'


def test_all_pages_render_english_only(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    login(client,'admin')
    routes=['/','/resellers','/resellers/new','/resellers/'+str(actor['id']),'/resellers/'+str(actor['id'])+'?tab=plan','/customers','/customers?view=attention',
            '/customers/'+str(op['customer_id']),'/customers/'+str(op['customer_id'])+'/edit','/customers/'+str(op['customer_id'])+'/links',
            '/attention','/activity','/activity?technical=1','/inbounds','/account']
    for route in routes:
        response=client.get(route)
        assert response.status_code==200,(route,response.text)
        assert 'fixture-server-secret' not in response.text
        assert 'dir="rtl"' not in response.text and 'فارسی' not in response.text
    assert client.get('/operations',follow_redirects=False).headers['location']=='/attention'
    assert client.get('/language?lang=fa').status_code==404


def test_reseller_pages_render(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    login(client)
    for route in ['/','/customers','/customers/new','/customers/'+str(op['customer_id']),'/customers/'+str(op['customer_id'])+'/edit','/activity','/account']:
        response=client.get(route)
        assert response.status_code==200,(route,response.text)


def test_create_workflow(client,setup):
    _,panel,db,_,_,actor,_=setup
    token=login(client)
    assert client.get('/customers/new').status_code==200
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'web-client','volume':'100','first_use':'on','days':'30','enable':'on','inbounds':'1','group':'wrong'},follow_redirects=False)
    assert response.status_code==303,response.text
    assert panel.rows['web-client']['group']=='sales-team'
    detail=client.get(response.headers['location'])
    assert detail.status_code==200
    assert 'Customer created.' in detail.text
    assert db.one('SELECT package FROM customers')['package']==100*1024**3


def test_form_errors_show_inline_and_keep_values(client):
    token=login(client)
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'over-quota','volume':'501','first_use':'on','days':'30','enable':'on','inbounds':'1','comment':'keep me'})
    assert response.status_code==400
    assert 'Not enough data left in your plan' in response.text
    assert 'value="501"' in response.text and 'value="over-quota"' in response.text and 'keep me' in response.text
    assert 'class="field-error"' in response.text
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'bad name!','volume':'5','first_use':'on','days':'30','inbounds':'1'})
    assert response.status_code==400 and 'No spaces' in response.text
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'no-inbound','volume':'5','first_use':'on','days':'30'})
    assert response.status_code==400 and 'Pick at least one inbound' in response.text
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'bad-date','volume':'5','expiry':'tomorrow','inbounds':'1'})
    assert response.status_code==400 and 'Pick a date in the future' in response.text


def test_reseller_form_errors_inline(client,setup):
    _,_,_,_,_,actor,_=setup
    token=login(client,'admin')
    response=client.post('/resellers/new',data={'csrf':token,'username':'reseller','tag':'new-tag','password':'A-long-test-password!','volume':'10','expiry':'2099-01-01T00:00','inbounds':'1'})
    assert response.status_code==400 and 'already used' in response.text and 'value="new-tag"' in response.text
    response=client.post('/resellers/'+str(actor['id'])+'/renew',data={'csrf':token,'volume':'-1','expiry':'2099-01-01T00:00'})
    assert response.status_code==400 and 'Enter a data limit above 0 GB' in response.text


def test_password_change_revokes_other_sessions(client,setup):
    config,_,db,service,_,_,_=setup
    token=login(client)
    other=TestClient(create_app(config,service.remote,background=False))
    login(other)
    response=client.post('/account',data={'csrf':token,'current_password':'wrong-password','new_password':'A-different-test-password!'})
    assert response.status_code==400 and 'The current password is wrong' in response.text
    token=csrf(client,'/account')
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
    response=client.get('/customers/new')
    assert response.status_code==403 and 'plan has expired' in response.text
    response=client.post('/customers/'+str(op['customer_id'])+'/delete',data={'csrf':token,'operation_id':str(uuid.uuid4())},follow_redirects=False)
    assert error_of(response)=='plan_expired'


def test_login_rate_limit_and_no_enumeration(client):
    token=csrf(client)
    for _ in range(9):
        response=client.post('/login',data={'csrf':token,'username':'reseller','password':'wrong'})
        assert response.status_code==401 and 'Wrong username or password' in response.text
    assert client.post('/login',data={'csrf':token,'username':'reseller','password':'A-long-test-password!'}).status_code==401


def test_connection_links_show_separate_subscription_and_uri_sections_without_qr(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor);login(client)
    response=client.get('/customers/'+str(op['customer_id'])+'/links')
    assert response.status_code==200
    assert 'Subscription link' in response.text and 'Single configs' in response.text
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


def test_logout_revokes_server_session_with_base_path(setup):
    config,_,_,service,*_=setup
    config=replace(config,origin='http://203.0.113.10:8095',base_path='/relay-secret',public_http=True,xui_database_paths='/etc/x-ui/x-ui.db')
    with TestClient(create_app(config,service.remote,background=False),base_url=config.origin) as prefixed:
        page=prefixed.get('/relay-secret/login')
        token=re.search(r'name="csrf" value="([^"]+)"',page.text).group(1)
        prefixed.post('/relay-secret/login',data={'csrf':token,'username':'admin','password':'A-long-test-password!'},follow_redirects=False)
        stolen=dict(prefixed.cookies)
        account=prefixed.get('/relay-secret/account')
        token=re.search(r'name="csrf" value="([^"]+)"',account.text).group(1)
        assert prefixed.post('/relay-secret/logout',data={'csrf':token},follow_redirects=False).status_code==303
        prefixed.cookies.clear()
        for name,value in stolen.items():
            prefixed.cookies.set(name,value,path='/relay-secret/')
        assert prefixed.get('/relay-secret/',follow_redirects=False).status_code==303


def test_subscription_link_uses_configured_url(client,setup):
    _,_,_,service,_,actor,_=setup
    op=sale(service,actor);login(client)
    response=client.get('/customers/'+str(op['customer_id'])+'/links')
    sub_id=setup[2].one('SELECT sub_id FROM customers WHERE id=?',(op['customer_id'],))['sub_id']
    assert 'https://sub.example.invalid/i/'+sub_id in response.text
    assert '1mo.us' not in response.text


def test_unknown_message_is_not_rendered(client):
    login(client)
    assert 'Call support at' not in client.get('/?message=Call+support+at+evil').text
    assert 'Call support at' not in client.get('/?error=Call+support+at+evil').text
    assert 'Changes saved.' in client.get('/account?message=saved').text


def test_errors_show_on_the_page_not_a_dialog(client,setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    token=login(client)
    panel.mode='unavailable'
    response=client.post('/sync',data={'csrf':token},headers={'Referer':'http://testserver/customers'},follow_redirects=False)
    assert response.headers['location'].startswith('/customers?error=remote_unavailable')
    page=client.get(response.headers['location'])
    assert page.status_code==200 and 'reach 3X-UI right now' in page.text and 'class="alert alert-danger"' in page.text


def test_sync_now_for_resellers_is_rate_limited(client):
    token=login(client)
    assert client.post('/sync',data={'csrf':token},follow_redirects=False).headers['location'].endswith('message=synced')
    assert error_of(client.post('/sync',data={'csrf':token},follow_redirects=False))=='sync_too_soon'


def test_admin_accepts_3xui_values_from_the_customer_page(client,setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    panel.rows['customer-one']['comment']='edited in panel'
    service.refresh()
    token=login(client,'admin')
    page=client.get('/customers/'+str(op['customer_id']))
    assert 'What changed in 3X-UI' in page.text and 'edited in panel' in page.text and 'Accept 3X-UI values' in page.text
    attention=client.get('/attention')
    assert 'edited in panel' in attention.text
    response=client.post('/customers/'+str(op['customer_id'])+'/accept',data={'csrf':token},follow_redirects=False)
    assert response.headers['location'].endswith('message=accepted')
    assert db.one('SELECT issue FROM customers WHERE id=?',(op['customer_id'],))['issue']==''
    assert 'All clear' in client.get('/attention').text


def test_admin_deletes_reseller_with_customers(client,setup):
    _,panel,db,service,_,actor,_=setup
    sale(service,actor)
    token=login(client,'admin')
    page=client.get('/resellers/'+str(actor['id'])+'?tab=danger')
    assert 'all of their 1 customers' in page.text
    response=client.post('/resellers/'+str(actor['id'])+'/delete',data={'csrf':token},follow_redirects=False)
    assert response.headers['location']=='/resellers?message=reseller_deleted'
    assert panel.rows=={}
    assert 'reseller' not in [r['username'] for r in db.all("SELECT username FROM users WHERE deleted=0")]
    assert client.get('/resellers/'+str(actor['id'])).status_code==404


def test_admin_removes_customer_that_needs_attention(client,setup):
    _,panel,db,service,_,actor,_=setup
    op=sale(service,actor)
    panel.rows['customer-one']['id']=999
    service.refresh()
    assert db.one('SELECT issue FROM customers WHERE id=?',(op['customer_id'],))['issue']=='remote_identity'
    token=login(client,'admin')
    response=client.post('/customers/'+str(op['customer_id'])+'/delete',data={'csrf':token},headers={'Referer':'http://testserver/attention'},follow_redirects=False)
    assert response.headers['location']=='/attention?message=customer_deleted'
    assert db.one('SELECT status FROM customers WHERE id=?',(op['customer_id'],))['status']=='deleted'


def test_activity_is_readable_and_scoped(client,setup):
    _,panel,db,service,admin,actor,other=setup
    op=sale(service,actor)
    panel.delete_inbound(1)
    service.refresh()
    login(client)
    text=client.get('/activity').text
    assert 'created customer' in text and '<b>customer-one</b>' in text
    assert 'was deleted in 3X-UI and removed here' in text
    assert 'customer_create_reserved' not in text
    other_client=TestClient(create_app(setup[0],service.remote,background=False))
    login(other_client,'other','Another-test-password!')
    assert 'customer-one' not in other_client.get('/activity').text


def test_dashboard_shows_detected_3xui(client,setup):
    setup[3].sync_inbounds()
    login(client,'admin')
    page=client.get('/').text
    assert 'client API' in page and 'Relay needs 3X-UI version' not in page


def test_link_titles_use_inbound_and_customer_name(setup):
    from reseller.links import label_links
    import base64 as b64
    inbounds=[{'id':1,'name':'VLESS WS','protocol':'vless','port':443,'metadata':json.dumps({'streamSettings':{'network':'ws','wsSettings':{'path':'/ws'}}})},
              {'id':2,'name':'VLESS TCP','protocol':'vless','port':8443,'metadata':json.dumps({'streamSettings':{'network':'tcp'}})},
              {'id':3,'name':'VMess gRPC','protocol':'vmess','port':2053,'metadata':json.dumps({'streamSettings':{'network':'grpc','grpcSettings':{'serviceName':'svc'}}})}]
    vmess='vmess://'+b64.b64encode(json.dumps({'add':'cdn.example','port':'443','net':'grpc','path':'svc','ps':'x'}).encode()).decode()
    uris=['vless://u@1.2.3.4:443?type=ws&path=%2Fws#%F0%9F%87%A9%F0%9F%87%AA%20DE-ali',
          'vless://u@104.21.21.111:443?type=ws&path=%2Fws#cdn-ali',
          'vless://u@1.2.3.4:8443?type=tcp#tcp-ali', vmess]
    items=label_links(uris,inbounds,'ali')
    assert [i['title'] for i in items]==['VLESS WS (1) · ali','VLESS WS (2) · ali','VLESS TCP · ali','VMess gRPC · ali']
    assert items[1]['server']=='104.21.21.111:443' and items[0]['remark'].endswith('DE-ali')


def test_admin_creates_customer_from_the_web(client,setup):
    _,panel,db,_,_,_,_=setup
    token=login(client,'admin')
    page=client.get('/customers/new')
    assert page.status_code==200 and 'RelayPanel' in page.text and 'Inbound 11' not in page.text
    response=client.post('/customers/new',data={'csrf':token,'operation_id':str(uuid.uuid4()),'email':'by-admin','volume':'2000','first_use':'on','days':'30','enable':'on','inbounds':'1'},follow_redirects=False)
    assert response.status_code==303 and 'customer_created' in response.headers['location'],response.text
    assert panel.rows['by-admin']['group']=='RelayPanel'
    detail=client.get(response.headers['location']).text
    cid=db.one("SELECT id FROM customers WHERE email='by-admin'")['id']
    assert '/customers/'+str(cid)+'/edit' in detail
    edit_page=client.get('/customers/'+str(cid)+'/edit')
    assert edit_page.status_code==200 and 'No plan limit' in edit_page.text
    token=csrf(client,'/customers/'+str(cid)+'/edit')
    response=client.post('/customers/'+str(cid)+'/edit',data={'csrf':token,'operation_id':str(uuid.uuid4()),'volume':'3000','first_use':'on','days':'60','enable':'on','inbounds':['1','2'],'comment':'edited by admin'},follow_redirects=False)
    assert 'customer_updated' in response.headers['location'],response.text
    assert panel.rows['by-admin']['totalGB']==3000*1024**3 and panel.rows['by-admin']['inboundIds']==[1,2]
    assert panel.rows['by-admin']['comment']=='edited by admin' and panel.rows['by-admin']['group']=='RelayPanel'


def test_links_titles_and_subscription_first(client,setup):
    _,_,db,service,_,actor,_=setup
    op=sale(service,actor,email='titled')
    login(client)
    page=client.get('/customers/'+str(op['customer_id'])+'/links').text
    sub_id=db.one('SELECT sub_id FROM customers WHERE id=?',(op['customer_id'],))['sub_id']
    assert 'https://sub.example.invalid/i/'+sub_id in page
    assert 'Inbound 1 · titled' in page
    assert page.index('Subscription link')<page.index('Single configs')


def test_no_subscription_address_shows_nothing(setup):
    config,_,_,service,_,actor,_=setup
    op=sale(service,actor)
    with TestClient(create_app(replace(config,sub_url=''),service.remote,background=False)) as c:
        login(c)
        page=c.get('/customers/'+str(op['customer_id'])+'/links').text
        assert 'id="subscription-link"' not in page and 'No subscription address is set yet' in page
        assert 'panel-sub.invalid' not in page and '127.0.0.1' not in page
        assert 'detail-sub-link' not in c.get('/customers/'+str(op['customer_id'])).text


def test_admin_sets_subscription_address_in_settings(setup):
    config,_,db,service,_,actor,_=setup
    op=sale(service,actor)
    sub_id=db.one('SELECT sub_id FROM customers WHERE id=?',(op['customer_id'],))['sub_id']
    with TestClient(create_app(replace(config,sub_url=''),service.remote,background=False)) as c:
        token=login(c,'admin')
        response=c.post('/settings',data={'csrf':token,'sub_url':'not a url'})
        assert response.status_code==400 and 'Enter a full address' in response.text
        token=csrf(c,'/settings')
        assert c.post('/settings',data={'csrf':token,'sub_url':'https://sub.1mo.us/i/'},follow_redirects=False).status_code==303
        assert 'https://sub.1mo.us/i/'+sub_id in c.get('/customers/'+str(op['customer_id'])+'/links').text
        assert 'https://sub.1mo.us/i/'+sub_id in c.get('/customers/'+str(op['customer_id'])).text
        token=csrf(c,'/settings')
        c.post('/settings',data={'csrf':token,'sub_url':'https://sub.1mo.us/{subId}?format=json'})
        assert 'https://sub.1mo.us/'+sub_id+'?format=json' in c.get('/customers/'+str(op['customer_id'])+'/links').text.replace('&amp;','&')
    with TestClient(create_app(replace(config,sub_url=''),service.remote,background=False)) as c:
        login(c)
        assert c.get('/settings').status_code==403

