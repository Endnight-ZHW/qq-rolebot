from rolebot.delivery import send_slots
import asyncio
import base64
import io
import json
import re
import time

from PIL import Image
import pytest

from rolebot import delivery
from rolebot.delivery import interval, recover, split_reply
from rolebot.engine import group_defaults
from rolebot.messaging import sticker_path
from rolebot.onebot import ActionUnknown, OneBotError, normalize
from rolebot.permissions import AccessConfig
from rolebot.reply_review import ReplyReview, relation

from support import OTHER, SCOPE, SECOND, USER, auth, event, message, participation, reply_decision, source


LONG = ('我觉得可以先选离大家近一点的地方，这样下班后赶过去也不用太着急，路上还能顺便买一点大家喜欢的零食。\n\n'
        '时间就先定七点半吧，晚到的群友直接过来就好，不用为了等人一直推迟，提前到的大家可以先找位置坐下来休息。\n\n'
        '店铺的话我更偏向上次那家，比较机器人，也方便大家坐着慢慢聊，如果有人想去别的地方，也可以一起商量看看。')


@pytest.mark.parametrize('gap', [0, 1, 2, 3, 4, 5])
async def test_quote_only_when_more_than_three_messages_intervene(runtime, gap):
    r = runtime
    target = source(r, '机器人，你觉得怎么样？', 1)
    batch = [target]+[source(r, '其他聊天内容', i+2) for i in range(gap)]
    r.model.script = [reply_decision(target, text='我觉得挺合适的。')]
    await r.engine.turn(SCOPE, batch)
    sent = r.bot.sent[-1][1]
    quotes = [s for s in sent if s['type'] == 'reply']
    assert bool(quotes) == (gap > 3)
    if quotes:
        assert quotes[0]['data']['id'] == target['message_id']
    stored = r.db.recent(SCOPE, 'default')[-1]
    assert stored['data']['replies'] == ([target['message_id']] if gap > 3 else [])
    assert any(s['data'].get('quoted') == (gap > 3) for s in r.db.all('trace')[0]['stages'] if s['name'] == 'sent')

async def test_quote_distance_is_checked_after_generation_and_review(runtime):
    r = runtime
    target = source(r, '机器人，你觉得怎么样？', 1)
    async def chat(scope, purpose, messages, **kwargs):
        if purpose == 'chat':
            newer = [source(r, '继续聊聊', i+2) for i in range(4)]
            r.engine.recent_events[SCOPE] = [target]+newer
            return reply_decision(target, text='我觉得挺合适的。')
        if messages[0]['content'].startswith('复核准备发送'):
            return {'content': json.dumps({'send': True, 'reason': '原话题仍适用'})}
        return reply_decision(target)
    r.model.chat = chat
    await r.engine.turn(SCOPE, [target])
    assert r.bot.sent[-1][1][0] == {'type': 'reply', 'data': {'id': '1'}}

def test_visible_command_image_and_optout_messages_count_once(runtime):
    r = runtime
    target = normalize(event('机器人你好', 1))  # No persisted row, as with an opted-out member.
    r.engine.observe_visible(event('机器人你好', 1))
    for i in range(2, 5):
        e = event('/bot status' if i == 2 else '临时内容', i)
        if i == 3:
            e['message'] = [{'type': 'image', 'data': {'url': 'https://example.org/image'}}]
        r.engine.observe_visible(e); r.engine.observe_visible(e)
    assert not r.engine.should_quote(SCOPE, target, [target])
    r.db.put('group', OTHER, {**r.group, 'id': OTHER})
    for i in range(6):
        r.engine.observe_visible(event('另一个群', i+10, scope=OTHER))
    assert not r.engine.should_quote(SCOPE, target, [target])
    e = event('第四条', 5); e['post_type'] = 'message_sent'
    r.engine.observe_visible(e)
    assert r.engine.should_quote(SCOPE, target, [target])
    assert '临时内容' not in repr(r.engine.visible_messages)

@pytest.mark.parametrize('scope,id', [('10000001:p:30000001', '1'), (SCOPE, 'join-1')])
def test_private_or_synthetic_target_does_not_get_qq_quote(runtime, scope, id):
    target = {'id': scope+':'+id, 'message_id': id, 'scope': scope, 'ts': 0}
    batch = [target]+[{'id': scope+':'+str(i), 'scope': scope, 'ts': i} for i in range(2, 8)]
    assert not runtime.engine.should_quote(scope, target, batch)

def compact(text): return re.sub(r'\s+', '', text)

@pytest.fixture
def segmented(runtime):
    r = runtime; r.group['segmented_reply'] = True; r.db.put('group', SCOPE, r.group)
    r.delays = []
    async def pause(seconds): r.delays.append(seconds)
    r.engine.delivery_sleep = pause
    return r

def prepare(r, text='机器人，聊一下明天安排', reply=LONG):
    m = source(r, text, 1)
    r.model.script = [reply_decision(m, text=reply)]
    if not r.engine.direct([m], r.db.get('persona', 'default')):
        r.model.script.insert(0, participation())
    return m

def assistants(r): return [m for m in r.db.recent(SCOPE, 'default') if m['role'] == 'assistant']


async def test_configured_five_segments_and_larger_send_budget(segmented):
    r = segmented
    r.group.update(segment_max_parts=5, send_per_minute=40)
    r.db.put('group', SCOPE, r.group)
    r.db.put('chat_state', SCOPE, {'sent':[time.time()]*25})
    target = prepare(r, reply=LONG*3)
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 5
    assert len(r.db.get('chat_state', SCOPE)['sent']) == 30
    assert compact(''.join(m['text'] for m in assistants(r))) == compact(LONG*3)


