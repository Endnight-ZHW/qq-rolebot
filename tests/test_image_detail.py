from support import tool_reply
from unittest.mock import AsyncMock
import base64
import hashlib
import io
import json
import time

from PIL import Image, ImageDraw
from pydantic import ValidationError
import httpx
import pytest

from rolebot.attachments import Attachments, image_id, register
from rolebot.image_assets import prepare, region
from rolebot.image_turn import ImageTurn
from rolebot.media import MediaError, normalize_image
from rolebot.tools import ImageRead

from support import SCOPE, OTHER, USER, source, configure_model, auth


def raw_image(fmt='PNG', size=(600, 300), orientation=None):
    im = Image.new('RGB', size, '#fafafa')
    draw = ImageDraw.Draw(im)
    draw.text((20, 12), 'HMCL 3.7.6 / SystemSystemUpdate:3', fill='black')
    draw.line((0, 70, size[0], 70), fill='#018080', width=1)
    b = io.BytesIO()
    kwargs = {}
    if orientation:
        exif = Image.Exif(); exif[274] = orientation; kwargs['exif'] = exif
    im.save(b, fmt, **kwargs)
    return b.getvalue()


def block_bytes(block):
    return base64.b64decode(block['image_url']['url'].split(',', 1)[1])


@pytest.fixture
def detail(runtime):
    r = runtime
    r.group.update(interaction_enabled=True, sticker_autocollect=False)
    for name in ('image_read', 'history_read', 'history_search'):
        r.group['tools'][name]['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    r.source_raw = raw_image(size=(2400, 1200))
    r.engine.media.image = AsyncMock(return_value=prepare(r.source_raw))
    return r


def picture(r, id=1, remember=True):
    m = source(r, '机器人，这张截图是什么？', id)
    m.update(images=['https://gchat.qpic.cn/example'], types=['image', 'text'], remember=remember)
    m['data'].update(image_count=1, types=['image', 'text'])
    r.db.patch_message_data(SCOPE, m['id'], m['data'])
    if not remember:
        r.db.execute('DELETE FROM messages WHERE id=?', (m['id'],))
    register(r.db, SCOPE, 'default', m)
    return m, image_id(m['id'], 0)


def answer(q):
    return {'role': 'assistant', 'content': json.dumps({'speak': True, 'text': '截图中的版本号是 3.7.6。',
        'mode': 'direct', 'reply_to_message_id': q['id'], 'continues_dialogue': False})}


@pytest.mark.parametrize('fmt', ['PNG', 'JPEG', 'WEBP'])
def test_small_static_images_preserve_original_bytes_and_mime(fmt):
    raw = raw_image(fmt)
    block = prepare(raw)
    assert block_bytes(block) == raw
    assert block.source == raw
    assert block['image_url']['url'].startswith('data:image/'+{'PNG': 'png', 'JPEG': 'jpeg', 'WEBP': 'webp'}[fmt])
    assert block.metadata['source_size'] == block.metadata['preview_size'] == [600, 300]
    assert 'source_sha256' not in json.dumps(block)  # Sidecar bytes/metadata never reach providers.
    assert set(block) == {'type', 'image_url'}


def test_large_screenshot_region_uses_original_pixels_not_resized_preview():
    raw = raw_image(size=(4000, 2000))
    block = prepare(raw)
    assert block.metadata['preview_size'] == [1600, 800]
    cropped, meta = region(block.source, [0, 0, 250, 150])
    assert meta['pixel_bounds'] == [0, 0, 1000, 300]
    with Image.open(io.BytesIO(raw)) as original, Image.open(io.BytesIO(block_bytes(cropped))) as view:
        assert view.format == 'PNG'
        assert view.size == (1000, 300)
        assert view.tobytes() == original.crop((0, 0, 1000, 300)).tobytes()


def test_original_over_eight_mib_is_previewed_and_keeps_region_pixels():
    im = Image.new('RGB', (1800, 1800), '#da8080')
    ImageDraw.Draw(im).rectangle((300, 300, 600, 600), fill='blue')
    out = io.BytesIO(); im.save(out, 'PNG', compress_level=0)
    raw = out.getvalue()
    assert 8*1024*1024 < len(raw) < 32*1024*1024
    block = prepare(raw)
    assert block.source == raw and len(block_bytes(block)) < 8*1024*1024
    assert block.metadata['source_size'] == [1800, 1800]
    crop, meta = region(block.source, [100, 100, 400, 400])
    with Image.open(io.BytesIO(block_bytes(crop))) as view:
        assert view.tobytes() == im.crop(tuple(meta['pixel_bounds'])).tobytes()


@pytest.mark.parametrize('limit', ['bytes', 'pixels'])
def test_oversized_sources_keep_distinct_errors(limit):
    if limit == 'bytes':
        raw = b'x'*(32*1024*1024+1)
    else:
        out = io.BytesIO(); Image.new('RGB', (5000, 4001)).save(out, 'PNG'); raw = out.getvalue()
    with pytest.raises(MediaError) as caught:
        prepare(raw)
    assert caught.value.code == ('image_too_large' if limit == 'bytes' else 'image_dimensions_exceeded')


def test_exif_orientation_has_same_coordinates_for_preview_and_region():
    raw = raw_image('JPEG', orientation=6)
    block = prepare(raw)
    assert block.metadata['source_size'] == block.metadata['preview_size'] == [300, 600]
    _, meta = region(raw, [0, 0, 1000, 500])
    assert meta['source_size'] == [300, 600] and meta['pixel_bounds'] == [0, 0, 300, 300]


@pytest.mark.parametrize('box', [[0, 0, 0, 100], [20, 20, 10, 30], [-1, 0, 200, 200],
                                 [0, 0, 1001, 1000], [0, 0, 1, 1], [0, 0, 1000], [0., 0, 100, 100]])
def test_region_rejects_invalid_or_subpixel_areas(box):
    with pytest.raises(MediaError):
        region(raw_image(), box)


@pytest.mark.parametrize('box', [[0, 0, 0, 100], [10, 0, 1, 100], [0, 0, 1001, 100], [0., 0, 100, 100]])
def test_tool_schema_validates_regions_before_io(box):
    with pytest.raises(ValidationError):
        ImageRead(image_ids=[], region={'image_id': 'img_'+'a'*32, 'box': box})


async def test_source_and_preview_survive_restart_then_forget(detail):
    m, aid = picture(detail)
    item, block = await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    assert item['mime'] == 'image/png' and item['source_size'] == [2400, 1200]
    assert block.source == detail.source_raw
    restarted = Attachments(detail.engine)
    detail.engine.media.image.reset_mock()
    item, restored = await restarted.read(SCOPE, 'default', aid, 0)
    assert restored.source == detail.source_raw and block_bytes(restored) == block_bytes(block)
    detail.engine.media.image.assert_not_called()
    assert restarted.paths(aid)[2].read_bytes() == detail.source_raw
    detail.db.invalidate_memory(SCOPE, source_ids=[m['id']])
    assert not any(p.exists() for p in restarted.paths(aid))
    with pytest.raises(MediaError):
        await restarted.read(SCOPE, 'default', aid, detail.engine.memory.epoch(SCOPE))


async def test_legacy_jpeg_upgrades_from_original_and_retains_rollback_file(detail):
    m, aid = picture(detail)
    legacy = normalize_image(detail.source_raw)
    paths = detail.engine.attachments.paths(aid)
    paths[0].write_bytes(legacy)
    old = detail.db.get('attachment', aid)
    old['sha256'] = hashlib.sha256(legacy).hexdigest(); detail.db.put('attachment', aid, old)
    item, block = await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    assert block['image_url']['url'].startswith('data:image/png')
    assert item['source_quality'] == 'retained_original'
    assert item['legacy_sha256'] == old['sha256']
    assert paths[0].read_bytes() == legacy and paths[2].read_bytes() == detail.source_raw


async def test_failed_legacy_upgrade_uses_honest_preview_and_bounded_retry(detail):
    m, aid = picture(detail)
    old = normalize_image(detail.source_raw)
    detail.engine.attachments.paths(aid)[0].write_bytes(old)
    detail.engine.media.image = AsyncMock(side_effect=RuntimeError('download unavailable'))
    item, block = await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    assert item['source_quality'] == 'legacy_preview' and block.source == old
    assert item['source_upgrade_after'] > time.time()
    calls = detail.engine.media.image.await_count
    await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    assert detail.engine.media.image.await_count == calls


async def test_optout_region_stays_in_memory(detail):
    m, aid = picture(detail, remember=False)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    await turn.prepare()
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'attached'
    assert not detail.db.all('attachment') and not list(detail.engine.attachments.root.iterdir())
    assert all(x['ephemeral'] for x in turn.loaded.values())


async def test_region_tool_adds_one_view_and_never_calls_search_or_caption(detail):
    m, aid = picture(detail)
    q = source(detail, '机器人，图上版本号的小字是多少？', 2)
    detail.model.script = [tool_reply('image_read', {'image_ids': [], 'region': {
            'image_id': aid, 'box': [0, 0, 500, 200]}, 'source_message_id': q['id']}, id='region1'), answer(q)]
    # Known history image remains source-scoped; no original needed in the first model call.
    await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    await detail.engine.turn(SCOPE, [q])
    assert len(detail.model.calls) == 2
    assert [c[3]['stage'] for c in detail.model.calls] == ['conversation.agent', 'conversation.agent']
    messages = detail.model.calls[1][2]
    result = json.loads(next(msg['content'] for msg in messages if msg['role'] == 'tool'))
    assert result['status'] == 'success' and result['result']['images'][0]['view'] == 'region'
    blocks = [b for msg in messages if isinstance(msg.get('content'), list) for b in msg['content'] if b['type'] == 'image_url']
    assert len(blocks) == 1 and blocks[0]['image_url']['url'].startswith('data:image/png')
    assert '局部' in str(messages) and not any('search' in call[0] for call in detail.bot.calls)


async def test_direct_screenshot_still_uses_single_main_call(detail):
    m, _ = picture(detail)
    detail.model.script = [answer(m)]
    await detail.engine.turn(SCOPE, [m])
    assert len(detail.model.calls) == 1 and len(detail.bot.sent) == 1
    assert 'data:image/png' in str(detail.model.calls[0][2])


async def test_only_one_region_per_turn_even_after_failure(detail):
    m, aid = picture(detail)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    first = await turn.read_tool([], region={'image_id': 'img_'+'f'*32, 'box': [0, 0, 1000, 100]}, actor=USER)
    second = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 1000, 100]}, actor=USER)
    assert first['images'][0]['status'] == 'unavailable'
    assert second['images'][0]['error_code'] == 'region_limit'
    assert not turn.loaded
    detail.engine.media.image.assert_not_called()


