"""Check publication inputs without printing matched credential values.

Run before committing and with --history before pushing. Operator-specific
denylists belong outside this repository, not in this shared checker.
"""
import argparse
import json
import re
import subprocess
from pathlib import Path, PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
BLOCKED_DIRS={'data','secrets','delivery','artifacts','backups','upgrade-backups','caddy-data','caddy-config',
              '.venv','node_modules','__pycache__','dist','art_library','private'}
BLOCKED_SUFFIXES={'.png','.jpg','.jpeg','.webp','.gif','.mp4','.svg','.pem','.key','.crt','.p12','.pfx',
                  '.sqlite','.db','.log','.zip','.gz','.tar','.bundle','.pyc','.bak'}
PATTERNS={
    'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github-token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    'cloud-credential':re.compile(r'\b(?:AKIA[A-Z0-9]{16}|(?:AKID|IKID)[A-Za-z0-9]{28,})\b'),
    'provider-key':re.compile(r'\bsk-[A-Za-z0-9_-]{24,}\b'),
    'credential-url':re.compile(r'https?://[^\s/]+:[^\s/@]+@'),
}


def git(*args):
    return subprocess.check_output(['git',*args],cwd=ROOT)


def check(name,raw,findings):
    p=PurePosixPath(name)
    if set(p.parts)&BLOCKED_DIRS or p.suffix.lower() in BLOCKED_SUFFIXES or (p.name.startswith('.env') and p.name!='.env.example'):
        findings.append({'path':name,'reason':'private-or-generated-path'})
    if b'\0' in raw:
        findings.append({'path':name,'reason':'binary-file'});return
    try:text=raw.decode('utf8')
    except UnicodeDecodeError:
        findings.append({'path':name,'reason':'non-text-file'});return
    for number,line in enumerate(text.splitlines(),1):
        for label,pattern in PATTERNS.items():
            # Explicit invalid-credential URL fixtures are needed to reject URL credentials.
            if label=='credential-url' and name.startswith('tests/') and any(v in line for v in ('user:password@','u:p@','user:pass@example.com')):continue
            if pattern.search(line):findings.append({'path':name,'line':number,'reason':label})


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--staged',action='store_true');parser.add_argument('--history',action='store_true')
    args=parser.parse_args();findings=[];count=0
    if args.history:
        for entry in git('rev-list','--objects','--all').decode().splitlines():
            oid,_,name=entry.partition(' ')
            kind=git('cat-file','-t',oid).strip()
            if kind==b'blob':check(name,git('cat-file','blob',oid),findings);count+=1
            elif kind==b'commit':check('<commit>',git('cat-file','commit',oid),findings)
    else:
        args_list=['ls-files','-z','--cached']
        if not args.staged:args_list+=['--others','--exclude-standard']
        names=sorted(set(git(*args_list).decode().strip('\0').split('\0'))-set(['']))
        for name in names:
            if args.staged:
                mode=git('ls-files','-s','--',name).decode().split()[0]
                if mode not in ('100644','100755'):findings.append({'path':name,'reason':'non-regular-file'});continue
                raw=git('show',':'+name)
            else:
                p=ROOT/name
                if not p.is_file():continue
                if p.is_symlink():findings.append({'path':name,'reason':'symlink'});continue
                raw=p.read_bytes()
            check(name,raw,findings);count+=1
    print(json.dumps({'ok':not findings,'files_checked':count,'findings':findings},ensure_ascii=False))
    raise SystemExit(bool(findings))


if __name__=='__main__':main()
