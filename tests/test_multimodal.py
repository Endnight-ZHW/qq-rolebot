from support import tool_reply
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import base64
import io
import json
import socket
import time

from PIL import Image
import httpx
import pytest

from rolebot import media as module
from rolebot.attachments import image_id, register
from rolebot.context import ContextTooLarge
from rolebot.image_assets import prepare
from rolebot.image_turn import ImageTurn
from rolebot.input_budget import check_images, measure
from rolebot.media import MediaError, image_error, public_url
from rolebot.onebot import normalize

from support import image_blocks, OTHER, SCOPE, USER, configure_model, event, source


@pytest.fixture
def mm(runtime):
    r = runtime
    r.group.update(interaction_enabled=True, sticker_autocollect=False)
    for name in ('history_read', 'history_search', 'image_read'):
        r.group['tools'][name]['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    buf = io.BytesIO()
    Image.new('RGB', (100, 100), '#bbcccc').save(buf, 'JPEG', quality=85)
    r.image_raw = buf.getvalue()
    r.image_block = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+base64.b64encode(r.image_raw).decode(), 'detail': 'auto'}}
    r.engine.media.image = AsyncMock(return_value=r.image_block)
    return r


def photo(r, id=1, text='机器人，这是不是你？'):
    m = source(r, text, id)
    m.update(images=['https://gchat.qpic.cn/fake'], types=['text', 'image'])
    m['data'].update(image_count=1, types=['text', 'image'])
    r.db.patch_message_data(SCOPE, m['id'], m['data'])
    register(r.db, SCOPE, 'default', m)
    return m


def answer(m, **kw):
    return {'role': 'assistant', 'content': json.dumps({'speak': True, 'text': '看起来很像我。', 'mode': 'direct',
        'reply_to_message_id': m['id'], 'continues_dialogue': False, **kw})}


def tool(m, ids, refs=False):
    return tool_reply('image_read', {'source_message_id': m['id'],
        'image_ids': ids, 'include_role_references': refs}, id='read1')


async def test_direct_image_one_call_role_and_original_together(mm):
    m = photo(mm)
    mm.model.script = [answer(m, image_observations=[{'image_id': image_id(m['id'], 0), 'description': '蓝色三角形'}])]
    await mm.engine.turn(SCOPE, [m])
    assert len(mm.model.calls) == len(mm.bot.sent) == 1
    messages = mm.model.calls[0][2]
    assert '合成测试' in messages[0]['content']
    assert m['text'] in str(messages)
    assert len(image_blocks(messages)) == 1
    assert mm.db.get('attachment', image_id(m['id'], 0))['observation'] == '蓝色三角形'
    assert not mm.db.all('persona_image') and not mm.db.all('role_image')  # A question is not confirmation.