@pytest.mark.parametrize('timeout,sent', [(120,False),(900,True)])
async def test_configured_reply_timeout_reaches_delivery(runtime, timeout, sent):
    r = runtime
    r.group['reply_timeout_seconds'] = timeout
    r.db.put('group', SCOPE, r.group)
    target = prepare(r, reply='这是仍然需要送达的回答。')
    target['ts'] -= 180
    await r.engine.turn(SCOPE,[target])
    assert bool(r.bot.sent) is sent

def test_split_limits_and_preserves_complete_content():
    parts = split_reply(LONG)
    assert len(parts) == 3 and all(len(p) >= 12 for p in parts)
    assert compact(''.join(parts)) == compact(LONG)
    assert len(split_reply(LONG[:110]+'')) == 2
    assert split_reply('你好。') == ['你好。']
    assert split_reply('长'*100) == ['长'*100]
    assert split_reply(LONG, max_parts=1) == [LONG]

@pytest.mark.parametrize('text', ['```python\n'+LONG+'\n```', '- 第一项\n- '+LONG, '1. 第一项\n2. '+LONG, '| 第一列 | 第二列 |\n'+LONG])
def test_structured_content_stays_together(text):
    assert split_reply(text) == [text]

def test_split_protects_links_actions_emoji_and_numbers():
    protected = ['https://example.org/a?query=yes#more', '（我歪歪头。看着大家！又点了点头）', '(๑•̀ㅂ•́)و✧', '`a.b!?c`', '3.14159', '👨‍👩‍👧‍👦']
    text = LONG.split('\n\n')[0]+''.join(protected)+LONG
    parts = split_reply(text)
    assert compact(''.join(parts)) == compact(text)
    for value in protected: assert any(value in p for p in parts)
    assert all(1 <= interval('字'*n, 1, 2.5) <= 2.5 for n in range(500))

async def test_confirmed_parts_persist_with_one_source_group_and_real_rate_count(segmented):
    r = segmented; m = prepare(r, text='聊一下明天安排')
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 3 and len(r.delays) == 2 and all(1 <= d <= 2.5 for d in r.delays)
    rows = assistants(r)
    assert compact(''.join(m['text'] for m in rows)) == compact(LONG)
    assert len({m['data']['delivery_id'] for m in rows}) == 1
    assert [m['data']['part'] for m in rows] == [1, 2, 3]
    state = r.db.get('chat_state', SCOPE)
    assert len(state['sent']) == 3 and len(state['proactive']) == 1 and len(state['answered']) == 1
    assert r.db.all('reply_delivery')[0]['status'] == 'sent'

@pytest.mark.parametrize('used,expected', [(18, 2), (19, 1), (20, 0)])
async def test_remaining_rate_slots_merge_whole_answer(segmented, used, expected):
    r = segmented; m = prepare(r)
    r.db.put('chat_state', SCOPE, {'sent': [time.time()]*used, 'proactive': []})
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == expected
    if expected: assert compact(''.join(x['text'] for x in assistants(r))) == compact(LONG)
    assert len(r.db.get('chat_state', SCOPE)['sent']) <= delivery.SEND_LIMIT

async def test_quote_is_only_on_first_part(segmented):
    r = segmented; m = prepare(r)
    batch = [m]+[source(r, '无关内容', i+2) for i in range(4)]
    await r.engine.turn(SCOPE, batch)
    assert [any(s['type'] == 'reply' for s in msg) for _, msg in r.bot.sent] == [True, False, False]

async def test_unrelated_interjection_continues_without_model_call(segmented):
    r = segmented; m = prepare(r)
    async def pause(seconds):
        if not r.delays:
            new = source(r, '篮球冠军终于赢了', 50, actor='30000002')
            r.engine.recent_events[SCOPE] = [m, new]
        r.delays.append(seconds)
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 3 and len(r.model.calls) == 1

@pytest.mark.parametrize('continue_sending', [True, False])
async def test_related_interjection_pauses_and_only_sent_content_enters_memory(segmented, continue_sending):
    r = segmented; m = prepare(r)
    r.model.script.append({'content': json.dumps({'send': continue_sending, 'reason': '复核结论'})})
    async def pause(seconds):
        if not r.delays:
            new = source(r, '时间改了，先停一下', 50)
            r.engine.recent_events[SCOPE] = [m, new]
        r.delays.append(seconds)
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2
    assert len(r.bot.sent) == (3 if continue_sending else 1)
    assert len(assistants(r)) == len(r.bot.sent)
    if not continue_sending:
        assert r.db.all('reply_delivery')[0]['status'] == 'partial'
        assert split_reply(LONG)[-1] not in json.dumps(r.db.all('reply_delivery'), ensure_ascii=False)
        assert split_reply(LONG)[-1] not in ''.join(x['text'] for x in assistants(r))

async def test_review_failure_stops_remaining_parts(segmented):
    r = segmented; m = prepare(r)
    r.model.script.append({'content': 'invalid json'})
    async def pause(seconds):
        r.engine.recent_events[SCOPE] = [m, source(r, '机器人，等等', 50)]
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 1 and r.db.all('reply_delivery')[0]['status'] == 'partial'

@pytest.mark.parametrize('change', ['paused', 'segmented_reply', 'role', 'forget'])
async def test_live_changes_stop_remaining_draft(segmented, change):
    r = segmented; m = prepare(r)
    async def pause(seconds):
        if change == 'forget': r.db.invalidate_memory(SCOPE, source_ids=[m['id']]); return
        group = r.db.get('group', SCOPE)
        if change == 'role': group['persona'] = 'another'
        else: group[change] = change == 'paused'
        r.db.put('group', SCOPE, group)
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 1
    if change == 'forget': assert not assistants(r)

