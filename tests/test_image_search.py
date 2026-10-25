from support import tool_reply
from unittest.mock import AsyncMock
import asyncio
import io
import json
import logging
import time

from PIL import Image
import httpx
import pytest

from rolebot.attachments import register
from rolebot.config import Settings
from rolebot.image_search import LensConfigWrite, LensError, LensService, search_image
from rolebot.image_turn import ImageTurn, active_images
from rolebot.permissions import AccessConfig
from rolebot.tools import SPECS

from support import OTHER, SCOPE, SECOND, USER, configure_model, source


KEY = 'synthetic-serpapi-key-123456'


def picture():
    out = io.BytesIO()
    Image.new('RGB', (100, 100), '#849dcc').save(out, 'PNG')
    return out.getvalue()


def account(**changes):
    return {'api_key': KEY, 'account_email': 'private@example.org', 'account_id': 'private-account',
            'account_status': 'Active', 'plan_id': 'free', 'plan_name': 'Free Plan',
            'plan_monthly_price': 0, 'searches_per_month': 250, 'plan_searches_left': 250,
            'extra_credits': 0, 'total_searches_left': 250, 'this_month_usage': 0,
            'this_hour_searches': 0, 'account_rate_limit_per_hour': 50, **changes}


@pytest.fixture
async def lens(db, tmp_path):
    service = LensService(db, Settings(tmp_path/'lens'))
    await service.client.aclose()
    service.save(LensConfigWrite(api_key=KEY, base_revision=0))
    yield service
    await service.client.aclose()


def transport(service, handler):
    requests = []
    def wrapped(req):
        requests.append(req)
        return handler(req)
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=False)
    return requests


def response(req, *, lens=None, quota=None):
    if req.url.path == '/account.json':
        return httpx.Response(200, json=quota or account())
    if req.url.path == '/image':
        return httpx.Response(200, json={'image_id': 'opaque+/=image-id-test'})
    return httpx.Response(200, json=lens if lens is not None else {
        'visual_matches': [{'title': '角色画作来源', 'link': 'https://example.org/art', 'source': '作者页'}]})


def test_admin_configuration_requires_authentication_and_csrf(client):
    assert client.get('/api/v1/image-search/config').status_code == 401
    assert client.put('/api/v1/image-search/config', json={}).status_code == 401
    assert client.post('/api/v1/image-search/test', files={'file': ('a.png', picture())}).status_code == 401
    from support import auth
    auth(client)
    assert client.put('/api/v1/image-search/config', json={'base_revision': 0, 'api_key': KEY}).status_code == 403


def test_key_write_only_preserved_and_independent_of_models(authed_client):
    c = authed_client
    initial = c.get('/api/v1/image-search/config').json()
    assert not initial['key_configured'] and initial['max_search_requests'] == 1
    result = c.put('/api/v1/image-search/config', json={'base_revision': 0, 'api_key': KEY})
    assert result.status_code == 200 and result.json()['key_configured']
    assert KEY not in result.text
    assert c.put('/api/v1/image-search/config', json={'base_revision': 1, 'api_key': ''}).json()['revision'] == 1
    models = c.get('/api/v1/models').json()
    models.pop('key_configured')
    assert c.put('/api/v1/models', json=models).status_code == 200
    assert c.app.state.image_search.key() == KEY
    assert Settings(c.app.state.settings.data_dir).secret['service_keys']['serpapi'] == KEY
    assert KEY not in str(c.app.state.model.records.clean({'notice': 'opaque '+KEY}))
    for path in ('/image-search/config', '/models', '/audit', '/settings'):
        assert KEY not in c.get('/api/v1'+path).text
    assert 'image_search' in SPECS
    assert c.get('/api/v1/image-search/config').json()['integration_state'] == 'available'


