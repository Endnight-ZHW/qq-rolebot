"""Ground explicit live search requests in this turn's bound tool executions."""
import json
import re

from . import prompts, tracing
from .model import parse_json


def explicit_search(text, aliases=()):
    """Conservative imperative detection, not a classifier for all retrieval.

    Only the caller's own current text is inspected. Ambiguous phrasing stays
    with the main model; quoted instructions, past reports and local lookups
    must not become compulsory web searches.
    """
    text = re.sub(r'\[回复消息:[^\]]*\]|\[CQ:[^\]]*\]', '', text)
    text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|`[^`]*`', '', text)
    # Let the semantic prompt resolve cancellations and mixed instructions;
    # a positive phrase earlier in the same message must not override them.
    if re.search(r'(?:不要|不用|无需|不必|先别|别|取消|停止).{0,6}(?:搜|查|联网|上网)|'
                 r'(?:搜索|搜图|查询).{0,3}(?:不用|取消|停止)|算了|不用了', text):
        return False
    for clause in re.split(r'[，,。！？!?；;\n]', text):
        clause = clause.strip()
        for alias in sorted(filter(None, aliases), key=len, reverse=True):
            if clause.startswith(alias):
                clause = clause[len(alias):].lstrip(' ，,：:')
                break
        match = re.match(
            r'^(?:(?:请问|请|麻烦你?|能不能|能否|可以|帮我|帮忙|给我|你|再|去)\s*)*'
            r'(?P<web>联网|上网|在网上|去网上|网上|百度|谷歌)?\s*'
            r'(?P<verb>搜索|搜一搜|搜搜|搜|查询|查找|查一查|查查|查)'
            r'(?P<target>.*)$', clause)
        if not match:
            continue
        target = re.sub(r'^(?:一下|下)\s*', '', match['target'].strip())
        if not target:
            continue
        if re.match(r'过|了|到|不到|不出|出来|不出来|完|不着|吗|么|'
                    r'图?(?:功能|工具|接口|能力|是什么|什么意思|会|可以|能否|能不能|怎么|如何|安全吗)', target):
            continue
        if not match['web'] and re.search(r'聊天记录|群聊记录|历史记录|记忆|表情包库|表情库|收藏|群里|之前.*说', target):
            continue
        if match['verb'].startswith('搜') or match['web'] or re.search(r'图片|这张图|这个图|来源|出处', target):
            return True
    return False


class SearchRequests:
    def __init__(self, db, scope, batch, aliases, direct_ids, tools):
        self.db, self.scope, self.batch = db, scope, batch
        self.required = {
            m['id']: m for m in batch if m['id'] in direct_ids and explicit_search(
                m.get('plain_text', m.get('data', {}).get('plain_text', m['text'])), aliases)
        }
        search = next((t for t in tools['tools'] if t['name'] == 'web_search'), {})
        image = next((t for t in tools['tools'] if t['name'] == 'image_search'), {})
        self.image_available = {mid for mid, m in self.required.items()
                                if image.get('enabled') and image.get('user_requests')
                                and m['user_id'] in image.get('authorized_requesters', [])}
        self.available = {mid for mid, m in self.required.items()
                          if search.get('enabled') and search.get('user_requests')
                          and m['user_id'] in search.get('authorized_requesters', [])} | self.image_available
        self.attempted, self.observed, self.retried = set(), set(), False

    def state(self):
        return {'source_ids': list(self.required), 'available_source_ids': sorted(self.available),
                'reverse_image_search_available': bool(self.image_available),
                'image_search_source_ids': sorted(self.image_available)}

    def source_id(self, args):
        if not isinstance(args, dict):
            return None
        value = args.get('source_message_id')
        matches = [m for m in self.batch if value in (m['id'], m['message_id'])] if value else self.batch
        return matches[0]['id'] if len(matches) == 1 else None

    def pending(self):
        return set(self.required) - self.attempted

    def before_tool(self, name, args):
        if name in ('message_send', 'sticker_send') and set(self.required) - self.observed:
            return {'status': 'denied', 'error_code': 'search_required',
                    'error': '当前明确搜索请求尚未执行；先完成绑定真实请求者的 web_search 或 image_search，再准备回复。不可用时说明未能检索。'}
        return None

    def record(self, name, args, result):
        # A malformed/wrongly-bound tool proposal is not a search attempt.
        valid = isinstance(args, dict) and (
            (name == 'web_search' and isinstance(args.get('query'), str) and args['query'].strip()) or
            (name == 'image_search' and isinstance(args.get('image_id'), str) and re.fullmatch(r'img_[0-9a-f]{32}', args['image_id'])))
        if (valid and result.get('error_code') not in
                ('invalid_arguments', 'source_mismatch', 'request_context_unclear', 'request_verification_failed')):
            mid = self.source_id(args)
            if mid in self.required:
                self.attempted.add(mid)

    def finish(self, response, can_retry):
        missing = self.pending()
        if not missing:
            return response, None
        if not self.retried and can_retry and missing <= self.available:
            self.retried = True
            tracing.stage(self.db, 'search_missing', {'source_ids': sorted(missing), 'action': 'retry'}, content=False)
            return response, prompts.render(self.db, self.scope, 'search.retry', source_ids=json.dumps(sorted(missing)))
        # Never send the abandoned draft (including observations) as a verified answer.
        try:
            selected = parse_json(response.get('content')).get('reply_to_message_id')
        except (ValueError, TypeError, AttributeError):
            selected = None
        target = next((m for mid, m in self.required.items() if mid in missing and selected in (mid, m['message_id'])),
                      next(m for mid, m in self.required.items() if mid in missing))
        text = prompts.render(self.db, self.scope, 'search.incomplete' if missing <= self.available else 'search.unavailable')
        tracing.stage(self.db, 'search_missing', {'source_ids': sorted(missing), 'action': 'incomplete_reply'}, content=False)
        return {'role': 'assistant', 'content': json.dumps({
            'speak': True, 'reply_to_message_id': target['id'], 'mode': 'direct',
            'continues_dialogue': False, 'expects_followup': False, 'text': text,
            'reason': '明确搜索请求尚未完成，不发送未经检索的猜测',
        }, ensure_ascii=False)}, None