@pytest.mark.parametrize('unknown', [True, False])
async def test_partial_send_failure_never_retries_or_records_unsent_text(segmented, unknown):
    r = segmented; m = prepare(r); send = r.bot.send; attempts = 0
    async def fail(scope, message):
        nonlocal attempts
        attempts += 1
        if attempts == 2: raise ActionUnknown('unknown') if unknown else OneBotError('failed')
        return await send(scope, message)
    r.bot.send = fail
    await r.engine.turn(SCOPE, [m])
    record = r.db.all('reply_delivery')[0]
    assert record['status'] == ('partial_unknown' if unknown else 'partial_failed') and record['sent_count'] == 1
    assert attempts == 2 and len(assistants(r)) == 1
    recover(r.db)
    assert attempts == 2 and r.db.all('reply_delivery')[0]['status'] == record['status']

async def test_forget_while_message_is_inflight_does_not_restore_content(segmented):
    r = segmented; m = prepare(r); send = r.bot.send
    async def forget(scope, outgoing):
        r.db.invalidate_memory(scope, source_ids=[m['id']])
        return await send(scope, outgoing)
    r.bot.send = forget
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 1 and not assistants(r)
    assert LONG not in json.dumps(r.db.all('trace'), ensure_ascii=False)

async def test_waiting_one_group_does_not_block_another(segmented):
    r = segmented; a = prepare(r)
    paused, release = asyncio.Event(), asyncio.Event()
    async def pause(seconds): paused.set(); await release.wait()
    r.engine.delivery_sleep = pause
    task = asyncio.create_task(r.engine.turn(SCOPE, [a]))
    await asyncio.wait_for(paused.wait(), 2)
    other = {**r.group, 'id': OTHER}; r.db.put('group', OTHER, other)
    b = {**a, 'id': OTHER+':99', 'message_id': '99', 'scope': OTHER, 'text': '机器人你好'}
    r.db.add_message(b['id'], OTHER, 'default', USER, 'user', b['text'])
    r.model.script = [reply_decision(b, text='你好')]
    await asyncio.wait_for(r.engine.turn(OTHER, [b]), 2)
    assert any(scope == OTHER for scope, _ in r.bot.sent)
    release.set(); await task

def test_restart_marks_pending_delivery_without_resuming(db):
    for status in ('pending', 'waiting', 'sending', 'sent'):
        db.put('reply_delivery', status, {'id': status, 'scope': SCOPE, 'status': status, 'sent_count': 1, 'trace_id': 'trace-'+status})
        db.put('trace', 'trace-'+status, {'id': 'trace-'+status, 'status': 'running', 'ts': time.time()})
    recover(db)
    assert db.get('reply_delivery', 'sending')['status'] == 'unknown'
    assert db.get('reply_delivery', 'waiting')['status'] == 'interrupted'
    assert db.get('reply_delivery', 'pending')['status'] == 'interrupted'
    assert db.get('reply_delivery', 'sent')['status'] == 'sent'
    assert db.get('trace', 'trace-sending')['status'] == 'unknown'
    assert db.get('trace', 'trace-waiting')['status'] == 'interrupted'

@pytest.mark.parametrize('during_send', [False, True])
async def test_cancellation_preserves_confirmed_prefix_and_marks_uncertainty(segmented, during_send):
    r = segmented; m = prepare(r); send = r.bot.send
    async def cancel_pause(seconds): raise asyncio.CancelledError()
    async def cancel_send(scope, outgoing):
        if r.bot.sent: raise asyncio.CancelledError()
        return await send(scope, outgoing)
    if during_send: r.bot.send = cancel_send
    else: r.engine.delivery_sleep = cancel_pause
    with pytest.raises(asyncio.CancelledError): await r.engine.turn(SCOPE, [m])
    assert len(assistants(r)) == 1
    assert r.db.all('reply_delivery')[0]['status'] == ('unknown' if during_send else 'interrupted')

async def test_disabled_segment_switch_sends_long_reply_once(runtime):
    r = runtime; m = prepare(r)
    await r.engine.turn(SCOPE, [m])
    assert len(r.bot.sent) == 1 and assistants(r)[0]['text'] == LONG

def test_group_api_partial_updates_preserve_settings(client):
    client.headers.update(auth(client))
    client.app.state.db.put('group', SCOPE, group_defaults(SCOPE))
    path = '/api/v1/groups/'+SCOPE
    cfg = {'segmented_reply': True, 'segment_threshold': 80, 'segment_max_parts': 3, 'segment_interval_min': 1, 'segment_interval_max': 2.5}
    assert client.put(path, json=cfg).status_code == 200
    result = client.put(path, json={'name': '旧客户端'}).json()
    assert all(result[k] == v for k, v in cfg.items())
    for bad in ({'segment_max_parts': 1}, {'segment_threshold': 0}, {'segment_interval_min': 4, 'segment_interval_max': 1}, {'segment_interval_min': 3}):
        assert client.put(path, json=bad).status_code == 422
    assert client.put(path, json={'segment_interval_max': 4}).status_code == 200
    assert client.put(path, json={'segment_interval_min': 3}).status_code == 200


def incoming_review_message(r, text, number=2, actor=SECOND, replies=(), mentions=(), scope=SCOPE):
    m = source(r, text, number, actor=actor)
    m.update(scope=scope, id=scope+':'+str(number), replies=list(replies), mentions=list(mentions))
    m['data'].update(replies=list(replies), mentions=list(mentions))
    if text == '[图片]':
        m.update(plain_text='', images=['https://example.invalid/image'])
        m['data'].update(image_count=1, types=['image'])
    r.engine.recent_events.setdefault(SCOPE, []).append(m)
    return m


def review_target(r):
    target = source(r, '机器人，搜一下这个图，看看人物是谁', 1)
    target['replies'] = ['old-image']
    return target, ReplyReview(r.engine, SCOPE, target, [target],
                              r.db.get('persona', 'default'), '检索结果最支持 Claude 娘。')


@pytest.mark.parametrize('case', ['image', 'face', 'chatter', 'another_question',
    'other_thread', 'requester_other_thread', 'requester_at_other', 'other_group', 'self'])
