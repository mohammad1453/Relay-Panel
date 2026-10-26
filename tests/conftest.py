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
    """Deterministic 3X-UI client-API (3.1+) envelopes, central records, per-inbound placements.

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
            if path=='setting/defaultSettings':
                return self.response({'subEnable':True,'subURI':'https://panel-sub.invalid:2096/sub/'})
            if path=='server/status':
                return self.response({'panelVersion':'3.11.0'})
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


class ClassicPanel:
    """Behaves like 3X-UI 2.x / 3.0.x: no client API, clients inside each inbound,
    session login without CSRF, duplicate emails rejected panel-wide, and the last
    client of an inbound cannot be deleted with delClient."""
    KEY={'trojan':'password','shadowsocks':'email','hysteria':'auth','hysteria2':'auth'}

    def __init__(self):
        self.lock=threading.RLock()
        self.inbounds=[{'id':i,'remark':f'Classic {i}','protocol':p,'port':2000+i,'enable':True,'listen':'','tag':f'inbound-{i}',
                        'settings':{'clients':[],'decryption':'none'} if p!='shadowsocks' else {'clients':[],'method':'2022-blake3-aes-256-gcm','password':'x'},
                        'streamSettings':{'network':'tcp'},'sniffing':'{}','up':0,'down':0,'total':0,'expiryTime':0}
                       for i,p in enumerate(['vless','vless','trojan','shadowsocks','wireguard'],1)]
        self.stats={}
        self.online=set()
        self.next_stat=100
        self.calls=[]
        self.logged_in=False

    def response(self,obj=None,success=True):
        return httpx.Response(200,json={'success':success,'msg':'','obj':obj})

    def find(self,iid):
        return next(ib for ib in self.inbounds if ib['id']==iid)

    def all_emails(self):
        return {c['email'] for ib in self.inbounds for c in ib['settings'].get('clients',[])}

    def __call__(self,request):
        with self.lock:
            path=request.url.path
            self.calls.append((request.method,path))
            if path.endswith('/csrf-token'):
                return httpx.Response(404,text='404 page not found')
            if path.endswith('/login'):
                self.logged_in=True
                return self.response()
            if request.headers.get('Authorization'):
                return httpx.Response(401)
            if not self.logged_in:
                return httpx.Response(307,headers={'Location':'/'})
            if path.endswith('/panel/setting/defaultSettings'):
                return self.response({'subEnable':True,'subURI':'http://classic-sub.invalid:2096/sub/'})
            api=path.split('/panel/api/')[-1]
            form=dict(httpx.QueryParams(request.content.decode())) if request.content else {}
            if api in ('server/status','clients/list','setting/defaultSettings') or api.startswith('clients/'):
                return httpx.Response(404,text='404 page not found')
            if api=='inbounds/list':
                rows=[]
                for ib in self.inbounds:
                    row=copy.deepcopy(ib)
                    row['settings']=json.dumps(ib['settings'])
                    row['streamSettings']=json.dumps(ib['streamSettings'])
                    row['clientStats']=[copy.deepcopy(s) for s in self.stats.values() if s['inboundId']==ib['id']]
                    rows.append(row)
                return self.response(rows)
            if api=='inbounds/onlines':
                return self.response(sorted(self.online))
            if api=='inbounds/addClient':
                ib=self.find(int(form['id']))
                clients=json.loads(form['settings'])['clients']
                for c in clients:
                    if c['email'] in self.all_emails():
                        return self.response(success=False)
                    if not c.get(self.KEY.get(ib['protocol'],'id')):
                        return self.response(success=False)
                    ib['settings']['clients'].append(c)
                    self.next_stat+=1
                    self.stats[c['email']]={'id':self.next_stat,'inboundId':ib['id'],'email':c['email'],'enable':True,'up':1,'down':2,'total':c.get('totalGB',0),'expiryTime':c.get('expiryTime',0)}
                return self.response()
            if api.startswith('inbounds/updateClient/'):
                client_id=api.split('/')[-1]
                ib=self.find(int(form['id']))
                new=json.loads(form['settings'])['clients'][0]
                key=self.KEY.get(ib['protocol'],'id')
                for index,c in enumerate(ib['settings']['clients']):
                    if c.get(key)==client_id:
                        ib['settings']['clients'][index]=new
                        return self.response()
                return self.response(success=False)
            if '/delClient/' in api:
                iid=int(api.split('/')[1]);client_id=api.split('/')[-1]
                ib=self.find(iid);key=self.KEY.get(ib['protocol'],'id')
                kept=[c for c in ib['settings']['clients'] if c.get(key)!=client_id]
                if len(kept)==len(ib['settings']['clients']) or not kept:
                    return self.response(success=False)   # not found, or "no client remained in Inbound"
                gone={c['email'] for c in ib['settings']['clients']}-{c['email'] for c in kept}
                ib['settings']['clients']=kept
                for email in gone:
                    self.stats.pop(email,None)
                return self.response()
            if api.startswith('inbounds/update/'):
                ib=self.find(int(api.split('/')[-1]))
                before={c['email'] for c in ib['settings']['clients']}
                ib['settings']=json.loads(form['settings'])
                for email in before-{c['email'] for c in ib['settings']['clients']}:
                    self.stats.pop(email,None)
                return self.response()
            raise AssertionError('Unverified classic endpoint '+api)


@pytest.fixture
def classic(tmp_path):
    config=Config(db=str(tmp_path/'app.db'),origin='http://testserver',production=False,xui_url='http://127.0.0.1:2053/base/',
                  xui_username='admin',xui_password='admin-password',sub_url='https://sub.example.invalid/i/')
    panel=ClassicPanel()
    remote=XUI(config,httpx.MockTransport(panel))
    db=Database(config.db);db.migrate()
    service=Service(db,remote)
    service.bootstrap('admin','A-long-test-password!')
    admin=db.one("SELECT * FROM users WHERE username='admin'")
    service.sync_inbounds()
    uid=service.reseller(admin,'reseller','A-long-test-password!','sales-team',500,now()+86400,[1,2,3,4])
    actor=db.one('SELECT * FROM users WHERE id=?',(uid,))
    with db.transaction() as conn:
        conn.execute('UPDATE users SET must_change=0')
    yield config,panel,db,service,admin,actor
    remote.close()
