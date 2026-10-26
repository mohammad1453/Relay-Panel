"""Explicit development-only UI fixture. Binds loopback; no live master connection.

Requires dev dependencies. Logins (password A-long-test-password! for all):
  review-admin      the administrator
  review-reseller   a reseller with customers in every state
Never install this fixture as a production service.
"""
import sys
import uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
import httpx
import uvicorn
from conftest import Panel,sale
from reseller.config import Config
from reseller.db import Database,now
from reseller.domain import Service
from reseller.web import create_app
from reseller.xui import XUI

GIB=1024**3
root=Path(__file__).resolve().parents[1]
port=int(sys.argv[1]) if len(sys.argv)>1 else 8095
config=Config(str(root/'state/preview/app.db'),f'http://127.0.0.1:{port}',production=False,xui_url='https://fixture.invalid/',
              xui_token='fixture-only',sub_url='https://sub.example.com/i/')
db=Database(config.db);db.migrate()
panel=Panel()
for ib,name in zip(panel.inbounds,['VLESS Reality 443','VLESS WS CDN','Trojan TLS','VMess WS','Shadowsocks','Hysteria2','WireGuard','AmneziaWG','MTProto','TUIC','HTTP proxy']):
    ib['remark']=name
remote=XUI(config,httpx.MockTransport(panel))
service=Service(db,remote)
service.bootstrap('review-admin','A-long-test-password!')
service.sync_inbounds()
admin=db.one("SELECT * FROM users WHERE role='admin'")
if not db.one("SELECT * FROM users WHERE role='reseller'"):
    uid=service.reseller(admin,'review-reseller','A-long-test-password!','review-team',500,now()+24*86400,[1,2,3,4,5,6,7,8,9,10])
    service.reseller(admin,'test-reseller','A-long-test-password!','test-team',100,now()+3*86400,[1,3])
    actor=db.one('SELECT * FROM users WHERE id=?',(uid,))
    day=86400*1000
    people=[('ali-phone',40,[1,2],'Paid until next month',30,True),('sara-laptop',20,[1],'',60,True),('reza-tv',10,[3],'Living room TV',2,True),
            ('maryam',50,[1],'Family plan',-30,True),('old-customer',5,[1],'',30,False),('office-router',100,[2],'Office',90,True)]
    for email,gb,inbounds,comment,days,enable in people:
        expiry=days*day if days<0 else now()*1000+days*day
        sale(service,actor,email=email,volume=str(gb),inbounds=inbounds,comment=comment,expiry=expiry,enable=enable)
    used={'ali-phone':31,'sara-laptop':4,'reza-tv':9.8,'office-router':12,'old-customer':5}
    for email,amount in used.items():
        panel.rows[email]['traffic']={'up':int(amount*GIB*0.2),'down':int(amount*GIB*0.8),'total':panel.rows[email]['totalGB']}
    panel.online={'ali-phone','office-router'}
    panel.rows['maryam']['comment']='changed directly in 3X-UI'
    panel.rows['maryam']['totalGB']=80*GIB
    panel.mode='drop'
    sale(service,actor,email='new-customer',volume='15',inbounds=[1])
    panel.mode='ok'
    panel.delete_inbound(4)
    service.refresh()
with db.transaction() as conn:conn.execute('UPDATE users SET must_change=0')
application=create_app(config,remote,background=False)


async def fixture_browser_bridge(scope,receive,send):
    # The embedded QA browser submits Origin:null. Normalize ONLY this disposable
    # fixture harness. Real application middleware continues rejecting null origins.
    if scope['type']=='http':
        scope=dict(scope)
        scope['headers']=[(k,f'http://127.0.0.1:{port}'.encode() if k==b'origin' and v==b'null' else v) for k,v in scope['headers']]
    await application(scope,receive,send)


uvicorn.run(fixture_browser_bridge,host='127.0.0.1',port=port,access_log=False)
