#!/usr/bin/env python3
"""Root-only Linux lifecycle tool. Fixed application paths; never operates on x-ui service/data.

No shell execution, environment eval, shared-package removal or recursive archive extraction.
"""
import argparse
import contextlib
import datetime
import getpass
import hashlib
import json
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

CODE=Path('/opt/3xui-reseller')
CONFIG=Path('/etc/3xui-reseller')
DATA=Path('/var/lib/3xui-reseller')
BACKUPS=Path('/var/backups/3xui-reseller')
UNIT=Path('/etc/systemd/system/xui-reseller.service')
SERVICE='xui-reseller.service'
APP_ID=0x58525253
ROOT=Path(__file__).resolve().parent.parent
OWNED=(CODE,CONFIG,DATA,BACKUPS)


def installed_version():
    try:
        return (CODE/'VERSION').read_text().strip()
    except OSError:
        return 'unknown (before 1.0.0)'


def run(args,check=True,**kwargs):
    return subprocess.run([str(a) for a in args],check=check,text=True,**kwargs)


def fail(message):
    raise RuntimeError(message)


def require_root():
    if platform.system()!='Linux' or os.geteuid()!=0:
        fail('Run on a supported Linux VPS with sudo. No changes were made.')


def safe_paths():
    for path in (*OWNED,UNIT):
        if path.is_symlink() or path.resolve()!=path:
            fail('Refusing a symlink or redirected application path: '+str(path))
    for path in OWNED:
        if path.exists() and any(path.iterdir()) and not (path/'.relay-owned').is_file():
            fail('Existing unrecognized directory: '+str(path))
    if UNIT.exists() and 'Independent 3X-UI reseller console' not in UNIT.read_text():
        fail('Unrecognized service unit: '+str(UNIT))


def config_read():
    result={}
    for line in (CONFIG/'app.env').read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        key,_,value=line.partition('=')
        result[key]=json.loads(value) if value.startswith('"') else value
    if Path(result.get('APP_DB','')).resolve()!=DATA/'app.db':
        fail('Configured database is outside this installation')
    return result


LOOPBACK=('localhost','127.0.0.1','::1')


def is_this_machine(host):
    """True when host resolves to an address of this VPS itself (a public or private
    interface address, not loopback). Binding only succeeds for local addresses."""
    try:
        infos=socket.getaddrinfo(host,None)
    except OSError:
        return False
    for family,_,_,_,address in infos:
        try:
            with socket.socket(family,socket.SOCK_DGRAM) as probe:
                probe.bind((address[0],0))
            return True
        except OSError:
            continue
    return False


def normalize_master_url(url):
    """Return (url, link_host) for the 3X-UI address the operator typed.

    When 3X-UI runs on this same VPS over plain HTTP (for example
    http://203.0.113.5:2052/x-ui/), Relay talks to it on 127.0.0.1 instead:
    the traffic never leaves the server and plain HTTP stays safe. The public
    host is kept as XUI_LINK_HOST so generated links still show it."""
    from urllib.parse import urlsplit,urlunsplit
    url=url.strip()
    if url and '://' not in url:
        url='http://'+url
    parsed=urlsplit(url)
    path=(parsed.path or '/').rstrip('/')+'/'
    if parsed.scheme=='http' and parsed.hostname and parsed.hostname not in LOOPBACK and is_this_machine(parsed.hostname):
        port=f':{parsed.port}' if parsed.port else ''
        return urlunsplit(('http','127.0.0.1'+port,path,parsed.query,parsed.fragment)),parsed.hostname
    return urlunsplit((parsed.scheme,parsed.netloc,path,parsed.query,parsed.fragment)),''


def port_free(port,public=False):
    if not 1024<=port<=65535:
        fail('Choose an unprivileged port from 1024 to 65535')
    with socket.socket() as sock:
        try:
            sock.bind(('0.0.0.0' if public else '127.0.0.1',port))
        except OSError:
            fail('Port '+str(port)+' is occupied. Choose --port with an unused port; no service was stopped.')