def test_revision_clear_and_sensitive_validation(authed_client):
    c = authed_client
    assert c.put('/api/v1/image-search/config', json={'base_revision': 0, 'api_key': KEY}).status_code == 200
    assert c.put('/api/v1/image-search/config', json={'base_revision': 0, 'clear_key': True}).status_code == 409
    bad = KEY+' invalid'
    r = c.put('/api/v1/image-search/config', json={'base_revision': 1, 'api_key': bad})
    assert r.status_code == 422 and KEY not in r.text
    assert c.put('/api/v1/image-search/config', json={'base_revision': 1, 'api_key': KEY, 'clear_key': True}).status_code == 422
    assert not c.put('/api/v1/image-search/config', json={'base_revision': 1, 'clear_key': True}).json()['key_configured']
    assert c.post('/api/v1/image-search/account').status_code == 409


def test_admin_multipart_verification_uses_one_search_without_chat_invocation(authed_client):
    c = authed_client
    assert c.put('/api/v1/image-search/config', json={'base_revision': 0, 'api_key': KEY}).status_code == 200
    service = c.app.state.image_search
    asyncio.run(service.client.aclose())
    reqs = transport(service, lambda req: response(req))
    result = c.post('/api/v1/image-search/test', files={'file': ('a.png', picture(), 'image/png')})
    assert result.status_code == 200 and result.json()['status'] == 'results_found'
    assert result.json()['search_requests_this_run'] == 1 and KEY not in result.text
    assert len([r for r in reqs if r.url.path == '/search.json']) == 1
    config = c.get('/api/v1/image-search/config').json()
    assert config['last_test']['status'] == 'results_found' and config['integration_state'] == 'available'
    assert not c.app.state.db.rows("SELECT 1 FROM audit WHERE kind='tool'")


async def test_account_response_redacts_credentials_and_private_metadata(lens):
    reqs = transport(lens, lambda req: response(req))
    result = await lens.account()
    assert result['free_eligible'] and len(reqs) == 1
    stored = json.dumps(lens.public())+str(lens.db.all('image_search_account'))
    assert KEY not in stored and 'private@example.org' not in stored and 'private-account' not in stored


async def test_http_request_logs_hide_serpapi_query_key(lens, caplog):
    caplog.set_level(logging.INFO, logger='httpx')
    transport(lens, lambda req: response(req))
    await lens.test(picture())
    assert '/search.json' in caplog.text
    assert KEY not in caplog.text and 'api_key=[hidden]' in caplog.text


@pytest.mark.parametrize('changes', [
    {'plan_id': 'starter', 'plan_name': 'Starter', 'plan_monthly_price': 25},
    {'plan_monthly_price': None}, {'extra_credits': 1}, {'extra_credits': None},
    {'account_status': 'Inactive'}, {'plan_searches_left': 0}, {'plan_searches_left': None},
    {'account_rate_limit_per_hour': 50, 'this_hour_searches': 50},
])
async def test_unconfirmed_free_quota_never_uploads_or_searches(lens, changes):
    reqs = transport(lens, lambda req: response(req, quota=account(**changes)))
    result = await lens.test(picture())
    assert result['status'] == 'error' and result['search_requests_this_run'] == 0
    assert all(r.url.path == '/account.json' for r in reqs)


async def test_single_lens_search_does_not_fetch_overview_token(lens):
    payload = {'visual_matches': [{'title': '画作来源', 'link': 'https://example.org/a'}],
               'ai_overview': {'page_token': 'private-transient-token', 'serpapi_link': 'https://evil.example/steal?api_key='+KEY}}
    reqs = transport(lens, lambda req: response(req, lens=payload))
    result = await lens.test(picture())
    searches = [r for r in reqs if r.url.path == '/search.json']
    assert len(searches) == result['search_requests_this_run'] == 1
    assert searches[0].url.params['engine'] == 'google_lens'
    assert result['overview_status'] == 'not_requested' and result['status'] == 'results_found'
    assert result['upload_bytes'] <= 500000
    assert KEY not in str(result) and 'private-transient-token' not in str(result)
    assert not lens.db.rows('SELECT * FROM usage')  # Search quota is not model token usage.
    assert not lens.db.rows('SELECT * FROM messages')
    count = len(reqs)
    cached = await lens.test(picture())
    assert cached['cached'] and cached['search_requests_this_run'] == 0 and len(reqs) == count


