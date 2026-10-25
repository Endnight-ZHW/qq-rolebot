"""Current QQ quotes supply context, never another member's authority."""
import json
import math
import re
import time
from contextvars import ContextVar

from . import tracing
from .onebot import normalize

active_request_evidence = ContextVar('active_request_evidence', default=None)


class RequestEvidence:
    def __init__(self, tools, scope, persona, source, *, fetch_missing=True):
        self.tools, self.db, self.scope, self.persona, self.source = tools, tools.db, scope, persona, source
        self.epoch = self.db.get('memory_epoch', scope, {}).get('value', 0)
        self.reset = self.db.get('context_reset', scope+':'+persona, {}).get('ts', 0)
        self.records, self.fetched, self.unavailable = {}, {}, []
        self.fetch_missing = fetch_missing
        self.references = (source.get('data', {}).get('replies') or source.get('replies', []))[:3]
        anchor = self.db.rows('SELECT seq FROM message_order WHERE message_id=?', (source['id'],))
        self.anchor = anchor[0]['seq'] if anchor else None

    def message_id(self, value):
        value = str(value)
        if ':' in value:
            if not value.startswith(self.scope+':'): return None
            value = value[len(self.scope)+1:]
        return str(int(value)) if re.fullmatch(r'-?\d{1,20}', value) else None

    def visible(self, row):
        floor = max(self.reset, time.time()-self.db.config()['retention_days']*86400)
        return (row['scope'] == self.scope and row['persona'] == self.persona and not row.get('excluded')
            and floor < row['ts'] <= self.source['ts']
            and (not self.anchor or row.get('seq', self.anchor-1) < self.anchor)
            and self.db.get('member', self.scope+':'+row['user_id'], {}).get('remember', True))

    def local(self, reference):
        mid = self.message_id(reference)
        if mid is None: return None, True
        rows = self.db.rows('SELECT m.*,o.seq FROM messages m JOIN message_order o ON o.message_id=m.id '
            "WHERE m.scope=? AND (m.id=? OR CAST(json_extract(m.data,'$.message_id') AS TEXT)=?)",
            (self.scope, self.scope+':'+mid, mid))
        if not rows: return None, False
        if len(rows) != 1 or not self.visible(rows[0]): return None, True
        row = rows[0]; row['data'] = json.loads(row['data'])
        return row, False

    def item(self, row):
        data = row['data']; target = None
        if row['role'] == 'assistant' and row['user_id'] == self.tools.onebot.self_id:
            delivery = self.db.get('reply_delivery', data.get('delivery_id', ''), {})
            if delivery.get('scope') == self.scope and delivery.get('persona') == self.persona:
                target, _ = self.local(delivery.get('target_id', ''))
        return {'id':row['id'], 'qq':row['user_id'], 'role':row['role'], 'text':row['text'][:4000],
            'timestamp':row['ts'],
            'mentions':data.get('mentions', []), 'replies':data.get('replies', []),
            'reply_to_qq':target['user_id'] if target else None, 'quoted_context':True}, target

    async def collect(self):
        if not self.source.get('remember', True) or not self.db.get('member', self.scope+':'+self.source['user_id'], {}).get('remember', True):
            return []
        queue = [(ref, 0) for ref in self.references]; visited = set(); network = 0
        while queue and len(self.records) < 12:
            ref, depth = queue.pop(0); mid = self.message_id(ref)
            if mid is None or mid in visited: continue
            visited.add(mid)
            row, blocked = self.local(mid)
            # Only the real incoming message's direct quotes may trigger QQ reads.
            if not row and not blocked and self.fetch_missing and depth == 0 and network < 2 and ':g:' in self.scope:
                network += 1
                row = await self.fetch(mid)
            if not row:
                self.unavailable.append(mid); continue
            item, target = self.item(row)
            self.records[row['id']] = row
            if depth < 3:
                linked = ([target['id']] if target else []) + row['data'].get('replies', [])[:3]
                queue.extend((reference, depth+1) for reference in linked)
        # Include intervening statements from the requester (e.g. cancellation),
        # even when the quote is older than the ordinary ten-minute window.
        linked_ids = set(self.records)
        if self.records:
            earliest = min(r['ts'] for r in self.records.values())
            rows = self.db.rows('SELECT m.id FROM messages m JOIN message_order o ON o.message_id=m.id '
                'WHERE m.scope=? AND m.persona=? AND m.user_id=? AND m.ts>=? AND m.ts<? '
                'ORDER BY o.seq DESC LIMIT 80', (self.scope, self.persona, self.source['user_id'], earliest, self.source['ts']))
            for entry in rows:
                row, _ = self.local(entry['id'])
                if row: self.records.setdefault(row['id'], row)
        # Quotes get their own bounded allowance and are never evicted by chatter.
        direct = {self.scope+':'+self.message_id(r) for r in self.references if self.message_id(r) is not None}
        ordered = sorted(self.records.values(), key=lambda r: (r['id'] in direct, r['id'] in linked_ids, r['ts']), reverse=True)
        selected, size = [], 0
        for row in ordered:
            item, _ = self.item(row); length = len(json.dumps(item, ensure_ascii=False))
            if size+length > 24000: continue
            selected.append(item); size += length
        return sorted(selected, key=lambda item: self.records[item['id']]['ts'])

    async def fetch(self, mid):
        token = tracing.active_trace.set(None)
        try:
            value = await self.tools.onebot.call('get_msg', {'message_id':mid})
        except Exception:
            return None
        finally:
            tracing.active_trace.reset(token)
        if not isinstance(value, dict): return None
        sender = value.get('sender') if isinstance(value.get('sender'), dict) else {}
        actor = str(value.get('user_id', sender.get('user_id', ''))); stamp = value.get('time')
        if (str(value.get('message_id')) != mid or str(value.get('group_id')) != self.scope.split(':')[2]
            or value.get('message_type', 'group') != 'group' or str(value.get('self_id', self.tools.onebot.self_id)) != self.tools.onebot.self_id
            or not re.fullmatch(r'\d{5,20}', actor) or str(sender.get('user_id', actor)) != actor
            or type(stamp) not in (int, float) or not math.isfinite(stamp)):
            return None
        try:
            message = normalize({**value, 'self_id':self.tools.onebot.self_id, 'user_id':actor, 'sender':sender})
        except (TypeError, ValueError, AttributeError):
            return None
        row = {'id':self.scope+':'+mid, 'scope':self.scope, 'persona':self.persona, 'user_id':actor,
            'role':'assistant' if actor == self.tools.onebot.self_id else 'user', 'text':message['text'], 'ts':stamp,
            'data':{'message_id':mid, 'mentions':message['mentions'], 'replies':message['replies']}}
        concurrent, blocked = self.local(mid)
        if blocked or not self.visible(row): return None
        if concurrent:
            return concurrent if concurrent['user_id'] == actor else None
        self.fetched[row['id']] = row
        # Ephemeral context only: no imported messages or business memories.
        return row

    def current(self):
        if (self.db.get('memory_epoch', self.scope, {}).get('value', 0) != self.epoch
            or self.db.get('context_reset', self.scope+':'+self.persona, {}).get('ts', 0) != self.reset
            or self.records and not self.db.get('member', self.scope+':'+self.source['user_id'], {}).get('remember', True)):
            return False
        for saved in self.records.values():
            row, blocked = self.local(saved['id'])
            if blocked or not self.visible(saved): return False
            if not row and saved['id'] not in self.fetched: return False
            if row and (row['user_id'], row['text']) != (saved['user_id'], saved['text']): return False
        return True
