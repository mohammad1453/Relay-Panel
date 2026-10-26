import copy
import json
import threading
import uuid
import httpx
import pytest
from reseller.config import Config
from reseller.db import Database, now
from reseller.domain import Service
from reseller.xui import XUI


class Panel:
    """Deterministic v3.8.5 envelopes, central records, per-inbound placements.

    Implements only verified endpoints used by the adapter. Never connects to a live panel.
    """
    def __init__(self):
        self.lock=threading.RLock()
        self.rows={}
        self.inbounds=[{'id':i,'remark':f'Inbound {i}','protocol':p,'port':440+i,'enable':True,'settings':{'clients':[]},'streamSettings':{}} for i,p in enumerate(['vless','vless','trojan','vmess','shadowsocks','hysteria','wireguard','amneziawg','mtproto','tuic','http'],1)]
        self.mode='ok'
        self.online=set()
        self.calls=[]
        self.count=0

    def delete_inbound(self,iid):
        """What 3X-UI does when an admin deletes an inbound: its clients lose that attachment."""
        with self.lock:
            self.inbounds=[ib for ib in self.inbounds if ib['id']!=iid]
            for record in self.rows.values():
                record['inboundIds']=[i for i in record['inboundIds'] if i!=iid]

    def response(self,obj=None,success=True):
        return httpx.Response(200,json={'success':success,'msg':'','obj':obj})

    def __call__(self,request):
        with self.lock:
            path=request.url.path.split('/panel/api/')[-1]
            self.calls.append((request.method,path))
            if request.url.path.endswith('/csrf-token'):
                return self.response('test-csrf')
            if request.url.path.endswith('/login'):
                assert request.headers.get('X-CSRF-Token')=='test-csrf'
                return self.response()
            if self.mode=='unavailable':
                raise httpx.ConnectTimeout('fixture')
            if path=='server/status':
                return self.response({'panelVersion':'3.8.5'})
            if path=='inbounds/list':
                return self.response(copy.deepcopy(self.inbounds))
            if path=='clients/list':
                return self.response(list(copy.deepcopy(self.rows).values()))
            if path=='clients/onlines':
                return self.response(sorted(self.online))
            if path.startswith('clients/links/'):
                return self.response(['vless://fixture@example.invalid:443?security=tls#fixture'])
            if path.startswith('clients/subLinks/'):
                return self.response(['https://example.invalid/sub/fixture'])
            payload=json.loads(request.content) if request.content else {}
            if self.mode=='drop':
                raise httpx.ReadTimeout('request lost before apply')
            if path=='clients/add':
                self.count+=1
                client=copy.deepcopy(payload['client'])
                client.setdefault('password','generated')
                client.setdefault('auth','generated')
                client.setdefault('publicKey','generated-key')
                client.setdefault('privateKey','generated-private-key')
                client.setdefault('secret','ee0123456789abcdef')
                ids=payload['inboundIds']
                if self.mode=='partial':
                    ids=ids[:1]
                for ib in self.inbounds:
                    if ib['id'] in ids:
                        ib['settings']['clients'].append(copy.deepcopy(client))
                record={**client,'id':self.count,'uuid':client.get('id',''),'inboundIds':ids,'traffic':{'up':3,'down':7,'total':client['totalGB']}}
                self.rows[client['email']]=record
            elif path.startswith('clients/update/'):
                email=path.split('/')[-1]
                record=self.rows[email]
                record.update({k:v for k,v in payload.items() if k!='id'})
                if payload.get('id'):
                    record['uuid']=payload['id']
                for ib in self.inbounds:
                    for c in ib['settings']['clients']:
                        if c['email']==email:
                            c.update(payload)
            elif path.endswith('/attach'):
                email=path.split('/')[-2]
                record=self.rows[email]
                client={**record,'id':record.get('uuid','')}
                for iid in payload['inboundIds']:
                    record['inboundIds'].append(iid)
                    next(ib for ib in self.inbounds if ib['id']==iid)['settings']['clients'].append(copy.deepcopy(client))
            elif path.endswith('/detach'):
                email=path.split('/')[-2]
                record=self.rows[email]
                record['inboundIds']=[i for i in record['inboundIds'] if i not in payload['inboundIds']]
                for ib in self.inbounds:
                    if ib['id'] in payload['inboundIds']:
                        ib['settings']['clients']=[c for c in ib['settings']['clients'] if c['email']!=email]
            elif path.startswith('clients/del/'):
                email=path.split('/')[-1]
                self.rows.pop(email,None)
                for ib in self.inbounds:
                    ib['settings']['clients']=[c for c in ib['settings']['clients'] if c['email']!=email]
            else:
                raise AssertionError('Unverified endpoint '+path)
            if self.mode=='timeout':
                raise httpx.ReadTimeout('response lost after apply')
            if self.mode=='partial':
                return self.response(success=False)
            if self.mode=='node':
                return self.response({'nodePending':True})
            return self.response()


@pytest.fixture
def setup(tmp_path):
    config=Config(db=str(tmp_path/'app.db'),origin='http://testserver',production=False,xui_url='https://panel.invalid/base/',xui_token='fixture-server-secret',sub_url='https://sub.example.invalid/i/')
    panel=Panel()
    remote=XUI(config,httpx.MockTransport(panel))
    db=Database(config.db);db.migrate()
    service=Service(db,remote)
    service.bootstrap('admin','A-long-test-password!')
    admin=db.one("SELECT * FROM users WHERE username='admin'")
    service.sync_inbounds()
    uid=service.reseller(admin,'reseller','A-long-test-password!','sales-team',500,now()+86400,[1,2,3,4,5,6,7,8,9,10])
    other=service.reseller(admin,'other','Another-test-password!','other-team',500,now()+86400,[1])
    actor=db.one('SELECT * FROM users WHERE id=?',(uid,))
    with db.transaction() as conn:
        conn.execute('UPDATE users SET must_change=0')
    yield config,panel,db,service,admin,actor,other
    remote.close()


def form(**kwargs):
    return {'email':'customer-one','volume':'100','expiry':(now()+86400)*1000,'inbounds':[1],**kwargs}


def sale(service,actor,**kwargs):
    op=service.reserve(actor,form(**kwargs),str(uuid.uuid4()))
    service.execute(op['id'])
    return op
