"""Bounded, process-local suppression before chat, tools, or memory see an input."""
from collections import OrderedDict
import hashlib
import json
import time
import unicodedata


WINDOW_SECONDS = 60


def content_key(message, bot_id):
    """Keep segment boundaries and targets; never equate different media by a label."""
    segments = message['event'].get('message', [])
    if isinstance(segments, str):
        segments = [{'type': 'text', 'data': {'text': segments}}]
    parts, text = [], []

    def flush():
        value = ''.join(text).strip()
        text.clear()
        if value and any(not c.isspace() and unicodedata.category(c) != 'Cf' for c in value):
            parts.append(['text', value])

    for segment in segments:
        kind, data = segment.get('type'), segment.get('data', {})
        if kind == 'text':
            text.append(str(data.get('text', '')))
        elif kind in ('at', 'reply'):
            flush()
            parts.append([kind, str(data.get('qq' if kind == 'at' else 'id', ''))])
        else:
            return None, None  # Images, faces, voice, files, and unknown types remain distinct.
    flush()
    if not parts or all(p == ['at', bot_id] for p in parts):
        return '空白消息或仅 @ 机器人', None
    if not any(p[0] == 'text' for p in parts):
        return None, None  # A quote or another member's mention can itself carry meaning.
    return None, hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).digest()


class IncomingFilter:
    def __init__(self, *, clock=time.monotonic, max_entries=8192):
        self.clock, self.max_entries = clock, max_entries
        self.accepted = OrderedDict()

    def check(self, message, bot_id, generation):
        now = self.clock()
        while self.accepted and next(iter(self.accepted.values())) <= now-WINDOW_SECONDS:
            self.accepted.popitem(last=False)
        if ':g:' not in message['scope'] or message['event'].get('request_type'):
            return None, None
        reason, digest = content_key(message, bot_id)
        if reason or digest is None:
            return reason, None
        key = (message['scope'], message['user_id'], *generation, digest)
        if key in self.accepted:
            return '60 秒内同一成员重复文字', None
        return None, key

    def remember(self, key):
        # Only successfully queued inputs start the window. Retries while paused,
        # disconnected, or queue-full must not suppress the first usable request.
        if key is None:
            return
        self.accepted.pop(key, None)
        self.accepted[key] = self.clock()
        while len(self.accepted) > self.max_entries:
            self.accepted.popitem(last=False)