async def test_crop_counts_toward_existing_image_budget_and_not_full_description(detail):
    m, aid = picture(detail)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    await turn.prepare()
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'attached' and len(turn.loaded) == 2
    turn.observe({'image_observations': [{'image_id': aid, 'description': '只有一个数字'}]})
    assert not detail.db.get('attachment', aid).get('observation')
    turn.remember_focus({'continues_dialogue': True})
    assert detail.db.get('image_focus', SCOPE+':default')['image_ids'] == [aid]
    limited = ImageTurn(detail.engine, SCOPE, 'default', [m])
    limited.limit = 1
    await limited.prepare()
    result = await limited.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'budget_exceeded' and len(limited.loaded) == 1


async def test_region_obeys_transport_and_context_budgets(detail):
    m, aid = picture(detail)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    await turn.prepare()
    cfg = detail.db.config(); cfg['image_request_mib'] = 1; detail.db.put('settings', 'global', cfg)
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'budget_exceeded'
    cfg['image_request_mib'] = 16; detail.db.put('settings', 'global', cfg)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    messages = []; await turn.flush(messages, input_budget=10)
    assert not any(b.get('type') == 'image_url' for msg in messages for b in msg.get('content', []))
    assert 'budget_exceeded' in str(messages)


@pytest.mark.parametrize('condition', ['other_group', 'other_role', 'forgotten', 'expired', 'optout'])
async def test_regions_obey_source_visibility(detail, condition):
    m, aid = picture(detail)
    await detail.engine.attachments.read(SCOPE, 'default', aid, 0, m)
    if condition == 'other_group': detail.db.execute('UPDATE messages SET scope=? WHERE id=?', (OTHER, m['id']))
    if condition == 'other_role': detail.db.execute("UPDATE messages SET persona='other' WHERE id=?", (m['id'],))
    if condition == 'forgotten': detail.db.invalidate_memory(SCOPE, source_ids=[m['id']])
    if condition == 'optout': detail.db.put('member', SCOPE+':'+USER, {'remember': False})
    if condition == 'expired':
        item = detail.db.get('attachment', aid); item['ts'] -= 25*3600; detail.db.put('attachment', aid, item)
    detail.engine.media.image.reset_mock()
    turn = ImageTurn(detail.engine, SCOPE, 'default')
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'unavailable' and not turn.loaded
    detail.engine.media.image.assert_not_called()


