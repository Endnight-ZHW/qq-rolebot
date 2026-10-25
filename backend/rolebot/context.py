"""Stable observations followed by request-local context. Estimates are characters, not billing."""
import json
import time

from . import prompts, memory_sources
from .knowledge import Knowledge
from .memory_semantics import view as memory_view


class ContextTooLarge(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)

def hard_budget(db, model):
    return int(model.context_budget() if hasattr(model, 'context_budget') else db.config()['context_tokens'])


def identity(db, scope, persona):
    # Read live versions even inside a trace's pinned prompt snapshot.
    token = prompts.active_snapshot.set(None)
    try:
        snap = prompts.snapshot(db, scope)
    finally:
        prompts.active_snapshot.reset(token)
    group = db.get('group', scope, {})
    return prompts.digest([scope, persona, snap['personas'].get(persona), group.get('guidance'),
        {k: v['sha256'] for k, v in snap['entries'].items()}, db.get('inference', 'global', {}),
        db.get('memory_epoch', scope, {}), db.get('summary_generation', scope, {}),
        db.get('context_reset', scope+':'+persona, {}),
        {k: db.config()[k] for k in ('working_initial_messages', 'working_max_messages', 'working_context_budget')}])


def observation(db, scope, persona, message, quote=False):
    data = message.get('data', {})
    get = lambda key, default=None: data.get(key, message.get(key, default))
    row = {'id': message['id'], 'qq': message['user_id'], 'role': message.get('role', 'user'),
           'speaker': get('name', ''), 'text': message['text'], 'time': get('sent_at', message['ts']),
           'message_id': get('message_id', ''), 'mentions': get('mentions', []), 'types': get('types', ['text'])}
    if get('image_descriptions'):
        row['images'] = get('image_descriptions')
    from .attachments import descriptors
    attachments = descriptors(db, scope, persona, message)
    if attachments:
        row['attachments'] = attachments if quote else [
            {k: a[k] for k in ('image_id', 'source_id', 'index', 'sender', 'time')} for a in attachments]
    if get('truncated'):
        row['notice'] = prompts.render(db, scope, 'context.long_message')
    # Quote expansions belong to the dynamic tail: adding an image description
    # to a referenced message must not rewrite every historical quoting row.
    if get('replies'):
        row['reply_ids'] = get('replies')[:3]
    if get('image_search_refs'):
        row['image_search_refs'] = get('image_search_refs')[:3]
    return row