async def test_inline_overview_and_sources_are_bounded_and_plain_text(lens):
    payload = {'ai_overview': {
        'text_blocks': [{'type': 'list', 'list': [{'text_blocks': [{'snippet': '<b>角色线索</b>'}]}]}],
        'references': [{'title': '来源', 'link': 'https://example.org/a?api_key=other-secret'},
                       {'title': '不安全', 'link': 'javascript:alert(1)'}]}}
    reqs = transport(lens, lambda req: response(req, lens=payload))
    result = await lens.test(picture())
    assert result['overview']['text'] == '角色线索'
    assert result['overview']['references'][0]['url'] == 'https://example.org/a'
    assert len(result['overview']['references']) == 1
    assert result['status'] == 'results_found' and result['overview_status'] == 'available'
    assert len([r for r in reqs if r.url.path == '/search.json']) == 1


async def test_empty_results_are_not_reported_as_identity_verification(lens):
    reqs = transport(lens, lambda req: response(req, lens={'visual_matches': [], 'ai_overview': {'references': {}}}))
    result = await lens.test(picture())
    assert result['status'] == 'no_results' and result['search_requests_this_run'] == 1
    assert not result['matches']
    assert len([r for r in reqs if r.url.path == '/search.json']) == 1


async def test_quota_exhaustion_after_upload_stops_before_search(lens):
    uploaded = False
    def handle(req):
        nonlocal uploaded
        if req.url.path == '/image':
            uploaded = True
        return response(req, quota=account(plan_searches_left=0 if uploaded else 1))
    reqs = transport(lens, handle)
    result = await lens.test(picture())
    assert result['error_code'] == 'quota_exhausted'
    assert not any(r.url.path == '/search.json' for r in reqs)


@pytest.mark.parametrize('failure', ['timeout', 'rate', 'key', 'redirect', 'json', 'large', 'error'])
async def test_provider_failure_never_retries_or_leaks_keys(lens, failure):
    def handle(req):
        if req.url.path == '/search.json':
            if failure == 'timeout':
                raise httpx.ReadTimeout('sensitive URL api_key='+KEY, request=req)
            if failure == 'rate': return httpx.Response(429, text=KEY)
            if failure == 'key': return httpx.Response(401, text=KEY)
            if failure == 'redirect': return httpx.Response(302, headers={'location': 'https://evil.example/'+KEY})
            if failure == 'json': return httpx.Response(200, text=KEY)
            if failure == 'large': return httpx.Response(200, content=b'x'*(2*1024*1024+1))
            return httpx.Response(200, json={'error': 'api_key='+KEY})
        return response(req)
    reqs = transport(lens, handle)
    result = await lens.test(picture())
    assert result['status'] == 'error' and result['search_requests_this_run'] == 1
    assert KEY not in json.dumps(lens.public())
    assert KEY not in str(lens.db.rows('SELECT * FROM audit'))
    assert len([r for r in reqs if r.url.path == '/search.json']) == 1
    assert all(r.url.host == 'serpapi.com' for r in reqs)


async def test_key_replacement_during_upload_stops_and_discards_old_result(lens):
    def handle(req):
        if req.url.path == '/image':
            lens.save(LensConfigWrite(api_key='replacement-secret-key', base_revision=1))
        return response(req)
    reqs = transport(lens, handle)
    with pytest.raises(LensError, match='密钥已变更'):
        await lens.test(picture())
    assert not any(r.url.path == '/search.json' for r in reqs)
    assert not lens.public()['last_test']


async def test_concurrent_validation_rejected_before_spending(lens):
    async with lens.lock:
        with pytest.raises(LensError, match='已有图片'):
            await lens.test(picture())


@pytest.mark.parametrize('kind', ['invalid', 'oversized'])
def test_invalid_image_is_rejected(kind):
    with pytest.raises(LensError):
        search_image(b'not an image' if kind == 'invalid' else b'x'*(8*1024*1024+1))


def test_upload_size_and_pixel_dimensions_are_bounded():
    im = Image.effect_noise((1800, 1800), 100).convert('RGB')
    raw = io.BytesIO()
    im.save(raw, 'PNG')
    data, info = search_image(raw.getvalue())
    assert len(data) <= 500000 and max(info['width'], info['height']) <= 1600
    assert Image.open(io.BytesIO(data)).format == 'JPEG'


