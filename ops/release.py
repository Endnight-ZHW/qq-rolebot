"""Build, test, deploy and roll back one release without restoring chat data.

Run from the workspace: python ops/release.py stage|deploy|rollback|status.
The SSH password is read from QQROLEBOT_SSH_PASSWORD or an interactive prompt.
"""
import argparse
import getpass
import hashlib
import json
import os
import shlex
from datetime import datetime
from pathlib import Path

from remote import ROOT, connect, run, upload, managed_files

WORKSPACE = Path(__file__).resolve().parents[1]
STATE = WORKSPACE/'artifacts'/'release.json'


def retired_sources(previous, current_files):
    """Only retire unchanged files recorded by a successful deployment."""
    prefixes = ('backend/', 'tests/', 'ops/', 'docs/', 'rendering/', 'frontend/src/', 'frontend/tests/')
    return [(name, sha) for name, sha in previous.get('file_hashes', {}).items()
            if name.startswith(prefixes) and name not in current_files]


def static_cleanup_script(root, retained):
    """Keep current and previous frontend chunks; never follow links or touch data."""
    return '''import pathlib,re,json
root=pathlib.Path(%r).resolve();folder=root/'frontend/dist/assets'
assert folder.resolve().is_relative_to(root) and not folder.is_symlink()
retained=%r
assert all(p.startswith('frontend/dist/') and '..' not in pathlib.PurePosixPath(p).parts for p in retained)
removed=[]
if folder.is_dir():
 for path in folder.iterdir():
  if path.is_file() and not path.is_symlink() and re.fullmatch(r'[A-Za-z0-9_-]+-[A-Za-z0-9_-]{8}\\.(?:js|css)',path.name):
   name=path.relative_to(root).as_posix()
   if name not in retained:
    assert path.resolve().is_relative_to(folder.resolve())
    path.unlink();removed.append(name)
print(json.dumps({'removed':removed}))
''' % (str(root), sorted(retained))


def checked(client, password, command, timeout=120):
    result = run(client, command, password=password, timeout=timeout)
    if result['exit_code']:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result['stdout']


