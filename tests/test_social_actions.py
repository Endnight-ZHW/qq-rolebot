from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import json
import time

import pytest

from rolebot import like_budget
from rolebot.onebot import ActionUnknown, OneBotError
from rolebot.permissions import AccessConfig
from rolebot.tools import default_policies, ToolDenied

from support import SCOPE, OTHER, USER, SECOND, source


ADMIN = '90000001'
NAMES = ['profile_like', 'member_poke']


def enable(r, name, member='self', admin='all', scope=SCOPE, **extra):
    g = r.db.get('group', scope, {**r.db.get('group', SCOPE), 'id': scope})
    g['interaction_enabled'] = True
    g['tools'].setdefault(name, {}).update(enabled=True, user_enabled=True,
        request_access={'member': member, 'admin': admin}, **extra)
    r.db.put('group', scope, g)


def ordinary_bot(r):
    original = r.bot.call
    async def call(action, params=None, **kwargs):
        result = await original(action, params, **kwargs)
        if action == 'get_group_member_info':
            result.update(role='member', group_id=params['group_id'])
        return result
    r.bot.call = call


async def run(r, name, *, actor=USER, target=USER, event='event', scope=SCOPE, **args):
    return await r.tools.run(scope, 'default', actor, scope+':'+event, name, {'user_id': target, 'reason': '本人请求', **args})


@pytest.mark.parametrize('name', NAMES)
async def test_default_off_and_non_admin_bot_can_act_only_after_explicit_configuration(runtime, name):
    r = runtime; ordinary_bot(r)
    defaults = default_policies()[name]
    assert not defaults['enabled'] and not defaults['autonomous_enabled']
    assert (await run(r, name, actor=ADMIN))['status'] == 'denied'
    enable(r, name)
    result = await run(r, name)
    assert result['status'] == 'success' and result['result']['submitted'], result
    assert result['result']['verification'] == 'platform_acknowledged'
    calls = [x for x in r.bot.calls if x[2]]
    assert len(calls) == 1
    assert calls[0][0] == ('send_like' if name == 'profile_like' else 'group_poke')
    assert calls[0][1] == ({'user_id': USER, 'times': 10} if name == 'profile_like' else {'group_id': SCOPE.split(':')[2], 'user_id': USER})


@pytest.mark.parametrize('name', NAMES)
async def test_self_hard_ceiling_protected_target_and_owner_rules(runtime, name):
    r = runtime; ordinary_bot(r); enable(r, name)
    r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'allow': ['tool:'+name]}}).model_dump())
    assert (await run(r, name, target=SECOND))['error_code'] == 'self_target_only'
    enable(r, name, admin='disabled')
    assert (await run(r, name, actor=ADMIN))['error_code'] == 'request_access_denied'
    enable(r, name, protected_ids=[USER])
    assert (await run(r, name))['error_code'] == 'explicit_target_protected'
    assert not any(x[2] for x in r.bot.calls)


@pytest.mark.parametrize('name', NAMES)
async def test_cross_group_and_concurrent_requests_share_target_limit(runtime, name):
    r = runtime; ordinary_bot(r)
    enable(r, name); enable(r, name, scope=OTHER)
    results = await asyncio.gather(run(r, name, event='a'), run(r, name, scope=OTHER, event='b'))
    assert sorted(x['status'] for x in results) == ['denied', 'success']
    assert len([x for x in r.bot.calls if x[2]]) == 1
    denied = next(x for x in results if x['status'] == 'denied')
    assert denied['error_code'] in (('like_daily_limit', 'like_request_in_progress') if name == 'profile_like' else ('poke_cooldown',))


@pytest.mark.parametrize('name', NAMES)
async def test_timeout_never_retries_or_switches_endpoints(runtime, name):
    r = runtime; ordinary_bot(r); enable(r, name); r.bot.unknown = True
    first = await run(r, name)
    assert first['status'] == 'unknown'
    repeated = await run(r, name)
    assert repeated['status'] == 'unknown' and repeated['duplicate']
    assert (await run(r, name, event='new-message'))['status'] == 'denied'
    assert len([x for x in r.bot.calls if x[2]]) == 1