@pytest.fixture
async def search_runtime(runtime):
    r = runtime
    r.group.update(interaction_enabled=True, sticker_autocollect=False)
    r.group['tools']['history_read']['enabled'] = True
    r.group['tools'].pop('image_read', None)
    r.db.put('group', SCOPE, r.group)
    service = LensService(r.db, r.settings)
    await service.client.aclose()
    service.save(LensConfigWrite(api_key='synthetic-lens-tool-key', base_revision=0))
    r.engine.image_search = service
    r.requests = []
    def handler(req):
        r.requests.append(req.url.path)
        if req.url.path == '/account.json':
            return httpx.Response(200, json={
                'account_status': 'Active', 'plan_id': 'free', 'plan_name': 'Free Plan',
                'plan_monthly_price': 0, 'extra_credits': 0, 'plan_searches_left': 249,
            })
        if req.url.path == '/image':
            return httpx.Response(200, json={'image_id': 'opaque-id'})
        return httpx.Response(200, json={
            'visual_matches': [{'title': 'Claude 与 DeepSeek 同人', 'link': 'https://example.org/art', 'source': '作品页'}],
            'ai_overview': {'page_token': 'must-not-follow'},
        })
    r.handler = handler
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: r.handler(req)))
    r.model.search = AsyncMock(return_value=[])
    yield r
    await service.client.aclose()


def image(r, number=1, scope=SCOPE, persona='default'):
    m = source(r, '[图片]', number)
    m.update(scope=scope, persona=persona, id=scope+':'+str(number), images=['https://gchat.qpic.cn/synthetic'])
    m['data'].update(types=['image'], image_count=1)
    r.db.add_message(m['id'], scope, persona, USER, 'user', '[图片]', m['data'], ts=m['ts'])
    item = register(r.db, scope, persona, m)[0]
    buf = io.BytesIO()
    Image.new('RGB', (80, 80), '#aabbcc').save(buf, 'JPEG')
    r.engine.attachments.store(item, buf.getvalue())
    return m, item['id']


def answer(m, text='搜到相关候选，尚不能确认身份：https://example.org/art'):
    return {'role': 'assistant', 'content': json.dumps({
        'speak': True, 'text': text, 'mode': 'direct', 'reply_to_message_id': m['id'],
        'continues_dialogue': False, 'expects_followup': False})}


def call(m, aid):
    return tool_reply('image_search', {'image_id': aid, 'source_message_id': m['id']}, id='search0')


async def execute(r, m, aid, *, batch=None, turn=None):
    turn = turn or ImageTurn(r.engine, SCOPE, 'default', batch or [m])
    token = active_images.set(turn)
    try:
        result = await r.engine.bound_tool(SCOPE, 'default', batch or [m], 'image_search',
                                          {'image_id': aid, 'source_message_id': m['id']})
        return result, turn
    finally:
        active_images.reset(token)


async def test_profile_demotion_during_account_check_blocks_image_upload(search_runtime):
    r = search_runtime
    group = r.db.get('group', SCOPE)
    group['tools']['image_search'] = {'enabled': True, 'request_access': {'member': 'disabled', 'admin': 'all'}}
    r.db.put('group', SCOPE, group)
    state = {'role': 'admin'}
    original = r.bot.call
    async def info(action, params=None, **kwargs):
        if action == 'get_group_member_info' and params['user_id'] == USER:
            return {'user_id': USER, 'group_id': params['group_id'], 'role': state['role']}
        return await original(action, params, **kwargs)
    r.bot.call = info
    handler = r.handler
    def demote(req):
        result = handler(req)
        if req.url.path == '/account.json': state['role'] = 'member'
        return result
    r.handler = demote
    _, aid = image(r)
    m = source(r, '机器人帮我搜一下这个图', 2, replies=['1']); m['replies'] = ['1']
    result, _ = await execute(r, m, aid)
    assert result['status'] == 'denied', result
    assert '/image' not in r.requests and '/search.json' not in r.requests


