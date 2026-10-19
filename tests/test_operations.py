import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import tarfile
from types import SimpleNamespace

import pytest
from reseller.db import Database
from reseller.config import Config

spec=importlib.util.spec_from_file_location('ops',Path(__file__).parents[1]/'deploy/ops.py')
ops=importlib.util.module_from_spec(spec);spec.loader.exec_module(ops)


@pytest.fixture
def installation(tmp_path,monkeypatch):
    for key in ('CODE','CONFIG','DATA','BACKUPS'):
        monkeypatch.setattr(ops,key,tmp_path/key.lower())
    monkeypatch.setattr(ops,'UNIT',tmp_path/'xui-reseller.service')
    monkeypatch.setattr(ops,'OWNED',(ops.CODE,ops.CONFIG,ops.DATA,ops.BACKUPS))
    monkeypatch.setattr(ops,'require_root',lambda:None)
    commands=[]
    def run(args,check=True,**kwargs):
        commands.append([str(a) for a in args])
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr(ops,'run',run)
    for path in ops.OWNED:ops.mark(path,0o700)
    database=Database(ops.DATA/'app.db');database.migrate()
    config={'APP_DB':str(ops.DATA/'app.db'),'APP_ORIGIN':'https://example.invalid','APP_PORT':'8095','XUI_TOKEN':'test-secret'}
    (ops.CONFIG/'app.env').write_text(''.join(k+'='+json.dumps(v)+'\n' for k,v in config.items()))
    monkeypatch.setattr(ops,'app',lambda *args,**kwargs:None)
    monkeypatch.setattr(ops,'health',lambda port:None)
    return database,commands


def test_online_backup_and_verified_restore(installation,tmp_path):
    db,commands=installation
    with db.transaction() as conn:conn.execute("INSERT INTO state VALUES('important','preserved')")
    archive=ops.backup()
    target=tmp_path/'restore';target.mkdir()
    ops.extract_backup(archive,target)
    with sqlite3.connect(target/'app.db') as conn:
        assert conn.execute("SELECT value FROM state WHERE key='important'").fetchone()[0]=='preserved'
    assert sorted(tarfile.open(archive).getnames())==['app.db','app.env','manifest.json']
    assert not any('x-ui.service' in command for command in commands)


def test_restore_locks_sales_and_invalidates_sessions(installation,monkeypatch):
    db,commands=installation
    archive=ops.backup()
    monkeypatch.setattr('builtins.input',lambda _: 'RESTORE')
    ops.restore(SimpleNamespace(archive=str(archive)))
    assert db.one("SELECT value FROM state WHERE key='write_lock'")['value']=='restored'
    assert ['systemctl','stop','xui-reseller.service'] in commands
    assert not any('x-ui.service' in command for command in commands)


def test_archive_traversal_and_symlink_rejected(installation,tmp_path):
    archive=tmp_path/'bad.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        member=tarfile.TarInfo('../app.db');member.size=3
        tar.addfile(member,io.BytesIO(b'bad'))
    with pytest.raises(RuntimeError,match='Invalid backup'):
        ops.extract_backup(archive,tmp_path)
    assert not (tmp_path.parent/'app.db').exists()


def test_uninstall_preserves_data(installation):
    db,commands=installation
    ops.uninstall(SimpleNamespace(purge_data=False))
    assert not ops.CODE.exists()
    assert Path(db.path).exists() and (ops.CONFIG/'app.env').exists() and ops.BACKUPS.exists()
    assert not any('x-ui.service' in command for command in commands)


def test_purge_requires_exact_confirmation(installation,monkeypatch):
    db,commands=installation
    monkeypatch.setattr('builtins.input',lambda _:'yes')
    with pytest.raises(RuntimeError,match='Cancelled'):
        ops.uninstall(SimpleNamespace(purge_data=True))
    assert Path(db.path).exists() and commands==[]


def test_purge_only_owned_paths(installation,monkeypatch,tmp_path):
    sentinel=tmp_path/'original-xui.db';sentinel.write_bytes(b'do not touch')
    monkeypatch.setattr('builtins.input',lambda _:'PURGE 3XUI-RESELLER')
    ops.uninstall(SimpleNamespace(purge_data=True))
    assert all(not p.exists() for p in ops.OWNED)
    assert sentinel.read_bytes()==b'do not touch'


def test_unrecognized_directory_refused(installation):
    (ops.DATA/'.relay-owned').unlink()
    with pytest.raises(RuntimeError,match='unrecognized'):
        ops.safe_paths()


def test_port_conflict_detection():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));sock.listen()
        with pytest.raises(RuntimeError,match='occupied'):
            ops.port_free(sock.getsockname()[1])


def test_dry_run_does_not_install(installation,monkeypatch):
    _,commands=installation
    checked=[]
    monkeypatch.setattr(ops,'preflight',lambda args:checked.append(True))
    ops.install(SimpleNamespace(dry_run=True))
    assert checked==[True] and commands==[]


def test_fresh_install_and_rerun_mocked(tmp_path,monkeypatch,installation):
    _,commands=installation
    # An interrupted fresh setup with protected configuration already present.
    monkeypatch.setattr(ops,'preflight',lambda args:ops.safe_paths())
    args=SimpleNamespace(dry_run=False,domain=None)
    ops.install(args)
    before=list(commands)
    assert (ops.CODE/'.installed').exists()
    assert ['systemctl','enable','--now','xui-reseller.service'] in commands
    ops.install(args)
    assert commands==before
    assert not any('x-ui.service' in command for command in commands)


