import asyncio
import json
import logging
import secrets
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import activity, __version__
from .links import label_links
from .telegram import Telegram, TelegramError, PREFS as TELEGRAM_PREFS
from .config import Config
from .db import Database, now, audit
from .domain import (Service, RuleError, GIB, PROTOCOLS, DRIFT_ISSUES, active_grant, owned, volume,
                     identifier, display_name)
from .i18n import translator, known
from .security import authenticate, digest, new_session, password_hash
from .xui import XUI, RemoteError

# Errors a form can show next to its fields instead of failing the whole page.
FORM_ERRORS = (RuleError, RemoteError, ValueError, sqlite3.IntegrityError)
# Which form field an error code belongs to.
FIELD_OF = {
    'invalid_volume': 'volume', 'insufficient_quota': 'volume', 'invalid_expiry': 'expiry',
    'invalid_identifier': 'email', 'identity_conflict': 'email', 'inbound_denied': 'inbounds',
    'inbound_unavailable': 'inbounds', 'mixed_protocols': 'inbounds', 'no_inbound': 'inbounds',
    'password_policy': 'password', 'wrong_password': 'current_password',
}
QUICK_VOLUMES = (10, 20, 50, 100)
QUICK_DAYS = (30, 60, 90)
PAGE = 25


def error_code(exc):
    if isinstance(exc, (RuleError, RemoteError)):
        return exc.code
    if isinstance(exc, sqlite3.IntegrityError):
        return 'identity_conflict'
    if str(exc) == 'password_policy':
        return 'password_policy'
    return 'invalid_input'


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


