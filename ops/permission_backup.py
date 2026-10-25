"""Back up/restore only tool and QQ permissions, never chat or memory tables."""
import argparse
import json
import re
import sqlite3
import time
import uuid
from pathlib import Path


def connect(path, mode):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode='+mode, uri=True)


def snapshot(path):
    with connect(path, 'ro') as db:
        rows = {(kind, key): json.loads(data) for kind, key, data in db.execute(
            "SELECT kind,id,data FROM objects WHERE kind IN ('group','qq_access')")}
    return {'format': 1, 'captured_at': time.time(), 'scopes': {
        key: {'tools': value.get('tools', {}), 'qq_access': rows.get(('qq_access', key))}
        for (kind, key), value in rows.items() if kind == 'group'}}


def restore(path, saved, apply=False):
    if saved.get('format') != 1 or not isinstance(saved.get('scopes'), dict) or not saved['scopes']:
        raise ValueError('Invalid permission snapshot')
    for scope, entry in saved['scopes'].items():
        if (not re.fullmatch(r'\d+:[gp]:\d+', scope) or set(entry) != {'tools', 'qq_access'}
                or not isinstance(entry['tools'], dict) or entry['qq_access'] is not None and not isinstance(entry['qq_access'], dict)):
            raise ValueError('Invalid permission scope: '+scope)
    with connect(path, 'rw' if apply else 'ro') as db:
        if apply: db.execute('BEGIN IMMEDIATE')
        updates = []
        for scope, entry in saved['scopes'].items():
            row = db.execute("SELECT data FROM objects WHERE kind='group' AND id=?", (scope,)).fetchone()
            if row is None: raise ValueError('Group no longer exists: '+scope)
            group = json.loads(row[0]); group['tools'] = entry['tools']
            group['revision'] = group.get('revision', 0)+1
            row = db.execute("SELECT data FROM objects WHERE kind='qq_access' AND id=?", (scope,)).fetchone()
            current = json.loads(row[0]) if row else {}
            access = dict(entry['qq_access'] or {})
            access['revision'] = max(current.get('revision', 0), access.get('revision', 0))+1
            updates.extend([('group', scope, group), ('qq_access', scope, access)])
        if apply:
            for kind, scope, value in updates:
                db.execute('INSERT INTO objects VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated',
                    (kind, scope, json.dumps(value, ensure_ascii=False), time.time()))
            for scope in saved['scopes']:
                db.execute('INSERT INTO audit VALUES(?,?,?,?,?)', (uuid.uuid4().hex, scope, 'permission_rollback',
                    json.dumps({'snapshot_at': saved.get('captured_at'), 'actor': 'operator'}), time.time()))
    return {'applied': apply, 'scopes': sorted(saved['scopes']), 'restored_fields': ['group.tools', 'qq_access']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['backup', 'restore'])
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--snapshot', required=True, type=Path)
    parser.add_argument('--apply', action='store_true', help='Apply restoration; otherwise only validate')
    args = parser.parse_args()
    if args.action == 'backup':
        data = snapshot(args.database)
        with args.snapshot.open('x', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
        args.snapshot.chmod(0o600)
        print(json.dumps({'scopes': sorted(data['scopes']), 'snapshot': str(args.snapshot)}))
    else:
        print(json.dumps(restore(args.database, json.loads(args.snapshot.read_text(encoding='utf-8')), args.apply)))