def test_unrelated_messages_do_not_enter_review(runtime, case):
    r = runtime
    target, guard = review_target(r)
    options = {
        'image': dict(text='[图片]'),
        'face': dict(text='[QQ表情:1]'),
        'chatter': dict(text='今晚去吃火锅吧'),
        'another_question': dict(text='机器人，明天会下雨吗', mentions=[r.bot.self_id]),
        'other_thread': dict(text='不是 Claude 吧', replies=['another-image']),
        'requester_other_thread': dict(text='不是这个，发错图了', actor=USER, replies=['another-image']),
        'requester_at_other': dict(text='别发了', actor=USER, mentions=['40000001']),
        'other_group': dict(text='不用了', actor=USER, scope=OTHER),
        'self': dict(text='Claude 是谁', actor=r.bot.self_id),
    }
    m = incoming_review_message(r, **options[case])
    assert relation(r.engine, m, target, guard.card, guard.reply) is None


@pytest.mark.parametrize('case,expected', [
    ('requester_cancel', 'same_requester'), ('requester_image', 'same_requester'),
    ('reply_request', 'reply_to_request'), ('reply_image', 'reply_to_request'),
    ('reply_sent', 'reply_to_request'), ('full_id', 'reply_to_request'),
    ('topic_correction', 'topic_overlap'), ('at_requester', 'mentions_requester'),
    ('addressed_correction', 'addressed_correction')])
def test_related_messages_keep_identity_and_quote_links(runtime, case, expected):
    r = runtime
    target, guard = review_target(r)
    options = {
        'requester_cancel': dict(text='不用搜了', actor=USER),
        'requester_image': dict(text='[图片]', actor=USER),
        'reply_request': dict(text='有个地方说错了', replies=['1']),
        'reply_image': dict(text='[图片]', replies=['old-image']),
        'reply_sent': dict(text='不是这个', replies=['501']),
        'full_id': dict(text='这里不对', replies=[SCOPE+':1']),
        'topic_correction': dict(text='不是 Claude，是另一个角色'),
        'at_requester': dict(text='这个出处找到了', mentions=[USER]),
        'addressed_correction': dict(text='机器人，先停一下', mentions=[r.bot.self_id]),
    }
    m = incoming_review_message(r, **options[case])
    assert relation(r.engine, m, target, guard.card, guard.reply, sent_ids=['501']) == expected


async def test_other_member_image_during_generation_does_not_cancel_reply(runtime):
    r = runtime
    r.db.put('group', SCOPE, r.group)
    target = source(r, '机器人，你觉得这幅插画怎么样', 1)
    chat = r.model.chat

    async def generate(scope, purpose, messages, **kwargs):
        if kwargs.get('stage') in ('reply.main', 'conversation.agent'):
            incoming_review_message(r, '[图片]')
        return await chat(scope, purpose, messages, **kwargs)
    response = {'content': '这幅插画的配色很柔和。'}
    r.model.script = [
        {'content': json.dumps({'speak': True, 'reply_to_message_id': target['id'],
                               'text': response['content'], 'mode': 'direct'})}]
    r.model.chat = generate
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 1
    assert len(r.model.calls) == 1
    assert r.db.all('trace')[0]['status'] == 'sent'
    assert all(c[3].get('stage') not in ('reply.review', 'reply.segment_review') for c in r.model.calls)


async def test_positive_review_survives_new_unrelated_traffic(runtime):
    r = runtime
    target, guard = review_target(r)
    supplement = incoming_review_message(r, '补充一下，是橙发的角色', actor=USER)
    chat = r.model.chat

    async def review(*args, **kwargs):
        incoming_review_message(r, '[图片]', 3)
        incoming_review_message(r, '今晚吃火锅', 4)
        return await chat(*args, **kwargs)
    r.model.chat = review
    r.model.script = [{'content': json.dumps({'send': True})}]
    assert await guard.check(lambda: True)
    assert len(r.model.calls) == 1 and guard.sources == [supplement]
    request = json.loads(r.model.calls[0][2][-1]['content'])
    assert request['request']['qq'] == USER and request['request']['id'] == target['id']
    assert request['request']['replies'] == ['old-image']
    assert request['new_messages'][0]['same_requester'] is True
    assert '今晚吃火锅' not in str(request)
    # A handoff to delivery shares the consumed IDs and does not review again.
    assert await delivery.deliver(r.engine, SCOPE, 'default', target, [target], guard.card,
        guard.reply, {}, [], {}, lambda: True, False, review=guard) == 'sent'
    assert len(r.model.calls) == 1 and len(r.bot.sent) == 1
    row = r.db.recent(SCOPE, 'default')[-1]
    assert supplement['id'] in row['data']['source_ids']


async def test_other_members_quoted_image_is_not_identified_as_requester(runtime):
    r = runtime
    target, guard = review_target(r)
    extra = incoming_review_message(r, '[图片]', replies=['old-image'])
    r.model.script = [{'content': json.dumps({'send': True})}]
    assert await guard.check(lambda: True)
    payload = json.loads(r.model.calls[0][2][-1]['content'])
    new = payload['new_messages'][0]
    assert payload['request']['qq'] == USER and new['qq'] == SECOND
    assert new['same_requester'] is False and new['relation'] == 'reply_to_request'
    assert new['replies'] == ['old-image'] and new['image_count'] == 1
    assert guard.sources == [extra]


