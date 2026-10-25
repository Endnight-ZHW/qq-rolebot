"""Source-scoped image records. Binary reads never grant access to another chat."""
import asyncio
import base64
import hashlib
import re
import time

from .media import MediaError, image_error
from .image_assets import VERSION, ImageBlock, prepare
from .messaging import sticker_path


def image_id(message_id, index):
    return 'img_' + hashlib.sha256(f'{message_id}:{index}'.encode()).hexdigest()[:32]


def slots(message):
    data = message.get('data', {})
    return max(len(message.get('images', [])), int(data.get('image_count') or message.get('image_count') or
               (1 if 'image' in data.get('types', message.get('types', [])) else 0)))


def visible(db, scope, persona, mid):
    row = db.message(scope, mid, persona)
    if not row or row['role'] not in ('user', 'assistant') or not db.get('member', scope+':'+row['user_id'], {}).get('remember', True):
        return None
    return row


def register(db, scope, persona, message):
    result = []
    for index in range(min(200, slots(message))):
        aid = image_id(message['id'], index)
        existing = db.get('attachment', aid)
        value = existing or {
            'id': aid, 'scope': scope, 'persona': persona, 'source_id': message['id'],
            'source_ids': [message['id']], 'actor_ids': [message['user_id']], 'index': index,
            'sender': message['user_id'], 'ts': message['ts'], 'status': 'unread',
            **({'sticker_id': message['data']['sticker_id']} if message.get('data', {}).get('sticker_id') else {}),
            **({k:message['data'][k] for k in ('creation_job_id','creation_artifact_id')} if message.get('data',{}).get('creation_artifact_id') else {}),
        }
        if not existing and message.get('remember', True) and visible(db, scope, persona, message['id']):
            db.put('attachment', aid, value)
        result.append(value)
    return result


def linked_sticker(db, item):
    from .stickers import origins
    direct = db.get('sticker', item.get('sticker_id', ''))
    if direct:
        return direct
    # Historical sticker records predate per-image IDs. Only a single-image
    # source is unambiguous; new records use an explicit attachment link.
    row = visible(db, item['scope'], item['persona'], item['source_id'])
    if row and slots(row) == 1:
        return next((s for s in db.all('sticker') if any(
            o.get('scope') == item['scope'] and item['source_id'] in o.get('source_ids', [])
            for o in origins(s))), None)


def descriptors(db, scope, persona, message):
    result = []
    for item in register(db, scope, persona, message):
        sticker = linked_sticker(db, item)
        observation = item.get('observation') or (sticker or {}).get('description')
        if not observation:
            observation = next((d.get('description') for d in message.get('data', {}).get('image_descriptions', [])
                                if d.get('index') == item['index'] and d.get('status') == 'observed'), None)
        expired = time.time() - item['ts'] > db.config()['media_hours'] * 3600
        result.append({'image_id': item['id'], 'source_id': item['source_id'], 'index': item['index'],
                       'sender': item['sender'], 'time': item['ts'],
                       'readability': 'saved_sticker' if sticker else 'expired' if expired else item['status'],
                       **({'observation': observation[:700]} if observation else {}),
                       **({'sticker_id': sticker['id'], 'name': sticker['name']} if sticker else {})})
    return result


