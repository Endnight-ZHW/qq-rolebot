"""Trusted host service. Only this process may manage renderer containers.

Expose a Unix socket to the bot; never expose the Docker socket to generated code.
The image, mounts, commands and resource envelope are operator-owned constants.
"""
import argparse
import base64
from collections import deque
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import shutil
import socketserver
import stat
import subprocess
import sys
import tarfile
import threading
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend/rolebot'))
from creation_contract import ASSET_NAME, JOB_ID, OUTPUT_LIMITS, MIB, RENDER_PROTOCOL, output_names, validate_data, validate_spec


def memory_available():
    return next(int(x.split()[1])*1024 for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:'))


def resource_envelope(available):
    """Keep a host reserve; adapt container memory, never exceed the hard cap."""
    memory_mib=min(2048,max(0,(available//MIB-768)//256*256))
    return {'can_start':memory_mib>=1024,'memory_mib':memory_mib,
            'host_reserve_mib':768,'minimum_available_mib':1792}


def docker(*args,timeout=30):
    return subprocess.check_output(['docker',*args],stderr=subprocess.STDOUT,timeout=timeout)


class Broker:
    def __init__(self, root, image, seccomp):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.image=docker('image','inspect',image,'--format','{{.Id}}').decode().strip()
        self.seccomp=str(Path(seccomp).resolve())
        self.jobs={};self.lock=threading.Lock();self.active=None
        self.owner=hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        # A restarted broker cannot resume untrusted processes. Only reclaim its
        # own labelled containers and its exact UUID directories.
        stale=docker('ps','-aq','--filter','label=qq-rolebot.render-owner='+self.owner).decode().split()
        if stale:docker('rm','-f',*stale)
        for path in self.root.iterdir():
            if JOB_ID.fullmatch(path.name) and not path.is_symlink() and path.is_dir():
                shutil.rmtree(self.path(path.name))

    def path(self,id):
        if not JOB_ID.fullmatch(id):raise ValueError('invalid job id')
        path=self.root/id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):raise ValueError('invalid job path')
        return path

    def cleanup(self):
        for id,job in list(self.jobs.items()):
            if id!=self.active and time.time()-job.get('finished_at',job['created_at'])>600:self.delete(id)

    def submit(self,body):
        self.cleanup()
        id=body.get('id','')
        self.path(id)
        spec=validate_spec(body['spec']);validate_data(body.get('data',{}))
        assets=body.get('assets',{})
        if not isinstance(assets,dict) or len(assets)>16:raise ValueError('invalid assets')
        for name,data in assets.items():
            if not ASSET_NAME.fullmatch(name) or not isinstance(data,str):raise ValueError('invalid asset name')
        if len(json.dumps(body).encode())>40*MIB:raise ValueError('input exceeds 40 MiB')
        timeout=body.get('timeout_seconds',120)
        if type(timeout) is not int or not 1<=timeout<=180:raise ValueError('invalid deadline')
        with self.lock:
            if id in self.jobs:return self.public(id)
            resources=resource_envelope(memory_available())
            if self.active or not resources['can_start']:raise BlockingIOError('renderer busy or host memory low')
            # Completed exports are short-lived and bounded even if a client dies.
            if sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())>128*MIB:
                raise BlockingIOError('renderer export quota full')
            self.active=id
            self.jobs[id]={'id':id,'state':'running','created_at':time.time(),'cancel':threading.Event(),
                           'spec':spec,'peak_memory_bytes':0,'artifacts':[],'memory_limit_mib':resources['memory_mib']}
        threading.Thread(target=self.execute,args=(id,{**body,'spec':spec}),daemon=True).start()
        return self.public(id)

    def public(self,id):
        job=self.jobs[id]
        return {k:v for k,v in job.items() if k not in ('cancel','spec')}

    def delete(self,id):
        job=self.jobs.get(id)
        if job:job['cancel'].set()
        if id==self.active:
            subprocess.run(['docker','rm','-f','qq-rolebot-render-'+id,'qq-rolebot-render-'+id+'-check'],
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        if id!=self.active:
            path=self.path(id)
            if path.exists():shutil.rmtree(path)
            self.jobs.pop(id,None)

    def options(self,name,memory='2g'):
        return ['--name',name,'--label','qq-rolebot.renderer=1','--label','qq-rolebot.render-owner='+self.owner,'--network','none','--read-only',
                '--user','1001:1001','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--security-opt','seccomp='+self.seccomp,'--cpus','2','--memory',memory,'--memory-swap',memory,
                '--pids-limit','128','--shm-size','128m','--oom-score-adj','750','--init',
                '--ulimit','nofile=1024:1024','--ulimit','fsize=33554432:33554432',
                '--tmpfs','/work:rw,size=268435456,uid=1001,gid=1001,mode=0700',
                '--tmpfs','/tmp:rw,size=67108864,mode=1777']

    def export(self,name,path,spec):
        """Read a frozen container's tmpfs through fixed, no-follow directory fds.

        Docker's archive endpoint cannot read a paused container's tmpfs. No
        untrusted path is resolved in the host namespace, nor archive extracted.
        """
        pid=int(docker('inspect',name,'--format','{{.State.Pid}}'))
        flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
        root=os.open(f'/proc/{pid}/root',os.O_RDONLY|os.O_DIRECTORY)
        work=out=None
        try:
            current=json.loads(docker('inspect',name,'--format','{{json .State}}'))
            if current['Pid']!=pid or not current['Paused']:raise ValueError('container changed during export')
            work=os.open('work',flags,dir_fd=root);out=os.open('out',flags,dir_fd=work)
            wanted=set(output_names(spec))
            if set(os.listdir(out))!=wanted:raise ValueError('unexpected or missing output path')
            for filename in wanted:
                fd=os.open(filename,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=out)
                with os.fdopen(fd,'rb') as source:
                    info=os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise ValueError('output must be a regular file')
                    if not 0<info.st_size<=OUTPUT_LIMITS[filename.rsplit('.',1)[-1]]:raise ValueError('artifact exceeds file limit')
                    with (path/filename).open('xb') as target:shutil.copyfileobj(source,target,64*1024)
                    os.chmod(path/filename,0o644)
        finally:
            for fd in (out,work,root):
                if fd is not None:os.close(fd)

    def execute(self,id,body):
        job=self.jobs[id];name='qq-rolebot-render-'+id;validator=name+'-check'
        start=time.monotonic();deadline=start+body.get('timeout_seconds',120)
        path=self.path(id);path.mkdir(mode=0o755);os.chmod(path,0o755)
        def guard():
            if job['cancel'].is_set():raise InterruptedError('cancelled')
            if time.monotonic()>deadline:raise TimeoutError('render deadline exceeded')
            if memory_available()<768*MIB:raise MemoryError('host memory pressure')
        stopped=threading.Event();aborted={}
        def watchdog():
            while not stopped.wait(.25):
                try:guard()
                except Exception as exc:
                    aborted['error']=exc
                    subprocess.run(['docker','rm','-f',name,validator],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
                    return
        watcher=threading.Thread(target=watchdog,daemon=True);watcher.start()
        try:
            buf=io.BytesIO()
            files={'spec.json':json.dumps(body['spec']).encode(),'data.json':json.dumps(body.get('data',{})).encode(),
                   'main.py' if body['spec']['runtime']=='python' else 'index.html':body['spec']['code'].encode()}
            total=0
            for filename,encoded in body.get('assets',{}).items():
                raw=base64.b64decode(encoded,validate=True);total+=len(raw)
                if total>24*MIB:raise ValueError('assets exceed 24 MiB')
                files['assets/'+filename]=raw
            with tarfile.open(fileobj=buf,mode='w') as archive:
                entry=tarfile.TarInfo('assets');entry.type=tarfile.DIRTYPE;entry.mode=0o755;entry.uid=entry.gid=1001;archive.addfile(entry)
                for filename,raw in files.items():
                    entry=tarfile.TarInfo(filename);entry.size=len(raw);entry.mode=0o644;entry.uid=entry.gid=1001
                    archive.addfile(entry,io.BytesIO(raw))
            guard();docker('run','-d',*self.options(name,str(job['memory_limit_mib'])+'m'),self.image)
            # Docker's copy API rejects writes into a read-only-root container,
            # even at a tmpfs mount. Extract our own tar inside the bounded tmpfs.
            copied=subprocess.run(['docker','exec','-i',name,'tar','-x','-C','/work'],
                                  input=buf.getvalue(),capture_output=True,timeout=20)
            if copied.returncode:raise RuntimeError(copied.stderr.decode('utf8','replace')[-2000:])
            del files,buf
            logs=deque(maxlen=8)
            process=subprocess.Popen(['docker','exec',name,'python','-I','/opt/runner.py'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            def drain():
                while chunk:=process.stdout.read(1024):logs.append(chunk)
            reader=threading.Thread(target=drain,daemon=True);reader.start()
            try:
                while process.poll() is None:
                    guard()
                    try:
                        peak=int(docker('exec',name,'/bin/cat','/sys/fs/cgroup/memory.peak',timeout=3))
                        job['peak_memory_bytes']=max(job['peak_memory_bytes'],peak)
                    except (ValueError,subprocess.SubprocessError):pass
                    time.sleep(.25)
                reader.join(timeout=2)
                try:
                    job['peak_memory_bytes']=max(job['peak_memory_bytes'],int(docker('exec',name,'/bin/cat','/sys/fs/cgroup/memory.peak',timeout=3)))
                except (ValueError,subprocess.SubprocessError):pass
                if process.returncode:raise RuntimeError(b''.join(logs).decode('utf8','replace')[-4000:] or 'renderer exited')
            finally:
                if process.poll() is None:
                    docker('rm','-f',name);process.wait(timeout=10)
            guard();docker('pause',name)
            self.export(name,path,body['spec'])
            wanted=set(output_names(body['spec']))
            docker('rm','-f',name)
            guard()
            (path/'spec.json').write_text(json.dumps(body['spec']));os.chmod(path/'spec.json',0o644)
            # A clean container validates codecs. It cannot import the submitted source.
            data=docker('run','--rm',*self.options(validator,'512m'),'-v',str(path)+':/input:ro',
                        self.image,'python','-I','/opt/validate.py',timeout=max(1,min(35,deadline-time.monotonic())))
            result=json.loads(data);guard()
            for item in result['artifacts']:
                if item['name'] not in wanted or hashlib.sha256((path/item['name']).read_bytes()).hexdigest()!=item['sha256']:
                    raise ValueError('artifact digest mismatch')
            (path/'spec.json').unlink()
            job.update(state='complete',artifacts=result['artifacts'])
        except BaseException as exc:
            exc=aborted.get('error',exc)
            detail=exc.output.decode('utf8','replace') if isinstance(exc,subprocess.CalledProcessError) and isinstance(exc.output,bytes) else str(exc)
            job.update(state='cancelled' if isinstance(exc,InterruptedError) else 'failed',
                       error_code=type(exc).__name__,error=detail[-4000:])
        finally:
            stopped.set();watcher.join(timeout=16)
            for container in (name,validator):
                subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=20)
            job.update(finished_at=time.time(),duration_seconds=round(time.monotonic()-start,3))
            if job['state']!='complete' and path.exists():shutil.rmtree(path)
            with self.lock:self.active=None
            if job['cancel'].is_set():self.delete(id)


def serve(broker,socket_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):self.handle_request()
        def do_POST(self):self.handle_request()
        def do_DELETE(self):self.handle_request()
        def handle_request(self):
            try:
                broker.cleanup()
                parts=self.path.strip('/').split('/')
                if self.command=='GET' and self.path=='/health':
                    available=memory_available();resources=resource_envelope(available)
                    reason='正在等待其他制作完成' if broker.active else ('宿主可用内存不足，正在等待至少 1792 MiB' if not resources['can_start'] else '')
                    value={'ready':True,'protocol':RENDER_PROTOCOL,'busy':bool(broker.active),'memory_available':available,**resources,
                           'reason':reason,'can_start':not broker.active and resources['can_start']}
                elif self.command=='POST' and self.path=='/jobs':
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<length<=40*MIB:raise ValueError('request size invalid')
                    value=broker.submit(json.loads(self.rfile.read(length)))
                elif len(parts)>=2 and parts[0]=='jobs':
                    id=parts[1];broker.path(id)
                    if self.command=='DELETE':broker.delete(id);value={'cancelled':True}
                    elif self.command=='GET' and len(parts)==2:value=broker.public(id)
                    elif self.command=='GET' and len(parts)==3:
                        job=broker.public(id);name=parts[2]
                        item=next(x for x in job['artifacts'] if x['name']==name)
                        if job['state']!='complete':raise ValueError('not complete')
                        path=broker.path(id)/item['name']
                        self.send_response(200);self.send_header('Content-Length',str(item['bytes']));self.end_headers()
                        with path.open('rb') as file:shutil.copyfileobj(file,self.wfile,64*1024)
                        return
                    else:raise KeyError()
                else:raise KeyError()
                raw=json.dumps(value).encode();self.send_response(200)
            except BlockingIOError:raw=b'{"error":"renderer_busy"}';self.send_response(429)
            except (KeyError,StopIteration,FileNotFoundError):raw=b'{"error":"not_found"}';self.send_response(404)
            except (ValueError,TypeError):raw=b'{"error":"invalid_request"}';self.send_response(400)
            self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
        daemon_threads=True
        def service_actions(self):broker.cleanup()
    socket=Path(socket_path);socket.parent.mkdir(parents=True,exist_ok=True);socket.unlink(missing_ok=True)
    with Server(str(socket),Handler) as server:
        os.chown(socket,1001,1001);os.chmod(socket,0o600);server.serve_forever()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True);parser.add_argument('--root',required=True)
    parser.add_argument('--seccomp',default=str(Path(__file__).with_name('seccomp.json')))
    parser.add_argument('--socket',default='/run/qq-rolebot-render/broker.sock')
    parser.add_argument('--once',type=Path);parser.add_argument('--cleanup',action='store_true');args=parser.parse_args()
    broker=Broker(args.root,args.image,args.seccomp)
    if args.cleanup:sys.exit(0)
    elif args.once:
        body=json.loads(args.once.read_text());body.setdefault('id',uuid.uuid4().hex);broker.submit(body)
        while broker.active:time.sleep(.2)
        result=broker.public(body['id']);print(json.dumps(result),flush=True)
        sys.exit(0 if result['state']=='complete' else 1)
    else:serve(broker,args.socket)
