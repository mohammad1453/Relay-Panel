"""Explicit development-only UI fixture. Binds loopback; no live master connection.

Requires dev dependencies. Test login: review-admin / A-long-test-password!
Never install this fixture as a production service.
"""
import sys
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

root=Path(__file__).resolve().parents[1]
config=Config(str(root/'state/preview/app.db'),'http://127.0.0.1:8095',production=False,xui_url='https://fixture.invalid/',xui_token='fixture-only')
db=Database(config.db);db.migrate()
remote=XUI(config,httpx.MockTransport(Panel()))
service=Service(db,remote)
service.bootstrap('review-admin','A-long-test-password!')
service.sync_inbounds()
admin=db.one("SELECT * FROM users WHERE role='admin'")
if not db.one("SELECT * FROM users WHERE role='reseller'"):
    uid=service.reseller(admin,'review-reseller','A-long-test-password!','review-team',500,now()+30*86400,[1,2,3,4,5,6,7,8,9,10])
    actor=db.one('SELECT * FROM users WHERE id=?',(uid,))
    sale(service,actor,email='review-customer',inbounds=[1,2])
    service.refresh()
with db.transaction() as conn:conn.execute('UPDATE users SET must_change=0')
application=create_app(config,remote,background=False)


async def fixture_browser_bridge(scope,receive,send):
    # The embedded QA browser submits Origin:null. Normalize ONLY this disposable
    # fixture harness. Real application middleware continues rejecting null origins.
    if scope['type']=='http':
        scope=dict(scope)
        scope['headers']=[(k,b'http://127.0.0.1:8095' if k==b'origin' and v==b'null' else v) for k,v in scope['headers']]
    await application(scope,receive,send)


uvicorn.run(fixture_browser_bridge,host='127.0.0.1',port=8095,access_log=False)
