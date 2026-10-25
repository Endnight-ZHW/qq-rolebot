"""Bounded, interruptible delivery of one logical reply; drafts stay in memory."""
import asyncio
import hashlib
import random
import re
import time

from .db import uid
from .config_schema import runtime_config, GROUP_NUMBERS
from .onebot import ActionUnknown
from . import tracing
from .reply_review import ReplyReview

DEFAULTS = {'segmented_reply':False, **{k:v for k,v in GROUP_NUMBERS.items() if k.startswith('segment_')}}
SEND_LIMIT = GROUP_NUMBERS['send_per_minute']
SEND_WINDOW = 60
RATE_WAIT_LIMIT = GROUP_NUMBERS['send_wait_seconds']


def send_slots(state, limit=SEND_LIMIT):
    return max(0, limit-len([t for t in state.get('sent', []) if t > time.time()-SEND_WINDOW]))


def split_reply(text, threshold=80, max_parts=3):
    text = text.strip()
    if len(text) < threshold or max_parts < 2:
        return [text]
    # Structured material stays intact. The original text is never shortened.
    if '```' in text or re.search(r'(?m)^\s*(?:[-*+]\s|\d+[.)、]\s*|\|)', text):
        return [text]
    protected = [(m.start(), m.end()) for m in re.finditer(r'https?://[^\s<>]+|`[^`\n]*`', text)]
    opening = dict(zip('([{（【「《“', ')]}）】」》”'))
    stack, cuts = [], []
    for i, char in enumerate(text):
        if any(a <= i < b for a, b in protected):
            continue
        if char in opening:
            stack.append(opening[char])
        elif stack and char == stack[-1]:
            stack.pop()
        if stack:
            continue
        boundary = char in '。！？!?；;\n' or (char == '.' and (i+1 == len(text) or text[i+1].isspace()))
        if boundary:
            cuts.append((i+1, char == '\n'))
    count = min(max_parts, 2 if len(text) <= 140 else max_parts, len(cuts)+1, max(1, len(text)//12))
    chosen, start = [], 0
    for index in range(1, count):
        candidates = [(pos, paragraph) for pos, paragraph in cuts if len(text[start:pos].strip()) >= 12 and len(text[pos:].strip()) >= 12*(count-index)]
        if not candidates:
            break
        desired = start+(len(text)-start)/(count-index+1)
        end, _ = min(candidates, key=lambda p: abs(p[0]-desired)-(8 if p[1] else 0))
        chosen.append(text[start:end].strip()); start = end
    chosen.append(text[start:].strip())
    return chosen


def interval(text, minimum, maximum):
    # Longer segments tend to take longer, with a small bounded variation.
    position = min(1.0, len(text)/70)
    fraction = max(0.0, min(1.0, .2+.6*position+random.uniform(-.2, .2)))
    return minimum+(maximum-minimum)*fraction


def fingerprint(text):
    return hashlib.sha256(''.join(c for c in text.lower() if c.isalnum()).encode()).hexdigest()


def recover(db):
    for item in db.scoped_objects('reply_delivery', statuses=('pending', 'waiting', 'sending'), limit=None):
        item.update(status='unknown' if item['status'] == 'sending' else 'interrupted', finished_at=time.time(), reason='服务重启，未补发剩余内容')
        db.put('reply_delivery', item['id'], item)
        trace = db.get('trace', item.get('trace_id')) if item.get('trace_id') else None
        if trace and trace.get('status') == 'running':
            trace.update(status=item['status'], duration_ms=round((time.time()-trace['ts'])*1000), recovery_notice=item['reason'])
            db.put('trace', trace['id'], trace)


async def deliver(engine, scope, persona, target, batch, card, reply, preview, executed, decision, current, proactive, reviewed=(), review=None):
    from . import messaging
    db = engine.db
    plan = decision.get('delivery_plan')
    group = db.get('group', scope, {})
    config = {**DEFAULTS, **group}
    enabled = bool(config['segmented_reply'] and ':g:' in scope)
    limits = runtime_config(db, scope)
    state = db.get('chat_state', scope, {})
    slots = send_slots(state, limits['send_per_minute'])
    sticker_pending = plan.get('sticker_id') if plan else None
    # A sticker is a separate actual message, including when text segmentation
    # is off. Reserve its slot by reducing text parts, never by mixing segments.
    text_slots = max(1, slots-bool(sticker_pending))
    parts = (split_reply(reply, config['segment_threshold'], min(text_slots, config['segment_max_parts']))
             if enabled else [reply]) if reply else []
    if not parts and not sticker_pending:
        parts = [reply]  # Preserve mentions-only handling and empty-message checks.
    item = {'id': uid(), 'scope': scope, 'persona': persona, 'target_id': target['id'], 'ts': time.time(),
            'status': 'pending', 'planned_count': len(parts)+bool(sticker_pending),
            'sent_ids': [], 'sent_count': 0, 'trace_id': tracing.active_trace.get()}
    db.put('reply_delivery', item['id'], item)
    tracing.stage(db, 'delivery', {'id': item['id'], 'planned_count': item['planned_count']}, content=False)
    review = review or ReplyReview(engine, scope, target, batch, card, reply, reviewed)
    epoch = engine.memory.epoch(scope)
    remember = review.remember
    provenance = engine.reply_provenance(scope, persona, preview, batch, executed)
    sent_text, relevant_sources, inflight = [], review.sources, False
    rate_waited = 0.0

    def finish(status, reason=''):
        item.update(status=status, reason=reason, finished_at=time.time())
        db.put('reply_delivery', item['id'], item)
        tracing.stage(db, 'delivery_end', {'id': item['id'], 'status': status, 'sent_count': item['sent_count'], 'reason': reason}, content=False)
        if status != 'sent':
            db.delete('followup', scope+':'+persona)
        return status

    def stopped():
        return (engine.stopping or not current() or time.time()-target['ts'] > decision.get('reply_timeout',120) or
                (enabled and not db.get('group', scope, {}).get('segmented_reply', False)))

    async def check_plan_permissions():
        await engine.tools.check_delivery(scope, plan)

    async def capacity():
        nonlocal rate_waited, sticker_pending
        while not send_slots(db.get('chat_state', scope, {}), runtime_config(db, scope)['send_per_minute']):
            # Optional images must not hold the next requested text reply in a queue.
            if sent_text and not parts and sticker_pending:
                sticker_pending = None
                tracing.stage(db, 'sticker_deferred', {'reason': '正文已发送，额度优先留给新请求；附图本轮省略'}, content=False)
                return False
            if proactive or rate_waited >= limits['send_wait_seconds'] or stopped():
                return False
            recent = [t for t in db.get('chat_state', scope, {}).get('sent', []) if t > time.time()-SEND_WINDOW]
            delay = min(2.0, max(.05, min(recent)+SEND_WINDOW-time.time()+.05), limits['send_wait_seconds']-rate_waited)
            item.update(status='waiting', wait_reason='send_rate', rate_wait_seconds=round(rate_waited,2))
            db.put('reply_delivery', item['id'], item)
            if not rate_waited:
                tracing.stage(db, 'delivery_rate_wait', {'limit': limits['send_per_minute'], 'window_seconds': SEND_WINDOW}, content=False)
            await engine.delivery_sleep(delay)
            rate_waited += delay
        return not stopped()

    try:
        while parts or sticker_pending:
            if item['sent_count']:
                delay = interval(parts[0] if parts else '', config['segment_interval_min'], config['segment_interval_max'])
                item['status'] = 'waiting'; db.put('reply_delivery', item['id'], item)
                tracing.stage(db, 'delivery_wait', {'seconds': round(delay, 2), 'next_part': item['sent_count']+1}, content=False)
                await engine.delivery_sleep(delay)
            if stopped():
                return finish('partial' if item['sent_count'] else 'cancelled', '会话、角色、记忆或发送开关已变化，或回复已过时')
            if not await capacity():
                if not parts and not sticker_pending:
                    return finish('sent', '正文已发出，附带表情因额度不足省略')
                return finish('partial' if item['sent_count'] else 'cancelled' if stopped() else 'rate_limited',
                              '发送等待期间状态变化或超过等待上限' if not proactive else '主动回复未等待发送额度')
            if not await review.check(lambda: not stopped(), sent=sent_text,
                    remaining=parts+(['[待发送表情]'] if sticker_pending else []),
                    sent_ids=item['sent_ids'], segmented=True):
                return finish('partial' if item['sent_count'] else 'stale', '相关插话复核未通过或会话状态变化')
            # Normal model replies can contain authorized image/forward reads too.
            # Refresh the whole audience before their current() guards run.
            await engine.permissions.refresh(scope, {m['user_id'] for m in batch})
            remember = remember and review.remember
            if stopped():
                return finish('partial' if item['sent_count'] else 'cancelled', '发送前状态已变化')
            state = db.get('chat_state', scope, {})
            state['sent'] = [t for t in state.get('sent', []) if t > time.time()-60]
            slots = send_slots(state, runtime_config(db, scope)['send_per_minute'])
            if slots <= 0:
                continue
            text_slots = max(1, slots-bool(sticker_pending))
            if len(parts) > text_slots:
                parts = split_reply('\n\n'.join(parts), 0, text_slots)
                tracing.stage(db, 'segments_merged', {'remaining_count': len(parts), 'available_slots': slots}, content=False)
            text = parts[0] if parts else ''
            sticker_id = sticker_pending if not parts else None
            quoted = item['sent_count'] == 0 and engine.should_quote(scope, target, batch)
            if plan:
                await check_plan_permissions()
                mentions = await messaging.recipients(engine.onebot, scope, plan.get('mentions', []) if not item['sent_count'] else [], plan['actor'])
                if stopped(): return finish('partial' if item['sent_count'] else 'cancelled', '发送前状态变化')
                await check_plan_permissions()
                if stopped(): return finish('partial' if item['sent_count'] else 'cancelled', '发送前状态变化')
                if sticker_pending:
                    # Validate before the caption too, and again after waiting.
                    sticker = messaging.available_sticker(db, scope, sticker_pending)
                    if not messaging.sticker_path(engine.settings, sticker).is_file():
                        raise ValueError('表情文件不存在')
                outgoing = messaging.segments(db, engine.settings, scope, text, mentions, sticker_id)
            else:
                outgoing = messaging.segments(db, engine.settings, scope, text)
            if quoted:
                outgoing.insert(0, {'type': 'reply', 'data': {'id': target['message_id']}})
            # Reserve the actual send attempt before I/O. A crash/unknown result
            # consumes a slot and marks the source answered, never causing a replay.
            state = db.get('chat_state', scope, {})
            state['sent'] = [t for t in state.get('sent', []) if t > time.time()-60]
            if len(state['sent']) >= runtime_config(db, scope)['send_per_minute']:
                continue
            state['sent'].append(time.time())
            if not item['sent_count']:
                state['answered'] = ([x for x in state.get('answered', []) if x['ts'] > time.time()-3600] + [{'id': target['id'], 'ts': time.time()}])[-100:]
                if proactive:
                    state['proactive'] = [t for t in state.get('proactive', []) if t > time.time()-3600]+[time.time()]
                    state['last_proactive'] = time.time()
            item.update(status='sending', current_part=item['sent_count']+1)
            with db.transaction():
                db.put('chat_state', scope, state); db.put('reply_delivery', item['id'], item)
            inflight = True
            try:
                result = await engine.onebot.send(scope, outgoing)
                if not result or result.get('message_id') is None:
                    raise ActionUnknown('未收到消息编号，发送结果无法确认')
            except ActionUnknown:
                inflight = False
                return finish('partial_unknown' if item['sent_count'] else 'unknown', '发送结果不明，停止后续分段且不自动重试')
            except Exception:
                inflight = False
                return finish('partial_failed' if item['sent_count'] else 'failed', '发送失败，停止后续分段且不自动重试')
            inflight = False
            id = str(result['message_id'])
            if parts:
                sent_text.append(text); parts.pop(0)
            else:
                sticker_pending = None
            item['sent_ids'].append(id); item['sent_count'] += 1
            item['status'] = 'waiting' if parts or sticker_pending else 'sent'
            # Forgetting during an in-flight send must not put its text back in storage.
            remember = remember and epoch == engine.memory.epoch(scope)
            metadata = {'message_id': id, 'name': card.get('name', '机器人'), 'replies': [target['message_id']] if quoted else [],
                        'types': list(dict.fromkeys(s['type'] for s in outgoing)), 'trace_id': tracing.active_trace.get(),
                        **provenance, 'delivery_id': item['id'], 'part': item['sent_count']}
            metadata['source_ids'] = sorted(set(metadata['source_ids']) | {m['id'] for m in relevant_sources})
            if sticker_id:
                metadata.update(sticker_id=sticker_id, image_count=1)
            state = db.get('chat_state', scope, {})
            state['reply_hashes'] = [r for r in state.get('reply_hashes', []) if r['ts'] > time.time()-120 and r.get('delivery_id') != item['id']]
            if remember and sent_text:
                state['reply_hashes'].append({'hash': fingerprint(''.join(sent_text)), 'ts': time.time(), 'delivery_id': item['id']})
            with db.transaction():
                db.add_message(scope+':'+id, scope, persona, engine.onebot.self_id, 'assistant', text if remember else '', metadata, excluded=not remember)
                if remember and metadata.get('sticker_id'):
                    from .attachments import register
                    register(db, scope, persona, db.message(scope, scope+':'+id, persona))
                db.put('reply_delivery', item['id'], item); db.put('chat_state', scope, state)
                from .working_context import evidence
                engine.memory.participation.record(scope, persona,
                    evidence(preview, [*batch, *relevant_sources], executed) | {scope+':'+id} if remember else (), epoch)
            tracing.stage(db, 'sent', {'message_id': id, 'text': text if remember else '',
                **({'sticker_id': sticker_id} if sticker_id and remember else {}),
                'target_id': target['id'], 'quoted': quoted, 'delivery_id': item['id'], 'part': item['sent_count']})
        if current() and decision.get('expects_followup') is True:
            db.put('followup', scope+':'+persona, {'target': target['user_id'], 'source_id': target['id'], 'topic': str(decision.get('topic', ''))[:120] if remember else '', 'expires': time.time()+group.get('followup_seconds', 120)})
        else:
            db.delete('followup', scope+':'+persona)
        return finish('sent')
    except asyncio.CancelledError:
        finish('unknown' if inflight else 'interrupted', '服务停止，未补发剩余内容')
        raise
    except Exception:
        finish('unknown' if inflight else 'partial_failed' if item['sent_count'] else 'failed', '发送流程中断，未补发剩余内容')
        raise