async def test_cancellation_arriving_during_review_is_checked(runtime):
    r = runtime
    target, guard = review_target(r)
    incoming_review_message(r, '补充一句，橙发那个', actor=USER)
    chat = r.model.chat

    async def review(*args, **kwargs):
        if not r.model.calls:
            incoming_review_message(r, '不用了，取消', 3, actor=USER)
        return await chat(*args, **kwargs)
    r.model.chat = review
    r.model.script = [{'content': '{"send":true}'}, {'content': '{"send":false}'}]
    assert not await guard.check(lambda: True)
    assert len(r.model.calls) == 2 and not r.bot.sent
    assert json.loads(r.model.calls[1][2][-1]['content'])['new_messages'][0]['text'] == '不用了，取消'


async def test_continuously_related_messages_keep_review_bounded(runtime):
    r = runtime
    _, guard = review_target(r)
    incoming_review_message(r, '需要补充一下', actor=USER)
    chat = r.model.chat

    async def review(*args, **kwargs):
        incoming_review_message(r, '纠正一下', len(r.model.calls)+3, actor=USER)
        return await chat(*args, **kwargs)
    r.model.chat = review
    r.model.script = [{'content': '{"send":true}'}]*2
    assert not await guard.check(lambda: True)
    assert len(r.model.calls) == 2


@pytest.mark.parametrize('response', ['invalid json', '{"send":false}', '{"send":"true"}'])
async def test_failed_or_negative_review_does_not_send(runtime, response):
    r = runtime
    _, guard = review_target(r)
    incoming_review_message(r, '取消', actor=USER)
    r.model.script = [{'content': response}]
    assert not await guard.check(lambda: True) and not r.bot.sent


async def test_related_optout_is_redacted_before_review_and_delivery(runtime):
    from rolebot import tracing
    r = runtime
    target, guard = review_target(r)
    m = incoming_review_message(r, '私密补充', actor=USER)
    m['remember'] = False
    tid, token = tracing.begin(r.db, SCOPE, 'default', [target])
    r.model.script = [{'content': '{"send":true}'}]
    try:
        assert await guard.check(lambda: True) and not guard.remember
        assert r.db.get('trace', tid)['redacted'] is True
        await delivery.deliver(r.engine, SCOPE, 'default', target, [target], guard.card,
            guard.reply, {}, [], {}, lambda: True, False, review=guard)
        row = r.db.rows("SELECT * FROM messages WHERE role='assistant'")[0]
        assert row['excluded'] and not row['text']
        assert '私密补充' not in json.dumps(r.db.get('trace', tid), ensure_ascii=False)
    finally:
        tracing.finish(r.db, tid, 'complete', token)


@pytest.mark.parametrize('at_bot', [False, True])
async def test_unrelated_traffic_between_segments_keeps_sending(runtime, at_bot):
    r = runtime
    r.group.update(segmented_reply=True, segment_threshold=40)
    r.db.put('group', SCOPE, r.group)
    target = source(r, '机器人，聊一下明天安排', 1)
    reply = ('可以先定在大家都方便的地方，这样晚一点下班的人也能慢慢赶过来。\n\n'
             '时间可以先定在七点半左右，提前到的朋友可以坐下来休息一下，不用急着等人。')
    r.model.script = [reply_decision(target, text=reply)]

    async def pause(seconds):
        incoming_review_message(r, '机器人，星星有多远' if at_bot else '[图片]',
                 mentions=[r.bot.self_id] if at_bot else [])
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 2 and len(r.model.calls) == 1


async def test_reply_to_sent_part_triggers_relevant_review(runtime):
    r = runtime
    r.group.update(segmented_reply=True, segment_threshold=40)
    r.db.put('group', SCOPE, r.group)
    target = source(r, '机器人，聊一下明天安排', 1)
    reply = ('可以先定在大家都方便的地方，这样晚一点下班的人也能慢慢赶过来。\n\n'
             '时间可以先定在七点半左右，提前到的朋友可以坐下来休息一下，不用急着等人。')
    r.model.script = [reply_decision(target, text=reply), {'content': '{"send":false}'}]

    async def pause(seconds):
        incoming_review_message(r, '这里有个错误', replies=['501'])
    r.engine.delivery_sleep = pause
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 1 and r.db.all('reply_delivery')[0]['status'] == 'partial'
    payload = json.loads(r.model.calls[-1][2][-1]['content'])
    assert payload['sent_message_ids'] == ['501']
    assert payload['new_messages'][0]['relation'] == 'reply_to_request'


CAPTION = '给你比个心'
STICKER_LONG = ('第一段完整的文字说明，今天可以一起慢慢聊聊，大家先坐下来休息一下，也可以先吃点东西，不用着急赶过去。\n\n'
        '第二段完整的文字说明，明天再准备需要的东西，现在先看看这张表情吧，其他的事情可以等到有时间的时候再说。\n\n'
        '第三段完整的文字说明，希望大家今天也能开开心心，晚点有空继续聊天，或者一起看看最近有没有什么好玩的游戏。')


@pytest.fixture
def stickers(runtime):
    r = runtime
    r.group.update(interaction_enabled=True, sticker_autocollect=False)
    r.db.put('group', SCOPE, r.group)
    raw = io.BytesIO()
    Image.new('RGB', (120, 140), 'red').save(
        raw, 'GIF', save_all=True, append_images=[Image.new('RGB', (120, 140), 'blue')],
        duration=120, loop=0)
    r.raw = raw.getvalue()
    r.sticker = r.engine.stickers.register(OTHER, r.raw, {'name': '共享动图'})
    r.delays = []

    async def sleep(seconds):
        r.delays.append(seconds)
    r.engine.delivery_sleep = sleep
    return r


async def sticker_plan(r, text=CAPTION, tool='message_send', mentions=()):
    m = source(r, '机器人，发一个表情包', 1)
    args = {'text': text, 'mentions': list(mentions)}
    args['id' if tool == 'sticker_send' else 'sticker_id'] = r.sticker['id']
    # sticker_send prepares only the image; its caption comes from the reply.
    if tool == 'sticker_send':
        args = {'id': r.sticker['id']}
    result = await r.tools.run(SCOPE, 'default', USER, m['id'], tool, args)
    assert result['status'] == 'success', result
    r.reply = text
    return m, result['result']['delivery']


