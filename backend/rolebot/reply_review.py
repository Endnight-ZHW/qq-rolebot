"""Review only new messages connected to the reply's bound request."""
import json
import re

from . import prompts, tracing
from .db import tokens
from .model import parse_json


def field(message, key, default=None):
    return message.get(key, message.get('data', {}).get(key, default))


def identifiers(scope, values):
    return {str(x) if str(x).startswith(scope+':') else scope+':'+str(x) for x in values if x}


def plain(message):
    text = field(message, 'plain_text', message.get('text', ''))
    return re.sub(r'\[(?:回复消息:[^\]]*|@QQ:[^\]]*|图片|其他消息|QQ表情:[^\]]*)\]', '', text).strip()


def relation(engine, message, target, card, reply, sources=(), sent_ids=()):
    scope = target['scope']
    if message.get('scope') != scope or message['user_id'] == engine.onebot.self_id:
        return None
    anchors = identifiers(scope, [target['id'], *field(target, 'replies', []),
                                  *(m['id'] for m in sources), *sent_ids])
    references = identifiers(scope, field(message, 'replies', []))
    if references & anchors:
        return 'reply_to_request'
    mentions = set(field(message, 'mentions', []))
    # A reply to another thread, or an @ directed elsewhere, is not an update
    # to this request just because the same person wrote it.
    if references or (mentions and not mentions.intersection({engine.onebot.self_id, target['user_id']})):
        return None
    if message['user_id'] == target['user_id']:
        return 'same_requester'
    text = plain(message)
    if not text:
        return None
    if target['user_id'] in mentions:
        return 'mentions_requester'
    correction = bool(re.search(r'别说了|不用说了|别发|不要发|先停|等等|取消|说错|纠正|更正|不对|不是|应该是|改成|换成', text))
    if correction and engine.direct([message], card):
        return 'addressed_correction'
    common = {'什么', '这个', '那个', '觉得', '可以', '知道', '一下', '今天', '怎么', '时候',
              '我们', '你们', '没有', '但是', '还是', '的话', '帮我', '帮忙', '看看', '搜一',
              '搜索', '图片', '人物', '角色', '是谁', '一下这', '', ''}
    def terms(value):
        for alias in sorted(filter(None, card.get('aliases', [])), key=len, reverse=True):
            value = value.replace(alias, '')
        return {w for w in tokens(value).split() if len(w) >= 2 and w not in common}
    overlap = terms(plain(target)+' '+reply) & terms(text)
    if len(overlap) >= 2 or (overlap and correction) or any(re.fullmatch(r'[a-z0-9_]{3,}', w) for w in overlap):
        return 'topic_overlap'
    return None


def describe(message):
    return {'id': message['id'], 'message_id': field(message, 'message_id', ''),
            'qq': message['user_id'], 'text': message.get('text', '')[:1500],
            'replies': field(message, 'replies', []), 'mentions': field(message, 'mentions', []),
            'image_count': max(len(message.get('images', [])), field(message, 'image_count', 0) or 0),
            'ts': message['ts']}


class ReplyReview:
    def __init__(self, engine, scope, target, batch, card, reply, reviewed=()):
        self.engine, self.db, self.scope = engine, engine.db, scope
        self.target, self.card, self.reply = target, card, reply
        self.seen = {m['id'] for m in [*batch, *reviewed]}
        self.start = max(m['ts'] for m in batch)
        self.sources = list(reviewed)
        self.remember = all(m.get('remember', True) for m in [*batch, *reviewed])

    def redact(self):
        trace = self.db.get('trace', tracing.active_trace.get())
        if trace:
            trace.update(redacted=True, context=None, stages=[], prompts=[])
            self.db.put('trace', trace['id'], trace)
            from .call_records import Records
            Records(self.db).redact(trace_id=trace['id'])

    async def check(self, current, *, sent=(), remaining=None, sent_ids=(), segmented=False):
        key = 'reply.segment_review' if segmented else 'reply.review'
        for attempt in range(3):
            if not current():
                return False
            new = [m for m in self.engine.recent_events.get(self.scope, [])
                   if m['id'] not in self.seen and m['ts'] >= self.start]
            self.seen.update(m['id'] for m in new)
            related = []
            for message in new:
                reason = relation(self.engine, message, self.target, self.card, self.reply, self.sources, sent_ids)
                if reason:
                    related.append({**describe(message), 'relation': reason,
                                    'same_requester': message['user_id'] == self.target['user_id']})
                    self.sources.append(message)
                    self.remember = self.remember and message.get('remember', True)
            if not related:
                return True
            if not self.remember:
                self.redact()
            if attempt == 2:
                tracing.stage(self.db, 'review_limit', {'reason': '连续相关插话，停止旧草稿'}, content=False)
                return False
            payload = {'target': self.target['text'], 'request': describe(self.target),
                       'draft': self.reply, 'sent': list(sent),
                       'sent_message_ids': list(sent_ids),
                       'remaining': list(remaining) if remaining is not None else [self.reply],
                       'new_messages': related[-8:]}
            try:
                response = await self.engine.model.chat(self.scope, 'decision', [
                    {'role': 'system', 'content': prompts.render(self.db, self.scope, key)},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                    json_mode=True, stage=key)
                result = parse_json(response.get('content'))
                permit = result.get('send') is True
                reason = str(result.get('reason', ''))[:300]
            except Exception:
                permit, reason = False, '复核失败，停止剩余草稿'
            tracing.stage(self.db, 'segment_review' if segmented else 'review', {
                'send': permit, 'reason': reason, 'source_ids': [m['id'] for m in related]})
            if not permit or not current():
                return False
            # Recheck only unseen related messages after model I/O. New unrelated
            # traffic cannot invalidate a positive review or cause another call.
        return False