def create_app(config=None, remote=None, background=True, telegram_transport=None):
    config = (config or Config.env()).validate()
    db = Database(config.db)
    db.migrate()
    remote = remote or XUI(config)
    service = Service(db,remote)
    service.settle_seconds = max(60, config.sync_seconds*2)
    service.interrupt_jobs()
    templates = Jinja2Templates(directory=str(Path(__file__).with_name('templates')))
    zone = ZoneInfo(config.timezone)
    t = translator()

    def set_state(conn, key, value):
        conn.execute('INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))

    def sub_template():
        """The subscription address set on the Settings page, else XUI_SUB_URL, else nothing."""
        row=db.one("SELECT value FROM state WHERE key='setting:sub_url'")
        return (row['value'] if row else '') or config.sub_url

    def subscription_link_for(sub_id):
        template=sub_template()
        if not template or not sub_id:
            return ''
        sub_id=quote(display_name(sub_id),safe='')
        if '{subId}' in template:
            return template.replace('{subId}',sub_id)
        return template.rstrip('/')+'/'+sub_id

    async def worker():
        # Full sync every SYNC_SECONDS; in between, quick checks of customers being set up.
        # On errors the full sync backs off (up to 5 minutes) so a down 3X-UI is not hammered.
        failures, next_full = 0, 0
        while True:
            if now() >= next_full:
                try:
                    await run_in_threadpool(service.refresh, 60)
                    failures = 0
                except RuleError:
                    pass  # another sync is running
                except RemoteError:
                    failures += 1
                except Exception:
                    failures += 1
                    # No exception text: remote payloads and credentials must not enter ordinary logs.
                    with db.transaction() as conn:
                        audit(conn,None,'worker',outcome='error')
                        set_state(conn,'sync_error',f'error:{now()}')
                delay = config.sync_seconds if not failures else min(300, config.sync_seconds*2**failures)
                next_full = now()+delay
            elif not failures:
                try:
                    await run_in_threadpool(service.reconcile_pending)
                except Exception:
                    pass
            await asyncio.sleep(min(10, config.sync_seconds))

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if background else None
        bot = threading.Thread(target=telegram.run_forever, daemon=True, name='relay-telegram') if background else None
        if bot:
            bot.start()
        yield
        telegram.stop.set()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(remote,'close'):
            remote.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    telegram = Telegram(db, service, config, lambda sub_id: subscription_link_for(sub_id), transport=telegram_transport)
    app.state.db,app.state.service,app.state.config,app.state.telegram = db,service,config,telegram
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(config.origin).hostname,'127.0.0.1','localhost'])
    app.mount('/static',StaticFiles(directory=Path(__file__).with_name('static')),name='static')
    cookie_name='relay_session_'+digest(config.base_path)[:12] if config.base_path else 'relay_session'

    # ----- Formatting helpers available to every template -----------------

    def number(value):
        if isinstance(value,float):
            return f'{value:,.2f}'.rstrip('0').rstrip('.')
        if isinstance(value,int):
            return f'{value:,}'
        return str(value)

    def date(value):
        if not value:
            return '—'
        return datetime.fromtimestamp(value,zone).strftime('%Y-%m-%d %H:%M')

    def gb(b):
        return number(round(b/GIB,2)) if b is not None else '—'

    def expiry_text(ms):
        """Plain words for a 3X-UI expiry value (ms; negative = days after first use)."""
        if not ms:
            return t('unlimited')
        if ms < 0:
            days = -ms//86400000
            return f"{days} {t('days_unit')} after first use"
        seconds = ms/1000-now()
        if seconds <= 0:
            return t('expired')
        days = int(seconds//86400)
        if days >= 1:
            return f"{days} {t('day_unit') if days == 1 else t('days_unit')} left"
        return f"{max(1,int(seconds//3600))} h left"

    def sync_status():
        rows = {r['key']: r['value'] for r in db.all("SELECT key,value FROM state WHERE key IN ('sync_ok','sync_error','connection')")}
        ok = int(rows.get('sync_ok') or rows.get('connection') or 0)
        error = rows.get('sync_error','')
        code, _, when = error.partition(':')
        failing = bool(error) and (not ok or int(when or 0) >= ok)
        return {'ok': ok, 'failing': failing, 'code': code if failing else '', 'ago': activity.relative(ok) if ok else ''}

    def attention_count():
        return db.one("""SELECT (SELECT count(*) FROM customers c WHERE (c.status='active' AND c.issue!='' AND NOT EXISTS(
                             SELECT 1 FROM operations o WHERE o.customer_id=c.id AND o.status IN ('sent','uncertain'))) OR c.status='failed')
                         + (SELECT count(*) FROM operations WHERE status IN ('sent','uncertain'))
                         + (SELECT count(*) FROM requests WHERE status='pending') n""")['n']

    def render(request,name,status=200,**context):
        user=request.state.user
        owner_id=(context.get('customer') or {}).get('user_id') or (user or {}).get('id')
        owner_row=db.one('SELECT role FROM users WHERE id=?',(owner_id,)) if owner_id else None
        # Admin-owned customers have no plan, so they are always editable.
        writable=bool(owner_row and (owner_row['role']=='admin' or db.one('SELECT id FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>?',(owner_id,now(),now()))))
        message=request.query_params.get('message','')
        error=request.query_params.get('error','')
        def link(**changes):
            # Same page with changed query values; '' removes a value. Paths start with '/'
            # so the browser script adds the base path.
            params={k:v for k,v in request.query_params.items() if k not in ('message','error')}
            for key,value in changes.items():
                if value in (None,''):
                    params.pop(key,None)
                else:
                    params[key]=str(value)
            return request.url.path+('?'+urlencode(params) if params else '')
        base={
            't':t,'user':user,'csrf':request.state.csrf,'path':request.url.path,'number':number,'date':date,'gb':gb,
            'rel':activity.relative,'expiry_text':expiry_text,'display_name':display_name,
            'now':now(),'timezone':config.timezone,'operation_id':str(uuid.uuid4()),'new_id':lambda:str(uuid.uuid4()),'writable':writable,
            'stale_after':config.sync_seconds*3,'message':message if known(message) else '',
            'error':error if known(error) else ('error' if error else ''),'PROTOCOLS':service.protocols,'base_path':config.base_path,
            'form_data':{},'field_errors':{},'page_error':'','sync':sync_status() if user else None,
            'attention_total':attention_count() if user and user['role']=='admin' else 0,
            'version':__version__,'subscription_link_for':subscription_link_for,'quick_volumes':QUICK_VOLUMES,'quick_days':QUICK_DAYS,'link':link,
        }
        base.update(context)
        return templates.TemplateResponse(request=request,name=name,context=base,status_code=status)

    def external(path):
        return config.base_path+path

    def redirect(path,message=None,error=None):
        query={}
        if message:
            query['message']=message
        if error:
            query['error']=error
        separator='&' if '?' in path else '?'
        return RedirectResponse(external(path)+(separator+urlencode(query) if query else ''),303)

    def back(request,default='/'):
        """The same-origin page the browser came from (without its query), else a default."""
        referer=request.headers.get('referer','')
        try:
            parts=urlsplit(referer)
            origin=urlsplit(config.origin)
        except ValueError:
            return default
        if not referer or parts.netloc not in (origin.netloc,request.url.netloc) or not parts.path.startswith(config.base_path+'/'):
            return default
        path=parts.path[len(config.base_path):] or '/'
        return path if path.startswith('/') and not path.startswith('//') else default

    def require(request,admin=False):
        user=request.state.user
        if not user:
            raise RuleError('forbidden',401)
        if admin and user['role']!='admin':
            raise RuleError('forbidden',403)
        return user

    @app.middleware('http')
    async def protection(request,call_next):
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
                response=redirect(back(request),error='too_large')
            else:
                body=bytearray()
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body)>65536:
                        break
                if len(body)>65536:
                    response=redirect(back(request),error='too_large')
                else:
                    request._body=bytes(body)  # Replay the bounded body to downstream form parsing.
                    # All browser mutations are URL-encoded forms, not multipart uploads.
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
                        # Show the problem on the page the user came from; it gets a fresh token there.
                        target='/login' if request.url.path=='/login' or not request.state.user else back(request)
                        response=redirect(target,error='session_expired')
                        response.headers['X-Relay-Session-Error']=reason
        public=request.url.path in ('/login','/healthz') or request.url.path.startswith('/static/')
        if not response and not public and not request.state.user:
            response=RedirectResponse(external('/login'),303)
        if not response and request.state.user and request.state.user['must_change'] and request.url.path not in ('/account','/logout') and not request.url.path.startswith('/static/'):
            response=redirect('/account','must_change')
        if not response and request.state.user and request.method=='POST':
            bucket='write:'+str(request.state.user['id'])
            with db.transaction() as conn:
                attempt=conn.execute('SELECT * FROM attempts WHERE bucket=?',(bucket,)).fetchone()
                if attempt and attempt['until_time']>now() and attempt['count']>=60:
                    response=redirect(back(request),error='rate_limited')
                else:
                    conn.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=CASE WHEN until_time<? THEN 1 ELSE count+1 END,until_time=CASE WHEN until_time<? THEN excluded.until_time ELSE until_time END',(bucket,now()+60,now(),now()))
        if response is None:
            try:
                response=await call_next(request)
            except Exception:
                with db.transaction() as conn:
                    audit(conn,(request.state.user or {}).get('id'),'request',outcome='error')
                response=render(request,'error.html',status=500,page_error='error')
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

    async def rule_error(request,error):
        code=error.code
        with db.transaction() as conn:
            audit(conn,(request.state.user or {}).get('id'),'request',outcome=code)
        if request.method=='POST':
            # Errors from buttons are shown as a message on the page the user came from.
            return redirect(back(request),error=code)
        if error.status==401:
            return RedirectResponse(external('/login'),303)
        return render(request,'error.html',status=error.status,page_error=code)

    @app.exception_handler(RuleError)
    async def rule_error_handler(request,error):
        return await rule_error(request,error)

    @app.exception_handler(RemoteError)
    async def remote_error(request,error):
        return await rule_error(request,RuleError(error.code,503))

    @app.exception_handler(ValueError)
    async def value_error(request,error):
        return await rule_error(request,RuleError(error_code(error)))

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request,error):
        return await rule_error(request,RuleError('identity_conflict',409))

    def form_values(form,fields):
        values={key:str(form.get(key,'')) for key in fields}
        values['inbounds']=[v for v in form.getlist('inbounds')]
        return values

    def field_errors(code,field_map=None):
        field=(field_map or {}).get(code) or FIELD_OF.get(code)
        return {field:code} if field else {}

    def int_param(request,name,default=1,low=1,high=100000):
        try:
            return max(low,min(int(request.query_params.get(name,default)),high))
        except ValueError:
            return default

    @app.get('/healthz')
    def health():
        db.one('SELECT 1 AS ok')
        return {'status':'ok','schema':5,'version':__version__}

    # ----- Sign in and account -----------------------------------------------

    @app.get('/login')
    def login_page(request:Request):
        if request.state.user:
            return redirect('/')
        return render(request,'login.html')

    @app.post('/login')
    async def login(request:Request):
        form=await request.form()
        username=str(form.get('username','')).strip().lower()
        user=await run_in_threadpool(authenticate,db,username,str(form.get('password','')),request.client.host)
        if not user:
            return render(request,'login.html',status=401,page_error='invalid_login',form_data={'username':username})
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

    def account_page(request,user,status=200,**context):
        link=telegram.linked(user['id'])
        ctx={'tg_ready':bool(telegram.setting('token')) and telegram.enabled(),'tg_link':link,
             'tg_prefs':telegram.prefs(user,link) if link else {},'tg_pref_labels':TELEGRAM_PREFS[user['role']],
             'tg_username':telegram.status()['username'],'tg_code':''}
        ctx.update(context)
        return render(request,'account.html',status=status,**ctx)

    @app.get('/account')
    def account(request:Request):
        return account_page(request,require(request))

    @app.post('/account/telegram/connect')
    def telegram_connect(request:Request):
        user=require(request)
        if not telegram.enabled():
            raise RuleError('telegram_off',409)
        if not telegram.status()['username']:
            try:
                telegram.check()
            except TelegramError:
                raise RuleError('telegram_unreachable',503) from None
        return account_page(request,user,tg_code=telegram.new_code(user))

    @app.post('/account/telegram/unlink')
    def telegram_unlink(request:Request):
        telegram.unlink(require(request))
        return redirect('/account','telegram_unlinked')

    @app.post('/account/telegram/prefs')
    async def telegram_prefs(request:Request):
        user=require(request)
        form=await request.form()
        telegram.set_prefs(user,set(form.getlist('pref')))
        return redirect('/account','saved')

    @app.post('/account')
    async def account_save(request:Request):
        user=require(request)
        form=await request.form()
        # Shared persistent login limiter also bounds old-password guessing.
        valid=await run_in_threadpool(authenticate,db,user['username'],str(form.get('current_password','')),request.client.host)
        if not valid:
            return account_page(request,user,status=400,page_error='wrong_password',field_errors={'current_password':'wrong_password'})
        try:
            hashed=await run_in_threadpool(password_hash,str(form.get('new_password','')))
        except ValueError:
            return account_page(request,user,status=400,page_error='password_policy',field_errors={'new_password':'password_policy'})
        with db.transaction() as conn:
            conn.execute('UPDATE users SET password_hash=?,must_change=0 WHERE id=?',(hashed,user['id']))
            conn.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            token,_=new_session(conn,config,user['id'])
            audit(conn,user['id'],'password_change')
        response=redirect('/account','password_changed')
        cookie(response,token)
        return response

    # ----- Dashboard -----------------------------------------------------------

    def grants(uid):
        return db.all('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC',(uid,))

    def current_plan(uid):
        return db.one('SELECT * FROM grants WHERE user_id=? AND closed=0 AND starts<=? AND expires>? ORDER BY id DESC LIMIT 1',(uid,now(),now()))

    def online_clause():
        return f'c.online=1 AND c.telemetry_at>{now()-config.sync_seconds*3}'

    @app.get('/')
    def dashboard(request:Request):
        user=require(request)
        feed=activity.feed(db,user,date,limit=10)
        if user['role']=='admin':
            stats={
                'resellers':db.one("SELECT count(*) n FROM users WHERE role='reseller' AND deleted=0")['n'],
                'customers':db.one("SELECT count(*) n FROM customers WHERE status='active'")['n'],
                'online':db.one(f"SELECT count(*) n FROM customers c WHERE c.status='active' AND {online_clause()}")['n'],
                'attention':attention_count(),
            }
            totals=db.one('SELECT coalesce(sum(allowance),0) granted,coalesce(sum(sold),0) sold FROM grants g JOIN users u ON u.id=g.user_id WHERE u.deleted=0')
            top=db.all(f"""SELECT u.id,u.username,u.suspended,(SELECT count(*) FROM customers c WHERE c.user_id=u.id AND c.status='active') customers,
                           g.allowance,g.sold,g.reserved,g.expires FROM users u
                           LEFT JOIN grants g ON g.id=(SELECT id FROM grants WHERE user_id=u.id AND closed=0 AND starts<={now()} AND expires>{now()} ORDER BY id DESC LIMIT 1)
                           WHERE u.role='reseller' AND u.deleted=0 ORDER BY customers DESC, u.username LIMIT 6""")
            inbound_count=db.one('SELECT count(*) n FROM inbounds')['n']
            info=db.one("SELECT value FROM state WHERE key='xui_info'")
            xui=json.loads(info['value']) if info else {}
            return render(request,'dashboard.html',stats=stats,totals=totals,top=top,feed=feed,inbound_count=inbound_count,plan=None,xui=xui)
        plan=current_plan(user['id'])
        counts=db.one(f"""SELECT count(*) n,coalesce(sum(json_extract(payload,'$.enable')),0) enabled,
                          coalesce(sum(CASE WHEN {online_clause()} THEN 1 ELSE 0 END),0) online,
                          coalesce(sum(CASE WHEN issue!='' THEN 1 ELSE 0 END),0) attention
                          FROM customers c WHERE user_id=? AND status='active'""",(user['id'],))
        movements=activity.ledger_rows(db,db.all("SELECT l.*,u.username actor_name FROM ledger l LEFT JOIN users u ON u.id=l.actor WHERE l.user_id=? ORDER BY l.id DESC LIMIT 30",(user['id'],)),reseller_view=True)[:8]
        last_plan=None if plan else db.one('SELECT * FROM grants WHERE user_id=? ORDER BY id DESC LIMIT 1',(user['id'],))
        last_request=db.one('SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 1',(user['id'],))
        return render(request,'dashboard.html',plan=plan,last_plan=last_plan,counts=counts,feed=feed,movements=movements,last_request=last_request)

    # ----- Sync ----------------------------------------------------------------

    @app.post('/sync')
    def sync(request:Request):
        user=require(request)
        bucket='sync:'+str(user['id'])
        with db.transaction() as conn:
            row=conn.execute('SELECT * FROM attempts WHERE bucket=?',(bucket,)).fetchone()
            if row and row['until_time']>now():
                raise RuleError('sync_too_soon',429)
            conn.execute('INSERT INTO attempts VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=1,until_time=excluded.until_time',(bucket,now()+15))
        service.refresh(wait=15)
        with db.transaction() as conn:
            audit(conn,user['id'],'sync')
        return redirect(back(request),'synced')

    # ----- Inbounds ------------------------------------------------------------

    @app.get('/inbounds')
    def inbound_page(request:Request):
        require(request,True)
        rows=db.all("""SELECT i.id,i.name,i.protocol,i.port,i.available,i.synced,
                       (SELECT count(*) FROM placements p JOIN customers c ON c.id=p.customer_id WHERE p.inbound_id=i.id AND c.status!='deleted') customers,
                       (SELECT count(*) FROM permissions WHERE inbound_id=i.id) resellers FROM inbounds i ORDER BY i.id""")
        return render(request,'inbounds.html',inbounds=rows)

    # ----- Resellers -----------------------------------------------------------

    @app.get('/resellers')
    def reseller_list(request:Request):
        require(request,True)
        q=request.query_params.get('q','')[:100]
        page=int_param(request,'page')
        status=request.query_params.get('status','')
        condition=''
        if status=='suspended':
            condition=' AND suspended=1'
        elif status=='active':
            condition=f' AND suspended=0 AND EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.expires>{now()})'
        elif status=='expired':
            condition=f' AND suspended=0 AND NOT EXISTS(SELECT 1 FROM grants g WHERE g.user_id=u.id AND g.closed=0 AND g.expires>{now()})'
        sort={'username':'username','created':'created DESC','sold':'lifetime DESC'}.get(request.query_params.get('sort'),'username')
        rows=db.all(f"""SELECT u.*, (SELECT sum(sold) FROM grants WHERE user_id=u.id) lifetime,
                        (SELECT count(*) FROM customers c WHERE c.user_id=u.id AND c.status='active') customers,
                        g.allowance,g.sold,g.reserved,g.expires plan_expires
                        FROM users u LEFT JOIN grants g ON g.id=(SELECT id FROM grants WHERE user_id=u.id AND closed=0 AND starts<={now()} AND expires>{now()} ORDER BY id DESC LIMIT 1)
                        WHERE role='reseller' AND deleted=0 AND (username LIKE ? OR tag LIKE ?)"""+condition+" ORDER BY "+sort+" LIMIT ? OFFSET ?",
                    ('%'+q+'%','%'+q+'%',PAGE,(page-1)*PAGE))
        return render(request,'resellers.html',rows=rows,q=q,page=page,filters=request.query_params,status_filter=status)

    def allowed(uid=None):
        if uid is None:
            return db.all('SELECT id,name,protocol,port,available FROM inbounds ORDER BY id')
        return db.all('SELECT i.id,i.name,i.protocol,i.port,i.available FROM inbounds i JOIN permissions p ON i.id=p.inbound_id WHERE p.user_id=? ORDER BY i.id',(uid,))

    def parse_expiry(value):
        try:
            moment=datetime.fromisoformat(str(value))
        except ValueError:
            raise RuleError('invalid_expiry') from None
        if moment.tzinfo is None:
            moment=moment.replace(tzinfo=zone)
        return int(moment.timestamp())

    def expiry_input(ts):
        return datetime.fromtimestamp(ts,zone).strftime('%Y-%m-%dT%H:%M') if ts and ts>0 else ''

    def reseller_page(request,reseller=None,status=200,**context):
        uid=reseller['id'] if reseller else None
        ctx={'reseller':reseller,'inbounds':allowed(),'selected':[i['id'] for i in allowed(uid)] if uid else [],
             'history':[],'ledger':[],'feed':[],'plan':None,'customer_total':0,'tab':context.pop('tab','overview'),
             'renew_data':{},'adjust_data':{},'renew_error':'','adjust_error':''}
        if reseller:
            ctx['history']=grants(uid)
            ctx['plan']=current_plan(uid)
            raw=db.all("""SELECT l.*,u.username actor_name,CASE WHEN l.operation_id IS NULL THEN 'ok' WHEN o.status='committed' THEN 'ok' WHEN o.status='failed' THEN 'failed' ELSE coalesce(o.status,'unknown') END action_status
                          FROM ledger l LEFT JOIN users u ON u.id=l.actor LEFT JOIN operations o ON o.id=l.operation_id WHERE l.user_id=? ORDER BY l.id DESC LIMIT 40""",(uid,))
            ctx['ledger']=activity.ledger_rows(db,raw,reseller_view=True)[:20]
            ctx['feed']=activity.feed(db,request.state.user,date,limit=15,reseller=uid)
            ctx['customer_total']=db.one("SELECT count(*) n FROM customers WHERE user_id=? AND status!='deleted'",(uid,))['n']
            plan=ctx['plan'] or {}
            ctx['plan_defaults']={'expiry':expiry_input(plan.get('expires') or now()+30*86400),
                                  'volume':gb(plan.get('allowance')).replace(',','') if plan else ''}
        ctx.update(context)
        return render(request,'reseller_form.html',status=status,**ctx)

    def get_reseller(uid):
        reseller=db.one("SELECT * FROM users WHERE id=? AND role='reseller' AND deleted=0",(uid,))
        if not reseller:
            raise RuleError('not_found',404)
        return reseller

    @app.get('/resellers/new')
    def reseller_new(request:Request):
        require(request,True)
        return reseller_page(request,form_data={'expiry':expiry_input(now()+30*86400),'inbounds':[]})

    @app.post('/resellers/new')
    async def reseller_create(request:Request):
        user=require(request,True)
        form=await request.form()
        values=form_values(form,('username','tag','volume','expiry'))
        try:
            if not values['inbounds']:
                raise RuleError('no_inbound')
            uid=await run_in_threadpool(service.reseller,user,values['username'],str(form.get('password','')),values['tag'],values['volume'],
                                        parse_expiry(values['expiry']),values['inbounds'])
        except FORM_ERRORS as exc:
            code=error_code(exc)
            fields={'invalid_identifier':'username','identity_conflict':'username','password_policy':'password'}
            return reseller_page(request,status=400,page_error=code,field_errors=field_errors(code,fields),form_data=values,
                                 selected=[int(i) for i in values['inbounds'] if str(i).isdigit()])
        return redirect('/resellers/'+str(uid),'reseller_created')

    @app.get('/resellers/{uid}')
    def reseller_detail(request:Request,uid:int):
        require(request,True)
        tab=request.query_params.get('tab','overview')
        return reseller_page(request,get_reseller(uid),tab=tab if tab in ('overview','plan','activity','danger') else 'overview')

    @app.post('/resellers/{uid}')
    async def reseller_save(request:Request,uid:int):
        user=require(request,True)
        reseller=get_reseller(uid)
        form=await request.form()
        values=form_values(form,('username','notes'))
        values['suspended']=form.get('suspended')=='on'
        suspended=values['suspended']
        try:
            reset=str(form.get('password',''))
            hashed=await run_in_threadpool(password_hash,reset) if reset else None
            ids=sorted(set(int(i) for i in values['inbounds']))
            username=identifier(values['username'])
            with db.transaction() as conn:
                current=conn.execute("SELECT suspended FROM users WHERE id=? AND role='reseller' AND deleted=0",(uid,)).fetchone()
                if not current:
                    raise RuleError('not_found',404)
                suspension_changed=bool(current['suspended'])!=suspended
                for iid in ids:
                    if not conn.execute('SELECT 1 FROM inbounds WHERE id=?',(iid,)).fetchone():
                        raise RuleError('inbound_denied')
                conn.execute('UPDATE users SET username=?,suspended=?,notes=? WHERE id=?',(username,int(suspended),values['notes'][:2000],uid))
                conn.execute('DELETE FROM permissions WHERE user_id=?',(uid,))
                conn.executemany('INSERT INTO permissions VALUES(?,?)',[(uid,i) for i in ids])
                if hashed:
                    conn.execute('UPDATE users SET password_hash=?,must_change=1 WHERE id=?',(hashed,uid))
                conn.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
                audit(conn,user['id'],'reseller_update',uid)
        except FORM_ERRORS as exc:
            code=error_code(exc)
            fields={'invalid_identifier':'username','identity_conflict':'username','password_policy':'password'}
            return reseller_page(request,reseller,status=400,page_error=code,field_errors=field_errors(code,fields),
                                 form_data=values,selected=[int(i) for i in values['inbounds'] if str(i).isdigit()])
        if suspension_changed and service.set_reseller_customer_enabled(uid,not suspended):
            return redirect('/resellers/'+str(uid),'suspension_sync_pending')
        return redirect('/resellers/'+str(uid),'saved')

    @app.post('/resellers/{uid}/renew')
    async def renew(request:Request,uid:int):
        user=require(request,True)
        reseller=get_reseller(uid)
        form=await request.form()
        values=form_values(form,('volume','expiry'))
        try:
            service.renew(user,uid,values['volume'],parse_expiry(values['expiry']))
        except FORM_ERRORS as exc:
            return reseller_page(request,reseller,status=400,tab='plan',renew_error=error_code(exc),renew_data=values)
        return redirect(f'/resellers/{uid}?tab=plan','plan_renewed')

    @app.post('/resellers/{uid}/adjust')
    async def adjust(request:Request,uid:int):
        user=require(request,True)
        reseller=get_reseller(uid)
        form=await request.form()
        values=form_values(form,('volume','expiry'))
        try:
            amount=volume(values['volume'])
            expires=parse_expiry(values['expiry'])
            with db.transaction() as conn:
                grant=active_grant(conn,uid)
                if amount<grant['sold']+grant['reserved']:
                    raise RuleError('insufficient_quota')
                if expires<=now():
                    raise RuleError('invalid_expiry')
                conn.execute('UPDATE grants SET allowance=?,expires=? WHERE id=?',(amount,expires,grant['id']))
                conn.execute("INSERT INTO ledger(user_id,grant_id,kind,amount,actor,created) VALUES(?,?,'adjust',?,?,?)",(uid,grant['id'],amount-grant['allowance'],user['id'],now()))
                audit(conn,user['id'],'grant_adjust',grant['id'])
        except FORM_ERRORS as exc:
            return reseller_page(request,reseller,status=400,tab='plan',adjust_error=error_code(exc),adjust_data=values)
        return redirect(f'/resellers/{uid}?tab=plan','plan_changed')

    @app.post('/resellers/{uid}/delete')
    async def reseller_delete(request:Request,uid:int):
        user=require(request,True)
        get_reseller(uid)
        failed=await run_in_threadpool(service.delete_reseller,user,uid)
        if failed:
            return redirect(f'/resellers/{uid}?tab=danger',error='reseller_delete_partial')
        return redirect('/resellers','reseller_deleted')

    # ----- Customers -----------------------------------------------------------

    VIEWS={
        'active':"c.status='active' AND c.issue='' AND json_extract(c.payload,'$.enable')=1",
        'disabled':"c.status='active' AND json_extract(c.payload,'$.enable')=0",
        'expired':"c.status='active' AND coalesce(c.remote_expiry,json_extract(c.payload,'$.expiryTime')) BETWEEN 1 AND {ms}",
        'online':"c.status='active' AND {online}",
        'attention':"((c.status='active' AND c.issue!='') OR c.status IN ('pending','failed'))",
        'removed':"c.status='deleted'",
    }

    def customer_filter(user,params):
        """SQL conditions for the customer list filters (also used to pick bulk targets)."""
        q=str(params.get('q',''))[:100]
        view=params.get('view','') if params.get('view','') in VIEWS else ''
        where=['(c.email LIKE ? OR json_extract(c.payload,\'$.comment\') LIKE ?)']
        args=['%'+q+'%','%'+q+'%']
        if user['role']!='admin':
            where.append('c.user_id=?');args.append(user['id'])
        elif str(params.get('owner','')).isdigit():
            where.append('c.user_id=?');args.append(int(params['owner']))
        where.append(VIEWS[view].format(ms=now()*1000,online=online_clause()) if view else "c.status!='deleted'")
        if str(params.get('inbound','')).isdigit():
            where.append('EXISTS(SELECT 1 FROM placements WHERE customer_id=c.id AND inbound_id=?)');args.append(int(params['inbound']))
        return q,view,' AND '.join(where),args

    @app.get('/customers')
    def customers(request:Request):
        user=require(request)
        params=request.query_params
        q,view,where,args=customer_filter(user,params)
        sort={'email':'c.email','package':'c.package DESC','created':'c.id DESC','usage':'coalesce(c.used,0) DESC'}.get(params.get('sort'),'c.id DESC')
        page=int_param(request,'page')
        total=db.one('SELECT count(*) n FROM customers c WHERE '+where,args)['n']
        rows=db.all('SELECT c.*,u.username owner FROM customers c JOIN users u ON c.user_id=u.id WHERE '+where+' ORDER BY '+sort+' LIMIT ? OFFSET ?',args+[PAGE,(page-1)*PAGE])
        for c in rows:
            c['p']=json.loads(c['payload'])
            c['inbound_names']=[r['name'] or t('unnamed_inbound') for r in db.all('SELECT i.name FROM placements p JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=?',(c['id'],))]
            c['stale']=not c['telemetry_at'] or c['telemetry_at']<now()-config.sync_seconds*3
            c['state'],c['tone']=activity.customer_state(c,c['p'])
            c['percent']=min(100,round((c['used'] or 0)*100/c['package'])) if c['package'] else 0
        owners=db.all("SELECT id,username FROM users WHERE role='reseller' AND deleted=0 ORDER BY username") if user['role']=='admin' else []
        if user['role']=='admin':
            any_customers=bool(db.one('SELECT 1 x FROM customers LIMIT 1'))
        else:
            any_customers=bool(db.one('SELECT 1 x FROM customers WHERE user_id=? LIMIT 1',(user['id'],)))
        return render(request,'customers.html',rows=rows,q=q,page=page,view=view,owners=owners,filters=params,any_customers=any_customers,total=total,
                      inbounds=allowed(None if user['role']=='admin' else user['id']))

    def customer_form_page(request,user,customer=None,status=200,form_data=None,**context):
        """The create/edit form, with current values or what the user just typed."""
        owner=customer['user_id'] if customer else user['id']
        admin_owned=db.one('SELECT role FROM users WHERE id=?',(owner,))['role']=='admin'
        plan=None if admin_owned else current_plan(owner)
        quota_remaining=max(0,plan['allowance']-plan['sold']-plan['reserved'])/GIB if plan else 0
        p=json.loads(customer['payload']) if customer else {}
        if customer and customer['remote_expiry'] and customer['remote_expiry']>0:
            p['expiryTime']=customer['remote_expiry']
        expiry=p.get('expiryTime',0)
        values={'email':'','volume':gb(customer['package']).replace(',','') if customer else '','comment':p.get('comment',''),
                'enable':p.get('enable',True),'first_use':expiry<0,'days':str(-expiry//86400000) if expiry<0 else '30',
                'expiry':expiry_input(expiry/1000) if expiry>0 else expiry_input(now()+30*86400),'uuid':'','sub_id':'','credential':''}
        selected=[r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?',(customer['id'],))] if customer else []
        if form_data:
            values.update({k:v for k,v in form_data.items() if k!='inbounds'})
            selected=[int(i) for i in form_data.get('inbounds',[]) if str(i).isdigit()]
        single=bool(getattr(remote,'single_inbound',False))
        return render(request,'customer_form.html',status=status,customer=customer,p=p,v=values,inbounds=allowed(None if admin_owned else owner),selected=selected,single_inbound=single,admin_owned=admin_owned,
                      quota_remaining=quota_remaining,plan=plan,**context)

    def read_customer_form(form):
        values=form_values(form,('email','volume','comment','uuid','sub_id','credential','expiry','days'))
        values['enable']=form.get('enable')=='on'
        values['first_use']=form.get('first_use')=='on'
        data={'comment':values['comment'],'volume':values['volume'],'enable':values['enable'],
              'uuid':values['uuid'],'sub_id':values['sub_id'],'credential':values['credential'],'inbounds':values['inbounds']}
        return values,data

    def apply_expiry(values,data):
        if values['first_use']:
            try:
                days=int(values['days'] or '0')
            except ValueError:
                raise RuleError('invalid_expiry') from None
            if not 1<=days<=3650:
                raise RuleError('invalid_expiry')
            data['expiry']=-days*86400000
        else:
            data['expiry']=parse_expiry(values['expiry'])*1000
        if not data['inbounds']:
            raise RuleError('no_inbound')

    def finish(op_id,customer_id,success,pending):
        result=db.one('SELECT status,diagnostic FROM operations WHERE id=?',(op_id,))
        if result and result['status']=='committed':
            return redirect('/customers/'+str(customer_id),success)
        if result and result['status'] in ('sent','uncertain'):
            return redirect('/customers/'+str(customer_id),pending)
        return redirect('/customers/'+str(customer_id),error=(result or {}).get('diagnostic') or 'error')

    @app.get('/customers/new')
    def customer_new(request:Request):
        user=require(request)
        if user['role']=='reseller':
            with db.transaction() as conn:
                active_grant(conn,user['id'])
        return customer_form_page(request,user)

    @app.post('/customers/new')
    async def customer_create(request:Request):
        user=require(request)
        form=await request.form()
        values,data=read_customer_form(form)
        data['email']=values['email']
        try:
            apply_expiry(values,data)
            op=service.reserve(user,data,str(form.get('operation_id','')))
        except FORM_ERRORS as exc:
            code=error_code(exc)
            return customer_form_page(request,user,status=400,form_data=values,page_error=code,field_errors=field_errors(code))
        await run_in_threadpool(service.execute,op['id'])
        return finish(op['id'],op['customer_id'],'customer_created','customer_activation_pending')

    def customer_view(user,cid):
        with db.transaction() as conn:
            c=dict(owned(conn,user,cid))
        return c

    @app.get('/customers/{cid}')
    def customer_detail(request:Request,cid:int):
        user=require(request)
        c=customer_view(user,cid)
        p=json.loads(c['payload'])
        known_ids={r['id']:r['name'] for r in db.all('SELECT id,name FROM inbounds')}
        placements=db.all('SELECT p.inbound_id,p.status,i.name,i.protocol,i.port FROM placements p LEFT JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=?',(cid,))
        c['placement_ids']=[r['inbound_id'] for r in placements]
        state,tone=activity.customer_state(c,p)
        ops=db.all('SELECT o.*,u.username actor_name FROM operations o LEFT JOIN users u ON u.id=o.actor WHERE o.customer_id=? ORDER BY o.created DESC LIMIT 10',(cid,))
        pending=next((o for o in ops if o['status'] in ('sent','uncertain','reserved')),None)
        snapshot=db.one('SELECT value FROM state WHERE key=?',(f'drift:{cid}',))
        diff=activity.drift_diff(c,p,json.loads(snapshot['value']) if snapshot else None,known_ids,date) if c['issue'] in DRIFT_ISSUES else []
        owner=db.one('SELECT username FROM users WHERE id=?',(c['user_id'],))
        percent=min(100,round((c['used'] or 0)*100/c['package'])) if c['package'] else 0
        feed=[e for e in activity.feed(db,user,date,limit=60) if e['link']==f'/customers/{cid}'][:10]
        failed_create=next((o for o in ops if o['status']=='failed' and o['kind']=='create'),None) if c['status']=='failed' else None
        return render(request,'customer_detail.html',customer=c,p=p,placements=placements,state=state,tone=tone,ops=ops,pending=pending,
                      diff=diff,owner=owner,percent=percent,feed=feed,failed_create=failed_create)

    @app.get('/customers/{cid}/edit')
    def customer_edit(request:Request,cid:int):
        user=require(request)
        c=customer_view(user,cid)
        if c['status']!='active':
            raise RuleError('reconcile_required',409)
        owner=db.one('SELECT role FROM users WHERE id=?',(c['user_id'],))
        if owner['role']!='admin':
            # Reseller customers can only be changed while the reseller's plan is active.
            # Admin customers have no plan.
            with db.transaction() as conn:
                active_grant(conn,c['user_id'])
        return customer_form_page(request,user,c)

    @app.post('/customers/{cid}/edit')
    async def customer_update(request:Request,cid:int):
        user=require(request)
        c=customer_view(user,cid)
        form=await request.form()
        values,data=read_customer_form(form)
        try:
            apply_expiry(values,data)
            op=service.reserve(user,data,str(form.get('operation_id','')),'update',cid)
        except FORM_ERRORS as exc:
            code=error_code(exc)
            return customer_form_page(request,user,c,status=400,form_data=values,page_error=code,
                                      field_errors=field_errors(code,{'identity_conflict':'sub_id'}))
        await run_in_threadpool(service.execute,op['id'])
        return finish(op['id'],cid,'customer_updated','submitted')

    @app.post('/customers/{cid}/delete')
    async def customer_delete(request:Request,cid:int):
        user=require(request)
        form=await request.form()
        customer_view(user,cid)
        came_from=back(request,'/customers')
        listing=came_from if came_from.startswith('/customers') and came_from!=f'/customers/{cid}' else '/customers'
        if user['role']=='admin':
            # The admin's delete works in every state, also for stuck or changed customers.
            await run_in_threadpool(service.force_remove,user,cid)
            return redirect(listing if came_from!='/attention' else '/attention','customer_deleted')
        op=service.reserve(user,{},str(form.get('operation_id','')),'delete',cid)
        await run_in_threadpool(service.execute,op['id'])
        result=db.one('SELECT status,diagnostic FROM operations WHERE id=?',(op['id'],))
        if result['status']=='committed':
            return redirect(listing,'customer_deleted')
        if result['status']=='failed':
            return redirect(f'/customers/{cid}',error=result['diagnostic'] or 'error')
        return redirect(f'/customers/{cid}','customer_delete_pending')

    @app.post('/customers/{cid}/accept')
    async def customer_accept(request:Request,cid:int):
        user=require(request,True)
        await run_in_threadpool(service.accept_remote,user,cid)
        return redirect(back(request,f'/customers/{cid}'),'accepted')

    def checked_links(user,cid):
        c=customer_view(user,cid)
        if c['status']!='active':
            raise RuleError('reconcile_required',409)
        records=remote.clients()
        r=next((r for r in records if r['email']==c['email']),None)
        if not r:
            raise RuleError('remote_missing',409)
        if not service._identity(c,r):
            raise RuleError('remote_identity',409)
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
        subscription_link=subscription_link_for(c['sub_id'])
        inbounds=db.all('SELECT i.id,i.name,i.protocol,i.port,i.metadata FROM placements p JOIN inbounds i ON i.id=p.inbound_id WHERE p.customer_id=? ORDER BY i.id',(cid,))
        items=label_links(links,inbounds,display_name(c['email']))
        return render(request,'links.html',customer=c,links=items,subscription_link=subscription_link)

    # ----- Bulk inbound changes ---------------------------------------------------

    BULK_FILTERS=('q','view','owner','inbound')

    def bulk_targets(user,source):
        """Customer ids picked on the list, or every customer matching the list filters."""
        ids=[int(i) for i in source.getlist('ids') if str(i).isdigit()] if hasattr(source,'getlist') else []
        if ids:
            return ids,{}
        filters={k:str(source.get(k,'')) for k in BULK_FILTERS if source.get(k)}
        _,_,where,args=customer_filter(user,filters)
        rows=db.all('SELECT c.id FROM customers c WHERE '+where+" AND c.status!='deleted' ORDER BY c.id LIMIT 5000",args)
        return [r['id'] for r in rows],filters

    def bulk_page(request,user,source,stage='choose',status=200,**context):
        ids,filters=bulk_targets(user,source)
        action=str(source.get('action','add'))
        if action not in service.BULK_ACTIONS:
            action='add'
        picked=[int(i) for i in (source.getlist('pick') if hasattr(source,'getlist') else []) if str(i).isdigit()]
        choices=[i for i in allowed(None if user['role']=='admin' else user['id']) if i['protocol'] in service.protocols]
        names={r['id']:(r['name'] or t('unnamed_inbound')) for r in db.all('SELECT id,name FROM inbounds')}
        sample=db.all('SELECT id,email FROM customers WHERE id IN ('+','.join('?'*len(ids[:8]))+')',ids[:8]) if ids else []
        explicit=[int(i) for i in source.getlist('ids') if str(i).isdigit()] if hasattr(source,'getlist') else []
        ctx={'ids':explicit,'filters':filters,'count':len(ids),
             'action':action,'picked':picked,'choices':choices,'names':names,'sample':sample,'plan':None,'changes':0,'skips':{}}
        if stage=='preview' and ids:
            plan=service.plan_bulk(user,ids,action,picked)
            ctx['plan']=plan
            ctx['changes']=sum(1 for i in plan if not i['skip'])
            skips={}
            for item in plan:
                if item['skip']:
                    skips[item['skip']]=skips.get(item['skip'],0)+1
            ctx['skips']=skips
        ctx.update(context)
        return render(request,'bulk.html',status=status,**ctx)

    @app.get('/bulk')
    def bulk_start(request:Request):
        user=require(request)
        return bulk_page(request,user,request.query_params)

    @app.post('/bulk')
    async def bulk_step(request:Request):
        user=require(request)
        form=await request.form()
        stage=str(form.get('stage','choose'))
        try:
            if stage=='apply':
                ids,_=bulk_targets(user,form)
                action=str(form.get('action',''))
                picked=[int(i) for i in form.getlist('pick') if str(i).isdigit()]
                plan=service.plan_bulk(user,ids,action,picked)
                if not any(not i['skip'] for i in plan):
                    raise RuleError('bulk_nothing')
                names={r['id']:(r['name'] or t('unnamed_inbound')) for r in db.all('SELECT id,name FROM inbounds')}
                label={'add':'Add','remove':'Remove','move':'Move to','enable':'Turn on','disable':'Turn off'}[action]
                summary=label+(' '+', '.join(names.get(i,f'#{i}') for i in picked) if action in ('add','remove','move') else '')
                job_id=service.create_bulk_job(user,action,picked,plan,summary)
                if background:
                    threading.Thread(target=service.run_bulk_job,args=(job_id,),daemon=True,name='relay-bulk').start()
                else:
                    service.run_bulk_job(job_id)
                return redirect('/jobs/'+job_id)
            return bulk_page(request,user,form,stage)
        except FORM_ERRORS as exc:
            code=error_code(exc)
            return bulk_page(request,user,form,'choose',status=400,page_error=code,field_errors=field_errors(code,{'no_inbound':'pick','mixed_protocols':'pick','single_inbound':'pick'}))

    @app.get('/jobs/{job_id}')
    def job_page(request:Request,job_id:str):
        user=require(request)
        job=db.one('SELECT j.*,u.username actor_name FROM jobs j JOIN users u ON u.id=j.actor WHERE j.id=?',(job_id,))
        if not job or (user['role']!='admin' and job['actor']!=user['id']):
            raise RuleError('not_found',404)
        items=json.loads(job['items'])
        names={r['id']:(r['name'] or t('unnamed_inbound')) for r in db.all('SELECT id,name FROM inbounds')}
        finished=job['done']+job['skipped']+job['failed']
        return render(request,'job.html',job=job,items=items,names=names,finished=finished)

    # ----- Settings (admin) ------------------------------------------------------

    def check_sub_template(value):
        value=value.strip()
        if not value:
            return ''
        test=value.replace('{subId}','SUBID')
        parts=urlsplit(test)
        if parts.scheme not in ('http','https') or not parts.hostname or parts.username or parts.password or any(c.isspace() for c in value):
            raise RuleError('invalid_sub_url')
        return value

    @app.get('/settings')
    def settings_page(request:Request):
        require(request,True)
        return settings_render(request)

    def settings_render(request,status=200,**context):
        row=db.one("SELECT value FROM state WHERE key='setting:sub_url'")
        ctx={'sub_url':row['value'] if row else '','env_sub_url':config.sub_url,'example':subscription_link_for('SUBSCRIPTION-ID'),
             'tg':telegram.status(),'tg_has_token':bool(telegram.setting('token')),'tg_proxy':telegram.setting('proxy'),'tg_api':telegram.setting('api'),
             'tg_hour':telegram.setting('report_hour','9'),'tg_linked':telegram.linked(request.state.user['id'])}
        ctx.update(context)
        return render(request,'settings.html',status=status,**ctx)

    @app.post('/settings')
    async def settings_save(request:Request):
        user=require(request,True)
        form=await request.form()
        typed=str(form.get('sub_url',''))
        try:
            value=check_sub_template(typed)
        except RuleError as exc:
            return settings_render(request,status=400,sub_url=typed,example='',page_error=exc.code,field_errors={'sub_url':exc.code})
        with db.transaction() as conn:
            set_state(conn,'setting:sub_url',value)
            audit(conn,user['id'],'settings_update','subscription link')
        return redirect('/settings','saved')

    @app.post('/settings/telegram')
    async def settings_telegram(request:Request):
        user=require(request,True)
        form=await request.form()
        token=str(form.get('token','')).strip()
        proxy=str(form.get('proxy','')).strip()
        api_url=str(form.get('api_url','')).strip().rstrip('/')
        hour=str(form.get('report_hour','9')).strip()
        errors={}
        if token and (':' not in token or any(c.isspace() for c in token) or len(token)>100):
            errors['token']='telegram_bad_token'
        if proxy and urlsplit(proxy).scheme not in ('http','https','socks5','socks5h'):
            errors['proxy']='telegram_bad_proxy'
        if api_url:
            parts=urlsplit(api_url)
            if parts.scheme!='https' or not parts.hostname or parts.query or parts.fragment or parts.username or any(c.isspace() for c in api_url) or len(api_url)>200:
                errors['api_url']='telegram_bad_api'
        if not hour.isdigit() or not 0<=int(hour)<=23:
            errors['report_hour']='invalid_input'
        if errors:
            return settings_render(request,status=400,page_error=next(iter(errors.values())),field_errors=errors,tg_proxy=proxy,tg_api=api_url,tg_hour=hour)
        telegram.save_settings(user,enabled='1' if form.get('enabled')=='on' else '0',token=token or None,proxy=proxy,api=api_url,report_hour=hour)
        if token:
            with db.transaction() as conn:
                conn.execute("DELETE FROM state WHERE key IN ('tg:username','tg:error','tg:offset')")
        if form.get('enabled')=='on':
            try:
                telegram.check()
            except TelegramError as exc:
                return settings_render(request,status=400,page_error='telegram_unreachable',tg_error_detail=str(exc))
        return redirect('/settings','saved')

    @app.post('/settings/telegram/test')
    def settings_telegram_test(request:Request):
        user=require(request,True)
        link=telegram.linked(user['id'])
        if not link:
            raise RuleError('telegram_not_linked',409)
        try:
            telegram.api().call('sendMessage',chat_id=link['chat_id'],text='✅ Test message from Relay. Alerts will arrive here.')
        except (TelegramError,AttributeError) as exc:
            return settings_render(request,status=400,page_error='telegram_unreachable',tg_error_detail=str(exc))
        return redirect('/settings','telegram_test_sent')

    # ----- Requests for more data ---------------------------------------------------

    @app.get('/requests/new')
    def request_new(request:Request):
        user=require(request)
        if user['role']!='reseller':
            raise RuleError('forbidden',403)
        return render(request,'request_form.html',form_data={'volume':'','days':'0','note':''})

    @app.post('/requests/new')
    async def request_create(request:Request):
        user=require(request)
        form=await request.form()
        values={k:str(form.get(k,'')) for k in ('volume','days','note')}
        try:
            service.create_request(user,values['volume'],values['days'] or 0,values['note'])
        except FORM_ERRORS as exc:
            code=error_code(exc)
            return render(request,'request_form.html',status=400,form_data=values,page_error=code,
                          field_errors=field_errors(code,{'invalid_expiry':'days'}))
        return redirect('/','request_sent')

    @app.post('/requests/{rid}/approve')
    async def request_approve(request:Request,rid:int):
        user=require(request,True)
        form=await request.form()
        service.decide_request(user,rid,True,form.get('volume') or None,form.get('days') or None,str(form.get('answer','')))
        return redirect(back(request,'/attention'),'request_approved_msg')

    @app.post('/requests/{rid}/decline')
    async def request_decline(request:Request,rid:int):
        user=require(request,True)
        form=await request.form()
        service.decide_request(user,rid,False,answer=str(form.get('answer','')))
        return redirect(back(request,'/attention'),'request_declined_msg')

    # ----- Activity ------------------------------------------------------------

    @app.get('/activity')
    def activity_page(request:Request):
        user=require(request)
        page=int_param(request,'page')
        category=request.query_params.get('type','')
        reseller=request.query_params.get('reseller','')
        technical=user['role']=='admin' and request.query_params.get('technical')=='1'
        rows=activity.feed(db,user,date,limit=PAGE+1,offset=(page-1)*PAGE,category=category,
                           reseller=int(reseller) if reseller.isdigit() and user['role']=='admin' else None,include_technical=technical)
        resellers=db.all("SELECT id,username FROM users WHERE role='reseller' AND deleted=0 ORDER BY username") if user['role']=='admin' else []
        return render(request,'activity.html',feed=rows[:PAGE],has_next=len(rows)>PAGE,page=page,category=category,
                      categories=activity.CATEGORY_LABELS,resellers=resellers,reseller=reseller,technical=technical,filters=request.query_params)

    # ----- Needs attention (admin) ------------------------------------------------

    def attention_items():
        items=[]
        names={r['id']:r['name'] for r in db.all('SELECT id,name FROM inbounds')}
        for o in db.all("""SELECT o.*,c.email,c.user_id,u.username owner,a.username actor_name FROM operations o JOIN customers c ON c.id=o.customer_id
                           JOIN users u ON u.id=c.user_id LEFT JOIN users a ON a.id=o.actor WHERE o.status IN ('sent','uncertain') ORDER BY o.created"""):
            items.append({'kind':'operation','op':o,'customer_id':o['customer_id'],'email':o['email'],'owner':o['owner'],'since':o['created'],
                          'text':'issue_node_pending' if o['diagnostic']=='node_pending' else 'issue_setting_up','tone':'warning'})
        for c in db.all("""SELECT c.*,u.username owner FROM customers c JOIN users u ON u.id=c.user_id
                           WHERE (c.status='active' AND c.issue!='') OR c.status='failed' ORDER BY c.id"""):
            if any(i['customer_id']==c['id'] for i in items):
                continue
            p=json.loads(c['payload'])
            c['placement_ids']=[r['inbound_id'] for r in db.all('SELECT inbound_id FROM placements WHERE customer_id=?',(c['id'],))]
            if c['status']=='failed':
                failed=db.one("SELECT * FROM operations WHERE customer_id=? AND status='failed' AND kind='create' ORDER BY created DESC LIMIT 1",(c['id'],))
                items.append({'kind':'failed','customer_id':c['id'],'email':c['email'],'owner':c['owner'],'since':c['created'],'op':failed,
                              'text':'issue_failed_create','tone':'danger','reason':(failed or {}).get('diagnostic','')})
                continue
            snapshot=db.one('SELECT value FROM state WHERE key=?',(f"drift:{c['id']}",))
            diff=activity.drift_diff(c,p,json.loads(snapshot['value']) if snapshot else None,names,date) if c['issue'] in DRIFT_ISSUES else []
            items.append({'kind':'issue','customer_id':c['id'],'email':c['email'],'owner':c['owner'],'since':c['telemetry_at'] or c['created'],
                          'text':'issue_'+c['issue'] if known('issue_'+c['issue']) else c['issue'],'issue':c['issue'],'diff':diff,
                          'tone':'warning' if c['issue']=='suspension_pending' else 'danger'})
        return items

    @app.get('/attention')
    def attention(request:Request):
        require(request,True)
        requests=db.all("SELECT r.*,u.username FROM requests r JOIN users u ON u.id=r.user_id WHERE r.status='pending' ORDER BY r.id")
        for r in requests:
            r['plan']=current_plan(r['user_id'])
        return render(request,'attention.html',items=attention_items(),requests=requests)

    @app.get('/operations')
    def operations(request:Request):
        require(request,True)
        return redirect('/attention')

    @app.post('/operations/{oid}/reconcile')
    def reconcile(request:Request,oid:str):
        user=require(request,True)
        service.reconcile(oid)
        with db.transaction() as conn:
            audit(conn,user['id'],'reconcile',oid)
        status=(db.one('SELECT status FROM operations WHERE id=?',(oid,)) or {}).get('status')
        return redirect(back(request,'/attention'),'marked_done' if status=='committed' else 'submitted')

    @app.post('/operations/{oid}/repair')
    def repair(request:Request,oid:str):
        service.repair(require(request,True),oid)
        return redirect(back(request,'/attention'),'submitted')

    @app.post('/operations/{oid}/done')
    def mark_done(request:Request,oid:str):
        user=require(request,True)
        service.reconcile(oid,force=True)
        with db.transaction() as conn:
            audit(conn,user['id'],'reconcile',oid,'forced')
        status=(db.one('SELECT status FROM operations WHERE id=?',(oid,)) or {}).get('status')
        if status!='committed':
            raise RuleError('remote_missing',409)
        return redirect(back(request,'/attention'),'marked_done')

    @app.post('/operations/{oid}/retry')
    async def retry(request:Request,oid:str):
        actor=require(request)
        form=await request.form()
        operation=service.retry_unsent(actor,oid,str(form.get('operation_id','')))
        await run_in_threadpool(service.execute,operation['id'])
        return finish(operation['id'],operation['customer_id'],'customer_created','customer_activation_pending')

    return PathPrefix(app,config.base_path)
