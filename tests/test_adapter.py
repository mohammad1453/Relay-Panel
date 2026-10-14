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
        if request.url.path=='/base/panel/api/server/status':
            return httpx.Response(200,json={'success':True,'obj':{'panelVersion':'3.8.5'}})
        assert request.url.path=='/base/panel/api/inbounds/list'
        assert request.headers['Authorization']=='Bearer fixture-server-secret'
        return httpx.Response(200,json={'success':True,'obj':[]})
    assert XUI(config,httpx.MockTransport(handler)).inbounds()==[]


def test_version_mismatch_blocks_mutation(setup):
    config,*_=setup
    calls=[]
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200,json={'success':True,'obj':{'panelVersion':'3.9.0'}})
    adapter=XUI(config,httpx.MockTransport(handler))
    with pytest.raises(RemoteError,match='remote_version'):
        adapter.create({'email':'test'},[1])
    assert calls==['/base/panel/api/server/status']
