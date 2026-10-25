"""Free-plan-only Lens queries; HTTP success is not an identity confirmation."""
import asyncio
import copy
import hashlib
import html
import io
import json
import logging
import math
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _HideQueryCredentials(logging.Filter):
    def filter(self, record):
        # HTTPX logs the full request URL at INFO; SerpApi requires a query key.
        text = record.getMessage()
        if 'api_key=' in text:
            record.msg = re.sub(r'(api_key=)[A-Za-z0-9_-]+', r'\1[hidden]', text)
            record.args = ()
        return True


logging.getLogger('httpx').addFilter(_HideQueryCredentials())


class LensError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class LensConfigWrite(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    api_key: str = Field(default='', max_length=512)
    clear_key: bool = False
    base_revision: int = Field(ge=0)

    @field_validator('api_key')
    @classmethod
    def key_format(cls, value):
        value = value.strip()
        if value and not re.fullmatch(r'[A-Za-z0-9_-]{16,512}', value):
            raise ValueError('SerpApi 密钥格式无效')
        return value

    @model_validator(mode='after')
    def distinct(self):
        if self.api_key and self.clear_key:
            raise ValueError('不能同时填写和清除密钥')
        return self


def search_image(raw):
    if not raw or len(raw) > 8*1024*1024:
        raise LensError('invalid_image', '请选择不超过 8 MiB 的 JPEG、PNG、WebP 或 GIF 图片', 422)
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in ('JPEG', 'PNG', 'WEBP', 'GIF'):
                raise ValueError('format')
            if original.width*original.height > 20_000_000:
                raise ValueError('pixels')
            original.seek(0)
            im = ImageOps.exif_transpose(original).convert('RGB')
            im.thumbnail((1600, 1600))
            for side in (1600, 1280, 1024, 800):
                im.thumbnail((side, side))
                for quality in (85, 70, 55):
                    buf = io.BytesIO()
                    im.save(buf, 'JPEG', quality=quality)
                    data = buf.getvalue()
                    if len(data) <= 500000:
                        return data, {'input_bytes': len(raw), 'upload_bytes': len(data),
                                      'width': im.width, 'height': im.height}
    except (ValueError, OSError, Image.DecompressionBombError):
        pass
    raise LensError('invalid_image', '图片无法读取或尺寸过大，请换一张图片', 422)


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


class LensService:
    BASE = 'https://serpapi.com'

    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False)
        self.lock = asyncio.Lock()

    def key(self):
        return self.settings.secret.get('service_keys', {}).get('serpapi', '')

    def revision(self):
        return self.db.get('image_search_config', 'serpapi', {}).get('revision', 0)

    def public(self):
        return {'provider': 'serpapi', 'key_configured': bool(self.key()), 'revision': self.revision(),
                'integration_state': 'available', 'free_only': True, 'max_search_requests': 1,
                'account': self.db.get('image_search_account', 'serpapi'),
                'last_test': self.db.get('image_search_probe', 'serpapi')}

    def save(self, body):
        if body.base_revision != self.revision():
            raise LensError('config_changed', '配置已在别处修改，请刷新后重试')
        new = '' if body.clear_key else body.api_key or self.key()
        if new != self.key():
            previous = copy.deepcopy(self.settings.secret)
            self.settings.secret.setdefault('service_keys', {})['serpapi'] = new
            try:
                self.settings.save_secrets()
            except OSError:
                self.settings.secret = previous
                raise LensError('save_failed', '密钥保存失败，请稍后重试', 503) from None
            self.db.put('image_search_config', 'serpapi', {'revision': self.revision()+1})
            self.db.delete('image_search_account', 'serpapi')
            self.db.delete('image_search_probe', 'serpapi')
            self.db.audit('', 'image_search_config', {'key_configured': bool(new), 'revision': self.revision()})
        return self.public()

    def credentials(self):
        if not self.key():
            raise LensError('key_missing', '请先保存 SerpApi API 密钥')
        return self.key(), self.revision()

    def current(self, credentials):
        if credentials != (self.key(), self.revision()):
            raise LensError('config_changed', '密钥已变更，本次验证已停止')

    def text(self, value, maximum=600):
        if not isinstance(value, str):
            return ''
        clean = html.unescape(re.sub(r'<[^>]*>', '', value))
        if self.key():
            clean = clean.replace(self.key(), '[密钥已隐藏]')
        return clean[:maximum]

    def link(self, value):
        if not isinstance(value, str) or len(value) > 4096:
            return ''
        try:
            p = urlsplit(value)
            if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
                return ''
            if self.key() and self.key() in value:
                return ''
            query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                     if k.lower() not in ('api_key', 'apikey', 'access_token', 'token', 'key')]
            return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), p.fragment))
        except ValueError:
            return ''

    async def request(self, path, credentials, *, params=None, image=None, guard=lambda: None, refresh_guard=None):
        if refresh_guard:
            await refresh_guard()
        self.current(credentials)
        guard()
        key = credentials[0]
        options = ({'data': {'api_key': key}, 'files': {'image': ('image.jpg', image, 'image/jpeg')}}
                   if image is not None else {'params': {**(params or {}), 'api_key': key}})
        try:
            async with self.client.stream('POST' if image is not None else 'GET', self.BASE+path, **options) as response:
                if response.status_code in (401, 403):
                    raise LensError('invalid_key', 'SerpApi 拒绝了密钥，请检查密钥和账户状态', 502)
                if response.status_code == 429:
                    raise LensError('rate_limited', 'SerpApi 请求频率或额度已受限，本次不自动重试')
                if response.status_code != 200:
                    raise LensError('upstream_http', 'SerpApi 暂时无法完成请求，本次不自动重试', 502)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2*1024*1024:
                        raise LensError('response_too_large', '搜图响应超过处理上限', 502)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            if value.get('error'):
                raise LensError('upstream_error', 'SerpApi 返回错误，本次未取得完整结果', 502)
        except httpx.TimeoutException:
            raise LensError('timeout', 'SerpApi 请求超时，本次不自动重试', 504) from None
        except httpx.RequestError:
            raise LensError('network_error', '暂时无法连接 SerpApi，本次不自动重试', 502) from None
        except (ValueError, UnicodeError):
            raise LensError('invalid_response', 'SerpApi 返回了无法解析的结果', 502) from None
        self.current(credentials)
        guard()
        return value

    async def account(self, credentials=None, *, guard=lambda: None, refresh_guard=None):
        credentials = credentials or self.credentials()
        raw = await self.request('/account.json', credentials, guard=guard, refresh_guard=refresh_guard)
        value = {k: self.text(raw.get(k), 80) for k in ('account_status', 'plan_id', 'plan_name', 'plan_renewal_date')}
        for key in ('plan_monthly_price', 'searches_per_month', 'plan_searches_left', 'extra_credits',
                    'total_searches_left', 'this_month_usage', 'this_hour_searches', 'account_rate_limit_per_hour'):
            value[key] = number(raw.get(key))
        # Unknown price/credits and paid plans must never silently spend funds.
        value['free_eligible'] = (value['account_status'].casefold() == 'active'
            and (value['plan_id'].casefold() in ('free', 'free_plan', 'freeplan') or value['plan_name'].casefold() in ('free', 'free plan'))
            and value['plan_monthly_price'] == 0 and value['extra_credits'] == 0)
        value['checked_at'] = time.time()
        self.db.put('image_search_account', 'serpapi', value)
        return value

    async def require_free(self, credentials, reserve=1, *, guard=lambda: None, refresh_guard=None):
        account = await self.account(credentials, guard=guard, refresh_guard=refresh_guard)
        if not account['free_eligible']:
            raise LensError('free_account_required', '当前仅允许已确认的免费套餐且无额外付费点数；无法确认时停止验证')
        remaining = account['plan_searches_left']
        if remaining is None or remaining < reserve:
            raise LensError('quota_exhausted', '免费套餐剩余额度不足，本次不执行搜图')
        limit, used = account['account_rate_limit_per_hour'], account['this_hour_searches']
        if limit is not None and used is not None and limit-used < reserve:
            raise LensError('hourly_limit', '当前小时剩余查询次数不足，请稍后验证')
        return account

    def matches(self, raw):
        results, seen = [], set()
        for kind in ('exact_matches', 'visual_matches', 'organic_results'):
            rows = raw.get(kind)
            for row in (rows[:20] if isinstance(rows, list) else []):
                if not isinstance(row, dict):
                    continue
                url = self.link(row.get('link'))
                if not url or url in seen:
                    continue
                seen.add(url)
                results.append({'title': self.text(row.get('title'), 300), 'url': url,
                                'source': self.text(row.get('source'), 100), 'kind': kind})
                if len(results) == 3:
                    return results
        return results

    def overview(self, raw):
        value = raw.get('ai_overview', raw)
        if not isinstance(value, dict):
            return {'text': '', 'references': []}
        lines, remaining = [], 4000
        def walk(node, depth=0):
            nonlocal remaining
            if depth > 5 or remaining <= 0:
                return
            if isinstance(node, list):
                for item in node[:20]:
                    walk(item, depth+1)
            elif isinstance(node, dict):
                snippet = self.text(node.get('snippet', node.get('title')), min(1000, remaining))
                if snippet:
                    lines.append(snippet)
                    remaining -= len(snippet)
                for key in ('text_blocks', 'list'):
                    walk(node.get(key), depth+1)
        walk(value.get('text_blocks'))
        refs = []
        references = value.get('references')
        for row in (references[:8] if isinstance(references, list) else []):
            if isinstance(row, dict) and (url := self.link(row.get('link'))):
                refs.append({'title': self.text(row.get('title'), 300), 'url': url})
            if len(refs) == 3:
                break
        return {'text': '\n'.join(lines)[:4000], 'references': refs}

    async def test(self, raw):
        return await self.search(raw)

    async def search(self, raw, *, context=None, guard=lambda: None, refresh_guard=None):
        """A single query, with a distinct, revocable cache for each chat."""
        guard()
        if self.lock.locked():
            raise LensError('busy', '已有图片正在检索，请稍后再试；本次未重复请求')
        async with self.lock:
            credentials = self.credentials()
            data, dimensions = await asyncio.to_thread(search_image, raw)
            self.current(credentials)
            guard()
            digest = hashlib.sha256(raw).hexdigest()
            kind = 'image_search_cache' if context is not None else 'image_search_probe'
            cache_id = context['scope']+':'+hashlib.sha256(json.dumps([context['scope'], context['persona'], context['epoch'],
                credentials[1], digest]).encode()).hexdigest() if context is not None else 'serpapi'
            cached = self.db.get(kind, cache_id, {})
            if (cached.get('input_sha256') == digest and cached.get('revision') == credentials[1]
                    and cached.get('status') in ('results_found', 'no_results') and cached.get('ts', 0) > time.time()-86400):
                return {**cached, 'cached': True, 'search_requests_this_run': 0,
                        **({'cache_id': cache_id} if context is not None else {})}
            result = {'provider': 'serpapi', 'revision': credentials[1], 'ts': time.time(),
                      'input_sha256': digest, 'upload_sha256': hashlib.sha256(data).hexdigest(),
                      **dimensions, 'status': 'running', 'search_requests_this_run': 0, 'cached': False,
                      'matches': [], 'overview': {'text': '', 'references': []}, 'overview_status': 'absent',
                      'notice': '检索候选与 AI 概览仅供核对，不表示角色身份已确认；相似匹配不是原图出处证明'}
            try:
                result['account_before'] = await self.require_free(credentials, guard=guard, refresh_guard=refresh_guard)
                upload = await self.request('/image', credentials, image=data, guard=guard, refresh_guard=refresh_guard)
                image_id = upload.get('image_id')
                if not isinstance(image_id, str) or not 1 <= len(image_id) <= 1000 or any(c.isspace() for c in image_id):
                    raise LensError('upload_failed', '图片上传未返回有效编号', 502)
                await self.require_free(credentials, guard=guard, refresh_guard=refresh_guard)
                result['search_requests_this_run'] += 1
                raw_lens = await self.request('/search.json', credentials,
                    params={'engine': 'google_lens', 'type': 'all', 'image_id': image_id, 'hl': 'zh-cn'}, guard=guard, refresh_guard=refresh_guard)
                result['matches'] = self.matches(raw_lens)
                result['overview'] = self.overview(raw_lens.get('ai_overview') or {})
                overview = raw_lens.get('ai_overview')
                token = overview.get('page_token') if isinstance(overview, dict) else None
                if result['overview']['text']:
                    result['overview_status'] = 'available'
                elif token:
                    result.update(overview_status='not_requested',
                                  overview_notice='本次只发起一次搜索，未另行获取 AI 概览')
                result['status'] = 'results_found' if result['matches'] or result['overview']['references'] else 'no_results'
            except LensError as exc:
                self.current(credentials)
                guard()
                result.update(status='error', error_code=exc.code, error=exc.message)
            try:
                result['account_after'] = await self.account(credentials, guard=guard, refresh_guard=refresh_guard)
            except LensError:
                self.current(credentials)
                guard()
                result['account_after'] = None
            if context is None:
                self.db.put(kind, cache_id, result)
            elif context.get('remember'):
                result['cache_id'] = cache_id
                self.db.put(kind, cache_id, {**result, **context, 'id': cache_id})
            self.db.audit((context or {}).get('scope', ''), 'image_search_query' if context is not None else 'image_search_test',
                          {k: result[k] for k in ('status', 'search_requests_this_run', 'input_sha256', 'overview_status')})
            return result


def register(api, service):
    from fastapi import File, HTTPException, UploadFile

    def fail(exc):
        raise HTTPException(exc.status, exc.message) from None

    @api.get('/image-search/config')
    async def config():
        return service.public()

    @api.put('/image-search/config')
    async def save(body: LensConfigWrite):
        try:
            return service.save(body)
        except LensError as exc:
            fail(exc)

    @api.post('/image-search/account')
    async def account():
        try:
            return await service.account()
        except LensError as exc:
            fail(exc)

    @api.post('/image-search/test')
    async def test(file: UploadFile = File(...)):
        try:
            raw = await file.read(8*1024*1024+1)
            return await service.test(raw)
        except LensError as exc:
            fail(exc)
        finally:
            await file.close()