async def test_unselected_images_metadata_only(mm):
    mm.group['listening_probability'] = 0
    mm.db.put('group', SCOPE, mm.group)
    mm.engine.queues[SCOPE] = asyncio.Queue()
    e = event(id=9)
    e['message'] = [{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/not-fetched'}}]
    await mm.engine.receive(e)
    m = mm.engine.queues[SCOPE].get_nowait()
    await mm.engine.turn(SCOPE, [m])
    assert len(mm.db.all('attachment')) == 1
    assert not mm.model.calls and not mm.bot.calls
    mm.engine.media.image.assert_not_called()


async def test_selected_picture_skips_gate_can_be_silent(mm):
    m = photo(mm, text='[图片]')
    mm.model.script = [answer(m, speak=False, text='')]
    await mm.engine.turn(SCOPE, [m])
    assert [x[3]['stage'] for x in mm.model.calls] == ['conversation.agent']
    assert image_blocks(mm.model.calls[0][2]) and not mm.bot.sent


async def test_explicit_reply_automatically_attaches_original(mm):
    original = photo(mm)
    await mm.engine.attachments.read(SCOPE, 'default', image_id(original['id'], 0), 0, original)
    m = source(mm, '机器人，这是谁？', 2)
    m['replies'] = [original['message_id']]
    mm.model.script = [answer(m)]
    await mm.engine.turn(SCOPE, [m])
    assert len(mm.model.calls) == 1 and len(image_blocks(mm.model.calls[0][2])) == 1


async def test_four_history_images_tool_adds_pixels_without_caption_call(mm):
    ids = []
    for i in range(1, 5):
        m = photo(mm, i, '[图片]')
        ids.append(image_id(m['id'], 0))
        await mm.engine.attachments.read(SCOPE, 'default', ids[-1], 0, m)
    question = source(mm, '机器人，上面四张表情都是谁？', 5)
    mm.model.script = [tool(question, ids), answer(question)]
    await mm.engine.turn(SCOPE, [question])
    assert len(mm.model.calls) == 2 and len(mm.bot.sent) == 1
    assert not image_blocks(mm.model.calls[0][2])
    assert len(image_blocks(mm.model.calls[1][2])) == 4
    assert all(c[3]['stage'] == 'conversation.agent' for c in mm.model.calls)
    assert all(aid in str(mm.model.calls[0][2]) for aid in ids)
    tool_messages = [m for m in mm.model.calls[1][2] if m['role'] == 'tool']
    assert len(tool_messages) == 1 and 'base64' not in tool_messages[0]['content']
    result = json.loads(tool_messages[0]['content'])
    assert result['status'] == 'success' and result['error'] is None
    assert all(row['status'] == 'attached' for row in result['result']['images'])


async def test_focus_keeps_recent_images_then_expires(mm):
    m = photo(mm)
    mm.model.script = [answer(m, continues_dialogue=True)]
    await mm.engine.turn(SCOPE, [m])
    follow = source(mm, '机器人，这个发饰呢？', 2)
    mm.model.script = [answer(follow, text='是蓝色圆形呀。', continues_dialogue=True)]
    await mm.engine.turn(SCOPE, [follow])
    assert len(image_blocks(mm.model.calls[-1][2])) == 1
    focus = mm.db.get('image_focus', SCOPE+':default')
    focus['expires'] = time.time()-1
    mm.db.put('image_focus', SCOPE+':default', focus)
    later = source(mm, '机器人，来聊天', 3)
    mm.model.script = [answer(later, text='来啦。')]
    await mm.engine.turn(SCOPE, [later])
    assert not image_blocks(mm.model.calls[-1][2])


@pytest.mark.parametrize('condition', ['other_group', 'other_role', 'forgotten', 'optout', 'expired'])
async def test_image_read_enforces_visibility(mm, condition):
    m = photo(mm)
    aid = image_id(m['id'], 0)
    if condition == 'forgotten': mm.db.invalidate_memory(SCOPE, source_ids=[m['id']])
    if condition == 'optout': mm.db.put('member', SCOPE+':'+USER, {'remember': False})
    if condition == 'other_group': mm.db.execute('UPDATE messages SET scope=? WHERE id=?', (OTHER, m['id']))
    if condition == 'other_role': mm.db.execute("UPDATE messages SET persona='other' WHERE id=?", (m['id'],))
    if condition == 'expired':
        item = mm.db.get('attachment', aid); item['ts'] -= 25*3600; mm.db.put('attachment', aid, item)
    turn = ImageTurn(mm.engine, SCOPE, 'default')
    result = await turn.read_tool([aid])
    assert result['images'][0]['status'] == 'unavailable'
    assert not turn.loaded and not mm.model.calls
    mm.engine.media.image.assert_not_called()


async def test_forget_during_image_download_cancels_before_main(mm):
    m = photo(mm)
    async def fetch(*a, **kw):
        mm.db.invalidate_memory(SCOPE, source_ids=[m['id']])
        return mm.image_block
    mm.engine.media.image = fetch
    await mm.engine.turn(SCOPE, [m])
    assert not mm.model.calls and not mm.db.all('attachment')
    assert not list(mm.engine.attachments.root.iterdir())


async def test_same_image_matches_only_uploaded_persona_reference(mm):
    original = photo(mm)
    aid = image_id(original['id'], 0)
    await mm.engine.attachments.read(SCOPE, 'default', aid, 0, original)
    ref = mm.engine.role_images.save('default', mm.image_raw, '机器人设定图')
    new = photo(mm, 3)
    second = ImageTurn(mm.engine, SCOPE, 'default', [new])
    result = await second.add(image_id(new['id'], 0), new)
    assert result['role_association']['basis'] == 'same_image_reference'
    mm.engine.role_images.delete('default', ref['id'])
    assert not second.current()


@pytest.mark.parametrize('text', ['这是不是你？', '这张不是你', '这张就是你', '都是你的哦'])
async def test_group_statements_never_create_role_reference(mm, text):
    m = photo(mm)
    aid = image_id(m['id'], 0)
    await mm.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    claim = source(mm, text, 2)
    turn = ImageTurn(mm.engine, SCOPE, 'default', [claim])
    await turn.add(aid)
    turn.observe({'image_confirmations': [{'image_id': aid, 'source_message_id': claim['id'], 'quote': text}]})
    assert not mm.db.all('persona_image') and not mm.db.all('role_image')


async def test_admin_reference_shared_by_persona_without_group_provenance(mm):
    ref = mm.engine.role_images.save('default', mm.image_raw, '后台设定')
    mm.db.put('group', OTHER, {**mm.group, 'id': OTHER})
    turn = ImageTurn(mm.engine, OTHER, 'default')
    rows = (await turn.read_tool([], True))['images']
    assert rows[0]['status'] == 'attached'
    messages = []
    await turn.flush(messages)
    assert len(image_blocks(messages)) == 1
    assert SCOPE not in str(messages) and USER not in str(messages) and '都是你的哦' not in str(messages)
    ref['enabled'] = False; mm.db.put('role_image', ref['id'], ref)
    assert not turn.current()


async def test_image_budget_does_not_count_base64_as_text(mm):
    large = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+'A'*500000}}
    data = measure({'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': '看看'}, large]}]})
    assert data['estimated_input'] < 5000 and data['request_bytes'] > 500000
    cfg = mm.db.config(); cfg['image_max_count'] = 1
    with pytest.raises(ContextTooLarge): check_images(measure([large, large]), cfg)
    cfg['image_request_mib'] = 0
    with pytest.raises(ContextTooLarge): check_images(data, cfg)