@pytest.mark.parametrize('name', NAMES)
async def test_platform_rejection_does_not_claim_success(runtime, name):
    r = runtime; ordinary_bot(r); enable(r, name)
    original = r.bot.call
    async def reject(action, params=None, **kwargs):
        value = await original(action, params, **kwargs)
        if kwargs.get('mutation'): raise OneBotError('QQ 接口失败，retcode=1200', retcode=1200,
            upstream_message='点赞失败 由于对方权限设置，点赞失败', request_sent=True)
        return value
    r.bot.call = reject
    result = await run(r, name)
    assert result['status'] == 'denied' and result['error_code'] == ('like_target_restricted' if name == 'profile_like' else 'social_platform_rejected')
    assert (await run(r, name, event='try-again'))['status'] == 'denied'
    assert len([x for x in r.bot.calls if x[2]]) == 1


async def test_like_count_bounds_and_next_beijing_day(runtime):
    r = runtime; ordinary_bot(r); enable(r, 'profile_like', like_max_times=3)
    assert (await run(r, 'profile_like', times=4))['error_code'] == 'like_times_limit'
    assert (await run(r, 'profile_like', times=11))['status'] == 'denied'
    assert (await run(r, 'profile_like', times=3))['status'] == 'success'
    record = r.db.all('social_attempt')[0]; record['day'] = '2000-01-01'; r.db.put('social_attempt', record['id'], record)
    assert (await run(r, 'profile_like', event='next-day'))['status'] == 'success'
    assert len([x for x in r.bot.calls if x[2]]) == 2


async def test_poke_cooldown_expiry_and_other_group_cannot_shorten_reservation(runtime):
    r = runtime; ordinary_bot(r); enable(r, 'member_poke', poke_cooldown_seconds=120)
    enable(r, 'member_poke', scope=OTHER, poke_cooldown_seconds=10)
    assert (await run(r, 'member_poke'))['status'] == 'success'
    assert (await run(r, 'member_poke', scope=OTHER))['error_code'] == 'poke_cooldown'
    record = r.db.all('social_attempt')[0]; record['blocked_until'] = 0; r.db.put('social_attempt', record['id'], record)
    assert (await run(r, 'member_poke', scope=OTHER, event='later'))['status'] == 'success'


@pytest.mark.parametrize('name', NAMES)
async def test_cross_group_platform_identity_mismatch_prevents_call(runtime, name):
    r = runtime; enable(r, name, member='all')
    original = r.bot.call
    async def mismatch(action, params=None, **kwargs):
        value = await original(action, params, **kwargs)
        if action == 'get_group_member_info' and params['user_id'] == SECOND:
            value['group_id'] = OTHER.split(':')[2]
        return value
    r.bot.call = mismatch
    assert (await run(r, name, target=SECOND))['error_code'] == 'platform_identity_unavailable'
    assert not any(x[2] for x in r.bot.calls)


@pytest.mark.parametrize('name', NAMES)
async def test_explicit_request_source_and_no_autonomous_use(runtime, name):
    r = runtime; ordinary_bot(r); enable(r, name)
    m = source(r, '机器人赞我一下' if name == 'profile_like' else '机器人戳我一下', 1)
    r.model.script = [{'content': json.dumps({'mode': 'request', 'request_message_id': m['id'], 'source_matches': True})}]
    result = await r.engine.bound_tool(SCOPE, 'default', [m], name,
        {'source_message_id': m['id'], 'user_id': USER, 'reason': '请求'})
    assert result['status'] == 'success', result
    m2 = source(r, '你应该自动互动', 2)
    r.model.script = [{'content': json.dumps({'mode': 'independent'})}]
    result = await r.engine.bound_tool(SCOPE, 'default', [m2], name,
        {'source_message_id': m2['id'], 'user_id': SECOND, 'reason': '自主互动'})
    assert result['status'] == 'denied'
    assert len([x for x in r.bot.calls if x[2]]) == 1


