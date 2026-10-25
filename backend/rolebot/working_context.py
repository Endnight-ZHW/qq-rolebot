"""Bounded, append-stable working history. Raw archives remain queryable."""
import json
import time

from . import memory_sources


def limits(db):
    cfg = db.config()
    return cfg['working_initial_messages'], cfg['working_max_messages'], cfg['working_context_budget']


def visible(db, scope, persona):
    floor = max(time.time()-db.config()['retention_days']*86400,
                db.get('context_reset', scope+':'+persona, {}).get('ts', 0))
    return ("FROM messages m JOIN message_order o ON o.message_id=m.id "
            "WHERE m.scope=? AND m.persona=? AND m.excluded=0 AND m.ts>? "
            "AND NOT EXISTS(SELECT 1 FROM objects u WHERE u.kind='member' AND u.id=m.scope||':'||m.user_id AND json_extract(u.data,'$.remember')=0)",
            [scope, persona, floor])


def load(db, scope, persona, signature, stage='reply'):
    initial, maximum, _ = limits(db)
    key = scope+':'+persona+(':task' if stage == 'task' else '')
    state = db.get('working_context', key, {})
    if state.get('identity') != signature or time.time()-state.get('updated_at', 0) >= db.config()['learning_idle_seconds']:
        state = {}
    sql, args = visible(db, scope, persona)
    count = db.rows('SELECT COUNT(*) n '+sql, args)[0]['n']
    ids = state.get('message_ids', [])
    kept = db.rows('SELECT m.*,o.seq '+sql+' AND m.id IN ('+','.join('?' for _ in ids)+') ORDER BY o.seq', (*args, *ids)) if ids else []
    added = db.rows('SELECT m.*,o.seq '+sql+' AND o.seq>? ORDER BY o.seq LIMIT ?', (*args, state.get('last_seq', 0), maximum+1)) if kept else []
    if not kept or len(kept)+len(added) > maximum:
        kept = db.rows('SELECT m.*,o.seq '+sql+' ORDER BY o.seq DESC LIMIT ?', (*args, initial))[::-1]
    else:
        kept += added
    for row in kept: row['data'] = json.loads(row['data'])
    protected = set()
    focus = db.get('followup', scope+':'+persona, {})
    if focus.get('expires', 0) > time.time() and focus.get('source_id'):
        protected.add(focus['source_id'])
    latest = db.rows('SELECT m.*,o.seq '+sql+" AND m.role='assistant' AND m.ts>? ORDER BY o.seq DESC LIMIT 1", (*args, time.time()-db.config()['learning_idle_seconds']))
    if latest:
        protected.add(latest[0]['id'])
        meta = json.loads(latest[0]['data'])
        delivery = db.get('reply_delivery', meta.get('delivery_id', ''), {})
        if delivery.get('target_id'): protected.add(delivery['target_id'])
    for mid in protected:
        if not any(m['id'] == mid for m in kept):
            row = memory_sources.available(db, scope, persona, mid)
            if row:
                row['seq'] = db.rows('SELECT seq FROM message_order WHERE message_id=?', (row['id'],))[0]['seq']
                kept.append(row)
    kept.sort(key=lambda m: (m.get('seq', 0), m['ts'], m['id']))
    pins = []
    if state.get('retrieved_until', 0) > time.time():
        pins = [row for mid in state.get('retrieved_ids', []) if (row := memory_sources.available(db, scope, persona, mid))]
    return kept, pins, protected, {'id': key, 'identity': signature, 'available_count': count,
        'retrieved_ids': [m['id'] for m in pins], 'retrieved_until': state.get('retrieved_until', 0)}


def save(db, state, messages):
    ids = [m['id'] for m in messages if m.get('seq')]
    db.put('working_context', state['id'], {**state, 'message_ids': ids,
        'last_seq': max((m.get('seq', 0) for m in messages), default=0), 'updated_at': time.time()})


def evidence(preview, batch, executed=()):
    context = preview.get('context', {})
    ids = {m['id'] for m in batch}
    ids.update(preview.get('image_source_ids', []))
    for key in ('消息', 'quotes', 'retrieved_history'):
        ids.update(m['id'] for m in context.get(key, []) if m.get('id'))
    for tool in executed:
        if tool.get('name') in ('history_search', 'history_read') and tool.get('status') == 'success':
            ids.update(m['id'] for m in (tool.get('result') or {}).get('messages', []) if m.get('id'))
    return ids


def retain_reads(db, scope, persona, executed):
    from .context import identity
    ids = []
    for tool in executed:
        if tool.get('name') in ('history_search', 'history_read') and tool.get('status') == 'success':
            ids.extend(m['id'] for m in (tool.get('result') or {}).get('messages', []) if m.get('id'))
    if not ids: return
    key = scope+':'+persona
    state = db.get('working_context', key, {})
    if state.get('identity') != identity(db, scope, persona): return
    state['retrieved_ids'] = list(dict.fromkeys(state.get('retrieved_ids', [])+ids))[-12:]
    state['retrieved_until'] = time.time()+db.config()['learning_idle_seconds']
    db.put('working_context', key, state)
