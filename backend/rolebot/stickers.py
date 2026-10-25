"""One shared sticker library with scoped provenance and original animations."""
import asyncio
import base64
import hashlib
import io
import time
from urllib.parse import urljoin

import httpx
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

from .config_schema import FIELDS
from . import prompts
from .db import uid, tokens
from .media import public_url, normalize_image
from .messaging import sticker_path
from .model import parse_json


LIBRARY_DEFAULTS = {v['key']:v['default'] for v in FIELDS.values() if v['scope']=='library'}


def origins(item):
    # Legacy scope is provenance, no longer a visibility boundary. Keep its ID/file.
    return item.get('origins') or [{k: item.get(k, '' if k == 'scope' else [])
                                   for k in ('scope', 'source_ids', 'actor_ids')}]


class StickerSave(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    message_id: str = Field(min_length=1, max_length=120)
    index: int = Field(default=0, ge=0, le=199)


def inspect_image(raw, limit=5*1024*1024):
    if len(raw) > limit: raise ValueError('表情文件超过当前大小限制')
    with Image.open(io.BytesIO(raw)) as im:
        fmt = im.format
        if fmt not in ('JPEG', 'PNG', 'GIF', 'WEBP'): raise ValueError('仅支持 JPEG、PNG、GIF、WebP')
        frames = getattr(im, 'n_frames', 1)
        if im.width*im.height*frames > 80_000_000 or frames > 300:
            raise ValueError('表情尺寸或动画帧数过大')
        first = None
        for i in range(frames):
            im.seek(i); im.load()
            if i == 0:
                first = hashlib.sha256(im.convert('RGBA').tobytes()+str(im.size).encode()).hexdigest()
    return {'ext': {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}[fmt],
            'mime': Image.MIME[fmt], 'animated': frames > 1, 'sha256': hashlib.sha256(raw).hexdigest(),
            'visual_hash': first if frames == 1 else None, 'bytes': len(raw)}


async def download(url, limit):
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        for attempt in range(4):
            await public_url(url, qq_only=True)
            async with client.stream('GET', url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('location')
                    if not location or attempt == 3: raise ValueError('图片地址无效')
                    url = urljoin(url, location); continue
                response.raise_for_status()
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > limit: raise ValueError('表情文件超过当前大小限制')
                    chunks.append(chunk)
                return b''.join(chunks)
    raise ValueError('图片不可读取')


class Stickers:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.running = False

    def config(self):
        return {**LIBRARY_DEFAULTS, **self.db.get('sticker_library', 'global', {})}

    def enqueue(self, scope, persona, batch):
        g = self.db.get('group', scope, {})
        if not g.get('interaction_enabled') or not g.get('sticker_autocollect'): return
        for m in batch:
            if not m.get('remember', True): continue
            for index in range(min(3, len(m.get('images', [])))):
                id = m['id']+':'+str(index)
                if not self.db.get('sticker_checked', id) and not self.db.get('sticker_job', id):
                    self.db.put('sticker_job', id, {'id': id, 'scope': scope, 'persona': persona,
                        'source_ids': [m['id']], 'actor_ids': [m['user_id']], 'index': index, 'status': 'pending', 'ts': time.time()})

    def tick(self):
        if self.running: return
        job = next((x for x in self.db.all('sticker_job') if x['status'] in ('pending', 'running')), None)
        if job:
            self.running = True
            self.engine.spawn(self.process(job))

    async def process(self, job):
        from . import tracing
        if not self.db.get('sticker_job', job['id']):
            self.running = False
            return
        id, token = tracing.begin(self.db, job['scope'], job['persona'],
            [{'id': job['source_ids'][0], 'user_id': job['actor_ids'][0], 'remember': True}], kind='sticker_collection')
        status = 'skipped'
        try:
            job['status'] = 'running'; self.db.put('sticker_job', job['id'], job)
            result = await self.save(job['scope'], job['persona'], job['source_ids'][0], job['index'], automatic=True)
            status = 'complete'; tracing.stage(self.db, 'sticker_collection', result)
        except asyncio.CancelledError:
            status = 'pending'; raise
        except Exception as exc:
            tracing.stage(self.db, 'sticker_collection', {'status': 'skipped', 'error_type': type(exc).__name__})
        finally:
            if self.db.get('sticker_job', job['id']):
                job['status'] = status; self.db.put('sticker_job', job['id'], job)
            tracing.finish(self.db, id, status, token)
            self.running = False

    def list(self, scope, query=''):
        items = [x for x in self.db.all('sticker') if x.get('enabled', True)]
        if query:
            wanted = set(tokens(query).split())
            scored = [(len(wanted & set(tokens(' '.join(str(x.get(k, '')) for k in ('name', 'tags', 'description', 'usage'))).split())), x) for x in items]
            items = [x for score, x in sorted(scored, key=lambda pair: pair[0], reverse=True) if score]
        return [{k: x.get(k) for k in ('id', 'name', 'tags', 'description', 'usage', 'animated')} for x in items[:30]]

    def candidates(self, scope, persona, batch, query=''):
        """Small, authorized shared-library menu; no source identities or extra model call."""
        group = self.db.get('group', scope, {})
        if not group.get('interaction_enabled') or not batch:
            return []
        grants = [set(self.engine.permissions.resolve(scope, m['user_id'])['allow']) for m in batch]
        def permitted(name):
            policy = self.engine.tools.policy(scope, name)
            return (policy.get('enabled') and (policy.get('user_enabled', True) or policy.get('autonomous_enabled', True))
                    and all('tool:'+name in allowed for allowed in grants))
        if not any(permitted(n) for n in ('sticker_list', 'sticker_search')) or not any(
                permitted(n) for n in ('message_send', 'sticker_send')):
            return []
        recent = {str(row['sticker_id']) for row in self.db.rows(
            "SELECT json_extract(data,'$.sticker_id') AS sticker_id FROM messages "
            "WHERE scope=? AND persona=? AND role='assistant' AND excluded=0 AND ts>? "
            "AND json_extract(data,'$.sticker_id') IS NOT NULL ORDER BY ts DESC LIMIT 12",
            (scope, persona, time.time()-600))}
        wanted = set(tokens(query).split())
        choices = []
        for item in self.db.all('sticker'):
            if not item.get('enabled', True):
                continue
            try:
                if not sticker_path(self.engine.settings, item).is_file():
                    continue
            except ValueError:
                continue
            score = len(wanted & set(tokens(' '.join(str(item.get(k, '')) for k in ('name', 'tags', 'description', 'usage'))).split()))
            choices.append((score, item['id'] in recent, item))
        choices.sort(key=lambda x: (-x[0], x[1], x[2]['id']))
        return [{**{key: str(item.get(key, ''))[:limit] for key, limit in
                    (('id', 80), ('name', 80), ('tags', 100), ('description', 160), ('usage', 120))},
                 'animated': bool(item.get('animated')), 'recently_used': used}
                for _, used, item in choices[:12]]

    def register(self, scope, raw, metadata, source_ids=(), actor_ids=()):
        info = inspect_image(raw, self.config()['sticker_max_mib']*1024*1024)
        origin = {'scope': scope, 'source_ids': list(source_ids), 'actor_ids': list(actor_ids)}
        # Registration is synchronous and serialized across groups, including capacity checks.
        with self.db.lock:
            config = self.config()
            if len(raw) > config['sticker_max_mib']*1024*1024:
                raise ValueError('表情文件超过当前大小限制')
            items = self.db.all('sticker')
            for x in items:
                if x.get('sha256') == info['sha256'] or (info['visual_hash'] and x.get('visual_hash') == info['visual_hash']):
                    # Disabled duplicates stay disabled; collecting cannot undo an administrator's choice.
                    sources = origins(x)
                    source = next((s for s in sources if s['scope'] == scope), None)
                    if source is None:
                        sources.append(origin)
                    else:
                        for key in ('source_ids', 'actor_ids'):
                            source[key] = sorted(set(source.get(key, [])) | set(origin[key]))
                    x['origins'] = sources
                    # Keep the original scope's legacy fields usable by older releases.
                    if x.get('scope', '') == scope:
                        for key in ('source_ids', 'actor_ids'):
                            x[key] = sorted(set(x.get(key, [])) | set(origin[key]))
                    self.db.put('sticker', x['id'], x)
                    return {**x, 'duplicate': True}
            if len(items) >= config['sticker_limit']: raise ValueError('共享表情库已达上限')
            item = {**info, **{k: str(metadata.get(k, ''))[:500] for k in ('name', 'tags', 'description', 'usage')},
                    'id': uid(), **origin, 'origins': [origin], 'enabled': True, 'created_at': time.time()}
            path = sticker_path(self.engine.settings, item); path.parent.mkdir(exist_ok=True)
            path.write_bytes(raw)
            self.db.put('sticker', item['id'], item)
            return item

    async def save(self, scope, persona, message_id, index=0, automatic=False):
        e = self.engine
        epoch = e.memory.epoch(scope)
        def retained():
            g = self.db.get('group', scope, {})
            if not g.get('interaction_enabled') or (automatic and not g.get('sticker_autocollect')) or not e.current(scope, persona, epoch):
                raise ValueError('会话、遗忘状态或收藏开关已变化')
            row = e.vision.retained(scope, persona, message_id)
            if not row: raise ValueError('原图片已遗忘、过期或停止记忆')
            return row
        source = retained()
        key = source['id']+':'+str(index)
        seen = self.db.get('sticker_checked', key)
        if automatic and seen: return {'skipped': True}
        urls = await e.vision.refresh(scope, persona, source, epoch)
        if index >= len(urls) or not urls[index]: raise ValueError('原消息没有该图片')
        limit = self.config()['sticker_max_mib']*1024*1024
        raw = await download(urls[index], limit)
        await asyncio.to_thread(inspect_image, raw, limit)
        retained()
        block = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+base64.b64encode(await asyncio.to_thread(normalize_image, raw)).decode()}}
        response = await e.model.chat(scope, 'chat', [{'role': 'system', 'content': prompts.render(self.db, scope, 'sticker.classify')},
                                      {'role': 'user', 'content': [block]}], json_mode=True, stage='sticker.classify')
        result = parse_json(response.get('content'))
        if not automatic:
            from .tools import invocation
            caller = invocation.get()
            if caller and caller['name'] == 'sticker_save':
                await e.permissions.check_tool_fresh(scope, caller['actor'], 'sticker_save', {})
                e.tools.check_policy(scope, 'sticker_save', {})
        retained()
        allowed = result.get('is_sticker') is True and all(result.get(k) is False for k in ('contains_photo', 'contains_private_info', 'is_screenshot'))
        self.db.put('sticker_checked', key, {'id': key, 'scope': scope, 'source_ids': [source['id']], 'actor_ids': [source['user_id']], 'accepted': allowed, 'ts': time.time()})
        if not allowed: return {'saved': False, 'reason': '图片未通过表情筛选；普通照片、截图或内容不明不会自动入库'}
        item = self.register(scope, raw, result, [source['id']], [source['user_id']])
        from .attachments import register
        attachment = register(self.db, scope, persona, source)[index]
        self.engine.attachments.store(attachment, await asyncio.to_thread(normalize_image, raw), result.get('description'), item)
        self.db.audit(scope, 'sticker_collected', {'id': item['id'], 'source_id': source['id'], 'automatic': automatic, 'duplicate': item.get('duplicate', False)})
        return {'saved': True, 'id': item['id'], 'name': item['name'], 'enabled': item['enabled'], 'duplicate': item.get('duplicate', False),
                'image_id': attachment['id'], 'description': item.get('description', '')}

    async def collect(self, scope, persona, batch):
        g = self.db.get('group', scope, {})
        if not g.get('interaction_enabled') or not g.get('sticker_autocollect'): return
        count = 0
        for m in batch:
            if not m.get('remember', True): continue
            for index in range(min(3, len(m.get('images', [])))):
                if count >= 3: return
                count += 1
                try:
                    await self.save(scope, persona, m['id'], index, automatic=True)
                except Exception as exc:
                    self.db.audit(scope, 'sticker_collection', {'source_id': m['id'], 'status': 'skipped', 'error_type': type(exc).__name__})

    def cleanup(self):
        keep = {sticker_path(self.engine.settings, x).name for x in self.db.all('sticker')}
        root = self.engine.settings.data_dir/'stickers'
        if root.exists():
            for path in root.iterdir():
                if path.is_file() and path.name not in keep: path.unlink()
