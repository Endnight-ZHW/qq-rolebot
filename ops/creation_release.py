"""Explicit, post-approval renderer activation and selective configuration rollback.

Run on the Linux host as root, with the chat app stopped. No chat data is restored.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time

TOOLS=('creation_create','creation_status','creation_cancel')
INSTALL=Path('/opt/qq-rolebot-render')
UNIT=Path('/etc/systemd/system/qq-rolebot-render.service')
ENV=Path('/etc/qq-rolebot-render.env')


def get(db,kind,id):
    row=db.execute('SELECT data FROM objects WHERE kind=? AND id=?',(kind,id)).fetchone()
    return json.loads(row[0]) if row else None


def put(db,kind,id,value):
    if value is None:db.execute('DELETE FROM objects WHERE kind=? AND id=?',(kind,id))
    else:db.execute('INSERT OR REPLACE INTO objects VALUES(?,?,?,?)',(kind,id,json.dumps(value,ensure_ascii=False),time.time()))


def snapshot(db,scopes):
    result={'creation_config':get(db,'creation_config','global'),'groups':{}}
    for scope in scopes:
        group=get(db,'group',scope)
        if not group or not group.get('enabled'):raise ValueError('Group missing or disabled: '+scope)
        result['groups'][scope]={name:group.get('tools',{}).get(name) for name in TOOLS}
    return result


def configure(db,state,restore=False):
    with db:
        if restore:put(db,'creation_config','global',state['creation_config'])
        else:
            cfg=get(db,'creation_config','global') or {}
            cfg.update(enabled=True,revision=cfg.get('revision',0)+1);put(db,'creation_config','global',cfg)
        for scope,previous in state['groups'].items():
            group=get(db,'group',scope)
            if not group:continue
            tools=group.setdefault('tools',{})
            for name in TOOLS:
                if restore:
                    if previous[name] is None:tools.pop(name,None)
                    else:tools[name]=previous[name]
                else:
                    tools[name]={**tools.get(name,{}),'enabled':True,'user_enabled':True,
                        'autonomous_enabled':name=='creation_create','request_access':{'member':'all','admin':'all'}}
            group['revision']=group.get('revision',0)+1;put(db,'group',scope,group)


def command(*args,check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True)


def stopped():
    if os.geteuid()!=0:raise ValueError('Run as root')
    state=command('docker','inspect','qq-rolebot','--format','{{.State.Running}}').stdout.strip()
    if state=='true':raise ValueError('Stop the app before changing feature configuration')
    if INSTALL.is_symlink() or INSTALL.resolve()!=INSTALL:raise ValueError('Unexpected install path')


def enable(root,backup,image,scopes):
    stopped()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):raise ValueError('Use the tested immutable renderer image ID')
    command('docker','image','inspect',image)
    backup.mkdir(parents=True,exist_ok=False,mode=0o700)
    with sqlite3.connect(root/'data/app.sqlite') as db:state=snapshot(db,scopes)
    state['service_enabled']=command('systemctl','is-enabled','qq-rolebot-render',check=False).returncode==0
    state['service_active']=command('systemctl','is-active','qq-rolebot-render',check=False).returncode==0
    state['renderer_image']=image
    (backup/'creation-config.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf8')
    with tarfile.open(backup/'renderer-install.tar.gz','w:gz') as archive:
        for path,name in ((INSTALL,'install'),(UNIT,'unit'),(ENV,'env')):
            if path.exists():archive.add(path,arcname=name)
    for path in backup.iterdir():path.chmod(0o600)
    command('systemctl','stop','qq-rolebot-render',check=False)
    for relative in ('rendering/broker.py','rendering/seccomp.json','backend/rolebot/creation_contract.py'):
        target=INSTALL/relative;target.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
        shutil.copyfile(root/relative,target);target.chmod(0o644);os.chown(target,0,0)
    shutil.copyfile(root/'rendering/qq-rolebot-render.service',UNIT);UNIT.chmod(0o644)
    ENV.write_text('RENDER_IMAGE='+image+'\n');ENV.chmod(0o600)
    command('systemctl','daemon-reload');command('systemctl','enable','--now','qq-rolebot-render')
    with sqlite3.connect(root/'data/app.sqlite') as db:configure(db,state)
    return state


def restore(root,backup):
    stopped();state=json.loads((backup/'creation-config.json').read_text('utf8'))
    command('systemctl','disable','--now','qq-rolebot-render',check=False)
    # Never restore the database file: touch only this feature's keys in place.
    with sqlite3.connect(root/'data/app.sqlite') as db:
        configure(db,state,restore=True)
        for (id,data) in db.execute("SELECT id,data FROM objects WHERE kind='creation_job'").fetchall():
            job=json.loads(data)
            if job['status'] in ('queued','generating','rendering','checking','ready','sending'):
                job.update(status='unknown' if job['status']=='sending' else 'interrupted',error='制作功能已回退，未自动重试')
                put(db,'creation_job',id,job)
    if INSTALL.exists():shutil.rmtree(INSTALL)
    UNIT.unlink(missing_ok=True);ENV.unlink(missing_ok=True)
    # This archive contains operator-owned source, not generated job files.
    with tarfile.open(backup/'renderer-install.tar.gz') as archive:
        for entry in archive:
            parts=Path(entry.name).parts
            if not parts or parts[0] not in ('install','unit','env') or '..' in parts or entry.issym() or entry.islnk():
                raise ValueError('Unexpected backup member')
            target=INSTALL.joinpath(*parts[1:]) if parts[0]=='install' else UNIT if parts[0]=='unit' else ENV
            if entry.isdir():target.mkdir(parents=True,exist_ok=True,mode=0o755)
            elif entry.isfile():
                target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('wb') as file:shutil.copyfileobj(archive.extractfile(entry),file)
                target.chmod(0o600 if target==ENV else 0o644)
            else:raise ValueError('Unexpected backup member type')
    command('systemctl','daemon-reload')
    if state['service_enabled']:command('systemctl','enable','qq-rolebot-render')
    if state['service_active']:command('systemctl','start','qq-rolebot-render')
    return {'restored_feature_config':True,'chat_database_restored':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('enable','restore'))
    parser.add_argument('--root',type=Path,default=Path('/opt/qq-rolebot'))
    parser.add_argument('--backup',type=Path,required=True)
    parser.add_argument('--image');parser.add_argument('--scopes',nargs='+',default=[])
    args=parser.parse_args()
    if args.action=='enable' and (not args.image or len(args.scopes)!=3):parser.error('Activation requires the tested image and three explicit scopes')
    print(json.dumps(enable(args.root,args.backup,args.image,args.scopes) if args.action=='enable' else restore(args.root,args.backup),ensure_ascii=False))