async def send_sticker(r, m, plan, batch=None, current=lambda: True):
    return await delivery.deliver(
        r.engine, SCOPE, 'default', m, batch or [m], {}, r.reply, {}, [],
        {'delivery_plan': plan, 'expects_followup': True}, current, True)


@pytest.mark.parametrize('tool', ['message_send', 'sticker_send'])
@pytest.mark.parametrize('segmented', [False, True])
async def test_caption_and_original_sticker_are_separate_messages(stickers, tool, segmented):
    r = stickers
    r.group['segmented_reply'] = segmented
    r.db.put('group', SCOPE, r.group)
    m, plan = await sticker_plan(r, tool=tool)
    assert await send_sticker(r, m, plan) == 'sent'
    assert len(r.bot.sent) == 2 and len(r.delays) == 1
    assert r.bot.sent[0][1] == [{'type': 'text', 'data': {'text': CAPTION}}]
    assert r.bot.sent[1][1] == [{'type': 'image', 'data': {
        'file': 'base64://'+base64.b64encode(r.raw).decode(),
        'sub_type': 1, 'summary': '[动画表情]'}}]
    assert sticker_path(r.settings, r.sticker).read_bytes() == r.raw
    text, image = assistants(r)
    assert text['text'] == CAPTION and text['data']['types'] == ['text']
    assert 'sticker_id' not in text['data'] and 'image_count' not in text['data']
    assert image['text'] == '' and image['data']['types'] == ['image']
    assert image['data']['sticker_id'] == r.sticker['id'] and image['data']['image_count'] == 1
    assert text['data']['delivery_id'] == image['data']['delivery_id']
    assert [text['data']['part'], image['data']['part']] == [1, 2]
    attachment = r.db.all('attachment')[0]
    assert attachment['source_id'] == image['id'] and attachment['sticker_id'] == r.sticker['id']
    state = r.db.get('chat_state', SCOPE)
    assert len(state['sent']) == 2 and len(state['answered']) == len(state['proactive']) == 1
    assert [h['hash'] for h in state['reply_hashes']] == [delivery.fingerprint(CAPTION)]
    assert not r.model.calls
    job = r.db.all('reply_delivery')[0]
    assert job['status'] == 'sent' and job['sent_count'] == job['planned_count'] == 2


async def test_image_only_delivery_has_no_empty_text_message(stickers):
    r = stickers
    m, plan = await sticker_plan(r, text='', tool='sticker_send')
    assert await send_sticker(r, m, plan) == 'sent'
    assert len(r.bot.sent) == len(assistants(r)) == 1 and not r.delays
    assert [s['type'] for s in r.bot.sent[0][1]] == ['image']
    assert assistants(r)[0]['data']['types'] == ['image']


def prepared_call(name, args, target, call_id):
    return {'id': call_id, 'type': 'function', 'function': {
        'name': name, 'arguments': json.dumps({**args, 'source_message_id': target['id']})}}


@pytest.mark.parametrize('tool', ['sticker_send', 'message_send'])
async def test_pure_sticker_passes_full_conversation_without_caption(stickers, tool):
    r = stickers
    target = source(r, '机器人，嘿嘿', 1)
    args = {'id': r.sticker['id']} if tool == 'sticker_send' else {'text': '', 'sticker_id': r.sticker['id']}
    r.model.script = [{'role': 'assistant', 'content': None, 'tool_calls': [prepared_call(tool, args, target, 'one')]},
                      reply_decision(target, text='')]
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 1 and [s['type'] for s in r.bot.sent[0][1]] == ['image']
    assert r.db.all('reply_delivery')[0]['status'] == 'sent'
    assert assistants(r)[0]['data']['sticker_id'] == r.sticker['id']
    assert r.db.all('trace')[0]['status'] == 'sent'


async def test_failed_like_caption_is_corrected_before_separate_text_and_sticker_delivery(stickers):
    from rolebot.conversation import Conversation
    r = stickers
    target = source(r, '机器人，为什么只点一次，补足十赞', 1)
    prepared = await r.tools.run(SCOPE, 'default', USER, target['id'], 'message_send', {
        'text': 'QQ 规定一天只能一次，明天我第一个冲过去补上！', 'sticker_id': r.sticker['id']})
    assert prepared['status'] == 'success'
    executed = [{'name': 'profile_like', 'status': 'denied', 'error_code': 'like_daily_limit'},
                {'name': 'message_send', **prepared}]
    decision = json.loads(reply_decision(target)['content'])
    result = await Conversation(r.engine).finish(SCOPE, 'default', [target], r.group,
        r.db.get('persona', 'default'), {}, decision, executed, {}, lambda: True)
    assert result == 'sent'
    assert len(r.bot.sent) == 2
    text, image = [message for scope, message in r.bot.sent]
    assert [segment['type'] for segment in text] == ['text']
    assert '后台' in text[0]['data']['text'] and '明天' not in text[0]['data']['text']
    assert [segment['type'] for segment in image] == ['image']


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('separate_rounds', [False, True])
async def test_complementary_caption_and_sticker_merge_in_conversation(stickers, reverse, separate_rounds):
    r = stickers
    target = source(r, '机器人，嘿嘿', 1)
    calls = [prepared_call('message_send', {'text': CAPTION}, target, 'text'),
             prepared_call('sticker_send', {'id': r.sticker['id']}, target, 'image')]
    if reverse:
        calls.reverse()
    r.model.script = ([{'role': 'assistant', 'content': None, 'tool_calls': [c]} for c in calls] if separate_rounds else
                      [{'role': 'assistant', 'content': None, 'tool_calls': calls}]) + [reply_decision(target, text='')]
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 2
    assert r.bot.sent[0][1] == [{'type': 'text', 'data': {'text': CAPTION}}]
    assert [s['type'] for s in r.bot.sent[1][1]] == ['image']
    assert len(r.db.all('reply_delivery')) == 1
    assert len(r.db.get('chat_state', SCOPE)['answered']) == 1