async def test_quoted_image_search_two_main_calls_one_lens_no_keyword_or_review(search_runtime):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '助手搜一下这个图，看看里面都是谁', 2, replies=['1'])
    m['replies'] = ['1']
    r.model.script = [call(m, aid), answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    assert all(c[3]['stage'] == 'conversation.agent' for c in r.model.calls)
    assert r.requests.count('/search.json') == 1
    r.model.search.assert_not_awaited()
    tool = next(json.loads(m['content']) for m in r.model.calls[-1][2] if m['role'] == 'tool')
    assert tool['result']['overview_status'] == 'not_requested'
    assert 'account_before' not in tool['result']
    assert 'Claude' in tool['result']['matches'][0]['title']
    assert not any(s['name'] == 'search_missing' for t in r.db.all('trace') for s in t['stages'])


@pytest.mark.parametrize('interjection', ['other_image', 'other_chatter', 'requester_cancel', 'quoted_correction'])
async def test_prepared_search_reply_only_reviews_related_interjections(search_runtime, interjection):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人，搜一下这个图，看看图里的人物是谁', 2, replies=['1'])
    m['replies'] = ['1']
    handler = r.handler

    def search(req):
        if req.url.path == '/search.json':
            new = source(r, {
                'other_image': '[图片]', 'other_chatter': '今晚吃火锅吧',
                'requester_cancel': '不用搜了，取消', 'quoted_correction': '认错图了，是另一张',
            }[interjection], 3, actor=USER if interjection == 'requester_cancel' else SECOND)
            if interjection == 'other_image':
                new.update(plain_text='', images=['https://example.invalid/unrelated'])
                new['data'].update(types=['image'], image_count=1)
            if interjection == 'quoted_correction':
                new['replies'] = ['1']
            r.engine.recent_events[SCOPE] = [m, new]
        return handler(req)
    r.handler = search
    caption = '检索结果最支持 Claude 娘。来源：https://example.org/art'
    prepared = {'role': 'assistant', 'tool_calls': [{'id': 'send0', 'type': 'function',
        'function': {'name': 'message_send', 'arguments': json.dumps({
            'source_message_id': m['id'], 'text': caption, 'mentions': [USER]})}}]}
    r.model.script = [call(m, aid), prepared, answer(m, caption), {'content': '{"send":false}'}]
    await r.engine.turn(SCOPE, [m])
    related = interjection in ('requester_cancel', 'quoted_correction')
    assert len(r.model.calls) == (4 if related else 3)
    assert r.requests.count('/search.json') == 1
    assert len(r.bot.sent) == (0 if related else 1)
    if not related:
        assert caption in str(r.bot.sent)
    assert r.db.all('trace')[0]['status'] == ('stale' if related else 'sent')
    r.model.search.assert_not_awaited()


async def test_missing_image_search_retry_accepts_lens_without_redundant_web(search_runtime):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人搜一下这张图的来源', 2)
    r.model.script = [answer(m, '猜测'), call(m, aid), answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 3 and r.requests.count('/search.json') == 1
    r.model.search.assert_not_awaited()
    assert '猜测' not in str(r.bot.sent)


async def test_single_search_per_turn_even_different_images(search_runtime):
    r = search_runtime
    _, aid = image(r)
    _, other = image(r, 2)
    m = source(r, '机器人搜一下这些图', 3)
    first, turn = await execute(r, m, aid)
    second, _ = await execute(r, m, other, turn=turn)
    assert first['status'] == 'success'
    assert second['error_code'] == 'search_limit'
    assert r.requests.count('/search.json') == 1


async def test_same_chat_cache_reuse_and_revoke_on_forget(search_runtime):
    r = search_runtime
    original, aid = image(r)
    m = source(r, '机器人搜一下这张图', 2)
    first, _ = await execute(r, m, aid)
    second, _ = await execute(r, m, aid)
    assert first['result']['cached'] is False
    assert second['result']['cached'] is True and second['result']['search_requests_this_run'] == 0
    assert len(r.db.all('image_search_cache')) == 1 and not r.db.all('image_search_probe')
    before = list(r.requests)
    r.db.invalidate_memory(SCOPE, [original['id']])
    denied, _ = await execute(r, m, aid)
    assert denied['status'] == 'denied' and r.requests == before
    assert not r.db.all('image_search_cache')


@pytest.mark.parametrize('change', ['cross_group', 'cross_persona', 'role_reference', 'expired', 'permission', 'history_permission', 'disabled', 'missing_key'])
async def test_invalid_target_or_permission_never_uploads(search_runtime, change):
    r = search_runtime
    original, aid = image(r, scope=OTHER if change == 'cross_group' else SCOPE,
                          persona='other' if change == 'cross_persona' else 'default')
    m = source(r, '机器人搜一下这张图', 2)
    if change == 'role_reference':
        aid = 'ref_'+'a'*32
    elif change == 'expired':
        value = r.db.get('attachment', aid)
        value['ts'] = time.time()-90000
        r.db.put('attachment', aid, value)
    elif change in ('permission', 'history_permission'):
        name = 'image_search' if change == 'permission' else 'image_read'
        r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'deny': ['tool:'+name]}}).model_dump())
    elif change == 'disabled':
        r.group['tools']['image_search']['enabled'] = False
        r.db.put('group', SCOPE, r.group)
    elif change == 'missing_key':
        r.engine.image_search.save(LensConfigWrite(clear_key=True, base_revision=1))
    result, _ = await execute(r, m, aid)
    assert result['status'] == 'denied'
    assert r.requests == []