async def test_text_only_compatibility_uses_existing_image_route(mm):
    m = photo(mm)
    mm.model.catalog = SimpleNamespace(resolve=lambda purpose: {
        'model': {'input_modalities': ['text'] if purpose == 'chat' else ['text', 'image'], 'enabled': True},
        'provider': {'enabled': True}})
    mm.model.script = [{'content': json.dumps({'images': [{'image_id': image_id(m['id'], 0), 'description': '蓝色三角形'}]})}, answer(m)]
    await mm.engine.turn(SCOPE, [m])
    assert [c[3]['stage'] for c in mm.model.calls] == ['vision.describe', 'conversation.agent']
    assert image_blocks(mm.model.calls[0][2])
    assert not image_blocks(mm.model.calls[1][2]) and '蓝色三角形' in str(mm.model.calls[1][2])


@pytest.mark.parametrize('protocol', ['openai-completions', 'anthropic-messages'])
async def test_actual_transport_image_after_complete_tool_results(model, protocol):
    configure_model(model, protocol, 'off')
    requests = []
    def handle(request):
        payload = json.loads(request.content); requests.append(payload)
        result = {'content': [{'type': 'text', 'text': '看到了'}]} if protocol == 'anthropic-messages' else {'choices': [{'message': {'role': 'assistant', 'content': '看到了'}}]}
        return httpx.Response(200, json=result)
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    image = {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+base64.b64encode(b'x'*50000).decode()}}
    messages = [{'role': 'system', 'content': '角色卡'}, {'role': 'user', 'content': '看历史图'},
                {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'read', 'type': 'function', 'function': {'name': 'image_read', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'read', 'content': '{"status":"success"}'}, {'role': 'user', 'content': [image]}]
    await model.chat(SCOPE, 'chat', messages, stage='conversation.agent')
    assert len(requests) == 1
    body = requests[0]
    if protocol == 'anthropic-messages':
        assert [x['type'] for x in body['messages'][-1]['content']] == ['tool_result', 'image']
    else:
        assert body['messages'][-2]['role'] == 'tool'
        assert body['messages'][-1]['content'][0]['type'] == 'image_url'
    detail = model.records.detail(model.records.listing()[0]['id'])
    assert detail['input']['image_count'] == 1
    assert detail['input']['estimated_input'] < 5000 and detail['input']['request_bytes'] > 50000


def test_all_image_slots_retained_and_stable_history_does_not_change_on_observation(mm):
    e = event(id=7)
    e['message'] = [{'type': 'image', 'data': {'url': 'https://gchat.qpic.cn/'+str(i)}} for i in range(8)]
    assert len(normalize(e)['images']) == 8
    m = photo(mm)
    first, _ = mm.engine.memory.build(SCOPE, 'default', m['text'], [m])
    mm.engine.attachments.record_observations(SCOPE, 'default',
        [{'image_id': image_id(m['id'], 0), 'description': '新生成的观察'}], {image_id(m['id'], 0)})
    second, _ = mm.engine.memory.build(SCOPE, 'default', m['text'], [m])
    assert first[0] == second[0] and first[1] == second[1]
    assert '新生成的观察' in second[2]['content']


async def test_restart_reads_local_file_and_collected_file_survives_media_ttl(mm):
    m = photo(mm)
    aid = image_id(m['id'], 0)
    await mm.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    from rolebot.attachments import Attachments
    replacement = Attachments(mm.engine)
    mm.engine.media.image = AsyncMock(side_effect=AssertionError('Should use retained file'))
    item, block = await replacement.read(SCOPE, 'default', aid, 0)
    assert block['image_url']['url'].startswith('data:')
    sticker = mm.engine.stickers.register(SCOPE, mm.image_raw, {'name': '机器人', 'description': '蓝色三角形'}, [m['id']], [USER])
    item['ts'] -= 25*3600
    replacement.store(item, mm.image_raw, sticker['description'], sticker)
    replacement.cleanup()
    item, block = await replacement.read(SCOPE, 'default', aid, 0)
    assert item['sticker_id'] == sticker['id'] and block
    mm.db.invalidate_memory(SCOPE, source_ids=[m['id']])
    assert not mm.db.all('attachment') and not mm.db.all('sticker')


async def test_read_permission_revocation_stops_attached_tool_images(mm):
    m = photo(mm)
    aid = image_id(m['id'], 0)
    await mm.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    turn = ImageTurn(mm.engine, SCOPE, 'default')
    await turn.read_tool([aid], actor=USER)
    assert turn.current()
    mm.group['tools']['image_read']['enabled'] = False
    mm.db.put('group', SCOPE, mm.group)
    assert not turn.current()


async def test_scheduled_generation_reuses_image_tool_loop(mm):
    m = photo(mm)
    aid = image_id(m['id'], 0)
    await mm.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    task = await mm.engine.scheduler.create(SCOPE, 'default', USER,
        {'text': '说明历史图片内容', 'delay_seconds': 5, 'mode': 'model'})
    mm.model.script = [{'role': 'assistant', 'tool_calls': [{'id': 'i', 'type': 'function',
        'function': {'name': 'image_read', 'arguments': json.dumps({'image_ids': [aid]})}}]},
        {'role': 'assistant', 'content': '图里是卡通角色。'}]
    await mm.engine.scheduler.execute(task)
    assert len(mm.model.calls) == 2 and image_blocks(mm.model.calls[-1][2])
    assert mm.db.get('reminder', task['id'])['status'] == 'sent'


def test_image_configuration_and_uploaded_reference_console(authed_client):
    c = authed_client
    engine, db = c.app.state.engine, c.app.state.db
    from rolebot.engine import group_defaults
    group = group_defaults(SCOPE); group['enabled'] = True; db.put('group', SCOPE, group)
    db.add_message(SCOPE+':1', SCOPE, 'default', USER, 'user', '[图片]', {'image_count': 1})
    m = db.message(SCOPE, SCOPE+':1', 'default')
    item = register(db, SCOPE, 'default', m)[0]
    buf = io.BytesIO(); Image.new('RGB', (20, 20), 'blue').save(buf, 'JPEG')
    engine.attachments.store(item, buf.getvalue())
    r = c.post('/api/v1/personas/default/images', json={'scope': SCOPE, 'image_id': item['id']})
    assert r.status_code == 422  # A group attachment ID cannot create a reference.
    r = c.post('/api/v1/personas/default/images?name=reference', files={'file': ('role.jpg', buf.getvalue(), 'image/jpeg')})
    assert r.status_code == 200 and r.json()['source_type'] == 'admin_upload'
    ref = r.json()
    assert c.get('/api/v1/attachments/'+item['id']+'/file').status_code == 200
    file_url = '/api/v1/personas/default/images/'+ref['id']+'/file'
    assert c.get(file_url).status_code == 200
    assert c.put('/api/v1/personas/default/images/'+ref['id'], json={'enabled': False}).status_code == 200
    assert not c.get('/api/v1/personas/default/images').json()[0]['enabled']
    assert c.put('/api/v1/personas/default/images/'+ref['id'], json={'scope': OTHER}).status_code == 422
    assert c.put('/api/v1/settings', json={'image_max_count': 6, 'image_input_budget': 24576, 'image_request_mib': 16}).status_code == 200
    for key, value in [('image_max_count', 0), ('image_input_budget', 100), ('image_request_mib', 129)]:
        assert c.put('/api/v1/settings', json={key: value}).status_code == 422
    db.invalidate_memory(SCOPE, source_ids=[m['id']])
    assert len(c.get('/api/v1/personas/default/images').json()) == 1
    assert c.get('/api/v1/attachments/'+item['id']+'/file').status_code == 404
    replacement = io.BytesIO(); Image.new('RGB', (25, 25), 'red').save(replacement, 'PNG')
    changed = c.put(file_url, files={'file': ('new.png', replacement.getvalue(), 'image/png')})
    assert changed.status_code == 200 and changed.json()['sha256'] != ref['sha256']
    assert not changed.json()['enabled']
    assert c.delete('/api/v1/personas/default/images/'+ref['id']).status_code == 200
    assert c.get(file_url).status_code == 404
    assert not list(engine.role_images.root.glob('*.jpg'))


@pytest.mark.parametrize('tool_name', ['message_send', 'sticker_send'])
async def test_quoted_bot_sticker_uses_the_same_library_file(mm, tool_name):
    item = mm.engine.stickers.register(SCOPE, mm.image_raw, {'name': '机器人比心'})
    m = source(mm, '机器人，发表情', 1)
    args = {'source_message_id': m['id'], **(
        {'text': '给你比心。', 'sticker_id': item['id']} if tool_name == 'message_send' else {'id': item['id']})}
    mm.model.script = [{'role': 'assistant', 'tool_calls': [{'id': 'send', 'type': 'function', 'function': {
        'name': tool_name, 'arguments': json.dumps(args)}}]}, answer(m, text='给你比心。')]
    await mm.engine.turn(SCOPE, [m])
    assert len(mm.model.calls) == 2 and len(mm.bot.sent) == 2
    assert [s['type'] for s in mm.bot.sent[0][1]] == ['text']
    assert [s['type'] for s in mm.bot.sent[1][1]] == ['image']
    assert mm.bot.sent[1][1][0]['data']['sub_type'] == 1
    original = mm.db.recent(SCOPE, 'default')[-1]
    assert original['data']['sticker_id'] == item['id'] and original['data']['image_count'] == 1
    follow = source(mm, '机器人，看看这张的发饰', 2)
    follow['replies'] = [original['data']['message_id']]
    mm.model.script = [answer(follow, text='发饰很可爱。')]
    before = len(mm.model.calls)
    await mm.engine.turn(SCOPE, [follow])
    assert len(mm.model.calls) == before+1 and len(image_blocks(mm.model.calls[-1][2])) == 1
    mm.engine.media.image.assert_not_called()


def test_inherited_image_policy_matches_console_and_unrelated_saves(authed_client):
    c = authed_client
    db, tools = c.app.state.db, c.app.state.tools
    db.put('group', SCOPE, {'id': SCOPE, 'persona': 'default', 'enabled': True, 'revision': 1,
                          'tools': {'history_read': {'enabled': True}}})
    group = next(g for g in c.get('/api/v1/groups').json() if g['id'] == SCOPE)
    assert group['tools']['image_read']['enabled'] and tools.policy(SCOPE, 'image_read')['enabled']
    result = c.put('/api/v1/groups/'+SCOPE+'/tools', json={'calculate': {'enabled': True}})
    assert result.status_code == 200 and result.json()['image_read']['enabled']


async def test_uploaded_reference_is_independent_of_group_pause_and_forgetting(mm):
    ref = mm.engine.role_images.save('default', mm.image_raw, '后台设定')
    mm.db.put('group', OTHER, {**mm.group, 'id': OTHER})
    mm.group['paused'] = True; mm.db.put('group', SCOPE, mm.group)
    turn = ImageTurn(mm.engine, OTHER, 'default')
    result = await turn.read_tool([], True)
    assert result['images'][0]['status'] == 'attached'
    assert turn.current()
    mm.db.invalidate_memory(SCOPE, user_id=USER)
    mm.engine.attachments.cleanup()
    assert mm.engine.role_images.available(ref) and turn.current()


async def test_legacy_chat_references_ignored_and_replacement_cancels_old_view(mm):
    mm.db.put('persona_image', 'legacy', {'id': 'legacy', 'scope': SCOPE, 'persona': 'default', 'global': True,
                                        'image_id': 'img_'+'0'*32, 'enabled': True, 'ts': time.time()})
    turn = ImageTurn(mm.engine, SCOPE, 'default')
    assert not (await turn.read_tool([], True))['images']
    ref = mm.engine.role_images.save('default', mm.image_raw, '设定')
    assert (await turn.read_tool([], True))['images'][0]['status'] == 'attached'
    other = io.BytesIO(); Image.new('RGB', (100, 100), 'red').save(other, 'PNG')
    mm.engine.role_images.save('default', other.getvalue(), replace_id=ref['id'])
    assert not turn.current()
    with pytest.raises(ValueError):
        mm.engine.role_images.save('missing-role', other.getvalue(), replace_id=ref['id'])


@pytest.mark.parametrize('case', ['invalid', 'svg', 'oversize'])
def test_invalid_reference_uploads_leave_no_files_or_records(mm, case):
    raw = {'invalid': b'not an image', 'svg': b'<svg>no</svg>'}.get(case, b'')
    if case == 'oversize':
        raw = b'x'*(8*1024*1024+1)
    with pytest.raises((ValueError, OSError)):
        mm.engine.role_images.save('default', raw)
    assert not list(mm.engine.role_images.root.iterdir()) and not mm.db.all('persona_image') and not mm.db.all('role_image')


NT_URL = 'https://multimedia.nt.qq.com.cn/download?appid=1407&rkey=private-sentinel'


@pytest.fixture
async def public_dns(monkeypatch):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)