def stage(client, password, retire=()):
    if not (WORKSPACE/'frontend/dist/index.html').is_file():
        raise ValueError('Build the frontend first: npm --prefix frontend run build')
    # Both files configure a live ingress. A routine stage must not overwrite
    # mounted gateway settings before an explicitly reviewed ingress cutover.
    ingress = ('compose.yaml', 'ops/Caddyfile')
    probe = 'import pathlib,hashlib,json;root=pathlib.Path('+repr(ROOT)+');print(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() if (root/n).is_file() else None for n in '+repr(ingress)+'}))'
    remote_hashes = json.loads(checked(client, password, 'python3 -c '+shlex.quote(probe)))
    changed = [n for n in ingress if remote_hashes.get(n) != hashlib.sha256((WORKSPACE/n).read_bytes()).hexdigest()]
    if changed:
        raise ValueError('Ingress configuration changed: '+', '.join(changed)+'. Use an isolated candidate directory and the reviewed panel-access cutover; no production files were uploaded.')
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    state = {'backup': ROOT+'/upgrade-backups/release-'+stamp, 'candidate':'qq-rolebot:release-'+stamp,
             'previous':'qq-rolebot:before-'+stamp}
    active_image = checked(client, password, "docker inspect qq-rolebot --format '{{.Image}}'").strip()
    previous = json.loads(STATE.read_text(encoding='utf8')) if STATE.exists() else {}
    current = {p.relative_to(WORKSPACE).as_posix() for p in managed_files(WORKSPACE)}
    retired = dict(retire)
    if previous.get('status') == 'deployed' and previous.get('deployed_image') == active_image:
        state['previous_static'] = [p for p in previous.get('files', []) if p.startswith('frontend/dist/')]
        for name, sha in retired_sources(previous, current):
            if name in retired and retired[name] != sha:
                raise ValueError('Conflicting retirement hashes: '+name)
            retired[name] = sha
    retire = sorted(retired.items())
    script = '''import pathlib,sqlite3,shutil,tarfile,json
root=pathlib.Path(%r);dest=pathlib.Path(%r);dest.mkdir(parents=True,mode=0o700)
source=sqlite3.connect('file:'+str(root/'data/app.sqlite')+'?mode=ro',uri=True);target=sqlite3.connect(str(dest/'app.sqlite'));source.backup(target);target.close();source.close()
with tarfile.open(dest/'source.tar.gz','w:gz') as t:
 for name in ['backend','frontend/dist','frontend/src','frontend/tests','frontend/package.json','frontend/package-lock.json','ops','tests','docs','rendering','Dockerfile','compose.yaml','README.md']:
  if (root/name).exists():t.add(root/name,arcname=name)
shutil.copy2(root/'data/secrets.json',dest/'secrets.json')
for p in dest.iterdir():p.chmod(0o600)
''' % (ROOT, state['backup'])
    checked(client, password, 'python3 -c '+shlex.quote(script))
    checked(client, password, 'docker image tag '+active_image+' '+state['previous'])
    if retire:
        # Explicit reviewed filenames only. The backup above captures their current
        # server contents; differing edits stop cleanup instead of being discarded.
        script = '''import pathlib,hashlib,json
root=pathlib.Path(%r).resolve();files=%r
for relative,expected in files:
 path=(root/relative).resolve()
 assert path.is_relative_to(root) and pathlib.PurePosixPath(relative).parts[0] in ('backend','frontend','ops','tests','docs','rendering')
 if path.exists():
  assert path.is_file() and not path.is_symlink(),relative
  assert hashlib.sha256(path.read_bytes()).hexdigest()==expected,'Server file changed: '+relative
for relative,expected in files:
 (root/relative).unlink(missing_ok=True)
print(json.dumps({'retired_source_files':len(files)}))
''' % (ROOT, list(retire))
        print(checked(client,password,'python3 -c '+shlex.quote(script)))
        state['retired']=[x[0] for x in retire]
    result = upload(client, WORKSPACE, include_static=False)
    if result['exit_code']:raise RuntimeError(result)
    checked(client, password, 'python3 '+shlex.quote(ROOT+'/ops/permission_backup.py')+' backup --database '+
            shlex.quote(state['backup']+'/app.sqlite')+' --snapshot '+shlex.quote(state['backup']+'/permissions.json'))
    checked(client, password, 'cd '+ROOT+' && docker build -t '+state['candidate']+' .', 300)
    state['image'] = checked(client, password, 'docker image inspect '+state['candidate']+" --format '{{.Id}}'").strip()
    expected = {p.relative_to(WORKSPACE/'backend').as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (WORKSPACE/'backend/rolebot').rglob('*.py')}
    smoke = ('import hashlib,pathlib,os; import rolebot.api; assert os.getuid()==1001; expected='+repr(expected)+
             '; assert all(hashlib.sha256((pathlib.Path("/app")/p).read_bytes()).hexdigest()==h for p,h in expected.items()); print("nonroot imports and source hashes verified")')
    checked(client, password, 'docker run --rm --network none --user 1001:1001 '+state['candidate']+' python -c '+shlex.quote(smoke))
    STATE.parent.mkdir(exist_ok=True); STATE.write_text(json.dumps(state), encoding='utf-8')
    # Read-only mounts, isolated data, and bounded resources. No QQ/model calls.
    command = ('docker run --rm --user 0:0 --memory 512m --cpus 1 '
               f'-v {ROOT}/backend:/backend:ro -v {ROOT}/tests:/tests:ro -v {ROOT}/ops:/ops:ro '
               f'-v {ROOT}/pytest.ini:/app/pytest.ini:ro -v {ROOT}/requirements-dev.txt:/dev-requirements.txt:ro '
               '-e PYTHONPATH=/app:/tests:/ops -e DATA_DIR=/tmp/testdata '+state['candidate']+
               " sh -c 'pip install --no-cache-dir -r /dev-requirements.txt -q && python -m pytest /tests -c /app/pytest.ini -o cache_dir=/tmp/pytest-cache -q'")
    output = checked(client, password, command, 600)
    (STATE.parent/'tests.txt').write_text(output,encoding='utf-8')
    state['tested'] = state['image']; STATE.write_text(json.dumps(state),encoding='utf-8')
    print(output)
    return state