def preflight(args):
    require_root()
    safe_paths()
    release={}
    for line in Path('/etc/os-release').read_text().splitlines():
        key,_,value=line.partition('=');release[key]=value.strip('"')
    if (release.get('ID'),release.get('VERSION_ID')) not in {('debian','12'),('debian','13'),('ubuntu','22.04'),('ubuntu','24.04'),('ubuntu','26.04')}:
        fail('Supported platforms: Debian 12/13 and Ubuntu 22.04/24.04/26.04 with systemd')
    if platform.machine() not in ('x86_64','aarch64') or sys.version_info<(3,10):
        fail('Requires x86_64/aarch64 and Python 3.10+')
    if not Path('/run/systemd/system').exists():
        fail('A running systemd host is required')
    if shutil.disk_usage('/var').free<1024**3:
        fail('At least 1 GiB free disk space is required')
    print('Preflight:',release.get('PRETTY_NAME'),platform.machine(),sys.version.split()[0])
    print('Isolated paths:',*(str(p) for p in OWNED))
    for name in ('x-ui.service','nginx.service','apache2.service','caddy.service'):
        state=run(['systemctl','is-active',name],check=False,capture_output=True)
        print(name+': '+state.stdout.strip())
    # Inspect unit paths only. Do not dump environment variables or master credentials.
    units=run(['systemctl','list-unit-files','--no-legend','--no-pager'],capture_output=True).stdout
    discovered=[line.split()[0] for line in units.splitlines() if 'x-ui' in line.lower() or '3x-ui' in line.lower()]
    for unit in discovered:
        location=run(['systemctl','show',unit,'--property=FragmentPath','--value'],capture_output=True).stdout.strip()
        print('Detected panel service:',unit,'unit file:',location)
    print('Existing conventional panel paths:',', '.join(p for p in ('/usr/local/x-ui','/etc/x-ui','/var/lib/x-ui') if Path(p).exists()) or 'none; inspect custom/container installation')
    if shutil.which('ss'):
        print(run(['ss','-ltnp'],capture_output=True).stdout)
    if (CONFIG/'app.env').exists():
        config_read()
        print('Existing application configuration will be preserved.')
    else:
        port_free(args.port,getattr(args,'public_http',False))
    print('Preflight completed without modifying the system.')


def mark(path,mode):
    path.mkdir(parents=True,exist_ok=True,mode=mode)
    path.chmod(mode)
    (path/'.relay-owned').touch(mode=0o600)