class Attachments:
    def __init__(self, engine):
        self.engine, self.db = engine, engine.db
        self.root = engine.settings.data_dir / 'attachments'
        self.root.mkdir(exist_ok=True)

    def path(self, aid):
        paths = self.paths(aid)
        return paths[1] if paths[1].exists() else paths[0]

    def paths(self, aid):
        if not re.fullmatch(r'img_[0-9a-f]{32}', aid):
            raise MediaError('invalid_image_id', '图片编号无效')
        return tuple(self.root / (aid + suffix) for suffix in ('.jpg', '.image', '.source'))

    def item(self, scope, persona, aid):
        item = self.db.get('attachment', aid)
        if not item or item['scope'] != scope or item['persona'] != persona or not visible(self.db, scope, persona, item['source_id']):
            raise MediaError('source_unavailable', '图片已遗忘、过期或不属于本群本角色')
        if item.get('creation_job_id'):
            job=self.db.get('creation_job',item['creation_job_id'],{})
            if job.get('deleted') or not job.get('artifacts') or (not job.get('favorite') and job['expires']<=time.time()):
                raise MediaError('source_unavailable','作品已清理或到保存期限')
        return item

    def store(self, item, raw, observation=None, sticker=None, *, source=None, metadata=None):
        """Persist only while the original message remains visible."""
        if not visible(self.db, item['scope'], item['persona'], item['source_id']):
            return
        if metadata is None:
            block = prepare(source if source is not None else raw)
            raw = base64.b64decode(block['image_url']['url'].split(',', 1)[1], validate=True)
            source, metadata = block.source, {**block.metadata, 'source_quality': 'stored_image'}
        digest = hashlib.sha256(raw).hexdigest()
        if item.get('sha256') and item['sha256'] != digest:
            item.setdefault('legacy_sha256', item['sha256'])
        item.update(metadata, sha256=digest, status='available', error_code=None)
        if sticker:
            item['sticker_id'] = sticker['id']
        elif time.time()-item['ts'] <= self.db.config()['media_hours']*3600:
            _, preview, original = self.paths(item['id'])
            # Keep legacy JPEGs for rollback; all variants share source retention.
            for path, data in ((preview, raw), (original, source)):
                if data is not None:
                    temporary = path.with_suffix(path.suffix+'.tmp')
                    temporary.write_bytes(data)
                    temporary.replace(path)
        if observation:
            item['observation'] = str(observation)[:700]
        self.db.put('attachment', item['id'], item)

    async def download(self, scope, persona, item, epoch, transient=None):
        if item.get('creation_artifact_id'):
            job=self.engine.creations.get(item['creation_job_id'],scope)
            artifact=self.engine.creations.artifact(job,item['creation_artifact_id'])
            if job.get('deleted') or not artifact or job['persona']!=persona:
                raise MediaError('source_unavailable','制作产物已清理')
            return await asyncio.to_thread(prepare,self.engine.creations.artifact_bytes(job,artifact))
        if item.get('forward_location') is not None:
            # Only the observed outer card grants access, never a child sender.
            return await self.engine.media.image(item.get('forward_url', ''), persist=False)
        urls = (transient or {}).get('images') or []
        refreshed = False
        ephemeral = transient is not None and not transient.get('remember', True)
        if item['index'] >= len(urls) or not urls[item['index']]:
            if ephemeral:
                raise MediaError('url_missing', '当前图片没有可读取地址')
            source = visible(self.db, scope, persona, item['source_id'])
            urls = await self.engine.vision.refresh(scope, persona, source, epoch)
            refreshed = True
        try:
            return await self.engine.media.image(urls[item['index']], persist=False)
        except Exception:
            if refreshed or ephemeral:
                raise
            source = visible(self.db, scope, persona, item['source_id'])
            urls = await self.engine.vision.refresh(scope, persona, source, epoch)
            return await self.engine.media.image(urls[item['index']], persist=False)

    async def read(self, scope, persona, aid, epoch, transient=None):
        def current():
            if not self.engine.current(scope, persona, epoch):
                raise MediaError('context_changed', '会话状态已变化')
        current()
        ephemeral = transient is not None and not transient.get('remember', True)
        if ephemeral:
            item = next((x for x in register(self.db, scope, persona, transient) if x['id'] == aid), None)
            if item is None:
                raise MediaError('source_unavailable', '当前图片不存在')
        else:
            item = self.item(scope, persona, aid)
        sticker = None if ephemeral else linked_sticker(self.db, item)
        path = self.path(aid)
        expired = time.time()-item['ts'] > self.db.config()['media_hours']*3600
        try:
            source, metadata = None, None
            if sticker:
                block = await asyncio.to_thread(prepare, sticker_path(self.engine.settings, sticker).read_bytes())
                source, metadata = block.source, {**block.metadata, 'source_quality': 'saved_sticker'}
                raw = base64.b64decode(block['image_url']['url'].split(',', 1)[1], validate=True)
            elif expired:
                raise MediaError('expired', '原图片已超过媒体保存期限')
            elif (not ephemeral and path.suffix == '.image' and path.exists() and item.get('image_processing_version') == VERSION
                  and (item.get('source_quality') != 'legacy_preview' or item.get('source_upgrade_after', 0) > time.time())):
                raw = path.read_bytes()
                original = self.paths(aid)[2]
                source = None if original.exists() else raw
                metadata = {k: item[k] for k in ('image_processing_version', 'source_size', 'preview_size', 'mime',
                    'source_mime', 'source_sha256', 'reference_sha256', 'source_quality', 'source_upgrade_after') if k in item}
                if not original.exists():
                    metadata['source_quality'] = 'stored_preview'
            else:
                try:
                    block = await self.download(scope, persona, item, epoch, transient)
                    raw = base64.b64decode(block['image_url']['url'].split(',', 1)[1], validate=True)
                    source = getattr(block, 'source', None) or raw
                    metadata = getattr(block, 'metadata', None) or (await asyncio.to_thread(prepare, source)).metadata
                    metadata = {**metadata, 'source_quality': 'retained_original', 'source_upgrade_after': 0}
                except Exception:
                    current()
                    if ephemeral or not path.exists():
                        raise
                    # No original available: keep a readable legacy preview, but
                    # never claim its lost detail was recovered. Bound refreshes.
                    raw = path.read_bytes()
                    block = await asyncio.to_thread(prepare, raw)
                    source = raw
                    metadata = {**block.metadata, 'source_quality': 'legacy_preview',
                                'source_upgrade_after': time.time()+300}
            current()
            if not ephemeral:
                self.item(scope, persona, aid)  # Forget/opt-out may happen during IO.
                self.store(item, raw, (sticker or {}).get('description'), sticker, source=source, metadata=metadata)
            else:
                item.update(metadata, sha256=hashlib.sha256(raw).hexdigest())
            original = self.paths(aid)[2]
            source_path = original if not ephemeral and not sticker and original.exists() else None
            return item, ImageBlock(raw, None if source_path else source, metadata, source_path=source_path)
        except Exception as exc:
            code = image_error(exc)
            if not ephemeral and self.engine.current(scope, persona, epoch) and visible(self.db, scope, persona, item['source_id']):
                item.update(status='unavailable', error_code=code)
                self.db.put('attachment', aid, item)
            raise MediaError(code, '图片不可读取：'+code) from exc

    def record_observations(self, scope, persona, observations, viewed):
        if not isinstance(observations, list):
            return
        for entry in observations[:12]:
            if not isinstance(entry, dict) or entry.get('image_id') not in viewed or not isinstance(entry.get('description'), str):
                continue
            try:
                item = self.item(scope, persona, entry['image_id'])
            except MediaError:
                continue
            item['observation'] = entry['description'][:700]
            item['observation_kind'] = 'model_observation'
            self.db.put('attachment', item['id'], item)

    def cleanup(self):
        for item in self.db.all('image_search_cache'):
            if (item.get('ts', 0) < time.time()-86400 or any(
                    not visible(self.db, item['scope'], item['persona'], mid) for mid in item.get('source_ids', []))):
                self.db.delete('image_search_cache', item['id'])
        for item in self.db.all('attachment'):
            expired = time.time()-item['ts'] > self.db.config()['media_hours']*3600
            valid = visible(self.db, item['scope'], item['persona'], item['source_id'])
            if expired or not valid:
                for path in self.paths(item['id']):
                    path.unlink(missing_ok=True)
                    path.with_suffix(path.suffix+'.tmp').unlink(missing_ok=True)
            if expired and 'forward_url' in item:
                item.pop('forward_url')
                self.db.put('attachment', item['id'], item)
            if not valid:
                self.db.delete('attachment', item['id'])
        kept = {x['id'] for x in self.db.all('attachment')}
        for path in self.root.glob('img_*'):
            if path.name.split('.')[0] not in kept:
                path.unlink(missing_ok=True)
