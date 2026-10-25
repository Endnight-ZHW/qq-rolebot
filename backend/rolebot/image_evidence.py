"""Bounded, revocable search evidence linked to chat images and replies.

Only the existing same-chat cache is read. Recovering evidence never uploads
an image or sends another Lens query.
"""
import json
import time

from .attachments import visible


def link(db, item, source, result):
    key = result.get('cache_id')
    record = db.get('image_search_cache', key) if key else None
    if not record or result.get('status') not in ('results_found', 'no_results') or not source.get('remember', True):
        return None
    association = {'image_id': item['id'], 'source_id': item['source_id'],
                   'request_id': source['id'], 'actor': source['user_id']}
    links = [row for row in record.get('links', []) if row != association]
    record['links'] = (links+[association])[-32:]
    record['source_ids'] = sorted({mid for row in record['links'] for mid in (row['source_id'], row['request_id'])})
    record['actor_ids'] = sorted({row['actor'] for row in record['links']} | {item['sender']})
    db.put('image_search_cache', key, record)
    return key


def access(db, scope, persona):
    from .image_turn import active_images
    from .tools import ToolDenied
    turn = active_images.get()
    if not turn or (turn.db, turn.scope, turn.persona) != (db, scope, persona) or not turn.batch:
        return None
    engine = turn.engine
    service = getattr(engine, 'image_search', None)
    if not service or not service.key() or not engine.current(scope, persona, turn.epoch):
        return None
    for name in ('image_search', 'image_read'):
        policy = engine.tools.policy(scope, name)
        if not policy.get('enabled') or not policy.get('user_enabled', True):
            return None
        for actor in {m['user_id'] for m in turn.batch}:
            try:
                engine.permissions.check_tool(scope, actor, name, {})
            except ToolDenied:
                return None
    return turn, service


def valid(db, scope, persona, record, turn, service):
    return (record.get('scope') == scope and record.get('persona') == persona
            and record.get('epoch') == turn.epoch and record.get('revision') == service.revision()
            and record.get('ts', 0) > time.time()-86400
            and record.get('status') in ('results_found', 'no_results'))


def candidates(db, scope, persona):
    authorized = access(db, scope, persona)
    if not authorized:
        return []
    turn, service = authorized
    result = []
    for record in db.scoped_objects('image_search_cache', scope, limit=None):
        if not valid(db, scope, persona, record, turn, service):
            continue
        for row in reversed(record.get('links', [])):
            item = db.get('attachment', row['image_id'], {})
            if (item.get('scope') != scope or item.get('persona') != persona
                    or item.get('source_id') != row['source_id']
                    or not visible(db, scope, persona, row['source_id'])
                    or not visible(db, scope, persona, row['request_id'])):
                continue
            result.append((record, row))
    return result


def compact(record, row):
    overview = record.get('overview') or {}
    return {'evidence_id': record['id'], 'image_id': row['image_id'], 'source_id': row['source_id'],
            'request_id': row['request_id'], 'searched_at': record['ts'], 'status': record['status'],
            'matches': [{k: m.get(k, '') for k in ('title', 'url', 'source', 'kind')}
                        for m in record.get('matches', [])[:3]],
            'overview': {'text': overview.get('text', '')[:600], 'references': overview.get('references', [])[:3]},
            'notice': '已有检索依据，本轮未重新搜图；需要视觉对照时按 image_id 读取原图。'}


def select(items, messages=(), image_ids=()):
    ids, refs = set(image_ids), set()
    mids = set()
    for m in messages:
        if m.get('id'):
            mids.add(m['id'])
        refs.update(m.get('data', m).get('image_search_refs', []))
        ids.update(a['image_id'] for a in m.get('attachments', []) if a.get('image_id'))
    selected, seen = [], set()
    for record, row in items:  # newest cache entries first
        if row['image_id'] in seen:
            continue
        if (row['image_id'] in ids or record['id'] in refs
                or {row['source_id'], row['request_id']} & mids):
            entry = compact(record, row)
            # Drop complete optional fields/entries; never cut JSON or source URLs.
            while len(json.dumps(selected+[entry], ensure_ascii=False)) > 4000:
                if entry['overview']['references']:
                    entry['overview']['references'].pop()
                elif entry['overview']['text']:
                    entry['overview']['text'] = ''
                elif entry['matches']:
                    entry['matches'].pop()
                else:
                    entry = None
                    break
            if entry is None:
                break
            selected.append(entry)
            seen.add(row['image_id'])
        if len(selected) >= 3:
            break
    return selected


def for_images(db, scope, persona, image_ids):
    return select(candidates(db, scope, persona), image_ids=image_ids)


def for_messages(db, scope, persona, messages):
    return select(candidates(db, scope, persona), messages)


def track(entries):
    from .image_turn import active_images
    from .tools import ToolDenied
    turn = active_images.get()
    if not turn:
        return
    for entry in entries:
        key = entry['evidence_id']
        if key in turn.search_refs:
            continue
        def check(entry=entry):
            authorized = access(turn.db, turn.scope, turn.persona)
            record = turn.db.get('image_search_cache', entry['evidence_id'], {})
            if (not authorized or not valid(turn.db, turn.scope, turn.persona, record, *authorized)
                    or not visible(turn.db, turn.scope, turn.persona, entry['source_id'])
                    or not visible(turn.db, turn.scope, turn.persona, entry['request_id'])):
                raise ToolDenied('已有搜图依据已失效', code='context_changed')
        turn.search_checks.append(check)
        turn.search_refs.add(key)
        turn.search_sources.add(entry['source_id'])