@pytest.mark.parametrize('condition', ['forget', 'revoke'])
async def test_revocation_during_region_processing_cancels(detail, monkeypatch, condition):
    from rolebot import image_assets
    m, aid = picture(detail)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    await turn.prepare()
    original = image_assets.region
    def changed(*args):
        result = original(*args)
        if condition == 'forget': detail.db.invalidate_memory(SCOPE, source_ids=[m['id']])
        else:
            detail.group['tools']['image_read']['enabled'] = False
            detail.db.put('group', SCOPE, detail.group)
        return result
    monkeypatch.setattr(image_assets, 'region', changed)
    result = await turn.read_tool([], region={'image_id': aid, 'box': [0, 0, 500, 200]}, actor=USER)
    assert result['images'][0]['status'] == 'unavailable'
    assert not any(d.get('region_of') for d in turn.loaded.values())
    assert not turn.current()


async def test_uploaded_role_reference_still_matches_same_png(detail):
    raw = raw_image()
    detail.engine.media.image = AsyncMock(return_value=prepare(raw))
    ref = detail.engine.role_images.save('default', raw, '上传参考')
    m, aid = picture(detail)
    turn = ImageTurn(detail.engine, SCOPE, 'default', [m])
    result = await turn.add(aid, m)
    assert result['role_association']['basis'] == 'same_image_reference'
    assert ref['sha256'] == detail.db.get('attachment', aid)['reference_sha256']


