from . import prompts
"""Image observation and bounded recovery for already-retained QQ messages."""
import time

from .media import MediaError
from .model import parse_json
from . import tracing


class Vision:
    def __init__(self, engine):
        self.engine = engine
        self.db = engine.db

    def current(self, scope, persona, epoch):
        return self.engine.current(scope, persona, epoch)

    def retained(self, scope, persona, id):
        row = self.db.message(scope, id, persona)
        if not row or row['role'] != 'user': return None
        if not (row['data'].get('image_count') or 'image' in row['data'].get('types', [])): return None
        if not self.db.get('member', scope+':'+row['user_id'], {}).get('remember', True): return None
        if time.time()-row['ts'] > self.db.config()['media_hours']*3600: return None
        return row

    async def refresh(self, scope, persona, message, epoch):
        row = self.retained(scope, persona, message['id'])
        if not row or not self.current(scope, persona, epoch):
            raise MediaError('source_unavailable', '原消息已遗忘、过期或不可读取')
        id = str(row['data'].get('message_id', row['id'].rsplit(':', 1)[-1]))
        if not id.lstrip('-').isdigit():
            raise MediaError('source_unavailable', '原消息编号不可读取')
        data = await self.engine.onebot.call('get_msg', {'message_id': id})
        if not self.current(scope, persona, epoch) or not self.retained(scope, persona, row['id']):
            raise MediaError('source_unavailable', '原消息已遗忘或不可读取')
        if not isinstance(data, dict):
            raise MediaError('source_unavailable', 'QQ 未返回可用消息')
        owner = str((data or {}).get('user_id', (data or {}).get('sender', {}).get('user_id', '')))
        valid_scope = str((data or {}).get('group_id', '')) == scope.split(':', 2)[2] if ':g:' in scope else owner == scope.split(':', 2)[2] and not (data or {}).get('group_id')
        if not isinstance(data, dict) or str(data.get('message_id')) != id or owner != row['user_id'] or not valid_scope:
            raise MediaError('source_mismatch', 'QQ 返回的图片消息与原群聊或发言人不匹配')
        segments = data.get('message')
        if not isinstance(segments, list):
            raise MediaError('source_unavailable', 'QQ 未返回可用的图片消息')
        return [str(s.get('data', {}).get('url') or '') for s in segments[:200] if s.get('type') == 'image']

    async def describe_blocks(self, scope, contents):
        """Text-only chat compatibility; reuse a configured image-capable route."""
        model = self.engine.model
        purpose = next((p for p in ('chat', 'decision', 'memory') if not hasattr(model, 'catalog') or
                        ('image' in model.catalog.resolve(p)['model']['input_modalities'] and
                         model.catalog.resolve(p)['model']['enabled'] and model.catalog.resolve(p)['provider']['enabled'])), None)
        if purpose is None:
            return [{'status': 'unknown', 'description': '当前已配置路由均不支持图片；仅可使用已有观察'}]
        try:
            response = await model.chat(scope, purpose, [
                {'role': 'system', 'content': prompts.render(self.db, scope, 'vision.compatibility')},
                {'role': 'user', 'content': contents}], json_mode=True, stage='vision.describe')
            result = parse_json(response.get('content'))
            return result.get('images', []) if isinstance(result.get('images'), list) else []
        except Exception as exc:
            tracing.stage(self.db, 'image_error', {'code': 'compatibility_failed', 'type': type(exc).__name__}, content=False)
            return [{'status': 'unknown', 'description': '兼容识图失败，图片内容未知；不能猜测'}]