async def test_current_quote_does_not_require_history_tool(search_runtime):
    r = search_runtime
    _, aid = image(r)
    r.group['tools']['history_read']['enabled'] = False
    r.db.put('group', SCOPE, r.group)
    m = source(r, '机器人搜一下这个图', 2, replies=['1'])
    m['replies'] = ['1']
    result, _ = await execute(r, m, aid)
    assert result['status'] == 'success' and r.requests.count('/search.json') == 1


@pytest.mark.parametrize('when', ['/image', '/search.json'])
async def test_revocation_during_network_stops_upload_chain_or_discards_result(search_runtime, when):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人搜一下这张图', 2)
    handler = r.handler
    def revoke(req):
        result = handler(req)
        if req.url.path == when:
            r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'deny': ['tool:image_search']}}).model_dump())
        return result
    r.handler = revoke
    result, turn = await execute(r, m, aid)
    assert result['status'] == 'denied' and not turn.current()
    assert '/search.json' not in r.requests if when == '/image' else r.requests.count('/search.json') == 1
    assert not r.db.all('image_search_cache')
    assert 'Claude' not in json.dumps(result)


async def test_failed_lens_does_not_retry_or_follow_overview(search_runtime):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人搜一下这张图', 2)
    handler = r.handler
    def fail(req):
        result = handler(req)
        if req.url.path == '/search.json':
            raise httpx.ReadTimeout('timeout', request=req)
        return result
    r.handler = fail
    first, turn = await execute(r, m, aid)
    assert first['result']['status'] == 'error' and first['result']['error_code'] == 'timeout'
    second, _ = await execute(r, m, aid, turn=turn)
    assert second['error_code'] == 'search_limit'
    assert r.requests.count('/search.json') == 1


