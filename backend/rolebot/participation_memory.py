"""Learning is scheduled from successful conversational sends, never group traffic."""
import time

from .db import uid
from .memory_sources import available


class ParticipationMemory:
    def __init__(self, db):
        self.db = db

    def windows(self, scope, persona):
        epoch = self.db.get('memory_epoch', scope, {'value': 0})['value']
        return [w for w in self.db.scoped_objects('learning_session', scope, statuses=('open', 'sealed'), limit=None, oldest=True)
                if w['persona'] == persona and w['epoch'] == epoch and w['status'] in ('open', 'sealed')]

    def deadline(self, window):
        cfg = self.db.config()
        return min(window['last_participated_at']+cfg['learning_idle_seconds'], window['started_at']+cfg['learning_max_seconds'])

    def seal_due(self, scope, persona):
        for window in self.windows(scope, persona):
            if window['status'] == 'open' and time.time() >= self.deadline(window):
                window.update(status='sealed', sealed_at=time.time())
                self.db.put('learning_session', window['id'], window)

    def record(self, scope, persona, source_ids, epoch):
        if self.db.get('group', scope, {}).get('persona') != persona:
            return
        if self.db.get('memory_epoch', scope, {'value': 0})['value'] != epoch: return
        with self.db.transaction():
            self.seal_due(scope, persona)
            window = next((w for w in self.windows(scope, persona) if w['status'] == 'open'), None)
            now = time.time()
            if not window:
                window = {'id': uid(), 'scope': scope, 'persona': persona, 'epoch': epoch,
                    'status': 'open', 'started_at': now, 'source_ids': []}
            # Store references only. A later job rechecks retention, opt-outs and forgetting.
            ids = [mid for mid in source_ids if available(self.db, scope, persona, mid)]
            window.update(last_participated_at=now, source_ids=sorted(set(window['source_ids']) | set(ids)))
            self.db.put('learning_session', window['id'], window)

    def next_sources(self, scope, persona):
        self.seal_due(scope, persona)
        for window in self.windows(scope, persona):
            if window['status'] != 'sealed': continue
            jobs = [j for j in self.db.scoped_objects('memory_job', scope, statuses=('pending','running','retry','failed'), limit=None)
                    if j.get('learning_session_id') == window['id']]
            if any(j['status'] in ('pending', 'running', 'retry', 'failed') for j in jobs): continue
            sources = []
            for mid in window['source_ids']:
                row = available(self.db, scope, persona, mid)
                if row and row['role'] == 'user' and not self.db.rows('SELECT 1 FROM memory_processed WHERE message_id=?', (mid,)):
                    row['seq'] = self.db.rows('SELECT seq FROM message_order WHERE message_id=?', (row['id'],))[0]['seq']
                    sources.append(row)
            sources.sort(key=lambda m: (m.get('seq', 0), m['ts'], m['id']))
            if sources: return window, sources[:100]
            window.update(status='complete', finished_at=time.time())
            self.db.put('learning_session', window['id'], window)
        return None, []

    def status(self, scope, persona):
        windows = self.windows(scope, persona)
        failed = {j.get('learning_session_id') for j in self.db.scoped_objects('memory_job', scope, statuses=('failed',), limit=None)}
        open_windows = [w for w in windows if w['status'] == 'open']
        ids = {mid for w in windows for mid in w['source_ids']}
        pending = sum(bool((row := available(self.db, scope, persona, mid)) and row['role'] == 'user' and
            not self.db.rows('SELECT 1 FROM memory_processed WHERE message_id=?', (mid,))) for mid in ids)
        return {'mode': 'participation', 'pending_messages': pending,
                'pending_sessions': len(windows),
                'last_participated_at': max((w['last_participated_at'] for w in windows), default=None),
                'next_due_at': min((self.deadline(w) for w in open_windows), default=None),
                'failed_sessions': sum(w['id'] in failed for w in windows),
                'ready_sessions': sum(w['id'] not in failed and (w['status'] == 'sealed' or self.deadline(w) <= time.time()) for w in windows),
                'idle_seconds': self.db.config()['learning_idle_seconds'], 'max_seconds': self.db.config()['learning_max_seconds']}