@pytest.mark.parametrize('protocol', ['openai-completions', 'anthropic-messages'])
async def test_png_region_uses_correct_mime_in_both_protocols(model, protocol):
    configure_model(model, protocol, 'off')
    captured = []
    def handle(req):
        captured.append(json.loads(req.content))
        value = {'content': [{'type': 'text', 'text': '看到了'}]} if protocol == 'anthropic-messages' else {
            'choices': [{'message': {'role': 'assistant', 'content': '看到了'}}]}
        return httpx.Response(200, json=value)
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    block, _ = region(raw_image(), [0, 0, 1000, 500])
    await model.chat(SCOPE, 'chat', [{'role': 'user', 'content': [block]}])
    b = captured[0]['messages'][-1]['content'][0]
    assert (b['source']['media_type'] if protocol == 'anthropic-messages' else b['image_url']['url'].split(';')[0][5:]) == 'image/png'
    assert 'source_sha256' not in json.dumps(captured)


def test_admin_preview_serves_png_and_media_expiry_cleans_all_variants(client):
    client.headers.update(auth(client))
    engine = client.app.state.engine
    mid = SCOPE+':987'
    data = {'message_id': '987', 'image_count': 1, 'types': ['image']}
    engine.db.add_message(mid, SCOPE, 'default', USER, 'user', '[图片]', data)
    message = engine.db.message(SCOPE, mid, 'default')
    item = register(engine.db, SCOPE, 'default', message)[0]
    raw = raw_image()
    engine.attachments.store(item, raw)
    aid = item['id']
    response = client.get('/api/v1/attachments/'+aid+'/file')
    assert response.status_code == 200 and response.headers['content-type'] == 'image/png'
    assert response.content == raw
    # Cleanup also removes the pre-upgrade variant if one was retained.
    engine.attachments.paths(aid)[0].write_bytes(normalize_image(raw))
    abandoned = [p.with_suffix(p.suffix+'.tmp') for p in engine.attachments.paths(aid)[1:]]
    for path in abandoned:
        path.write_bytes(raw)
    item = engine.db.get('attachment', aid); item['ts'] -= 25*3600; engine.db.put('attachment', aid, item)
    assert client.get('/api/v1/attachments/'+aid+'/file').status_code == 410
    engine.attachments.cleanup()
    assert not any(p.exists() for p in engine.attachments.paths(aid))
    assert not any(p.exists() for p in abandoned)