def copy_source(source,destination):
    # Explicit allowlist excludes master reference checkout, local state and secrets.
    destination.mkdir(parents=True,exist_ok=True)
    for name in ('reseller','deploy','docs','VERSION','requirements.txt','requirements.lock','requirements-dev.txt','README.md','.env.example','install.sh','update.sh','backup.sh','restore.sh','status.sh','uninstall.sh'):
        src=source/name
        if not src.exists():
            if name=='VERSION':
                continue  # releases before 1.0.0 had no VERSION file
            fail('Source package is incomplete: '+name)
        if src.is_symlink() or src.is_dir() and any(p.is_symlink() for p in src.rglob('*')):
            fail('Source symlinks are not allowed')
        if source.resolve()==destination.resolve():
            continue
        if src.is_dir():
            shutil.copytree(src,destination/name,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        else:
            shutil.copy2(src,destination/name)
    (destination/'.relay-owned').touch(mode=0o600)


def app(command,*args,check=True):
    # Application schema creation and bootstrap run as its service user.
    return run(['runuser','-u','xui-reseller','--',CODE/'.venv/bin/python','-m','reseller.manage',command,'--env',CONFIG/'app.env',*args],cwd=CODE,check=check)


def health(port):
    base_path=config_read().get('APP_BASE_PATH','')
    for _ in range(20):
        try:
            with urllib.request.urlopen('http://127.0.0.1:'+str(port)+base_path+'/healthz',timeout=2) as response:
                if json.load(response).get('status')=='ok':
                    return
        except (OSError,ValueError):
            time.sleep(1)
    fail('Application health check failed. Inspect journalctl -u xui-reseller.service -n 60')


def install(args):
    public_http=getattr(args,'public_http',False)
    tunnel_http=getattr(args,'tunnel_http',False)
    if tunnel_http and public_http:
        fail('Choose either --tunnel-http or --public-http')
    preflight(args)
    if args.dry_run:
        return
    if (CONFIG/'app.env').exists() and (CODE/'.installed').exists():
        if args.domain:
            configure_proxy(args,config_read())
        print('Installation already exists; data, secrets and administrator unchanged. Use update.sh for upgrades.')
        return
    values=None
    if not (CONFIG/'app.env').exists():
        # Collect and validate configuration BEFORE modifying packages/directories.
        supplied=args.origin or input('Application URL: ').strip()
        from urllib.parse import urlsplit
        public=urlsplit(supplied)
        base_path=public.path.rstrip('/')
        origin=public.scheme+'://'+public.netloc
        if public.username or public.password or public.query or public.fragment or not public.hostname:
            fail('Application URL must not contain credentials, a query, or a fragment')
        if tunnel_http:
            if origin not in ('http://localhost:'+str(args.port),'http://127.0.0.1:'+str(args.port)) or base_path:
                fail('Tunnel HTTP mode requires a loopback origin with the chosen port')
        elif public_http:
            import ipaddress
            try:
                address=ipaddress.ip_address(public.hostname or '')
            except ValueError:
                fail('Public HTTP mode requires a literal VPS IP address')
            if public.scheme!='http' or address.is_loopback or public.port!=args.port or not base_path:
                fail('Use --origin http://VPS_IP:PORT/web-path/ matching --port')
            print('WARNING: public HTTP sends Relay passwords, sessions and customer data without transport encryption.')
        elif not origin.startswith('https://'):
            fail('Use an HTTPS origin or explicitly choose --tunnel-http for SSH-tunnel-only access')
        print('3X-UI panel address, including its web path, as you open it in the browser.')
        print('  Examples: http://127.0.0.1:2053/your-web-path/  or  https://panel.example.com:2053/your-web-path/')
        typed=input('3X-UI panel address: ').strip()
        url,link_host=normalize_master_url(typed)
        if link_host:
            print(f'3X-UI runs on this VPS: Relay will connect to it locally at {url}')
            print(f'(links shown to customers keep using {link_host}).')
        parsed=urlsplit(url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.hostname:
            fail('The 3X-UI address must not contain a username, password, query or fragment')
        if parsed.scheme!='https' and not(parsed.scheme=='http' and parsed.hostname in LOOPBACK):
            fail('3X-UI over plain HTTP only works when it runs on this same VPS (Relay then uses 127.0.0.1). '
                 'For a 3X-UI on another server, use its https:// address.')
        original=input('Original panel SQLite paths (comma separated), or external for PostgreSQL: ').strip()
        if not original:
            fail('Inspect the original panel database configuration and rerun')
        for value in original.split(','):
            if value!='external' and (Path(value).resolve()==DATA/'app.db' or not Path(value).is_absolute()):
                fail('Original database path conflicts or is not absolute')
        sub_url=input('Public subscription base URL, e.g. https://sub.example.com/i/ (blank to hide subscription links): ').strip()
        if sub_url and not sub_url.startswith('https://'):
            fail('Subscription URL must use HTTPS')
        token=getpass.getpass('Master admin API token (blank for username/password): ')
        username=input('Master username: ') if not token else ''
        password=getpass.getpass('Master password: ') if not token else ''
        if not token and not (username and password):
            fail('Master authentication is required')
        values={'APP_ENV':'public-http' if public_http else 'development' if tunnel_http else 'production','APP_ORIGIN':origin,'APP_BASE_PATH':base_path,'APP_DB':str(DATA/'app.db'),
                'APP_PORT':str(args.port),'APP_TIMEZONE':'Asia/Tehran','XUI_DATABASE_PATHS':original,
                'XUI_URL':url,'XUI_LINK_HOST':link_host,'XUI_SUB_URL':sub_url,'XUI_TOKEN':token,'XUI_USERNAME':username,'XUI_PASSWORD':password,'SYNC_SECONDS':'30'}
    # Ubuntu's apt hook may otherwise restart unrelated services automatically.
    package_env={**os.environ,'NEEDRESTART_MODE':'l','NEEDRESTART_SUSPEND':'1'}
    run(['apt-get','update'],env=package_env)
    run(['apt-get','install','-y','python3-venv','ca-certificates'],env=package_env)
    if run(['id','xui-reseller'],check=False,capture_output=True).returncode:
        run(['useradd','--system','--home-dir',DATA,'--shell','/usr/sbin/nologin','xui-reseller'])
    for path,mode in ((CODE,0o755),(CONFIG,0o750),(DATA,0o700),(BACKUPS,0o700)):
        mark(path,mode)
    run(['chown','xui-reseller:xui-reseller',DATA])
    run(['chown','root:xui-reseller',CONFIG])
    copy_source(ROOT,CODE)
    run(['python3','-m','venv',CODE/'.venv'])
    run([CODE/'.venv/bin/pip','install','-r',CODE/'requirements.txt','-c',CODE/'requirements.lock'])
    if values is not None:
        fd=os.open(CONFIG/'app.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o640)
        with os.fdopen(fd,'w') as stream:
            stream.write(''.join(k+'='+json.dumps(v)+'\n' for k,v in values.items()))
        run(['chown','root:xui-reseller',CONFIG/'app.env'])
    app('migrate')
    app('bootstrap')
    shutil.copy2(CODE/'deploy/xui-reseller.service',UNIT)
    run(['systemctl','daemon-reload'])
    run(['systemctl','enable','--now',SERVICE])
    config=config_read()
    health(int(config['APP_PORT']))
    (CODE/'.installed').touch(mode=0o600)
    if args.domain:
        configure_proxy(args,config)
    app('check',check=False)
    print('Installed Relay',installed_version()+'. Access:',config['APP_ORIGIN']+config.get('APP_BASE_PATH','')+'/')
    if config.get('APP_ENV')=='development':
        print('SSH tunnel: ssh -L '+config['APP_PORT']+':127.0.0.1:'+config['APP_PORT']+' YOUR_SSH_USER@YOUR_VPS')
    print('Configuration: /etc/3xui-reseller/app.env; service: xui-reseller.service. Original panel untouched.')


def backup():
    require_root();safe_paths()
    config=config_read()
    mark(BACKUPS,0o700)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    archive=BACKUPS/('relay-'+stamp+'.tar.gz')
    with tempfile.TemporaryDirectory(prefix='relay-backup-') as temp:
        temp=Path(temp)
        with contextlib.closing(sqlite3.connect('file:'+str(DATA/'app.db')+'?mode=ro',uri=True)) as source,contextlib.closing(sqlite3.connect(temp/'app.db')) as target:
            if source.execute('PRAGMA application_id').fetchone()[0]!=APP_ID:
                fail('Not an application database')
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                fail('Database backup integrity check failed')
        shutil.copy2(CONFIG/'app.env',temp/'app.env')
        manifest={'format':1,'schema':1,'files':{name:hashlib.sha256((temp/name).read_bytes()).hexdigest() for name in ('app.db','app.env')}}
        (temp/'manifest.json').write_text(json.dumps(manifest))
        fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as output,tarfile.open(fileobj=output,mode='w:gz') as tar:
            for name in ('app.db','app.env','manifest.json'):
                tar.add(temp/name,arcname=name)
    archive.chmod(0o600)
    print('Backup:',archive)
    return archive


def extract_backup(archive,destination):
    destination=Path(destination)
    expected={'app.db','app.env','manifest.json'}
    with tarfile.open(archive,'r:gz') as tar:
        members=tar.getmembers()
        if {m.name for m in members}!=expected or len(members)!=3 or any(not m.isfile() or m.size>4*1024**3 for m in members):
            fail('Invalid backup archive members')
        for member in members:
            with tar.extractfile(member) as source,open(destination/member.name,'wb') as out:
                shutil.copyfileobj(source,out)
    manifest=json.loads((destination/'manifest.json').read_text())
    if manifest.get('format')!=1 or manifest.get('schema')!=1:
        fail('Unsupported backup version')
    for name in ('app.db','app.env'):
        if hashlib.sha256((destination/name).read_bytes()).hexdigest()!=manifest['files'].get(name):
            fail('Backup checksum mismatch')
    with contextlib.closing(sqlite3.connect('file:'+str(destination/'app.db')+'?mode=ro',uri=True)) as conn:
        if conn.execute('PRAGMA application_id').fetchone()[0]!=APP_ID or conn.execute('PRAGMA user_version').fetchone()[0] not in (1,2,3,4,5) or conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            fail('Backup database validation failed')


def restore(args):
    require_root();safe_paths()
    if not args.archive:
        fail('--archive is required')
    with tempfile.TemporaryDirectory(prefix='relay-restore-') as temp:
        temp=Path(temp)
        extract_backup(args.archive,temp)
        # Validate restored DB path before any mutation of installation.
        lines=(temp/'app.env').read_text().splitlines()
        values={k:json.loads(v) if v.startswith('"') else v for k,sep,v in (line.partition('=') for line in lines if line and not line.startswith('#')) if sep}
        if Path(values.get('APP_DB','')).resolve()!=DATA/'app.db':
            fail('Backup configuration does not target this application database')
        print('Restore replaces ONLY:',DATA/'app.db',CONFIG/'app.env')
        if input('Type RESTORE to proceed: ')!='RESTORE':
            fail('Cancelled')
        rollback=backup()
        run(['systemctl','stop',SERVICE])
        try:
            replace_state(temp)
            app('migrate')
            run(['systemctl','start',SERVICE])
            current=config_read();health(int(current['APP_PORT']))
        except Exception:
            run(['systemctl','stop',SERVICE],check=False)
            with tempfile.TemporaryDirectory(prefix='relay-rollback-') as old:
                extract_backup(rollback,Path(old));replace_state(Path(old))
            run(['systemctl','start',SERVICE],check=False)
            raise
    print('Restore complete. Reconcile master state before allowing new sales; the independent master was not rolled back.')


def replace_state(source):
    for suffix in ('-wal','-shm'):
        (DATA/('app.db'+suffix)).unlink(missing_ok=True)
    shutil.copy2(source/'app.db',DATA/'app.db')
    shutil.copy2(source/'app.env',CONFIG/'app.env')
    (DATA/'app.db').chmod(0o600);(CONFIG/'app.env').chmod(0o640)
    run(['chown','xui-reseller:xui-reseller',DATA/'app.db'])
    run(['chown','root:xui-reseller',CONFIG/'app.env'])
    with contextlib.closing(sqlite3.connect(DATA/'app.db')) as conn:
        with conn:
            conn.execute('DELETE FROM sessions')
            conn.execute("INSERT INTO state VALUES('write_lock','restored') ON CONFLICT(key) DO UPDATE SET value='restored'")


def extract_release(archive,expected,destination):
    if not expected or len(expected)!=64 or hashlib.sha256(Path(archive).read_bytes()).hexdigest()!=expected.lower():
        fail('Release SHA-256 mismatch; obtain expected hash through a trusted channel')
    with tarfile.open(archive,'r:gz') as tar:
        for item in tar.getmembers():
            path=Path(destination)/item.name
            if not item.isfile() or not path.resolve().is_relative_to(Path(destination).resolve()) or item.size>64*1024**2:
                fail('Unsafe release archive')
            path.parent.mkdir(parents=True,exist_ok=True)
            with tar.extractfile(item) as src,open(path,'wb') as out:
                shutil.copyfileobj(src,out)
    if not (Path(destination)/'reseller/__init__.py').exists():
        fail('Release is missing application files')


def update(args):
    require_root();safe_paths();config_read()
    if not args.archive or not args.sha256:
        fail('Supply --archive and a trusted --sha256')
    with tempfile.TemporaryDirectory(prefix='relay-release-') as temp:
        source=Path(temp)/'source';source.mkdir()
        extract_release(args.archive,args.sha256,source)
        rollback=backup()
        old=Path(temp)/'previous'
        copy_source(CODE,old)
        run(['systemctl','stop',SERVICE])
        try:
            copy_source(source,CODE)
            run([CODE/'.venv/bin/pip','install','-r',CODE/'requirements.txt','-c',CODE/'requirements.lock'])
            app('migrate')
            run(['systemctl','start',SERVICE])
            current=config_read();health(int(current['APP_PORT']))
        except Exception:
            run(['systemctl','stop',SERVICE],check=False)
            copy_source(old,CODE)
            state=Path(temp)/'state';state.mkdir();extract_backup(rollback,state);replace_state(state)
            run([CODE/'.venv/bin/pip','install','-r',CODE/'requirements.txt','-c',CODE/'requirements.lock'],check=False)
            run(['systemctl','start',SERVICE],check=False)
            print('Upgrade failed. Previous source and database restored. Verify service health; backup:',rollback)
            raise
    print('Upgrade to Relay',installed_version(),'complete. Only xui-reseller.service was restarted.')


def configure_proxy(args,config):
    import re
    domain=args.domain
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?',domain) or '..' in domain:
        fail('Invalid dedicated domain')
    if config['APP_ORIGIN']!='https://'+domain:
        fail('Proxy domain must match APP_ORIGIN')
    socket.getaddrinfo(domain,443)
    if not shutil.which('nginx') or not args.tls_cert or not args.tls_key:
        fail('Automatic proxy setup requires existing Nginx and --tls-cert/--tls-key. No proxy changes made.')
    if not all(re.fullmatch(r'/[A-Za-z0-9_./-]+',p) and Path(p).is_file() for p in (args.tls_cert,args.tls_key)):
        fail('Invalid certificate paths')
    run(['openssl','x509','-in',args.tls_cert,'-noout','-checkhost',domain])
    run(['openssl','x509','-in',args.tls_cert,'-noout','-checkend','86400'])
    site=Path('/etc/nginx/conf.d/3xui-reseller.conf')
    content='server {\n listen 443 ssl;\n server_name '+domain+';\n ssl_certificate '+args.tls_cert+';\n ssl_certificate_key '+args.tls_key+';\n client_max_body_size 64k;\n location / { proxy_pass http://127.0.0.1:'+config['APP_PORT']+'; proxy_set_header Host $host; proxy_set_header X-Forwarded-For $remote_addr; proxy_set_header X-Forwarded-Proto https; proxy_read_timeout 120s; }\n}\n'
    if site.is_symlink():
        fail('Proxy file is a symlink; refusing changes')
    if site.exists():
        if site.read_text()==content:
            run(['nginx','-t'])
            print('Dedicated proxy configuration already matches; no reload needed.')
            return
        fail('Proxy file exists with different contents; refusing overwrite')
    current=run(['nginx','-T'],capture_output=True)
    if domain in current.stdout or domain in current.stderr:
        fail('Domain already appears in Nginx configuration; configure it manually')
    site.write_text(content)
    try:
        run(['nginx','-t'])
        run(['systemctl','reload','nginx.service'])
    except Exception:
        site.unlink(missing_ok=True)
        raise
    print('Dedicated Nginx site configured; existing sites and certificates unchanged.')


def uninstall(args):
    require_root();safe_paths()
    print('Removing application service and code:',UNIT,CODE)
    if args.purge_data:
        print('DESTRUCTIVE: also permanently remove:',CONFIG,DATA,BACKUPS)
        if input('Type PURGE 3XUI-RESELLER to confirm: ')!='PURGE 3XUI-RESELLER':
            fail('Cancelled')
    run(['systemctl','disable','--now',SERVICE],check=False)
    UNIT.unlink(missing_ok=True)
    run(['systemctl','daemon-reload'])
    # All paths have fixed literals and were resolved/ownership-checked before removal.
    if CODE.exists():
        shutil.rmtree(CODE)
    if args.purge_data:
        for path in (CONFIG,DATA,BACKUPS):
            if path.exists():
                shutil.rmtree(path)
    print('Removed application code/service. Shared packages, original panel and all proxy sites untouched.')
    if not args.purge_data:
        print('Application database, configuration and backups preserved.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['install','update','backup','restore','status','uninstall'])
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--port',type=int,default=8095)
    parser.add_argument('--origin')
    parser.add_argument('--tunnel-http',action='store_true')
    parser.add_argument('--public-http',action='store_true')
    parser.add_argument('--domain')
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--archive')
    parser.add_argument('--sha256')
    parser.add_argument('--purge-data',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='install':install(args)
        elif args.command=='backup':backup()
        elif args.command=='update':update(args)
        elif args.command=='restore':restore(args)
        elif args.command=='uninstall':uninstall(args)
        else:
            require_root();safe_paths()
            print('Relay version:',installed_version())
            run(['systemctl','status',SERVICE,'--no-pager'],check=False)
            config=config_read();health(int(config['APP_PORT']))
            app('check')
    except (RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as exc:
        print('Stopped safely:',str(exc),file=sys.stderr)
        print('Existing application data is preserved. Inspect /etc/3xui-reseller/app.env and journalctl -u xui-reseller.service. Correct the issue and rerun.',file=sys.stderr)
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