async def test_ntqq_domain_accepted_and_lookalikes_rejected(public_dns):
    assert await public_url(NT_URL, qq_only=True) == NT_URL
    for url in ['https://multimedia.nt.qq.com.cn.evil.example/x', 'https://evil-qq.com.cn/x', 'https://u:p@multimedia.nt.qq.com.cn/x', 'file:///etc/passwd']:
        with pytest.raises(ValueError): await public_url(url, qq_only=True)

async def test_ntqq_domain_with_private_dns_is_still_blocked(monkeypatch):
    async def resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', port))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', resolve)
    with pytest.raises(ValueError, match='内网'): await public_url(NT_URL, qq_only=True)

async def test_media_download_follows_only_validated_qq_redirects(runtime, monkeypatch, public_dns):
    raw = io.BytesIO(); Image.new('RGB', (90, 40), 'blue').save(raw, 'PNG')
    calls = []
    def handle(req):
        calls.append(str(req.url))
        if req.url.host == 'multimedia.nt.qq.com.cn':
            return httpx.Response(302, headers={'location': 'https://gchat.qpic.cn/image'})
        return httpx.Response(200, content=raw.getvalue())
    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    image = await runtime.engine.media.image(NT_URL, persist=False)
    assert len(calls) == 2 and image['image_url']['url'].startswith('data:image/png;base64,')
    assert image['image_url']['detail'] == 'auto'
    assert not list(runtime.engine.media.root.iterdir())

