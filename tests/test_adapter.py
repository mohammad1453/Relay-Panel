import json
from dataclasses import replace
import httpx
import pytest
from reseller.xui import XUI,RemoteError


@pytest.mark.parametrize('response',[httpx.Response(200,json={'success':False,'msg':'secret'}),httpx.Response(200,text='<html>login</html>'),httpx.Response(401),httpx.Response(500)])
def test_envelope_and_http_errors(setup,response):
    config,*_=setup
    adapter=XUI(config,httpx.MockTransport(lambda request:response))
    with pytest.raises(RemoteError) as err:adapter.inbounds()
    assert 'secret' not in str(err.value)


def test_master_session_login_csrf(setup):
    config,panel,*_=setup
    config.xui_token='';config.xui_username='admin';config.xui_password='secret'
    adapter=XUI(config,httpx.MockTransport(panel))
    assert len(adapter.inbounds())==11
    assert adapter.logged_in


def test_path_base_and_credentials_server_only(setup):
    config,*_=setup
    def handler(request):
        assert request.headers['Authorization']=='Bearer fixture-server-secret'
        if request.url.path=='/base/panel/api/server/status':
            return httpx.Response(200,json={'success':True,'obj':{'panelVersion':'3.11.0'}})
        if request.url.path=='/base/panel/api/clients/list':
            return httpx.Response(200,json={'success':True,'obj':[]})
        assert request.url.path=='/base/panel/api/inbounds/list'
        return httpx.Response(200,json={'success':True,'obj':[]})
    adapter=XUI(config,httpx.MockTransport(handler))
    assert adapter.inbounds()==[]
    assert adapter.info()=={'mode':'clients','version':'3.11.0'}


@pytest.mark.parametrize('version',['3.1.0','3.8.5','3.9.0','3.10.2','3.11.0','4.0.0','v5.2.1',''])
def test_any_panel_version_with_the_client_api_works(setup,version):
    config,panel,*_=setup
    def handler(request):
        if request.url.path.endswith('/server/status'):
            return httpx.Response(200,json={'success':True,'obj':{'panelVersion':version} if version else {}})
        return panel(request)
    adapter=XUI(config,httpx.MockTransport(handler))
    adapter.create({'email':'any-version','id':'u','subId':'s','totalGB':1,'expiryTime':0,'enable':True},[1])
    assert 'any-version' in panel.rows and adapter.mode=='clients'


def test_classic_panel_is_detected(classic):
    config,panel,db,service,*_=classic
    assert service.remote.mode=='classic'
    assert service.remote.single_inbound and service.remote.detach_first
    assert 'wireguard' not in service.remote.protocols
    assert db.one('SELECT available FROM inbounds WHERE id=5')['available']==0   # wireguard: no clients in classic panels
    assert json.loads(db.one("SELECT value FROM state WHERE key='xui_info'")['value'])['mode']=='classic'


def test_classic_panel_rejects_api_token(classic):
    config,panel,*_=classic
    adapter=XUI(replace(config,xui_token='token'),httpx.MockTransport(panel))
    with pytest.raises(RemoteError,match='remote_auth'):
        adapter.inbounds()