def test_api_exposes_social_limits_and_preserves_old_tool_settings(authed_client):
    c = authed_client
    c.put('/api/v1/groups/'+SCOPE, json={'name': '测试群'})
    original = next(g for g in c.get('/api/v1/groups').json() if g['id'] == SCOPE)
    for name in NAMES:
        assert original['tools'][name]['enabled'] is False
        assert original['tools'][name]['autonomous_enabled'] is False
        assert original['tools'][name]['request_access'] is None
    url = '/api/v1/groups/'+SCOPE+'/tools?expected_revision='+str(original['revision'])
    assert original['tools']['profile_like']['like_max_times'] == 10
    assert original['tools']['profile_like']['like_daily_limit'] == 10
    body = {'profile_like': {'like_max_times': 10, 'like_daily_limit': 8, 'autonomous_enabled': True},
            'member_poke': {'poke_cooldown_seconds': 90, 'autonomous_enabled': True}}
    result = c.put(url, json=body)
    assert result.status_code == 200
    assert result.json()['profile_like']['like_max_times'] == 10
    assert result.json()['profile_like']['like_daily_limit'] == 8
    saved = c.put('/api/v1/groups/'+SCOPE+'/tools', json={'profile_like': {'max_per_hour': 12}})
    assert saved.json()['profile_like']['like_daily_limit'] == 8  # Older clients keep the new field.
    assert not result.json()['profile_like']['autonomous_enabled'] and not result.json()['member_poke']['autonomous_enabled']
    assert all(result.json()[name] == old for name, old in original['tools'].items() if name not in NAMES)
    assert c.put('/api/v1/groups/'+SCOPE+'/tools', json={'member_poke': {'poke_cooldown_seconds': 0}}).status_code == 422
    assert c.put('/api/v1/groups/'+SCOPE+'/tools', json={'profile_like': {'like_max_times': 11}}).status_code == 200
    for invalid in (0, 2147483648):
        assert c.put('/api/v1/groups/'+SCOPE+'/tools', json={'profile_like': {'like_daily_limit': invalid}}).status_code == 422


async def test_fill_remaining_ten_and_report_actual_count_across_groups(runtime):
    r = runtime; enable(r, 'profile_like'); enable(r, 'profile_like', scope=OTHER)
    first = await run(r, 'profile_like', times=1)
    assert first['result']['submitted_times'] == 1
    duplicate = await run(r, 'profile_like', times=1)
    assert duplicate['duplicate']
    topup = await run(r, 'profile_like', times=10, scope=OTHER, event='top-up')
    assert topup['result']['submitted_times'] == 9
    assert topup['result']['daily_submitted'] == 10 and topup['result']['remaining_today'] == 0
    assert topup['result']['automatic_followup'] is False
    assert (await run(r, 'profile_like', event='again'))['error_code'] == 'like_daily_limit'
    assert [p['times'] for a, p, mutation in r.bot.calls if a == 'send_like'] == [1, 9]


async def test_changed_arguments_cannot_replay_same_message_but_new_request_can_top_up(runtime):
    r=runtime;enable(r,'profile_like')
    assert (await run(r,'profile_like',times=1))['status']=='success'
    result=await run(r,'profile_like',times=9,reason='换个理由再次提交')
    assert result['error_code']=='like_request_already_processed'
    fresh=await run(r,'profile_like',event='explicit-new-request')
    assert fresh['result']['submitted_times']==9
    assert [p['times'] for a,p,_ in r.bot.calls if a=='send_like']==[1,9]