def rollback(client, password, state):
    # Releases with explicit configuration changes own their selective rollback.
    # Keep that recorded entry point instead of reintroducing retired migrations.
    if state.get('rollback_command'):
        command = state['rollback_command'].removeprefix('sudo ')
        checked(client, password, command)
        state['status']='rolled_back';STATE.write_text(json.dumps(state),encoding='utf-8')
        return
    checked(client, password, 'cd '+ROOT+' && docker compose stop -t 20 app')
    checked(client, password, 'docker image tag '+state['previous']+' qq-rolebot:1.0.0')
    # Restore only static assets; runtime data, certificates and keys stay intact.
    script = '''import tarfile,pathlib
root=pathlib.Path(%r);archive=pathlib.Path(%r)/'source.tar.gz'
with tarfile.open(archive) as t:
 members=[m for m in t.getmembers() if m.name.startswith('frontend/dist/')]
 assert all(not m.issym() and not m.islnk() and (root/m.name).resolve().is_relative_to(root.resolve()) for m in members)
 t.extractall(root,members=members)
''' % (ROOT,state['backup'])
    checked(client,password,'python3 -c '+shlex.quote(script))
    checked(client,password,'cd '+ROOT+' && docker compose up -d --no-deps --wait --wait-timeout 90 app')
    state['status']='rolled_back';STATE.write_text(json.dumps(state),encoding='utf-8')


def deploy(client, password, state):
    actual=checked(client,password,'docker image inspect '+state['candidate']+" --format '{{.Id}}'").strip()
    if state.get('tested') != actual:raise ValueError('The exact candidate image must pass stage tests first')
    try:
        checked(client,password,'cd '+ROOT+' && docker compose stop -t 20 app')
        checked(client,password,'docker image tag '+state['candidate']+' qq-rolebot:1.0.0')
        checked(client,password,'cd '+ROOT+' && docker compose up -d --no-deps --wait --wait-timeout 90 app')
        result=upload(client,WORKSPACE,include_static=True)
        if result['exit_code']:raise RuntimeError(result)
        if state.get('previous_static'):
            retained = set(state['previous_static']) | {
                p.relative_to(WORKSPACE).as_posix() for p in (WORKSPACE/'frontend/dist').rglob('*') if p.is_file()}
            cleanup = checked(client, password, 'python3 -c '+shlex.quote(static_cleanup_script(ROOT, retained)))
            state['stale_assets_removed'] = json.loads(cleanup)['removed']
    except Exception:
        rollback(client,password,state)
        raise
    state.update(status='deployed',deployed_image=actual)
    state['file_hashes'] = {p.relative_to(WORKSPACE).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in managed_files(WORKSPACE)}
    state['files'] = list(state['file_hashes'])
    STATE.write_text(json.dumps(state),encoding='utf-8')
    return state


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['stage','deploy','rollback','status'])
    parser.add_argument('--retire-from',type=Path,help='Reviewed JSON array of [relative path, previous SHA-256] pairs')
    args=parser.parse_args();password=os.environ.get('QQROLEBOT_SSH_PASSWORD') or getpass.getpass('SSH password: ')
    client=connect(password)
    try:
        if args.action=='stage':result=stage(client,password,json.loads(args.retire_from.read_text()) if args.retire_from else ())
        elif args.action=='status':result=checked(client,password,"docker inspect qq-rolebot --format '{{.Image}} {{.State.Health.Status}}'")
        else:
            state=json.loads(STATE.read_text(encoding='utf-8'))
            result=deploy(client,password,state) if args.action=='deploy' else rollback(client,password,state)
        print(json.dumps(result,ensure_ascii=False))
    finally:client.close()