@pytest.mark.parametrize('actor', [USER, SECOND])
async def test_sticker_cannot_merge_across_trigger_messages(stickers, actor):
    r = stickers
    first = source(r, '机器人，你好', 1)
    second = source(r, '机器人，另一条消息', 2, actor=actor)
    r.model.script = [{'role': 'assistant', 'content': None, 'tool_calls': [
        prepared_call('message_send', {'text': CAPTION}, first, 'text'),
        prepared_call('sticker_send', {'id': r.sticker['id']}, second, 'image')]},
        reply_decision(first, text='')]
    await r.engine.turn(SCOPE, [first, second])
    assert len(r.bot.sent) == 1 and all(s['type'] != 'image' for s in r.bot.sent[0][1])
    results = [json.loads(m['content']) for m in r.model.calls[-1][2] if m['role'] == 'tool']
    assert results[-1]['status'] == 'denied'


async def test_second_sticker_and_caption_rewrite_are_rejected(stickers):
    r = stickers
    target = source(r, '机器人，你好', 1)
    r.model.script = [
        {'role': 'assistant', 'content': None, 'tool_calls': [
            prepared_call('message_send', {'text': CAPTION}, target, 'text'),
            prepared_call('sticker_send', {'id': r.sticker['id']}, target, 'image'),
            prepared_call('message_send', {'text': '覆盖原话'}, target, 'rewrite'),
            prepared_call('sticker_send', {'id': r.sticker['id']}, target, 'again')]},
        reply_decision(target, text='')]
    await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 2 and r.bot.sent[0][1][0]['data']['text'] == CAPTION
    results = [json.loads(m['content']) for m in r.model.calls[-1][2] if m['role'] == 'tool']
    assert [r['status'] for r in results] == ['success', 'success', 'denied', 'denied']


@pytest.mark.parametrize('revoked_tool', ['message_send', 'sticker_send'])
async def test_merged_plan_rechecks_both_permissions_between_parts(stickers, revoked_tool):
    r = stickers
    target = source(r, '机器人，你好', 1)
    r.model.script = [{'role': 'assistant', 'content': None, 'tool_calls': [
        prepared_call('message_send', {'text': CAPTION}, target, 'text'),
        prepared_call('sticker_send', {'id': r.sticker['id']}, target, 'image')]},
        reply_decision(target, text='')]
    async def revoke(seconds):
        access = AccessConfig(members={USER: {'deny': ['tool:'+revoked_tool]}})
        r.db.put('qq_access', SCOPE, access.model_dump())
    r.engine.delivery_sleep = revoke
    from rolebot.tools import ToolDenied
    with pytest.raises(ToolDenied):
        await r.engine.turn(SCOPE, [target])
    assert len(r.bot.sent) == 1 and all(s['type'] != 'image' for s in r.bot.sent[0][1])
    assert r.db.all('reply_delivery')[0]['status'] == 'partial_failed'


async def test_quote_and_mentions_only_appear_on_caption(stickers):
    r = stickers
    m, plan = await sticker_plan(r, mentions=['self'])
    batch = [m]+[source(r, '无关消息', n) for n in range(2, 6)]
    assert await send_sticker(r, m, plan, batch=batch) == 'sent'
    assert [s['type'] for s in r.bot.sent[0][1]] == ['reply', 'at', 'text']
    assert [s['type'] for s in r.bot.sent[1][1]] == ['image']
    assert assistants(r)[0]['data']['replies'] == ['1'] and not assistants(r)[1]['data']['replies']


@pytest.mark.parametrize('used,expected,status', [(0, 4, 'sent'), (17, 3, 'sent'),
                                                 (18, 2, 'sent'), (19, 1, 'sent'), (20, 0, 'rate_limited')])
async def test_rate_slots_include_sticker_and_never_merge_with_text(stickers, used, expected, status):
    r = stickers
    r.group.update(segmented_reply=True)
    r.db.put('group', SCOPE, r.group)
    m, plan = await sticker_plan(r, text=STICKER_LONG)
    r.db.put('chat_state', SCOPE, {'sent': [time.time()]*used})
    assert await send_sticker(r, m, plan) == status
    assert len(r.bot.sent) == expected
    assert len(r.db.get('chat_state', SCOPE)['sent']) <= delivery.SEND_LIMIT
    if not expected:
        return
    delivered = ''.join(s['data']['text'] for _, msg in r.bot.sent for s in msg if s['type'] == 'text')
    assert ''.join(delivered.split()) == ''.join(STICKER_LONG.split())
    for _, msg in r.bot.sent:
        assert not {'image', 'text'} <= {s['type'] for s in msg}
    if status == 'sent' and expected > 1:
        assert r.bot.sent[-1][1][0]['type'] == 'image'
    else:
        assert not r.db.all('attachment') and not r.db.get('followup', SCOPE+':default')


@pytest.mark.parametrize('change', ['sticker_disabled', 'file_removed', 'tool_disabled', 'permission_revoked'])
async def test_revoke_between_caption_and_sticker_stops_remaining_send(stickers, change):
    r = stickers
    m, plan = await sticker_plan(r)

    async def revoke(seconds):
        if change == 'sticker_disabled':
            r.db.put('sticker', r.sticker['id'], {**r.sticker, 'enabled': False})
        elif change == 'file_removed':
            sticker_path(r.settings, r.sticker).unlink()
        elif change == 'tool_disabled':
            r.group['tools']['message_send']['enabled'] = False
            r.db.put('group', SCOPE, r.group)
        else:
            cfg = AccessConfig(members={USER: {'deny': ['tool:message_send']}}).model_dump()
            r.db.put('qq_access', SCOPE, cfg)
    r.engine.delivery_sleep = revoke
    with pytest.raises(Exception):
        await send_sticker(r, m, plan)
    assert len(r.bot.sent) == len(assistants(r)) == 1
    assert not r.db.all('attachment')
    assert r.db.all('reply_delivery')[0]['status'] == 'partial_failed'
    assert not r.db.get('followup', SCOPE+':default')