async def test_known_refusal_does_not_spend_likes_new_request_can_retry_after_cooldown(runtime):
    r = runtime; enable(r, 'profile_like')
    original = r.bot.call
    async def reject(action, params=None, **kwargs):
        value = await original(action, params, **kwargs)
        if action == 'send_like':
            raise OneBotError('retcode=1200', retcode=1200, upstream_message='由于对方权限设置，点赞失败', request_sent=True)
        return value
    r.bot.call = reject
    assert (await run(r, 'profile_like'))['error_code'] == 'like_target_restricted'
    row = r.db.all('social_attempt')[0]
    assert row['submitted_count'] == 0 and not row.get('unknown')
    assert (await run(r, 'profile_like', event='too-soon'))['error_code'] == 'like_retry_cooldown'
    row['retry_after'] = 0; r.db.put('social_attempt', row['id'], row)
    r.bot.call = original
    result = await run(r, 'profile_like', event='fixed-settings')
    assert result['result']['submitted_times'] == 10


@pytest.mark.parametrize('error,expected', [
    (OneBotError('unclassified failure', retcode=1200, request_sent=True), 'unknown'),
    (OneBotError('NapCat 未连接', request_sent=False), 'denied'),
    (OneBotError('quota', upstream_message='今日点赞次数已达上限', request_sent=True), 'denied'),
])
async def test_like_errors_have_distinct_budget_outcomes(runtime, error, expected):
    r = runtime; enable(r, 'profile_like'); original = r.bot.call
    async def reject(action, params=None, **kwargs):
        if action == 'send_like': raise error
        return await original(action, params, **kwargs)
    r.bot.call = reject
    result = await run(r, 'profile_like')
    assert result['status'] == expected
    row = r.db.all('social_attempt')[0]
    assert row['submitted_count'] == 0
    if expected == 'unknown':
        assert row['unknown']
        assert (await run(r, 'profile_like', event='again'))['error_code'] == 'like_result_unknown'
    elif error.upstream_message:
        assert result['error_code'] == 'like_platform_daily_limit'
        assert (await run(r, 'profile_like', event='again'))['error_code'] == 'like_platform_daily_limit'
    else:
        assert result['error_code'] == 'social_platform_rejected'
        assert not row.get('unknown')


async def test_like_cancelled_in_flight_is_not_reissued(runtime):
    r = runtime; enable(r, 'profile_like'); original = r.bot.call; started = asyncio.Event()
    async def hang(action, params=None, **kwargs):
        if action == 'send_like':
            started.set()
            await asyncio.Future()
        return await original(action, params, **kwargs)
    r.bot.call = hang
    task = asyncio.create_task(run(r, 'profile_like'))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert r.db.all('social_attempt')[0]['unknown']
    assert (await run(r, 'profile_like', event='again'))['error_code'] == 'like_result_unknown'


async def test_default_quantity_respects_saved_caps_and_runtime_facts(runtime):
    r = runtime; enable(r, 'profile_like', like_max_times=3, like_daily_limit=5)
    assert (await run(r, 'profile_like'))['result']['submitted_times'] == 3
    assert (await run(r, 'profile_like', event='more', times=None))['result']['submitted_times'] == 2
    snapshot = r.tools.runtime_status(SCOPE, [{'user_id': USER}])
    limits = next(t['like_limits'] for t in snapshot['tools'] if t['name'] == 'profile_like')
    assert limits['per_request'] == 3 and limits['per_day'] == 5 and limits['origin'] == 'bot_configuration'


def test_like_failures_not_hidden_by_successful_caption_or_sticker():
    from rolebot.tool_feedback import ground_reply
    fake_claim = 'QQ 规定同一个号一天只能一次，明天我第一个冲过去补上！'
    delivery = [{'name': name, 'status': 'success'} for name in ('message_send', 'sticker_send', 'sticker_search')]
    for code, expected in [('like_daily_limit', '后台'), ('like_target_restricted', '对方的点赞权限'), ('like_platform_daily_limit', 'QQ 返回')]:
        result, reason = ground_reply(fake_claim, [], [{'name': 'profile_like', 'status': 'denied', 'error_code': code}, *delivery])
        assert expected in result and '明天' not in result and '只能一次' not in result
        assert reason == 'grounded_tool_failure:'+code
    result, reason = ground_reply(fake_claim, [], [{'name': 'profile_like', 'status': 'unknown'}, *delivery])
    assert '还没确认' in result and reason == 'grounded_like_unknown'
    success = [{'name': 'profile_like', 'status': 'success', 'result': {'submitted': True, 'submitted_times': 9}}, *delivery]
    result, reason = ground_reply('明天我第一个冲过去补上！', [], success)
    assert '9 赞' in result and '明天' not in result and reason
    assert ground_reply('9 赞请求已经提交啦。', [], success) == ('9 赞请求已经提交啦。', None)
    mixed = [{'name': 'profile_like', 'status': 'denied', 'error_code': 'like_daily_limit'}, {'name': 'member_card', 'status': 'success'}]
    assert ground_reply('改名成功，点赞没执行。', [], mixed) == ('改名成功，点赞没执行。', None)