async def test_no_unsolicited_upload_and_no_borrowing_other_requester(search_runtime):
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人，这是不是你', 2)
    r.model.script = [answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 1 and r.requests == []
    request = source(r, '机器人搜一下这张图', 3)
    other = source(r, '随便聊聊', 4, actor=SECOND)
    r.model.script = [{'role': 'assistant', 'content': '{"mode":"independent"}'}]
    result, _ = await execute(r, other, aid, batch=[request, other])
    assert result['error_code'] == 'request_context_unclear' and r.requests == []


async def test_cache_isolated_by_group_persona_and_reset(search_runtime):
    r = search_runtime
    service = r.engine.image_search
    buf = io.BytesIO()
    Image.new('RGB', (10, 10)).save(buf, 'JPEG')
    raw = buf.getvalue()
    context = {'scope': SCOPE, 'persona': 'default', 'epoch': 0, 'remember': True, 'source_ids': [], 'actor_ids': []}
    for changes in ({}, {'scope': OTHER}, {'persona': 'other'}):
        result = await service.search(raw, context={**context, **changes})
        assert not result['cached']
    cached = await service.search(raw, context=context)
    assert cached['cached'] and r.requests.count('/search.json') == 3
    r.db.clear_derived(SCOPE)
    assert len(r.db.all('image_search_cache')) == 1
    result = await service.search(raw, context=context)
    assert not result['cached'] and r.requests.count('/search.json') == 4


@pytest.mark.parametrize('protocol', ['openai-completions', 'anthropic-messages'])
async def test_lens_tool_chain_both_native_protocols(search_runtime, protocol):
    from rolebot.model import Model
    r = search_runtime
    r.settings.set_key('synthetic-provider-key')
    model = Model(r.db, r.settings)
    await model.client.aclose()
    configure_model(model, protocol)
    r.engine.model = model
    _, aid = image(r)
    # A historical target, not a current/quoted image already attached by prepare().
    m = source(r, '机器人搜一下刚才那个图', 2)
    captured = []
    def handler(req):
        body = json.loads(req.content)
        captured.append(body)
        if len(captured) == 1:
            response = ({'content': [
                {'type': 'thinking', 'thinking': 'synthetic', 'signature': 'native-signature'},
                {'type': 'tool_use', 'id': 'search0', 'name': 'image_search',
                 'input': {'image_id': aid, 'source_message_id': m['id']}}]}
                if protocol == 'anthropic-messages' else {'choices': [{'message': {**call(m, aid), 'reasoning_content': 'synthetic'}}]})
        else:
            response = ({'content': [{'type': 'text', 'text': answer(m)['content']}]}
                        if protocol == 'anthropic-messages' else {'choices': [{'message': answer(m)}]})
        return httpx.Response(200, json=response)
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await r.engine.turn(SCOPE, [m])
        assert len(captured) == 2 and len(r.bot.sent) == 1
        assert r.requests.count('/search.json') == 1
        assert 'Claude' in str(captured[-1])
        assert image_blocks(captured[0]['messages']) == 0
        assert image_blocks(captured[-1]['messages']) == 1
        assert 'search_for_source' in str(captured[-1])
        assert 'native-signature' in str(captured[-1]) if protocol == 'anthropic-messages' else 'reasoning_content' in str(captured[-1])
    finally:
        await model.close()


def image_blocks(messages):
    return sum(b.get('type') in ('image_url', 'image') for m in messages
               for b in (m.get('content') if isinstance(m.get('content'), list) else []))


def image_read(m, aid):
    return tool_reply('image_read', {'image_ids': [aid], 'source_message_id': m['id']}, id='read0')


async def test_eleven_minute_followup_recovers_search_and_pixels_without_new_query(search_runtime, monkeypatch):
    r = search_runtime
    clock = [time.time()]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    _, aid = image(r)
    m = source(r, '机器人搜一下刚才那个图', 2)
    r.model.script = [call(m, aid), answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert image_blocks(r.model.calls[0][2]) == 0
    assert image_blocks(r.model.calls[1][2]) == 1
    saved = r.db.rows("SELECT data FROM messages WHERE scope=? AND role='assistant' ORDER BY ts", (SCOPE,))
    assert json.loads(saved[-1]['data'])['image_search_refs']
    requests = list(r.requests)
    clock[0] += 660  # Beyond both the 120s image focus and 600s working-session TTL.
    followup = source(r, '机器人，你猜猜他们俩分别是谁', 3)
    r.model.script = [image_read(followup, aid), answer(followup)]
    await r.engine.turn(SCOPE, [followup])
    assert len(r.model.calls) == 4
    dynamic = r.db.get('context_preview', SCOPE+':default')['context']
    assert m['id'] in [msg['id'] for msg in dynamic['消息']]
    evidence = dynamic['image_search_evidence']
    assert evidence[0]['image_id'] == aid and 'Claude' in str(evidence)
    assert image_blocks(r.model.calls[-1][2]) == 1
    assert 'image_search_evidence' in str(r.model.calls[-1][2])
    assert r.requests == requests and r.requests.count('/search.json') == 1
    r.model.search.assert_not_awaited()


@pytest.mark.parametrize('target', ['original', 'request', 'reply'])
async def test_history_restores_linked_evidence_without_lens(search_runtime, target):
    from rolebot.history import History
    r = search_runtime
    original, aid = image(r)
    m = source(r, '机器人搜一下那个图', 2)
    r.model.script = [call(m, aid), answer(m)]
    await r.engine.turn(SCOPE, [m])
    reply = r.db.rows("SELECT id FROM messages WHERE scope=? AND role='assistant' ORDER BY ts DESC", (SCOPE,))[0]
    selected = {'original': original['id'], 'request': m['id'], 'reply': reply['id']}[target]
    followup = source(r, '机器人继续说说', 3)
    requests = list(r.requests)
    token = active_images.set(ImageTurn(r.engine, SCOPE, 'default', [followup]))
    try:
        result = History(r.db, SCOPE, 'default').read(selected, before=0, after=0)
        assert result['image_search_evidence'][0]['image_id'] == aid
        assert 'Claude' in str(result['image_search_evidence'])
        assert r.requests == requests
    finally:
        active_images.reset(token)


@pytest.mark.parametrize('change', ['image_permission', 'search_permission', 'disabled', 'forget',
                                   'optout', 'expired', 'key', 'persona', 'cross_group', 'reset'])
async def test_recovered_search_evidence_respects_current_access(search_runtime, change):
    from rolebot.image_evidence import for_messages
    r = search_runtime
    original, aid = image(r)
    m = source(r, '机器人搜一下那个图', 2)
    result, _ = await execute(r, m, aid)
    assert result['status'] == 'success'
    followup = source(r, '机器人继续说说', 3)
    requests = list(r.requests)
    token = active_images.set(ImageTurn(r.engine, SCOPE, 'default', [followup]))
    try:
        assert for_messages(r.db, SCOPE, 'default', [m])
        if change in ('image_permission', 'search_permission'):
            tool = 'image_read' if change == 'image_permission' else 'image_search'
            r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'deny': ['tool:'+tool]}}).model_dump())
        elif change == 'disabled':
            r.group['tools']['image_search']['enabled'] = False
            r.db.put('group', SCOPE, r.group)
        elif change == 'forget':
            r.db.invalidate_memory(SCOPE, [original['id']])
        elif change == 'optout':
            member = r.db.get('member', SCOPE+':'+USER)
            member['remember'] = False
            r.db.put('member', SCOPE+':'+USER, member)
        elif change == 'expired':
            cache = r.db.all('image_search_cache')[0]
            cache['ts'] = time.time()-86401
            r.db.put('image_search_cache', cache['id'], cache)
        elif change == 'key':
            r.engine.image_search.save(LensConfigWrite(api_key='replacement-synthetic-key', base_revision=1))
        elif change == 'reset':
            r.db.clear_derived(SCOPE)
        scope = OTHER if change == 'cross_group' else SCOPE
        persona = 'other' if change == 'persona' else 'default'
        assert not for_messages(r.db, scope, persona, [m])
        assert r.requests == requests
    finally:
        active_images.reset(token)