@pytest.mark.parametrize('on_attempt', [1, 2])
@pytest.mark.parametrize('kind', ['unknown', 'failed', 'missing_id'])
async def test_send_failure_never_retries_or_persists_unsent_sticker(stickers, on_attempt, kind):
    r = stickers
    m, plan = await sticker_plan(r)
    original, attempts = r.bot.send, []

    async def fail(scope, outgoing):
        attempts.append(outgoing)
        if len(attempts) == on_attempt:
            if kind == 'missing_id':
                return {}
            raise ActionUnknown('unknown') if kind == 'unknown' else OneBotError('failed')
        return await original(scope, outgoing)
    r.bot.send = fail
    expected = ('partial_' if on_attempt == 2 else '')+('failed' if kind == 'failed' else 'unknown')
    assert await send_sticker(r, m, plan) == expected
    assert len(attempts) == on_attempt
    assert len(assistants(r)) == on_attempt-1 and not r.db.all('attachment')
    assert len(r.db.get('chat_state', SCOPE)['sent']) == on_attempt
    delivery.recover(r.db)
    assert r.db.all('reply_delivery')[0]['status'] == expected


async def test_quota_consumed_while_waiting_cancels_sticker(stickers):
    r = stickers
    m, plan = await sticker_plan(r)

    async def consume(seconds):
        state = r.db.get('chat_state', SCOPE)
        state['sent'] = [time.time()]*delivery.SEND_LIMIT
        r.db.put('chat_state', SCOPE, state)
    r.engine.delivery_sleep = consume
    assert await send_sticker(r, m, plan) == 'sent'
    assert len(r.bot.sent) == 1 and not r.db.all('attachment')


@pytest.mark.parametrize('used',[6,20])
async def test_direct_reply_is_not_silenced_by_separate_sticker_rate(runtime,monkeypatch,used):
    r=runtime;target=source(r,'机器人，继续说吧',1)
    clock=[time.time()];monkeypatch.setattr(delivery.time,'time',lambda:clock[0])
    async def wait(seconds):clock[0]+=seconds
    r.engine.delivery_sleep=wait
    r.db.put('chat_state',SCOPE,{'sent':[clock[0]]*used})
    r.model.script=[reply_decision(target,text='这条明确请求应该能收到回复。')]
    await r.engine.turn(SCOPE,[target])
    assert len(r.bot.sent)==1 and len(r.model.calls)==1
    trace=r.db.all('trace')[0]
    assert trace['status']=='sent'
    assert any(s['name']=='delivery_rate_wait' for s in trace['stages']) is (used==20)


@pytest.mark.parametrize('change',['pause','forget','revoke'])
async def test_rate_wait_rechecks_context_and_permissions_before_first_send(stickers,change):
    r=stickers;target,plan=await sticker_plan(r)
    r.db.put('chat_state',SCOPE,{'sent':[time.time()]*delivery.SEND_LIMIT})
    async def wait(seconds):
        r.db.put('chat_state',SCOPE,{'sent':[]})
        if change=='pause':
            r.group['paused']=True;r.db.put('group',SCOPE,r.group)
        elif change=='forget':r.db.invalidate_memory(SCOPE,source_ids=[target['id']])
        else:r.db.put('qq_access',SCOPE,AccessConfig(members={USER:{'deny':['tool:message_send']}}).model_dump())
    r.engine.delivery_sleep=wait
    from rolebot.conversation import Conversation
    from rolebot.tools import ToolDenied
    decision=json.loads(reply_decision(target)['content'])
    executed=[{'name':'message_send','status':'success','result':{'prepared':True,'delivery':plan}}]
    try:
        await Conversation(r.engine).finish(SCOPE,'default',[target],r.group,r.db.get('persona','default'),{},
            decision,executed,{},lambda:r.engine.current(SCOPE,'default',0))
    except ToolDenied:
        assert change=='revoke'
    assert not r.bot.sent


async def test_relevant_interjection_reviews_pending_sticker(stickers):
    r = stickers
    m, plan = await sticker_plan(r)

    async def interrupt(seconds):
        r.engine.recent_events[SCOPE] = [m, source(r, '机器人，别发了', 2)]
    r.engine.delivery_sleep = interrupt
    r.model.script = [{'content': json.dumps({'send': False})}]
    assert await send_sticker(r, m, plan) == 'partial'
    assert len(r.bot.sent) == 1 and len(r.model.calls) == 1
    request = json.loads(r.model.calls[0][2][-1]['content'])
    assert request['sent'] == [CAPTION] and request['remaining'] == ['[待发送表情]']
    assert not r.db.all('attachment')


async def test_changed_conversation_stops_sticker_without_model_call(stickers):
    r = stickers
    m, plan = await sticker_plan(r)
    assert await send_sticker(r, m, plan, current=lambda: not r.bot.sent) == 'partial'
    assert len(r.bot.sent) == 1 and not r.model.calls


def test_extended_segments_and_send_slots():
    text='这是一段需要保留完整内容并按照标点划分的自然语言句子。'*20
    parts=split_reply(text,80,5)
    assert len(parts)==5 and ''.join(parts)==text
    assert split_reply('```python\nprint(1)\n```'+text,80,5)==['```python\nprint(1)\n```'+text]
    state={'sent':[time.time()]*25}
    assert send_slots(state,40)==15 and send_slots(state,20)==0