def test_package_setup_disables_automatic_service_restarts(installation,monkeypatch):
    original_run=ops.run
    package_envs=[]
    def checked_run(args,**kwargs):
        if str(args[0])=='apt-get':
            package_envs.append(kwargs.get('env',{}))
        return original_run(args,**kwargs)
    monkeypatch.setattr(ops,'run',checked_run)
    monkeypatch.setattr(ops,'preflight',lambda args:ops.safe_paths())
    monkeypatch.setenv('NEEDRESTART_MODE','a')
    ops.install(SimpleNamespace(dry_run=False,domain=None))
    assert len(package_envs)==2
    assert all(env['NEEDRESTART_MODE']=='l' and env['NEEDRESTART_SUSPEND']=='1'
               for env in package_envs)


def make_release(tmp_path):
    import hashlib
    archive=tmp_path/'release.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name in ['reseller','deploy','docs','requirements.txt','requirements.lock','requirements-dev.txt','README.md','.env.example','install.sh','update.sh','backup.sh','restore.sh','status.sh','uninstall.sh']:
            root=ops.ROOT/name
            for p in root.rglob('*') if root.is_dir() else [root]:
                if p.is_file() and '__pycache__' not in p.parts:
                    tar.add(p,arcname=p.relative_to(ops.ROOT).as_posix())
    return archive,hashlib.sha256(archive.read_bytes()).hexdigest()


def test_update_integrity_gate_before_stop(installation,tmp_path):
    _,commands=installation
    archive,hash_value=make_release(tmp_path)
    with pytest.raises(RuntimeError,match='SHA-256'):
        ops.update(SimpleNamespace(archive=str(archive),sha256='0'*64))
    assert commands==[]


def test_update_and_failure_rollback_mocked(installation,tmp_path,monkeypatch):
    db,commands=installation
    ops.copy_source(ops.ROOT,ops.CODE)
    archive,hash_value=make_release(tmp_path)
    args=SimpleNamespace(archive=str(archive),sha256=hash_value)
    ops.update(args)
    assert ['systemctl','start','xui-reseller.service'] in commands
    (ops.CODE/'README.md').write_text('Previous source sentinel')
    monkeypatch.setattr(ops,'health',lambda _:ops.fail('simulated health failure'))
    with pytest.raises(RuntimeError,match='simulated'):
        ops.update(args)
    assert (ops.CODE/'README.md').read_text()=='Previous source sentinel'
    assert db.one("SELECT value FROM state WHERE key='write_lock'")['value']=='restored'
    assert not any('x-ui.service' in command for command in commands)


def test_install_failure_preserves_data(installation,monkeypatch):
    db,commands=installation
    monkeypatch.setattr(ops,'preflight',lambda _:None)
    monkeypatch.setattr(ops,'health',lambda _:ops.fail('simulated health failure'))
    with pytest.raises(RuntimeError):ops.install(SimpleNamespace(dry_run=False,domain=None))
    assert Path(db.path).exists() and (ops.CONFIG/'app.env').exists()
    assert not (ops.CODE/'.installed').exists()


def test_foreign_database_refused(tmp_path):
    path=tmp_path/'app.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE xui_users(id int)')
    with pytest.raises(ValueError,match='not owned'):Database(path).migrate()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('xui_users',)]


def test_config_rejects_db_collision(tmp_path):
    path=str(tmp_path/'app.db')
    with pytest.raises(ValueError,match='conflicts'):
        Config(path,'https://example.invalid',xui_database_paths=path).validate()


def test_public_http_config_requires_remote_origin_and_valid_path(tmp_path):
    path=str(tmp_path/'app.db')
    master=dict(xui_database_paths='/etc/x-ui/x-ui.db',xui_url='https://panel.invalid/',xui_token='t')
    Config(path,'http://203.0.113.10:8095',production=False,public_http=True,base_path='/relay-secret',**master).validate()
    # Public HTTP keeps the production configuration checks.
    with pytest.raises(ValueError,match='XUI_DATABASE_PATHS'):
        Config(path,'http://203.0.113.10:8095',production=False,public_http=True,base_path='/relay-secret').validate()
    with pytest.raises(ValueError,match='master'):
        Config(path,'http://203.0.113.10:8095',production=False,public_http=True,base_path='/relay-secret',xui_database_paths='/etc/x-ui/x-ui.db').validate()
    with pytest.raises(ValueError):
        Config(path,'http://localhost:8095',production=False,public_http=True,base_path='/relay-secret').validate()
    with pytest.raises(ValueError):
        Config(path,'http://203.0.113.10:8095',production=False,public_http=True,base_path='/../bad').validate()


def test_shell_syntax():
    candidates=[shutil.which('bash'),r'C:\Program Files\Git\bin\bash.exe']
    bash=next((x for x in candidates if x and Path(x).exists()),None)
    if not bash:pytest.skip('Bash unavailable')
    for path in ops.ROOT.glob('*.sh'):
        subprocess.run([bash,'-n',str(path)],check=True)


def test_original_service_never_mutated_static():
    source=Path(ops.__file__).read_text()
    assert "SERVICE='xui-reseller.service'" in source
    assert "['systemctl','restart','x-ui.service']" not in source
    assert 'XUI_DB_DSN' not in source and 'XUI_DB_TYPE' not in source