def build(db, model, scope, persona, query, transient=(), stage='reply', runtime=None):
    from . import working_context
    cfg, group = db.config(), db.get('group', scope, {})
    card = prompts.persona(db, scope, persona)
    hard = hard_budget(db, model)
    is_gate = stage == 'gate'
    soft = min(hard, cfg['gate_context_budget'] if is_gate else cfg['working_context_budget'])
    current = {m['id']: m for m in transient or []}
    forward_summary = set((runtime or {}).get('_forward_summary_sources', [])) if stage == 'reply' else set()
    rows = []
    working, pins, protected = None, [], set(current)
    if is_gate:
        recent = [m for page in db.recent_pages(scope, persona, max_messages=cfg['gate_max_messages']) for m in page]
        recent.reverse()
    else:
        recent, pins, keep, working = working_context.load(db, scope, persona, identity(db, scope, persona), stage)
        protected.update(keep)
    seen = {m['id'] for m in recent}
    recent.extend(m for m in transient or [] if m['id'] not in seen)
    if forward_summary:
        recent = [m for m in recent if m['id'] in set(current) | forward_summary]
    for message in recent:
        # Persisted messages are canonical, including their timestamp and role.
        rows.append(observation(db, scope, persona, message))
    quotes = []
    for m in transient or []:
        for ref in (m.get('data', {}).get('replies') or m.get('replies', []))[:3]:
            match = next((x for x in current.values() if ref in (x['id'], x.get('message_id'))), None) or memory_sources.available(db, scope, persona, ref)
            if match:
                item = observation(db, scope, persona, match, quote=True)
                if len(item['text']) > 4000:
                    item['text'] = item['text'][:4000]
                    item['notice'] = prompts.render(db, scope, 'context.long_quote')
                quotes.append(item)
            else:
                quotes.append({'message_id': ref, 'notice': prompts.render(db, scope, 'context.missing_quote')})
    subjects = sorted({m['user_id'] for m in transient or []})
    ledger = Knowledge(db)
    memories = [] if forward_summary else db.recall(scope, persona, query, subject_ids=subjects)
    profiles = [] if forward_summary else [p for p in ledger.profiles(scope, persona) if p['user_id'] in subjects]
    unique = {m['id']: m for m in memories}
    for p in profiles:
        unique.update({m['id']: m for m in p['facts'][:8] if m['id'] not in unique})
    reserve = int((runtime or {}).get('_tool_schema_chars', 4000))
    dynamic = {'current_ids': list(current), 'quotes': quotes,
        '成员档案': [{'qq': p['user_id'], 'name': p.get('name', ''), 'aliases': p.get('aliases', []), 'memory_ids': [m['id'] for m in p['facts'][:8]]} for p in profiles],
        '相关记忆': [memory_view(db, m, compact=True) for m in unique.values()],
        '群表达参考': [{k: s[k] for k in ('id', 'expression', 'meaning', 'usage')} for s in ledger.styles(scope, active_only=True)[:8]] if group.get('learning_enabled', True) and not forward_summary else [],
        'runtime': {k:v for k,v in (runtime or {}).items() if not k.startswith('_')}}
    if stage == 'reply' and not forward_summary and (runtime or {}).get('_sticker_candidates'):
        dynamic['sticker_candidates'] = list(runtime['_sticker_candidates'])
    if working and not forward_summary:
        dynamic['retrieved_history'] = [observation(db, scope, persona, m) for m in pins if m['id'] not in seen]
    if forward_summary:
        dynamic['answer_scope'] = {'kind': 'forward_summary', 'source_ids': sorted(forward_summary),
                                  'notice': '只总结这些记录与内部图片，不补充周围群聊或未经核实的判断'}
    base = '\n'.join([prompts.render(db, scope, 'optimized.system.base'), prompts.render(db, scope, 'context.persona_header')+card.get('prompt', ''),
        prompts.render(db, scope, 'optimized.context.rules'), prompts.render(db, scope, 'tool.failure_rules'), prompts.render(db, scope, 'optimized.context.image_rules'),
        prompts.render(db, scope, 'runtime.boundaries'), prompts.render(db, scope, 'runtime.gate' if is_gate else 'task.execute' if stage == 'task' else 'runtime.agent'),
        *([prompts.render(db, scope, 'runtime.closing')] if stage != 'task' else [])])
    if not is_gate:
        base += '\n'+prompts.render(db, scope, 'runtime.capabilities')
        if stage == 'reply':
            base += '\n'+prompts.render(db, scope, 'runtime.social_tone')
            if any(t.get('name')=='creation_create' and t.get('enabled') for t in (runtime or {}).get('tools',{}).get('tools',[])):
                base += '\n'+prompts.render(db, scope, 'runtime.creation')
        base += '\n'+prompts.render(db, scope, 'runtime.history_lookup')
        base += '\n'+prompts.render(db, scope, 'runtime.multimodal')
        base += '\n'+prompts.render(db, scope, 'runtime.image_detail')
        base += '\n'+prompts.render(db, scope, 'runtime.forwards')
        base += '\n'+prompts.render(db, scope, 'runtime.search')
    if not is_gate and stage != 'task' and group.get('interaction_enabled'):
        base += '\n'+prompts.render(db, scope, 'interaction.decision')
    stable = {'scope': scope, '角色身份': {'id': persona, 'name': card.get('name'), 'qq': scope.split(':')[0]},
              '群说明': prompts.guidance(db, scope), '关系绑定': card.get('relationships', {})}
    prefix = prompts.render(db, scope, 'context.intro')+encode(stable)+'\n'
    prefix += '历史消息（按接收顺序）：\n'
    original_count = len(rows)
    from .attachments import descriptors
    image_details = {m['id']: descriptors(db, scope, persona, m) for m in recent}
    from . import image_evidence
    evidence_candidates = image_evidence.candidates(db, scope, persona) if not is_gate else []
    def assemble():
        # Mutable observations/readability belong to the tail; reading a picture
        # must not rewrite the stable historical prefix on the next turn.
        dynamic['image_catalog'] = [a for r in rows for a in image_details.get(r['id'], [])][-12:]
        evidence = image_evidence.select(evidence_candidates, rows+quotes+dynamic.get('retrieved_history', []))
        if evidence:
            dynamic['image_search_evidence'] = evidence
        else:
            dynamic.pop('image_search_evidence', None)
        if working:
            dynamic['history_coverage'] = {'loaded_messages': len(rows), 'available_messages': working['available_count'],
                'older_records_queryable': True, 'first_loaded_id': rows[0]['id'] if rows else None,
                'last_loaded_id': rows[-1]['id'] if rows else None}
        return [{'role': 'system', 'content': base}, {'role': 'user', 'content': prefix+'\n'.join(encode(r) for r in rows)},
                {'role': 'user', 'content': '本轮资料：'+encode(dynamic)+(('\n'+prompts.render(db, scope, 'runtime.output')) if stage == 'reply' else '')}]
    def estimated():
        return sum(len(m['content']) for m in assemble())+reserve
    if is_gate:
        while estimated() > soft and any(r['id'] not in current for r in rows):
            rows.pop(next(i for i,r in enumerate(rows) if r['id'] not in current))
    for key in ('sticker_candidates', '群表达参考', '相关记忆', '成员档案', 'retrieved_history'):
        if key not in dynamic: continue
        while estimated() > soft and dynamic[key]:
            dynamic[key].pop()
    if working:
        # The memory idle timer must not erase a recent exchange. Prefer a
        # small conversational tail, but never exceed the model's hard budget.
        recent_ids = {r['id'] for r in rows[-6:]} if stage == 'reply' else set()
        # Stop as soon as the budget fits. A fixed 20-message deletion could
        # discard the entire recent exchange for a tiny budget overrun.
        while estimated() > soft and any(r['id'] not in protected | recent_ids for r in rows):
            rows.pop(next(i for i, r in enumerate(rows) if r['id'] not in protected | recent_ids))
        while estimated() > hard and any(r['id'] not in protected for r in rows):
            rows.pop(next(i for i, r in enumerate(rows) if r['id'] not in protected))
    if estimated() > hard:
        raise ContextTooLarge('当前问题、引用和受保护上下文超过输入预算；不会静默删去当前问题')
    retained = {m['id'] for m in dynamic['相关记忆']}
    for profile in dynamic['成员档案']:
        profile['memory_ids'] = [mid for mid in profile['memory_ids'] if mid in retained]
    messages = assemble()
    image_evidence.track(dynamic.get('image_search_evidence', []))
    if working and not forward_summary:
        retained_ids = {r['id'] for r in rows}
        working_context.save(db, working, [m for m in recent if m['id'] in retained_ids])
    blocks = {**stable, **dynamic, '消息': rows, '摘要': []}
    preview = {'persona': persona, 'stage': stage, 'estimated_tokens': estimated(), 'estimate_source': 'conservative_characters',
        'budget': hard, 'soft_budget': soft, 'history_loaded': len(rows), 'history_limit': cfg['gate_max_messages'] if is_gate else cfg['working_max_messages'],
        'history_limited': len(rows) < (working['available_count'] if working else original_count),
        'omitted_messages': max(0, (working['available_count'] if working else original_count)-len(rows)), 'context': blocks,
        'memory_ids': list(retained),
        'sections': [{'name': '固定规则', 'estimated_tokens': len(base)}, {'name': '稳定历史', 'estimated_tokens': len(messages[1]['content'])},
                     {'name': '本轮资料', 'estimated_tokens': len(messages[2]['content'])}], 'ts': time.time(), 'notices': []}
    return messages, preview