async def test_onebot_response_keeps_error_details_for_like_classification():
    from rolebot.onebot import OneBot
    bot = OneBot(); future = asyncio.get_running_loop().create_future()
    bot.pending['echo'] = (future, True)
    bot.response({'echo': 'echo', 'status': 'failed', 'retcode': 1200, 'wording': '由于对方权限设置，点赞失败'})
    with pytest.raises(OneBotError) as caught: await future
    assert caught.value.retcode == 1200 and caught.value.request_sent is True
    assert caught.value.upstream_message == '由于对方权限设置，点赞失败'
    assert str(caught.value) == 'QQ 接口失败，retcode=1200'


BOT = SCOPE.split(':')[0]
KEY = f'{BOT}:profile_like:{USER}'
POLICY = {'like_max_times': 10, 'like_daily_limit': 10}
SOURCE = SCOPE+':old-like'


def reserve(db, **extra):
    return like_budget.reserve(db, BOT, extra.get('scope', SCOPE), USER, extra.get('source', SOURCE),
        USER, extra.get('times'), extra.get('policy', POLICY))


@pytest.mark.parametrize('version', [None, 1, 3])
def test_unsupported_like_ledger_is_preserved_without_new_request(db, version):
    row={'version':version,'day':like_budget.day_at(time.time()),'submitted_count':7,'unknown':True}
    db.put('social_attempt',KEY,row)
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_state_unavailable'
    assert db.get('social_attempt',KEY)==row


def test_atomic_reservation_serializes_parallel_cross_group_requests(db):
    def attempt(scope):
        try: return reserve(db, scope=scope)
        except ToolDenied as error: return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [SCOPE, OTHER]))
    success = [x for x in results if isinstance(x, dict)]
    assert len(success) == 1 and 'like_request_in_progress' in results
    like_budget.settle(db, success[0], 'accepted')
    with pytest.raises(ToolDenied) as error: reserve(db, scope=OTHER)
    assert error.value.code == 'like_daily_limit'


