import asyncio
import json
import logging
import secrets
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from urllib.parse import quote, urlsplit

from .config import Config
from .db import Database, now, audit
from .domain import Service, RuleError, GIB, PROTOCOLS, active_grant, owned, volume
from .i18n import PAIRS, translator
from .security import authenticate, digest, new_session, password_hash, verify
from .xui import XUI, RemoteError


class PathPrefix:
    """Serve the application directly below a configured URL prefix."""
    def __init__(self, app, prefix):
        self.app,self.prefix=app,prefix

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http','websocket') or not self.prefix:
            return await self.app(scope,receive,send)
        path=scope.get('path','')
        if path==self.prefix:
            return await RedirectResponse(self.prefix+'/',307)(scope,receive,send)
        if not path.startswith(self.prefix+'/'):
            return await Response(status_code=404)(scope,receive,send)
        child=dict(scope)
        child['path']=path[len(self.prefix):] or '/'
        child['raw_path']=child['path'].encode()
        return await self.app(child,receive,send)


def create_app(config=None, remote=None, background=True):
    config = (config or Config.env()).validate()
    db = Database(config.db)
    db.migrate()
    remote = remote or XUI(config)
    service = Service(db,remote)
    templates = Jinja2Templates(directory=str(Path(__file__).with_name('templates')))

    async def worker():
        while True:
            try:
                await run_in_threadpool(service.refresh)
            except (RemoteError, RuleError):
                with db.transaction() as conn:
                    conn.execute("INSERT INTO state VALUES('connection_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(now()),))
            except Exception:
                # No exception text: remote payloads and credentials must not enter ordinary logs.
                with db.transaction() as conn:
                    audit(conn,None,'worker',outcome='error')
            await asyncio.sleep(config.sync_seconds)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if background else None
        yield
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(remote,'close'):
            remote.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db,app.state.service,app.state.config = db,service,config
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.origin).hostname,'127.0.0.1','localhost'])
    app.mount('/static',StaticFiles(directory=Path(__file__).with_name('static')),name='static')
    cookie_name='relay_session_'+digest(config.base_path)[:12] if config.base_path else 'relay_session'

    def render(request,name,**context):
        lang=request.state.lang
        owner_id=(context.get('customer') or {}).get('user_id') or (request.state.user or {}).get('id')
        writable=bool(owner_id and db.one('SELECT id FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>?',(owner_id,now(),now())))
        def number(value):
            text=f'{value:,.2f}'.rstrip('0').rstrip('.') if isinstance(value,float) else str(value)
            return text.translate(str.maketrans('0123456789','۰۱۲۳۴۵۶۷۸۹')) if lang=='fa' else text
        def date(value):
            if not value:
                return '—'
            return number(datetime.fromtimestamp(value,ZoneInfo(config.timezone)).strftime('%Y-%m-%d %H:%M'))
        return templates.TemplateResponse(request=request,name=name,context={
            't':translator(lang),'lang':lang,'user':request.state.user,'csrf':request.state.csrf,
            'path':request.url.path,'number':number,'date':date,'gb':lambda b:number(round(b/GIB,3)) if b is not None else '—',
            'now':now(),'timezone':config.timezone,'operation_id':str(uuid.uuid4()),'writable':writable,'stale_after':config.sync_seconds*3,
            'message':request.query_params.get('message','') if request.query_params.get('message','') in PAIRS else '','PROTOCOLS':PROTOCOLS,'base_path':config.base_path,
            'form_data':{},'inline_error':'',**context})

    def external(path):
        return config.base_path+path

    def redirect(path,message='saved'):
        return RedirectResponse(external(path)+'?message='+message,303)

    def require(request,admin=False):
        user=request.state.user
        if not user:
            raise RuleError('forbidden',401)
        if admin and user['role']!='admin':
            raise RuleError('forbidden',403)
        return user

    @app.middleware('http')
    async def protection(request,call_next):
        request.state.lang='fa' if request.cookies.get('language')=='fa' else 'en'
        request.state.user,request.state.csrf=None,''
        token=request.cookies.get(cookie_name,'')
        session=db.one('SELECT * FROM sessions WHERE token_hash=?',(digest(token),)) if token else None
        if session and session['expires']>now() and session['seen']>now()-config.idle_seconds:
            user=db.one('SELECT * FROM users WHERE id=?',(session['user_id'],)) if session['user_id'] else None
            if not user or not user['suspended']:
                request.state.user=user
                request.state.csrf=session['csrf']
                with db.transaction() as conn:
                    conn.execute('UPDATE sessions SET seen=? WHERE token_hash=?',(now(),digest(token)))
        fresh=None
        if request.url.path=='/login' and not request.state.csrf:
            with db.transaction() as conn:
                fresh,request.state.csrf=new_session(conn,config,old=token)
        response=None
        if request.method not in ('GET','HEAD','OPTIONS'):
            length=request.headers.get('content-length','0') or '0'
            if len(length)>10 or not length.isdigit() or int(length)>65536:
                response=Response(status_code=413)
            else:
                body=bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>65536:
                        break
                if len(body)>65536:
                    response=Response(status_code=413)
                else:
                    request._body=bytes(body)  # Replay the bounded body to downstream form parsing.
                    # All browser mutations are URL-encoded forms, not multipart uploads.
                    from urllib.parse import parse_qs
                    try:
                        form=parse_qs(body.decode('utf-8',errors='replace'),max_num_fields=100)
                    except ValueError:
                        form={}
                    supplied=form.get('csrf',[''])[0]
                    origin=request.headers.get('origin')
                    reason=('session' if not request.state.csrf else 'form' if not secrets.compare_digest(supplied,request.state.csrf)
                            else 'origin' if origin and origin.rstrip('/')!=config.origin.rstrip('/') else '')
                    if reason:
                        logging.getLogger('relay.security').warning('csrf_rejected reason=%s path=%s',reason,request.url.path)
                        response=render(request,'error.html',error='csrf',status=403)
                        response.status_code=403
                        response.headers['X-Relay-Session-Error']=reason
        public=request.url.path in ('/login','/healthz','/language') or request.url.path.startswith('/static/')
        if not response and not public and not request.state.user:
            response=RedirectResponse(external('/login'),303)
        if not response and request.state.user and request.state.user['must_change'] and request.url.path not in ('/account','/logout','/language') and not request.url.path.startswith('/static/'):
            response=redirect('/account','must_change')
        if not response and request.state.user and request.method=='POST':
            bucket='write:'+str(request.state.user['id'])
            with db.transaction() as conn:
                attempt=conn.execute('SELECT * FROM attempts WHERE bucket=?',(bucket,)).fetchone()
                if attempt and attempt['until_time']>now() and attempt['count']>=60:
                    response=render(request,'error.html',error='rate_limited',status=429)
                    response.status_code=429
                else:
                    conn.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=CASE WHEN until_time<? THEN 1 ELSE count+1 END,until_time=CASE WHEN until_time<? THEN excluded.until_time ELSE until_time END',(bucket,now()+60,now(),now()))
        if response is None:
            try:
                response=await call_next(request)
            except Exception as exc:
                with db.transaction() as conn:
                    audit(conn,(request.state.user or {}).get('id'),'request',outcome='error')
                response=render(request,'error.html',error='error',status=500)
                response.status_code=500
        if fresh:
            cookie(response,fresh)
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
                                 'Referrer-Policy':'same-origin','Permissions-Policy':'camera=(), microphone=(), geolocation=()',
                                 'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"})
        if config.production:
            response.headers['Strict-Transport-Security']='max-age=31536000'
        return response

    def cookie(response,token):
        # Remove cookies created by older root-path releases before setting the
        # path-scoped cookie. Browsers may otherwise send both values and make
        # a valid CSRF token appear to belong to a different session.
        if config.base_path:
            response.delete_cookie('relay_session',path='/')
            response.delete_cookie('relay_session',path=config.base_path+'/')
        response.set_cookie(cookie_name,token,secure=config.production,httponly=True,samesite='strict',max_age=config.session_seconds,path=config.base_path+'/')

    @app.exception_handler(RuleError)
    async def rule_error(request,error):
        with db.transaction() as conn:
            audit(conn,(request.state.user or {}).get('id'),'request',outcome=error.code)
        response=render(request,'error.html',error=error.code,status=error.status)
        response.status_code=error.status
        return response

    @app.exception_handler(RemoteError)
    async def remote_error(request,error):
        return await rule_error(request,RuleError(error.code,503))

    @app.exception_handler(ValueError)
    async def value_error(request,error):
        return await rule_error(request,RuleError('password_policy' if str(error)=='password_policy' else 'invalid_input'))

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request,error):
        return await rule_error(request,RuleError('identity_conflict',409))

    @app.get('/healthz')
    def health():
        db.one('SELECT 1 AS ok')
        return {'status':'ok','schema':1}

    @app.get('/language')
    def language(request:Request):
        response=RedirectResponse(external('/' if request.state.user else '/login'),303)
        response.set_cookie('language','fa' if request.query_params.get('lang')=='fa' else 'en',samesite='strict',secure=config.production)
        return response

    @app.get('/login')
    def login_page(request:Request):
        return render(request,'login.html')

    @app.post('/login')
    async def login(request:Request):
        form=await request.form()
        user=await run_in_threadpool(authenticate,db,str(form.get('username','')).strip().lower(),str(form.get('password','')),request.client.host)
        if not user:
            response=render(request,'login.html',login_error=True)
            response.status_code=401
            return response
        with db.transaction() as conn:
            token,_=new_session(conn,config,user['id'],request.cookies.get(cookie_name))
        response=RedirectResponse(external('/account' if user['must_change'] else '/'),303)
        cookie(response,token)
        return response

    @app.post('/logout')
    def logout(request:Request):
        user=require(request)
        with db.transaction() as conn:
            conn.execute('DELETE FROM sessions WHERE token_hash=?',(digest(request.cookies.get(cookie_name,'')),))
            audit(conn,user['id'],'logout')
        response=RedirectResponse(external('/login'),303)
        response.delete_cookie(cookie_name,path=config.base_path+'/')
        if config.base_path:
            response.delete_cookie('relay_session',path='/')
        return response

    @app.get('/account')
    def account(request:Request):
        require(request)
        return render(request,'account.html')

    @app.post('/account')
    async def account_save(request:Request):
        user=require(request)
        form=await request.form()
        # Shared persistent login limiter also bounds old-password guessing.
        valid=await run_in_threadpool(authenticate,db,user['username'],str(form.get('current_password','')),request.client.host)
        if not valid:
            raise RuleError('invalid_login',403)
        hashed=await run_in_threadpool(password_hash,str(form.get('new_password','')))
        with db.transaction() as conn:
            conn.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?',(hashed,user['id']))
            conn.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            token,_=new_session(conn,config,user['id'])
            audit(conn,user['id'],'password_change')
        response=redirect('/account')
        cookie(response,token)
        return response

    def grants(uid):
        return db.all('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC',(uid,))

    @app.get('/')
    def dashboard(request:Request):
        user=require(request)
        if user['role']=='admin':
            stats={key:db.one(sql)['n'] for key,sql in {
                'total_resellers':"SELECT count(*) n FROM users WHERE role='reseller'",'total_customers':"SELECT count(*) n FROM customers WHERE status='active'",
                'active_plans':f"SELECT count(*) n FROM users u WHERE role='reseller' AND suspended=0 AND EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.starts<={now()} AND g.expires>{now()})",
                'inactive_resellers':f"SELECT count(*) n FROM users u WHERE role='reseller' AND (suspended=1 OR NOT EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.starts<={now()} AND g.expires>{now()}))",
                'attention':"SELECT count(*) n FROM operations WHERE status IN ('sent','uncertain','failed')"}.items()}
            totals=db.one('SELECT coalesce(sum(allowance),0) granted,coalesce(sum(sold),0) sold FROM grants')
            events=db.all('SELECT a.*,u.username actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 15')
            return render(request,'dashboard.html',stats=stats,totals=totals,plan=None,connection=db.one("SELECT value FROM state WHERE key='connection'"),events=events)
        history=grants(user['id'])
        plan=next((g for g in history if not g['closed'] and g['starts']<=now()<g['expires']),None)
        count=db.one("SELECT count(*) n,coalesce(sum(json_extract(payload,'$.enable')),0) enabled FROM customers WHERE user_id=? AND status='active'",(user['id'],))
        events=db.all("SELECT a.*,u.username actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor WHERE a.actor=? OR (a.action='customer_remote_missing' AND EXISTS(SELECT 1 FROM customers c WHERE c.id=CAST(a.subject AS INTEGER) AND c.user_id=?)) ORDER BY a.id DESC LIMIT 15",(user['id'],user['id']))
        return render(request,'dashboard.html',stats={'total_customers':count['n'],'enabled_customers':count['enabled']},totals={},plan=plan,history=history,events=events)

    @app.get('/inbounds')
    def inbound_page(request:Request):
        require(request,True)
        return render(request,'inbounds.html',inbounds=db.all('SELECT id,name,protocol,port,available,synced FROM inbounds ORDER BY id'))

    @app.post('/sync')
    def sync(request:Request):
        user=require(request,True)
        service.refresh()
        with db.transaction() as conn:
            audit(conn,user['id'],'sync')
        return redirect('/inbounds')

    @app.get('/resellers')
    def reseller_list(request:Request):
        require(request,True)
        q=request.query_params.get('q','')[:100]
        page=max(1,min(int(request.query_params.get('page',1)),100000))
        status=request.query_params.get('status','')
        condition=''
        if status=='suspended':
            condition=' AND suspended=1'
        elif status=='active':
            condition=f' AND suspended=0 AND EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.expires>{now()})'
        elif status=='expired':
            condition=f' AND NOT EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.expires>{now()})'
        sort={'username':'username','created':'created DESC','sold':'lifetime DESC'}.get(request.query_params.get('sort'),'username')
        rows=db.all(f"SELECT u.*, (SELECT sum(sold) FROM grants WHERE user_id=u.id) lifetime, EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.starts<={now()} AND g.expires>{now()}) plan_active FROM users u WHERE role='reseller' AND (username LIKE ? OR tag LIKE ?)"+condition+" ORDER BY "+sort+" LIMIT 25 OFFSET ?",('%'+q+'%','%'+q+'%',(page-1)*25))
        return render(request,'resellers.html',rows=rows,q=q,page=page,filters=request.query_params)

    def allowed(uid=None):
        if uid is None:
            return db.all('SELECT id,name,protocol,port,available FROM inbounds ORDER BY id')
        return db.all('SELECT i.id,i.name,i.protocol,i.port,i.available FROM inbounds i JOIN permissions p ON i.id=p.inbound_id WHERE p.user_id=? ORDER BY i.id',(uid,))

    @app.get('/resellers/new')
    def reseller_new(request:Request):
        require(request,True)
        return render(request,'reseller_form.html',reseller=None,inbounds=allowed(),selected=[],history=[],ledger=[])

    def parse_expiry(value):
        date=datetime.fromisoformat(value)
        if date.tzinfo is None:
            date=date.replace(tzinfo=ZoneInfo(config.timezone))
        return int(date.timestamp())

    @app.post('/resellers/new')
    async def reseller_create(request:Request):
        user=require(request,True)
        form=await request.form()
        uid=await run_in_threadpool(service.reseller,user,str(form.get('username','')),str(form.get('password','')),str(form.get('tag','')),form.get('volume',''),parse_expiry(str(form.get('expiry',''))),form.getlist('inbounds'))
        return redirect('/resellers/'+str(uid))

    @app.get('/resellers/{uid}')
    def reseller_detail(request:Request,uid:int):
        require(request,True)
        reseller=db.one("SELECT * FROM users WHERE id=? AND role='reseller'",(uid,))
        if not reseller:
            raise RuleError('not_found',404)
        ledger=db.all("SELECT l.*,u.username actor_name,CASE WHEN l.operation_id IS NULL THEN 'ok' WHEN o.status='committed' THEN 'ok' WHEN o.status='failed' THEN 'failed' ELSE coalesce(o.status,'unknown') END action_status FROM ledger l LEFT JOIN users u ON u.id=l.actor LEFT JOIN operations o ON o.id=l.operation_id WHERE l.user_id=? ORDER BY l.id DESC LIMIT 15",(uid,))
        return render(request,'reseller_form.html',reseller=reseller,inbounds=allowed(),selected=[i['id'] for i in allowed(uid)],history=grants(uid),ledger=ledger)

    @app.post('/resellers/{uid}')
    async def reseller_save(request:Request,uid:int):
        user=require(request,True)
        form=await request.form()
        reset=str(form.get('password',''))
        hashed=await run_in_threadpool(password_hash,reset) if reset else None
        suspension_changed=False
        suspended=form.get('suspended')=='on'
        with db.transaction() as conn:
            current=conn.execute("SELECT suspended FROM users WHERE id=? AND role='reseller'",(uid,)).fetchone()
            if not current:
                raise RuleError('not_found',404)
            suspension_changed=bool(current['suspended'])!=suspended
            ids=sorted(set(int(i) for i in form.getlist('inbounds')))
            for iid in ids:
                if not conn.execute('SELECT 1 FROM inbounds WHERE id=?',(iid,)).fetchone():
                    raise RuleError('inbound_denied')
            from .domain import identifier
            username=identifier(str(form.get('username','')))
            conn.execute('UPDATE users SET username=?,suspended=?,notes=? WHERE id=?',(username,int(suspended),str(form.get('notes',''))[:2000],uid))
            conn.execute('DELETE FROM permissions WHERE user_id=?',(uid,))
            conn.executemany('INSERT INTO permissions VALUES(?,?)',[(uid,i) for i in ids])
            if hashed:
                conn.execute('UPDATE users SET password_hash=?,must_change=1 WHERE id=?',(hashed,uid))
            conn.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            audit(conn,user['id'],'reseller_update',uid)
        if suspension_changed and service.set_reseller_customer_enabled(uid,not suspended):
            return redirect('/resellers/'+str(uid),'suspension_sync_pending')
        return redirect('/resellers/'+str(uid))

    @app.post('/resellers/{uid}/renew')
    async def renew(request:Request,uid:int):
        user=require(request,True)
        form=await request.form()
        service.renew(user,uid,form.get('volume',''),parse_expiry(str(form.get('expiry',''))))
        return redirect('/resellers/'+str(uid))

    @app.post('/resellers/{uid}/adjust')
    async def adjust(request:Request,uid:int):
        user=require(request,True)
        form=await request.form()
        amount=volume(form.get('volume',''))
        expires=parse_expiry(str(form.get('expiry','')))
        with db.transaction() as conn:
            grant=active_grant(conn,uid)
            if amount<grant['sold']+grant['reserved'] or expires<=now():
                raise RuleError('invalid_input')
            conn.execute('UPDATE grants SET allowance=?,expires=? WHERE id=?',(amount,expires,grant['id']))
            conn.execute("INSERT INTO ledger(user_id,grant_id,kind,amount,actor,created) VALUES(?,?,'adjust',?,?,?)",(uid,grant['id'],amount-grant['allowance'],user['id'],now()))
            audit(conn,user['id'],'grant_adjust',grant['id'])
        return redirect('/resellers/'+str(uid))

    @app.get('/customers')
    def customers(request:Request):
        user=require(request)
        params=request.query_params
        q=params.get('q','')[:100]
        where=['(c.email LIKE ? OR json_extract(c.payload,\'$.comment\') LIKE ?)']
        args=['%'+q+'%','%'+q+'%']
        if user['role']!='admin':
            where.append('c.user_id=?');args.append(user['id'])
        status=params.get('status','')
        if status in ('active','deleted','pending','failed'):
            where.append('c.status=?');args.append(status)
        else:
            where.append("c.status!='deleted'")
        if params.get('enable') in ('0','1'):
            where.append("json_extract(c.payload,'$.enable')=?");args.append(int(params['enable']))
        if params.get('online') in ('0','1'):
            where.append('c.online=? AND c.telemetry_at>?');args.extend([int(params['online']),now()-config.sync_seconds*3])
        if params.get('expired')=='1':
            where.append('coalesce(c.remote_expiry,json_extract(c.payload,\'$.expiryTime\')) BETWEEN 1 AND ?');args.append(now()*1000)
        if params.get('inbound'):
            where.append('EXISTS(SELECT 1 FROM placements WHERE customer_id=c.id AND inbound_id=?)');args.append(int(params['inbound']))
        sort={'email':'c.email','package':'c.package','created':'c.id DESC'}.get(params.get('sort'),'c.id DESC')
        page=max(1,min(int(params.get('page',1)),100000))
        rows=db.all('SELECT c.*,u.username owner FROM customers c JOIN users u ON c.user_id=u.id WHERE '+' AND '.join(where)+' ORDER BY '+sort+' LIMIT 25 OFFSET ?',args+[(page-1)*25])
        for c in rows:
            c['p']=json.loads(c['payload'])
            c['inbounds']=db.one('SELECT count(*) n FROM placements WHERE customer_id=?',(c['id'],))['n']
            c['stale']=not c['telemetry_at'] or c['telemetry_at']<now()-config.sync_seconds*3
        return render(request,'customers.html',rows=rows,q=q,page=page,inbounds=allowed(None if user['role']=='admin' else user['id']),filters=params)

    @app.get('/customers/new')
    def customer_new(request:Request):
        user=require(request)
        if user['role']!='reseller':
            raise RuleError('forbidden',403)
        with db.transaction() as conn:
            grant=active_grant(conn,user['id'])
        quota_remaining=max(0,grant['allowance']-grant['sold']-grant['reserved'])/GIB
        return render(request,'customer_form.html',customer=None,p={},inbounds=allowed(user['id']),selected=[],quota_remaining=quota_remaining,form_data={})

    async def customer_form(request,editing=False):
        form=await request.form()
        data={'comment':str(form.get('comment','')),'volume':str(form.get('volume','')),'enable':form.get('enable')=='on',
              'uuid':str(form.get('uuid','')),'sub_id':str(form.get('sub_id','')),'credential':str(form.get('credential',''))}
        if form.get('first_use')=='on':
            days=int(form.get('days','0'))
            if not 1<=days<=3650:
                raise RuleError('invalid_expiry')
            data['expiry']=-days*86400000
        else:
            data['expiry']=parse_expiry(str(form.get('expiry','')))*1000
        data['inbounds']=form.getlist('inbounds')
        if not editing:
            data.update(email=str(form.get('email','')),uuid=str(form.get('uuid','')),sub_id=str(form.get('sub_id','')),credential=str(form.get('credential','')))
        return data,str(form.get('operation_id',''))

    @app.post('/customers/new')
    async def customer_create(request:Request):
        user=require(request)
        data,oid=await customer_form(request)
        try:
            op=service.reserve(user,data,oid)
        except RuleError as exc:
            if exc.code!='insufficient_quota':
                raise
            form=await request.form()
            with db.transaction() as conn:
                grant=active_grant(conn,user['id'])
            quota_remaining=max(0,grant['allowance']-grant['sold']-grant['reserved'])/GIB
            permitted={i['id'] for i in allowed(user['id'])}
            selected=[]
            for value in form.getlist('inbounds'):
                try:
                    iid=int(value)
                except (TypeError,ValueError):
                    continue
                if iid in permitted:
                    selected.append(iid)
            form_data={key:str(form.get(key,'')) for key in ('email','volume','comment','uuid','sub_id','credential','expiry','days')}
            form_data['enable']=form.get('enable')=='on'
            form_data['first_use']=form.get('first_use')=='on'
            return render(request,'customer_form.html',customer=None,p={},inbounds=allowed(user['id']),selected=selected,
                          quota_remaining=quota_remaining,form_data=form_data,inline_error='insufficient_quota')
        await run_in_threadpool(service.execute,op['id'])
        result=db.one('SELECT status FROM operations WHERE id=?',(op['id'],))
        message='customer_created' if result and result['status']=='committed' else 'customer_activation_pending' if result and result['status'] in ('sent','uncertain') else 'customer_create_failed'
        return redirect('/customers/'+str(op['customer_id']),message)

    @app.get('/customers/{cid}')
    def customer_detail(request:Request,cid:int):
        user=require(request)
        with db.transaction() as conn:
            c=dict(owned(conn,user,cid))
        placements=db.all('SELECT p.status,i.name FROM placements p LEFT JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=?',(cid,))
        return render(request,'customer_detail.html',customer=c,p=json.loads(c['payload']),placements=placements,ops=db.all('SELECT o.*,u.username actor_name FROM operations o LEFT JOIN users u ON u.id=o.actor WHERE o.customer_id=? ORDER BY o.created DESC LIMIT 15',(cid,)),links=[],subs=[])

    @app.get('/customers/{cid}/edit')
    def customer_edit(request:Request,cid:int):
        user=require(request)
        with db.transaction() as conn:
            c=dict(owned(conn,user,cid))
            active_grant(conn,c['user_id'])
        p=json.loads(c['payload'])
        if c['remote_expiry'] and c['remote_expiry']>0:
            p['expiryTime']=c['remote_expiry']
        return render(request,'customer_form.html',customer=c,p=p,inbounds=allowed(c['user_id']),selected=[r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?',(cid,))],expiry_input=datetime.fromtimestamp(max(0,p['expiryTime'])/1000,ZoneInfo(config.timezone)).strftime('%Y-%m-%dT%H:%M'))

    @app.post('/customers/{cid}/edit')
    async def customer_update(request:Request,cid:int):
        user=require(request)
        data,oid=await customer_form(request,True)
        op=service.reserve(user,data,oid,'update',cid)
        await run_in_threadpool(service.execute,op['id'])
        return redirect('/customers/'+str(cid),'submitted')

    @app.post('/customers/{cid}/delete')
    async def customer_delete(request:Request,cid:int):
        user=require(request)
        form=await request.form()
        op=service.reserve(user,{},str(form.get('operation_id','')),'delete',cid)
        await run_in_threadpool(service.execute,op['id'])
        return redirect('/customers/'+str(cid),'submitted')

    def checked_links(user,cid):
        with db.transaction() as conn:
            c=dict(owned(conn,user,cid))
        records=remote.clients()
        r=next((r for r in records if r['email']==c['email']),None)
        ids={x['inbound_id'] for x in db.all('SELECT inbound_id FROM placements WHERE customer_id=?',(cid,))}
        if not r or c['status']!='active' or not service._identity(c,r) or set(r['inboundIds'])!=ids:
            raise RuleError('remote_drift',409)
        links=remote.links(c['email'])
        sub_segment='/'+quote(c['sub_id'],safe='')
        def is_subscription_uri(link):
            # Subscription URLs are shown separately; keep only per-inbound configuration URIs.
            try:
                parts=urlsplit(link)
                return parts.scheme in ('http','https') and parts.path.rstrip('/').endswith(sub_segment)
            except ValueError:
                return False
        links=list(dict.fromkeys(link for link in links if not is_subscription_uri(link)))
        return c,links

    @app.get('/customers/{cid}/links')
    def links(request:Request,cid:int):
        c,links=checked_links(require(request),cid)
        subscription_link=config.sub_url.rstrip('/')+'/'+quote(c['sub_id'],safe='') if config.sub_url else ''
        return render(request,'links.html',customer=c,links=links,subscription_link=subscription_link)

    @app.get('/operations')
    def operations(request:Request):
        require(request,True)
        ops=db.all('SELECT o.*,u.username actor_name FROM operations o LEFT JOIN users u ON u.id=o.actor ORDER BY o.created DESC LIMIT 15')
        events=db.all('SELECT a.*,u.username actor_name FROM audit a LEFT JOIN users u ON u.id=a.actor ORDER BY a.id DESC LIMIT 15')
        ledger=db.all("SELECT l.*,u.username actor_name,CASE WHEN l.operation_id IS NULL THEN 'ok' WHEN o.status='committed' THEN 'ok' WHEN o.status='failed' THEN 'failed' ELSE coalesce(o.status,'unknown') END action_status FROM ledger l LEFT JOIN users u ON u.id=l.actor LEFT JOIN operations o ON o.id=l.operation_id ORDER BY l.id DESC LIMIT 15")
        return render(request,'operations.html',ops=ops,events=events,ledger=ledger)

    @app.post('/operations/{oid}/reconcile')
    def reconcile(request:Request,oid:str):
        user=require(request,True)
        service.reconcile(oid)
        with db.transaction() as conn:
            audit(conn,user['id'],'reconcile',oid)
        return redirect('/operations')

    @app.post('/operations/{oid}/repair')
    def repair(request:Request,oid:str):
        service.repair(require(request,True),oid)
        return redirect('/operations','submitted')

    @app.post('/operations/{oid}/retry')
    async def retry(request:Request,oid:str):
        actor=require(request)
        form=await request.form()
        operation=service.retry_unsent(actor,oid,str(form.get('operation_id','')))
        await run_in_threadpool(service.execute,operation['id'])
        return redirect('/customers/'+str(operation['customer_id']),'submitted')

    return PathPrefix(app,config.base_path)