async def test_unlinked_cache_is_not_recovered_from_historical_attachments(search_runtime):
    from rolebot.image_evidence import for_messages
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人搜一下那个图', 2)
    await execute(r, m, aid)
    cache = r.db.all('image_search_cache')[0]
    cache.pop('links')
    cache.pop('cache_id', None)
    r.db.put('image_search_cache', cache['id'], cache)
    followup = source(r, '机器人继续说说', 3)
    requests = list(r.requests)
    token = active_images.set(ImageTurn(r.engine, SCOPE, 'default', [followup]))
    try:
        assert not for_messages(r.db, SCOPE, 'default', [m])
        assert r.requests == requests
    finally:
        active_images.reset(token)


async def test_evidence_revocation_before_final_reply_cancels_send(search_runtime):
    from rolebot.image_evidence import for_messages, track
    r = search_runtime
    _, aid = image(r)
    m = source(r, '机器人搜一下那个图', 2)
    await execute(r, m, aid)
    followup = source(r, '机器人继续说说', 3)
    turn = ImageTurn(r.engine, SCOPE, 'default', [followup])
    token = active_images.set(turn)
    try:
        track(for_messages(r.db, SCOPE, 'default', [m]))
        assert turn.current()
        r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'deny': ['tool:image_search']}}).model_dump())
        assert not turn.current()
    finally:
        active_images.reset(token)
