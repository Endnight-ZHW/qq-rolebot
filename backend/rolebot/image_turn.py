"""A bounded set of actual images appended after text, shared across tool rounds."""
import asyncio
import base64
import hashlib
import json
import time
from contextvars import ContextVar

from . import tracing
from .attachments import register, visible
from .input_budget import IMAGE_ALLOWANCE, measure, projected
from .media import MediaError

active_images = ContextVar('active_images', default=None)


def multimodal(model):
    return not hasattr(model, 'catalog') or 'image' in model.catalog.resolve('chat')['model']['input_modalities']


class ImageTurn:
    def __init__(self, engine, scope, persona, batch=()):
        self.engine, self.db = engine, engine.db
        self.scope, self.persona, self.batch = scope, persona, list(batch)
        self.epoch = engine.memory.epoch(scope)
        self.native = multimodal(engine.model)
        self.loaded, self.pending, self.failures = {}, [], []
        self.limit = min(self.db.config()['image_max_count'], self.db.config()['image_input_budget']//IMAGE_ALLOWANCE)
        self.references_loaded = {}
        self.read_actors = set()
        self.associations = {}
        self.read_modes = set()
        self.search_attempted = False
        self.search_checks = []
        self.search_sources = set()
        self.search_refs = set()
        self.region_attempted = False
        self.region_sources = set()

    async def add(self, aid, transient=None, reference=None, prepared=None):
        if aid in self.loaded:
            return {'image_id': aid, 'status': 'budget_exceeded' if self.loaded[aid].get('omitted') else 'already_attached'}
        if len(self.loaded) >= self.limit:
            result = {'image_id': aid, 'status': 'budget_exceeded', 'notice': '本轮图片预算已满'}
            self.failures.append(result)
            return result
        try:
            if reference:
                item, block = self.engine.role_images.read(self.persona, reference['id'])
            else:
                item, block = prepared or await self.engine.attachments.read(self.scope, self.persona, aid, self.epoch, transient)
                item = {**item, **getattr(block, 'metadata', {})}
            if not self.current():
                raise MediaError('context_changed', '本轮上下文已变化')
            # Base64 is transport, not text tokens. Bound bytes before adding it.
            used = sum(len(x['block']['image_url']['url']) for x in self.loaded.values())
            if used + len(block['image_url']['url']) > (self.db.config()['image_request_mib']-1)*1024*1024:
                result = {'image_id': aid, 'status': 'budget_exceeded', 'notice': '本轮图片传输预算已满'}
                self.failures.append(result)
                return result
            self.loaded[aid] = {'item': item, 'block': block, 'ephemeral': bool(transient and not transient.get('remember', True))}
            if reference:
                self.references_loaded[aid] = item
            self.pending.append(aid)
            result = {'image_id': aid, 'status': 'attached',
                      **({'reference_persona': self.persona, 'reference_only': True} if reference else {
                          'source_id': item['source_id'],
                          **{k: item[k] for k in ('source_size', 'preview_size', 'source_quality') if k in item}})}
            matches = [r for r in self.engine.role_images.list(self.persona)
                       if r['sha256'] == item.get('reference_sha256', item.get('sha256')) and self.engine.role_images.available(r)] if not reference else []
            if matches:
                result['role_association'] = {'persona': self.persona, 'basis': 'same_image_reference',
                    'confirmation': '与后台上传的角色参考图相同'}
                self.associations[aid] = matches[0]
            self.loaded[aid]['label'] = result
            return result
        except MediaError as exc:
            result = {'image_id': aid, 'status': 'unavailable', 'error_code': exc.code}
            self.failures.append(result)
            return result

    async def prepare(self):
        from .forwards import is_forward
        forward_target = any(is_forward(m) for m in self.batch)
        for message in self.batch:
            for item in register(self.db, self.scope, self.persona, message):
                await self.add(item['id'], message)
        for message in self.batch:
            for mid in message.get('replies', message.get('data', {}).get('replies', []))[:3]:
                row = visible(self.db, self.scope, self.persona, mid)
                if row:
                    forward_target = forward_target or is_forward(row)
                    for item in register(self.db, self.scope, self.persona, row):
                        await self.add(item['id'])
        focus = self.db.get('image_focus', self.scope+':'+self.persona, {})
        from .context import identity
        if (not forward_target and focus.get('expires', 0) > time.time() and self.batch and
            focus.get('identity') == identity(self.db, self.scope, self.persona) and
            {m['user_id'] for m in self.batch} == {focus.get('actor')} and
            not any(q != self.engine.onebot.self_id for m in self.batch for q in m.get('mentions', []))):
            for aid in focus.get('image_ids', [])[:self.limit]:
                await self.add(aid)

    async def read_region(self, image_id, box):
        result = {'image_id': image_id, 'view': 'region', 'region': box}
        if self.region_attempted:
            return {**result, 'status': 'limit_reached', 'error_code': 'region_limit',
                    'notice': '本轮已尝试局部补看，不再次读取'}
        self.region_attempted = True
        if len(self.loaded) >= self.limit:
            return {**result, 'status': 'budget_exceeded', 'notice': '局部图计入现有图片预算，额度已满'}
        try:
            if not self.current():
                raise MediaError('context_changed', '本轮授权或上下文已变化')
            data = self.loaded.get(image_id)
            if data is None:
                transient = next((m for m in self.batch if any(x['id'] == image_id for x in register(
                    self.db, self.scope, self.persona, m))), None)
                item, block = await self.engine.attachments.read(self.scope, self.persona, image_id, self.epoch, transient)
                data = {'item': item, 'block': block, 'ephemeral': bool(transient and not transient.get('remember', True))}
            if not self.current():
                raise MediaError('context_changed', '本轮授权或上下文已变化')
            raw = getattr(data['block'], 'source', None)
            if raw is None:
                raw = base64.b64decode(data['block']['image_url']['url'].split(',', 1)[1], validate=True)
            from .image_assets import region
            block, details = await asyncio.to_thread(region, raw, box)
            if not self.current() or (not data['ephemeral'] and not visible(
                    self.db, self.scope, self.persona, data['item']['source_id'])):
                raise MediaError('context_changed', '本轮授权或图片来源已变化')
            used = sum(len(x['block']['image_url']['url']) for x in self.loaded.values())
            if used+len(block['image_url']['url']) > (self.db.config()['image_request_mib']-1)*1024*1024:
                return {**result, 'status': 'budget_exceeded', 'notice': '局部图超过本轮传输预算'}
            view_id = 'crop_'+hashlib.sha256(json.dumps([image_id, box]).encode()).hexdigest()[:32]
            label = {**result, **details, 'crop_id': view_id, 'status': 'attached',
                     'source_id': data['item']['source_id'],
                     'source_quality': data['item'].get('source_quality', 'stored_preview'),
                     'notice': '这是指定图片的局部原像素，不是新图片；缺失的笔画未被补全，局部内容不能代表整张图'}
            self.loaded[view_id] = {**data, 'block': block, 'label': label, 'region_of': image_id}
            self.region_sources.add(image_id)
            self.pending.append(view_id)
            return label
        except (MediaError, ValueError, OSError) as exc:
            from .media import image_error
            return {**result, 'status': 'unavailable', 'error_code': image_error(exc)}

    async def read_tool(self, image_ids, include_role_references=False, region=None, actor=None, mode='request'):
        self.read_modes.add(mode)
        if actor:
            self.read_actors.add(actor)
        rows = []
        for aid in dict.fromkeys(image_ids):
            transient = next((m for m in self.batch if any(x['id'] == aid for x in register(self.db, self.scope, self.persona, m))), None)
            rows.append(await self.add(aid, transient))
        references = self.engine.role_images.list(self.persona) if include_role_references else []
        if include_role_references:
            for ref in references[:2]:
                row = await self.add(ref['image_id'], reference=ref)
                rows.append({'image_id': ref['image_id'], 'status': row['status'],
                             'reference_persona': self.persona, 'reference_only': True, 'label': ref['label']})
        if region:
            rows.append(await self.read_region(**region))
        from .image_evidence import for_images, track
        evidence = for_images(self.db, self.scope, self.persona, image_ids+([region['image_id']] if region else []))
        track(evidence)
        return {'images': rows, 'image_search_evidence': evidence,
                'notice': 'attached 的原图将进入下一次主模型调用；参考相似不等于确定身份',
                **({'reference_status': 'available' if references else 'not_configured'} if include_role_references else {})}

    def current(self):
        if not self.engine.current(self.scope, self.persona, self.epoch):
            return False
        from .tools import ToolDenied
        try:
            for check in self.search_checks:
                check()
        except ToolDenied:
            return False
        if self.read_modes:
            policy = self.engine.tools.policy(self.scope, 'image_read')
            if not policy.get('enabled') or any(
                (mode in ('request', 'scheduled_admin', 'shared') and not policy.get('user_enabled', True)) or
                (mode in ('assistance', 'shared') and not policy.get('autonomous_enabled', True))
                for mode in self.read_modes):
                return False
            if any('tool:image_read' not in self.engine.permissions.resolve(self.scope, actor)['allow'] for actor in self.read_actors):
                return False
        for ref in [*self.associations.values(), *self.references_loaded.values()]:
            if not self.engine.role_images.current(ref):
                return False
        for aid, data in self.loaded.items():
            if data['ephemeral'] or aid in self.references_loaded:
                continue
            item = data['item']
            if not visible(self.db, item['scope'], self.persona, item['source_id']):
                return False
        return True

    async def flush(self, messages, input_budget=None, schemas=()):
        if not self.pending and not self.failures:
            return
        content = []
        for aid in self.pending:
            data = self.loaded[aid]
            label = ({'image_id': aid, 'reference_persona': self.persona, 'reference_only': True,
                      'label': self.references_loaded[aid]['label']}
                     if aid in self.references_loaded else data['label'])
            candidate = [{'type': 'text', 'text': json.dumps(label, ensure_ascii=False)}, data['block']]
            metrics = measure({'messages': projected(messages)+[{'role': 'user', 'content': content+candidate}], 'tools': schemas})
            if self.native and (input_budget and metrics['estimated_input'] > input_budget):
                self.failures.append({'image_id': aid, 'status': 'budget_exceeded', 'notice': '图片未附入：输入硬预算不足'})
                data['omitted'] = True
                continue
            content.extend(candidate)
        if content and not self.native:
            descriptions = await self.engine.vision.describe_blocks(self.scope, content)
            for observation in descriptions:
                self.engine.attachments.record_observations(self.scope, self.persona, [observation], set(self.loaded))
            content = [{'type': 'text', 'text': json.dumps({'compatibility_image_observations': descriptions,
                'notice': '当前回复模型只支持文字；以下为兼容识图结果，不是原图'}, ensure_ascii=False)}]
        if self.failures:
            content.append({'type': 'text', 'text': json.dumps({'unreadable_images': self.failures}, ensure_ascii=False)})
        if content:
            messages.append({'role': 'user', 'content': content})
        trace_id = tracing.active_trace.get()
        trace = self.db.get('trace', trace_id, {}) if trace_id else {}
        if trace and not trace.get('redacted'):
            dependencies = []
            for aid, data in self.loaded.items():
                if data.get('omitted') or aid in self.references_loaded:
                    continue
                dependencies.append({'scope': data['item']['scope'],
                                     'source_ids': [data['item']['source_id']]})
            trace['image_dependencies'] = dependencies
            trace['role_image_dependencies'] = [{'id': r['id'], 'revision': r['revision']} for r in self.references_loaded.values()]
            self.db.put('trace', trace_id, trace)
        tracing.stage(self.db, 'multimodal_input', {'image_ids': self.pending, 'native': self.native,
                      'unavailable': self.failures, **measure({'messages': projected(messages), 'tools': schemas})}, content=False)
        self.pending, self.failures = [], []

    def observe(self, decision):
        viewed = {aid for aid, data in self.loaded.items() if not data.get('omitted') and aid not in self.references_loaded
                  and not data.get('region_of') and aid not in self.region_sources}
        self.engine.attachments.record_observations(self.scope, self.persona, decision.get('image_observations'), viewed)

    def remember_focus(self, decision):
        key = self.scope+':'+self.persona
        if decision.get('continues_dialogue') is not True:
            self.db.delete('image_focus', key)
            return
        if len({m['user_id'] for m in self.batch}) != 1 or not all(m.get('remember', True) for m in self.batch):
            return
        ids = list(dict.fromkeys(data.get('region_of', aid) for aid, data in self.loaded.items()
            if not data.get('omitted') and aid not in self.references_loaded))
        if ids:
            from .context import identity
            self.db.put('image_focus', key, {'scope': self.scope, 'persona': self.persona, 'image_ids': ids[:self.limit],
                        'actor': self.batch[0]['user_id'], 'expires': time.time()+120,
                        'identity': identity(self.db, self.scope, self.persona)})