def test_stale_pending_and_unknown_block_until_next_beijing_day(db, monkeypatch):
    stamp = datetime(2026, 10, 21, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    monkeypatch.setattr(like_budget.time, 'time', lambda: stamp)
    pending = reserve(db)
    stamp += 121  # Process never received a receipt; stale operation cannot be retried that day.
    # This one crossed midnight: a new day has its own budget once the in-flight window expires.
    fresh = reserve(db)
    assert fresh['amount'] == 10 and fresh['id'] != pending['id']
    like_budget.settle(db, fresh, 'unknown')
    with pytest.raises(ToolDenied) as error: reserve(db)
    assert error.value.code == 'like_result_unknown'
    stamp += 86400
    assert reserve(db)['amount'] == 10


def test_stale_pending_same_day_is_unknown(db, monkeypatch):
    stamp = datetime(2026, 10, 21, 12, 0, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    monkeypatch.setattr(like_budget.time, 'time', lambda: stamp)
    reserve(db)
    stamp += 121
    with pytest.raises(ToolDenied) as error: reserve(db)
    assert error.value.code == 'like_result_unknown'


def test_inflight_cross_midnight_is_counted_on_completion_day(db, monkeypatch):
    stamp = datetime(2026, 10, 21, 23, 59, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    monkeypatch.setattr(like_budget.time, 'time', lambda: stamp)
    pending = reserve(db, times=1)
    stamp += 2
    with pytest.raises(ToolDenied) as error: reserve(db)
    assert error.value.code == 'like_request_in_progress'
    assert like_budget.settle(db, pending, 'accepted')['daily_submitted'] == 1
    assert reserve(db)['amount'] == 9


def test_group_caps_count_shared_accepted_likes_and_stay_independent(db):
    first = reserve(db, times=3)
    like_budget.settle(db, first, 'accepted')
    second = reserve(db, scope=OTHER, policy={'like_max_times': 10, 'like_daily_limit': 5})
    assert second['amount'] == 2
    like_budget.settle(db, second, 'accepted')
    assert reserve(db)['amount'] == 5


def receipt_trace(db, *, id='receipt', echo='actual', response=None, kind='chat', target=USER, amount=1, stamp=None, scope=SCOPE):
    stamp = time.time() if stamp is None else stamp
    stages = [{'name':'onebot_request','ts':stamp,'data':{'action':'send_like','echo':echo,'params':{'user_id':target,'times':amount}}}]
    if response is not None:
        stages.append({'name':'onebot_response','ts':stamp+.001,'data':{'echo':echo,'response':{'echo':echo,**response}}})
    return db.put('trace', id, {'kind':kind,'scope':scope,'source_ids':[SOURCE],'stages':stages})


def unconfirmed_attempt(db):
    db.put('social_attempt',KEY,{'version':2,'day':like_budget.day_at(time.time()),
        'submitted_count':0,'source_id':SOURCE,'attempted_at':time.time()-60,'unknown':True})


def test_old_refusal_is_not_hidden_by_memory_traces_or_later_local_denial(db):
    unconfirmed_attempt(db)
    receipt_trace(db,stamp=time.time()-40,response={'status':'failed','retcode':1200,'wording':'由于对方权限设置，点赞失败'})
    for i in range(15):db.put('trace',str(i),{'kind':'extract','scope':SCOPE,'source_ids':[SOURCE],'stages':[]})
    db.audit(SCOPE,'tool',{'name':'profile_like','event_id':SOURCE,'args':{'user_id':USER,'times':10},
        'status':'denied','error_code':'like_daily_limit'})
    assert reserve(db)['amount']==10


@pytest.mark.parametrize('kind',['extract','replay'])
def test_nonexecution_trace_cannot_resolve_unknown(db,kind):
    unconfirmed_attempt(db)
    receipt_trace(db,kind=kind,response={'status':'failed','wording':'对方权限设置'})
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_result_unknown'


@pytest.mark.parametrize('change',['other_target','other_scope','conflicting','missing','generic_error'])
def test_receipt_matching_does_not_guess_uncertain_old_outcomes(db,change):
    unconfirmed_attempt(db)
    value=receipt_trace(db,target='99999999' if change=='other_target' else USER,
        scope=OTHER if change=='other_scope' else SCOPE,
        response=None if change=='missing' else {'status':'failed','wording':'未知错误' if change=='generic_error' else '对方权限设置'})
    if change=='conflicting':
        value['stages'].append({'name':'onebot_response','data':{'echo':'actual','response':{'status':'ok','retcode':0}}})
        db.put('trace','receipt',value)
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_result_unknown'


def test_uncertain_current_attempt_ignores_receipts_from_earlier_attempts(db):
    first=reserve(db,times=1);like_budget.settle(db,first,'accepted')
    receipt_trace(db,response={'status':'ok','retcode':0},stamp=time.time()-60)
    second=reserve(db,times=2);like_budget.settle(db,second,'unknown')
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_result_unknown'


async def test_actual_late_onebot_receipt_resolves_budget_once_without_resending(db):
    from rolebot.onebot import OneBot, ActionUnknown
    from rolebot import tracing
    bot=OneBot();bot.trace_db=db
    class Socket:
        calls=[]
        async def send_json(self,data):self.calls.append(data)
    socket=Socket();await bot.connect(socket,BOT)
    db.put('trace','late',{'kind':'chat','scope':SCOPE,'source_ids':[SOURCE],'stages':[]})
    token=tracing.active_trace.set('late')
    pending=reserve(db,times=3)
    try:
        with pytest.raises(ActionUnknown):await bot.call('send_like',{'user_id':USER,'times':3},mutation=True,timeout=.01)
    finally:tracing.active_trace.reset(token)
    like_budget.settle(db,pending,'unknown')
    assert len(socket.calls)==1
    echo=socket.calls[0]['echo']
    assert bot.response({'echo':echo,'status':'ok','retcode':0})
    assert not bot.response({'echo':echo,'status':'ok','retcode':0})
    topup=reserve(db)
    assert topup['amount']==7
    receipt=like_budget.settle(db,topup,'accepted')
    assert receipt['daily_submitted']==10 and len(socket.calls)==1
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_daily_limit'


def test_late_failure_preserves_cooldown_without_blocking_indefinitely(db,monkeypatch):
    stamp=time.time();monkeypatch.setattr(like_budget.time,'time',lambda:stamp)
    pending=reserve(db);like_budget.settle(db,pending,'unknown')
    receipt_trace(db,response={'status':'failed','retcode':1200,'wording':'对方权限设置'})
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_retry_cooldown'
    stamp+=31
    assert reserve(db)['amount']==10


def test_late_receipt_across_midnight_counts_on_receipt_day(db,monkeypatch):
    stamp=datetime(2026,10,21,23,59,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    monkeypatch.setattr(like_budget.time,'time',lambda:stamp)
    pending=reserve(db,times=3);like_budget.settle(db,pending,'unknown')
    value=receipt_trace(db,amount=3,response={'status':'ok','retcode':0})
    stamp+=2
    value['stages'][1]['ts']=stamp;db.put('trace','receipt',value)
    assert reserve(db)['amount']==7


def test_unknown_near_midnight_keeps_short_receipt_window(db,monkeypatch):
    stamp=datetime(2026,10,21,23,59,59,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
    monkeypatch.setattr(like_budget.time,'time',lambda:stamp)
    pending=reserve(db);like_budget.settle(db,pending,'unknown');stamp+=2
    with pytest.raises(ToolDenied) as error:reserve(db)
    assert error.value.code=='like_result_unknown'
    stamp+=121
    assert reserve(db)['amount']==10


async def test_late_onebot_receipt_does_not_restore_forgotten_trace(db):
    from rolebot.onebot import OneBot, ActionUnknown
    from rolebot import tracing
    bot=OneBot();bot.trace_db=db
    class Socket:
        async def send_json(self,data):self.data=data
    socket=Socket();await bot.connect(socket,BOT)
    db.put('trace','redacted',{'kind':'chat','scope':SCOPE,'source_ids':[SOURCE],'stages':[]})
    token=tracing.active_trace.set('redacted')
    try:
        with pytest.raises(ActionUnknown):await bot.call('send_like',{'user_id':USER,'times':1},mutation=True,timeout=.01)
    finally:tracing.active_trace.reset(token)
    db.put('trace','redacted',{'redacted':True,'stages':[]})
    bot.response({'echo':socket.data['echo'],'status':'failed','wording':'对方权限设置'})
    assert '权限设置' not in json_dump(db.get('trace','redacted'))


def json_dump(value):
    import json
    return json.dumps(value,ensure_ascii=False)


def test_like_program_limits_above_ten_and_cross_group_ledger(db):
    policy={'like_max_times':30,'like_daily_limit':50}
    first=like_budget.reserve(db,'10000001',SCOPE,USER,'first',USER,None,policy)
    assert first['amount']==30
    like_budget.settle(db,first,'accepted')
    second=like_budget.reserve(db,'10000001','10000001:g:20000002',USER,'second',USER,None,policy)
    assert second['amount']==20
    like_budget.settle(db,second,'accepted')