async def test_redirect_to_untrusted_or_private_host_is_not_requested(runtime, monkeypatch, public_dns):
    calls = []
    def handle(req):
        calls.append(str(req.url)); return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    with pytest.raises(ValueError): await runtime.engine.media.image(NT_URL, persist=False)
    assert len(calls) == 1


@pytest.mark.parametrize('declared', [False, True])
async def test_source_download_is_bounded_with_or_without_content_length(runtime, monkeypatch, public_dns, declared):
    monkeypatch.setattr(module, 'MAX_SOURCE_BYTES', 128)
    received = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(4):
                received.append(1)
                yield b'x'*65536
    def handle(req):
        return httpx.Response(200, headers={'content-length': '1000'} if declared else {}, stream=Stream())
    client = httpx.AsyncClient
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kw: client(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(MediaError) as caught:
        await runtime.engine.media.image(NT_URL)
    assert caught.value.code == 'image_too_large'
    assert len(received) == (0 if declared else 1)
    assert not list(runtime.engine.media.root.iterdir())

def picture(r, id=1):
    m = source(r, '[图片]', id)
    m['images'] = [NT_URL]; m['types'] = ['image']
    r.db.patch_message_data(SCOPE, m['id'], {'image_count': 1, 'types': ['image'], 'image_descriptions': [{'index': 0, 'source_id': m['id'], 'status': 'unknown', 'description': '图片读取失败，内容未知'}]})
    return m

def refreshed(m, **override):
    return {'message_id': m['message_id'], 'group_id': SCOPE.split(':')[-1], 'user_id': USER,
            'message': [{'type': 'image', 'data': {'url': NT_URL+'&fresh=1'}}], **override}


async def test_failed_download_refreshes_known_message_once_and_never_logs_url(runtime):
    r = runtime; m = picture(r); calls = []
    async def image(url, persist=False):
        calls.append(url)
        if 'fresh=1' not in url:
            raise httpx.HTTPStatusError('secret '+NT_URL, request=httpx.Request('GET', NT_URL), response=httpx.Response(403))
        raw=io.BytesIO();Image.new('RGB',(20,20),'red').save(raw,'PNG')
        return prepare(raw.getvalue())
    async def call(action, params, **kwargs):
        assert action == 'get_msg' and params['message_id'] == '1'
        return refreshed(m)
    r.engine.media.image = image; r.bot.call = call
    aid=register(r.db,SCOPE,'default',m)[0]['id']
    await r.engine.attachments.read(SCOPE,'default',aid,r.engine.memory.epoch(SCOPE),transient=m)
    data = r.db.get('attachment',aid)
    assert len(calls) == 2 and data['status'] == 'available'
    assert 'private-sentinel' not in json.dumps(data)
    assert 'private-sentinel' not in str(r.db.all('trace'))


@pytest.mark.parametrize('condition', ['forgotten', 'optout', 'other_group', 'other_role', 'expired'])
async def test_inaccessible_images_never_refetched(runtime, condition):
    r = runtime; m = picture(r); later = source(r, '机器人，这张图能看吗', 2); later['replies'] = [m['id']]
    aid=register(r.db,SCOPE,'default',m)[0]['id']
    if condition == 'forgotten': r.db.invalidate_memory(SCOPE, source_ids=[m['id']])
    if condition == 'optout': r.db.put('member', SCOPE+':'+USER, {'remember': False})
    if condition == 'other_group': r.db.execute('UPDATE messages SET scope=? WHERE id=?', (OTHER, m['id']))
    if condition == 'other_role': r.db.execute("UPDATE messages SET persona='other' WHERE id=?", (m['id'],))
    if condition == 'expired': r.db.execute('UPDATE messages SET ts=? WHERE id=?', (time.time()-25*3600, m['id']))
    calls = []
    async def forbidden(*a, **kw):
        calls.append(1)
        raise AssertionError('Must not query inaccessible QQ history')
    r.bot.call = forbidden
    with pytest.raises(MediaError):
        await r.engine.attachments.read(SCOPE,'default',aid,r.engine.memory.epoch(SCOPE))
    assert not calls and not r.model.calls

async def test_refresh_checks_group_and_sender(runtime):
    r = runtime; m = picture(r)
    for mismatch in ({'group_id': '99999999'}, {'user_id': '99999999'}, {'message_id': '99999999'}):
        async def call(*a, **kw): return refreshed(m, **mismatch)
        r.bot.call = call
        with pytest.raises(MediaError, match='不匹配'): await r.engine.vision.refresh(SCOPE, 'default', m, r.engine.memory.epoch(SCOPE))

async def test_forget_during_refresh_prevents_vision_and_writeback(runtime):
    r = runtime; m = picture(r); later = source(r, '机器人，这张图', 2); later['replies'] = ['1']
    async def call(*args, **kwargs):
        r.db.invalidate_memory(SCOPE, source_ids=[m['id']]); return refreshed(m)
    r.bot.call = call
    with pytest.raises(MediaError):
        await r.engine.vision.refresh(SCOPE,'default',m,r.engine.memory.epoch(SCOPE))
    assert not r.model.calls and not r.db.message(SCOPE, m['id'])


def test_image_without_url_keeps_slot_and_error_codes_are_safe():
    e = event(); e['message'] = [{'type': 'image', 'data': {'file': 'photo.jpg'}}]
    assert normalize(e)['images'] == ['']
    assert image_error(ValueError(NT_URL)) == 'source_rejected'
