"""Deployment transport. SSH credentials only arrive through environment or caller memory."""
import io
import os
import re
import shlex
import tarfile
from pathlib import Path

import paramiko

ROOT = os.environ.get('QQROLEBOT_REMOTE_ROOT', '/opt/qq-rolebot')
if not re.fullmatch(r'/[A-Za-z0-9_./-]+',ROOT) or '..' in ROOT.split('/') or len(ROOT.strip('/').split('/'))<2:
    raise ValueError('QQROLEBOT_REMOTE_ROOT must be an absolute dedicated directory without traversal or shell characters')


def connect(password=None):
    client = paramiko.SSHClient()
    client.load_host_keys(str(Path.home()/".ssh"/"known_hosts"))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    host = os.environ.get('QQROLEBOT_SSH_HOST')
    user = os.environ.get('QQROLEBOT_SSH_USER')
    if not host or not user:
        raise ValueError('Set QQROLEBOT_SSH_HOST and QQROLEBOT_SSH_USER before remote operations')
    client.connect(host,port=int(os.environ.get('QQROLEBOT_SSH_PORT','22')),username=user,
                   password=password or os.environ.get('QQROLEBOT_SSH_PASSWORD'),look_for_keys=True,allow_agent=True,timeout=15)
    return client


def run(client, command, password=None, timeout=120):
    if password:
        command = "sudo -S -p '' sh -c " + shlex.quote(command)
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    if password:
        stdin.write(password+"\n"); stdin.flush(); stdin.channel.shutdown_write()
    out, err = stdout.read().decode("utf-8","replace"), stderr.read().decode("utf-8","replace")
    code = stdout.channel.recv_exit_status()
    return {"exit_code":code,"stdout":out,"stderr":err}


def managed_files(local_root, include_static=True):
    """Only source/build outputs; runtime data and credentials are never uploaded."""
    local_root = Path(local_root).resolve()
    paths = [local_root/name for name in ('Dockerfile', '.dockerignore', 'compose.yaml', 'README.md', 'pytest.ini', '.gitignore', '.gitattributes', 'requirements-dev.txt')]
    for folder, patterns in {'backend':('*.py','requirements.txt'), 'ops':('*.py','Caddyfile'), 'tests':('*.py',),
                             'rendering':('*.py','*.json','*.service','Dockerfile','Dockerfile.dockerignore','requirements.txt'),
                             'docs':('*.md',), 'frontend/src':('*',), 'frontend/tests':('*.mjs',)}.items():
        for pattern in patterns: paths.extend((local_root/folder).rglob(pattern))
    paths.extend(local_root/'frontend'/name for name in ('package.json','package-lock.json','index.html','vite.config.ts','tsconfig.json'))
    if include_static: paths.extend((local_root/'frontend/dist').rglob('*'))
    return sorted({p for p in paths if p.is_file() and '__pycache__' not in p.parts}, key=lambda p:(p.name=='index.html',str(p)))


def upload(client, local_root, include_static=True):
    local_root = Path(local_root).resolve()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer,mode="w:gz") as tar:
        for p in managed_files(local_root, include_static):
            tar.add(p,arcname=p.relative_to(local_root).as_posix(),recursive=False)
    buffer.seek(0)
    sftp = client.open_sftp()
    try:
        sftp.stat(ROOT)
    except FileNotFoundError:
        sftp.mkdir(ROOT,0o700)
    sftp.chmod(ROOT,0o700)
    with sftp.open(ROOT+"/upload.tar.gz","wb") as remote:
        remote.write(buffer.getvalue())
    sftp.close()
    script = "import tarfile,pathlib; root=pathlib.Path('"+ROOT+"').resolve(); t=tarfile.open(root/'upload.tar.gz'); members=t.getmembers(); assert all(not m.issym() and not m.islnk() and (root/m.name).resolve().is_relative_to(root) for m in members); t.extractall(root,members=members); t.close(); (root/'upload.tar.gz').unlink(); (root/'data').mkdir(mode=0o700,exist_ok=True)"
    return run(client,"python3 -c "+shlex.quote(script))
