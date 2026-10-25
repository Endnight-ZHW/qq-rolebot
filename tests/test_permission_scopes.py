from pathlib import Path
import importlib.util
import json
import time

import pytest

from rolebot import delivery
from rolebot.message_targets import active_recall
from rolebot.onebot import ActionUnknown
from rolebot.permissions import AccessConfig, SELF_TOOLS, SELF_RECALL
from rolebot.tool_feedback import ground_reply
from rolebot.tools import ToolDenied

from support import SCOPE, OTHER, USER, SECOND, source


"""User-visible audience/target policy through real API and execution boundaries."""


@pytest.mark.parametrize('mode', ['legacy', 'profile'])
async def test_permission_facts_and_execution_share_request_switch(runtime, mode):
    r = runtime; platform(r); configure(r, 'member_mute', 'all')
    assignments(r, members={USER: {'allow': ['tool:member_mute']}})
    group = r.db.get('group', SCOPE)
    group['tools']['member_mute'].update(user_enabled=False)
    if mode == 'legacy': group['tools']['member_mute']['request_access'] = None
    r.db.put('group', SCOPE, group)
    await r.tools.permissions.refresh(SCOPE, [USER])
    snapshot = next(t for t in r.tools.runtime_status(SCOPE,[{'user_id':USER}])['tools'] if t['name']=='member_mute')
    preview = r.tools.permission_status(SCOPE,USER,'member_mute')
    result = await operate(r,'member_mute')
    assert snapshot['status']=='user_requests_disabled' and not snapshot['authorized_requesters']
    assert snapshot['requester_access'][USER]['error_code']==preview['error_code']==result['error_code']=='user_requests_disabled'
    assert result['submitted'] is False and not any(c[2] for c in r.bot.calls)


async def test_effective_limits_merge_account_group_and_default_protection(runtime):
    r=runtime;platform(r);configure(r,'member_mute','all')
    assignments(r,members={USER:{'max_duration':30,'protected_ids':[SECOND]}})
    await r.tools.permissions.refresh(SCOPE,[USER])
    limits=r.tools.permission_status(SCOPE,USER,'member_mute')['limits']
    assert limits['max_duration']==30 and limits['duration_sources']=={'tool':600,'requester':30}
    assert SECOND in limits['protected_ids'] and ADMIN in limits['protection_sources']['default']
    result=await r.tools.run(SCOPE,'default',USER,'limit','member_mute',{'user_id':USER,'duration':60,'reason':'本人请求'})
    assert result['error_code']=='mute_duration_limit' and '30' in result['error']


@pytest.mark.parametrize('mode,switch', [('request','user_enabled'),('shared','user_enabled'),('shared','autonomous_enabled'),('assistance','autonomous_enabled')])
async def test_prepared_media_rechecks_original_mode_after_text(runtime,mode,switch):
    from PIL import Image
    import io
    r=runtime;platform(r);configure(r,'message_send','all')
    raw=io.BytesIO();Image.new('RGB',(20,20),'red').save(raw,'PNG')
    sticker=r.engine.stickers.register(SCOPE,raw.getvalue(),{'name':'测试','tags':'开心'})
    m=source(r,'机器人发文字和表情',1)
    plan=(await r.tools.run(SCOPE,'default',USER,m['id'],'message_send',{'text':'你好','sticker_id':sticker['id']},mode=mode))['result']['delivery']
    async def revoke(_):
        group=r.db.get('group',SCOPE);group['tools']['message_send'][switch]=False
        group['revision']+=1;r.db.put('group',SCOPE,group)
    r.engine.delivery_sleep=revoke
    with pytest.raises(ToolDenied) as caught:
        await delivery.deliver(r.engine,SCOPE,'default',m,[m],{},'你好',{},[],{'delivery_plan':plan},lambda:True,False)
    assert caught.value.code==('user_requests_disabled' if switch=='user_enabled' else 'autonomous_disabled')
    assert len(r.bot.sent)==1 and all(s['type']!='image' for s in r.bot.sent[0][1])


async def test_prepared_first_reply_uses_own_quota_once_and_cannot_borrow_proof(runtime):
    r=runtime;platform(r);configure(r,'message_send','all')
    group=r.db.get('group',SCOPE);group['tools']['message_send']['max_per_hour']=1;r.db.put('group',SCOPE,group)
    m=source(r,'机器人你好',1)
    result=await r.tools.run(SCOPE,'default',USER,m['id'],'message_send',{'text':'你好'})
    plan=result['result']['delivery']
    with pytest.raises(ToolDenied): await r.tools.check_delivery(SCOPE,{**plan,'actor':SECOND})
    with pytest.raises(ToolDenied): await r.tools.check_delivery(OTHER,plan)
    assert await delivery.deliver(r.engine,SCOPE,'default',m,[m],{},'你好',{},[],{'delivery_plan':plan},lambda:True,False)=='sent'
    assert len(r.bot.sent)==1
    result=await r.tools.run(SCOPE,'default',USER,'next','message_send',{'text':'第二条'})
    assert result['error_code']=='tool_hourly_limit' and result['submitted'] is False


@pytest.mark.parametrize('exception', ['lookup', 'cancel'])
async def test_preflight_failure_is_never_an_unknown_mutation(runtime,exception):
    import asyncio
    from rolebot.onebot import OneBotError
    r=runtime;platform(r);configure(r,'member_mute','all');original=r.bot.call
    async def call(action,params=None,**kwargs):
        if action=='get_group_member_info' and params['user_id']==r.bot.self_id:
            if exception=='cancel':raise asyncio.CancelledError()
            raise OneBotError('查询超时')
        return await original(action,params,**kwargs)
    r.bot.call=call
    if exception=='cancel':
        with pytest.raises(asyncio.CancelledError):await operate(r,'member_mute')
        result=json.loads(r.db.rows("SELECT data FROM audit WHERE kind='tool' ORDER BY ts DESC LIMIT 1")[0]['data'])
        assert result['status']=='error'
    else:
        result=await operate(r,'member_mute')
        assert result['error_code']=='platform_identity_unavailable'
    assert result['submitted'] is False and result['phase']=='platform_check'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('text', ['禁言他60秒','把这个成员禁言60秒','他再欺负你 就给他禁言'])
@pytest.mark.parametrize('caption', [False,True])
async def test_permission_claim_cannot_confuse_requester_with_bot(runtime,text,caption):
    r=runtime;platform(r);configure(r,'member_mute','disabled','disabled')
    m=source(r,text,1);await r.tools.permissions.refresh(SCOPE,[USER])
    executed=[{'name':'message_send','status':'success'}] if caption else []
    reply,reason=ground_reply('我没有禁言权限。',[m],executed,r.tools.runtime_status(SCOPE,[m]),target=m)
    assert '按你的请求' in reply and reason=='grounded_request_permission'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('text,expected', [('机器人会禁言吗',True),('有禁言权限吗',True),('机器人，禁言他60秒',False),('他再欺负你就禁言他',False),('测试成员甲有没有禁言权限',False)])
def test_capability_question_aliases_do_not_turn_requests_into_demonstrations(text,expected):
    from rolebot.capability_facts import inquiry
    assert bool(inquiry(text)) is expected


@pytest.mark.parametrize('status,code,expected', [('denied','platform_identity_unavailable','这次没执行'),('unknown','platform_result_unknown','不重复执行')])
def test_read_success_and_caption_do_not_hide_action_failure(status,code,expected):
    executed=[{'name':'capabilities','status':'success'}, {'name':'member_info','status':'success'},
              {'name':'member_mute','status':status,'error_code':code}, {'name':'message_send','status':'success'}]
    text,_=ground_reply('我没有权限。',[{'text':'禁言他30秒'}],executed)
    assert expected in text and '没有权限' not in text


ADMIN = '90000001'


def configure(r, name, member='self', admin='all'):
    group = r.db.get('group', SCOPE)
    group['interaction_enabled'] = True
    group['tools'].setdefault(name, {}).update(enabled=True, user_enabled=True,
        request_access={'member': member, 'admin': admin})
    r.db.put('group', SCOPE, group)


def assignments(r, **kwargs):
    r.db.put('qq_access', SCOPE, AccessConfig(**kwargs).model_dump())


def platform(r, roles=None):
    roles = roles if roles is not None else {}
    values = {}
    original = r.bot.call
    async def call(action, params=None, mutation=False, **kwargs):
        result = await original(action, params, mutation=mutation, **kwargs)
        if action == 'get_group_member_info':
            return {'user_id': params['user_id'], 'group_id': params['group_id'],
                'role': roles.get(params['user_id'], 'owner' if params['user_id'] == r.bot.self_id else 'member'),
                **values.get(params['user_id'], {})}
        if action in ('set_group_special_title', 'set_group_card'):
            values.setdefault(params['user_id'], {}).update(
                {'title': params['special_title']} if action == 'set_group_special_title' else {'card': params['card']})
        return result
    r.bot.call = call
    return roles


ARGS = {
    'member_title': {'title': '小猫'}, 'member_card': {'card': '新名片'},
    'member_mute': {'duration': 10}, 'member_unmute': {}, 'member_kick': {}, 'member_info': {},
}


async def operate(r, name, target=USER, actor=USER, id='op'):
    args = {'user_id': target, **ARGS[name]}
    if name != 'member_info': args['reason'] = '本人请求'
    return await r.tools.run(SCOPE, 'default', actor, id, name, args)


@pytest.mark.parametrize('name', ARGS)
async def test_self_profile_is_base_grant_and_hard_target_ceiling(runtime, name):
    r = runtime; platform(r); configure(r, name)
    # A legacy role and personal allow may NOT widen self to all.
    assignments(r, roles={'helper': {'allow': ['tool:'+name]}},
        members={USER: {'roles': ['helper'], 'allow': ['tool:'+name]}})
    result = await operate(r, name)
    assert result['status'] == 'success', result
    before = len([c for c in r.bot.calls if c[2]])
    result = await operate(r, name, SECOND, id='other')
    assert result['error_code'] == 'self_target_only', result
    assert len([c for c in r.bot.calls if c[2]]) == before
    info = r.tools.permission_status(SCOPE, USER, name)
    assert info['available'] and info['target_scope'] == 'self'
    runtime_info = next(x for x in r.tools.runtime_status(SCOPE, [{'user_id': USER}])['tools'] if x['name'] == name)
    assert runtime_info['self_only_requesters'] == [USER]


@pytest.mark.parametrize('role', ['owner', 'admin'])
async def test_qq_admin_receives_profile_without_legacy_staff_inheritance(runtime, role):
    r = runtime; platform(r, {USER: role}); configure(r, 'member_title', 'disabled')
    assignments(r, inherit_qq_admin=False)
    assert (await operate(r, 'member_title', SECOND))['status'] == 'success'
    preview = await r.tools.permissions.effective(SCOPE, USER)
    assert preview['tool_access']['member_title']['target_scope'] == 'all'
    assert 'command:pause' not in preview['allow']


@pytest.mark.parametrize('actor', [USER, ADMIN])
async def test_admin_self_disabled_and_explicit_denial_apply_to_backend_owner_too(runtime, actor):
    r = runtime; platform(r, {actor: 'admin'}); configure(r, 'member_title', 'all', 'self')
    assert (await operate(r, 'member_title', SECOND, actor))['error_code'] == 'self_target_only'
    configure(r, 'member_title', 'all', 'disabled')
    assert (await operate(r, 'member_title', actor, actor))['error_code'] == 'request_access_denied'
    configure(r, 'member_title', 'all', 'all')
    assignments(r, members={actor: {'deny': ['tool:member_title']}})
    assert (await operate(r, 'member_title', actor, actor))['error_code'] == 'user_permission_denied'
    if actor == ADMIN:
        assert 'command:resume' in r.tools.permissions.resolve(SCOPE, actor)['allow']


async def test_custom_staff_is_not_qq_admin_and_expiry_does_not_promote(runtime):
    r = runtime; platform(r); configure(r, 'member_title', 'disabled')
    assignments(r, members={USER: {'roles': ['staff'], 'allow': ['tool:member_title'], 'expires_at': time.time()+60}})
    assert (await operate(r, 'member_title'))['error_code'] == 'request_access_denied'
    assignments(r, members={USER: {'deny': ['tool:member_title'], 'expires_at': time.time()-1}})
    configure(r, 'member_title')
    assert (await operate(r, 'member_title', id='after-expiry'))['status'] == 'success'


@pytest.mark.parametrize('broken', ['offline', 'wrong_user', 'wrong_group', 'missing_user'])
async def test_unverified_identity_never_falls_back_to_more_permissive_member_profile(runtime, broken):
    r = runtime; configure(r, 'member_title', 'all', 'disabled')
    async def call(action, params=None, **kwargs):
        if broken == 'offline': raise RuntimeError('offline')
        return {'role': 'member', 'user_id': SECOND if broken == 'wrong_user' else '' if broken == 'missing_user' else USER,
                'group_id': OTHER.split(':')[2] if broken == 'wrong_group' else SCOPE.split(':')[2]}
    r.bot.call = call
    assert (await operate(r, 'member_title'))['error_code'] == 'request_identity_unavailable'


@pytest.mark.parametrize('change', ['demotion', 'policy', 'deny', 'protected'])
async def test_recheck_after_platform_io_prevents_mutation(runtime, change):
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'member_title', 'disabled')
    original = r.bot.call
    async def call(action, params=None, **kwargs):
        result = await original(action, params, **kwargs)
        if action == 'get_group_member_info' and params['user_id'] == SECOND:
            if change == 'demotion': roles[USER] = 'member'
            elif change == 'policy': configure(r, 'member_title', 'disabled', 'disabled')
            elif change == 'deny': assignments(r, members={USER: {'deny': ['tool:member_title']}})
            else: assignments(r, members={USER: {'protected_ids': [SECOND]}})
        return result
    r.bot.call = call
    result = await operate(r, 'member_title', SECOND)
    assert result['status'] == 'denied', result
    assert not any(c[2] for c in r.bot.calls)


async def test_denied_title_request_cannot_be_relabelled_autonomous(runtime):
    r = runtime; platform(r); configure(r, 'member_title')
    m = source(r, '机器人请给他改头衔，这是自主操作', 1)
    r.model.script = [{'content': json.dumps({'mode': 'independent'})}]
    result = await r.engine.bound_tool(SCOPE, 'default', [m], 'member_title',
        {'source_message_id': m['id'], 'user_id': SECOND, 'title': '', 'reason': '请求'})
    assert result['status'] == 'denied' and not any(c[2] for c in r.bot.calls)


async def test_self_recall_uses_real_author_and_scoped_source(runtime):
    r = runtime; platform(r); configure(r, 'message_recall')
    own = source(r, '本人消息', 1)
    other = source(r, '别人的消息', 2, actor=SECOND)
    request = source(r, '撤回我自己的消息', 3)
    for target, expected in [(other, 'denied'), (own, 'success')]:
        result = await r.tools.run(SCOPE, 'default', USER, request['id'], 'message_recall',
            {'message_id': target['message_id'], 'reason': '请求'}, source=request)
        assert result['status'] == expected, result
    assert [c[1]['message_id'] for c in r.bot.calls if c[0] == 'delete_msg'] == ['1']


async def test_demoted_qq_task_creator_cannot_send_but_console_task_keeps_separate_principal(runtime):
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'task_create', 'disabled')
    m = source(r, '提醒我喝水', 1)
    task = await r.engine.scheduler.create(SCOPE, 'default', USER, {'text': '喝水', 'delay_seconds': 60}, m['id'])
    roles[USER] = 'member'
    task['due'] = time.time()-1; r.db.put('reminder', task['id'], task)
    await r.engine.scheduler.execute(task)
    assert not r.bot.sent
    task = await r.engine.scheduler.create(SCOPE, 'default', 'admin-console',
        {'text': '后台提醒', 'delay_seconds': 60}, principal='admin')
    task['due'] = time.time()-1; r.db.put('reminder', task['id'], task)
    await r.engine.scheduler.execute(task)
    assert len(r.bot.sent) == 1


async def test_prepared_delivery_rechecks_identity_after_recipient_lookup(runtime):
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'message_send', 'disabled')
    m = source(r, '机器人发一句话', 1)
    result = await r.tools.run(SCOPE, 'default', USER, m['id'], 'message_send', {'text': 'hello', 'mentions': [SECOND]})
    assert result['status'] == 'success'
    original = r.bot.call
    async def call(action, params=None, **kwargs):
        value = await original(action, params, **kwargs)
        if action == 'get_group_member_info' and params['user_id'] == SECOND: roles[USER] = 'member'
        return value
    r.bot.call = call
    with pytest.raises(ToolDenied):
        await delivery.deliver(r.engine, SCOPE, 'default', m, [m], {}, 'hello', {}, [],
            {'delivery_plan': result['result']['delivery']}, lambda: True, False)
    assert not r.bot.sent


def test_api_profile_validation_version_conflict_and_legacy_write_preservation(authed_client):
    c = authed_client
    assert c.put('/api/v1/groups/'+SCOPE, json={'name': '测试群', 'interaction_enabled': True}).status_code == 200
    group = next(g for g in c.get('/api/v1/groups').json() if g['id'] == SCOPE)
    url = '/api/v1/groups/'+SCOPE+'/tools'
    policy = {'member_title': {'enabled': True, 'request_access': {'member': 'self', 'admin': 'all'}}}
    assert c.put(url, json=policy).status_code == 422
    assert c.put(url+'?expected_revision='+str(group['revision']), json=policy).status_code == 200
    assert c.put(url+'?expected_revision='+str(group['revision']), json=policy).status_code == 409
    saved = c.put(url, json={'member_title': {'max_per_hour': 8}})
    assert saved.status_code == 200
    assert saved.json()['member_title']['request_access'] == policy['member_title']['request_access']
    assert c.put(url, json={'group_notice': {'request_access': {'member': 'self', 'admin': 'all'}}}).status_code == 422
    assert c.put(url, json={'member_title': {'request_access': {'member': 'all'}}}).status_code == 422
    catalog = c.get('/api/v1/tools').json()
    assert {t['name'] for t in catalog if 'self' in t['supported_scopes']} == SELF_TOOLS
    # Restoring legacy mode is a permission change too.
    assert c.put(url, json={'member_title': {'request_access': None}}).status_code == 422
    group = next(g for g in c.get('/api/v1/groups').json() if g['id'] == SCOPE)
    assert c.put(url+'?expected_revision='+str(group['revision']), json={'member_title': {'request_access': None}}).status_code == 200


async def test_profile_does_not_override_platform_owner_requirement_or_explicit_protection(runtime):
    r = runtime; configure(r, 'member_title')
    assert (await operate(r, 'member_title'))['error_code'] == 'platform_owner_required'
    platform(r)
    group = r.db.get('group', SCOPE); group['tools']['member_title']['protected_ids'] = [USER]; r.db.put('group', SCOPE, group)
    assert (await operate(r, 'member_title'))['error_code'] == 'explicit_target_protected'
    assert not any(c[2] for c in r.bot.calls)


async def test_spoofed_source_and_unknown_effect_never_retry(runtime):
    r = runtime; platform(r); configure(r, 'member_mute')
    m = source(r, '请求', 1, actor=SECOND)
    result = await r.tools.run(SCOPE, 'default', USER, m['id'], 'member_mute',
        {'user_id': USER, 'duration': 10, 'reason': '请求'}, source=m)
    assert result['error_code'] == 'source_mismatch'
    r.bot.unknown = True
    result = await operate(r, 'member_mute')
    assert result['status'] == 'unknown'
    result = await operate(r, 'member_mute')
    assert result['status'] == 'unknown' and result['duplicate']
    assert len([c for c in r.bot.calls if c[2]]) == 1


async def test_text_and_sticker_separate_and_demotion_stops_pending_sticker(runtime):
    from PIL import Image
    import io
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'message_send', 'disabled')
    configure(r, 'sticker_send', 'disabled')
    raw = io.BytesIO(); Image.new('RGB', (20, 20), 'red').save(raw, 'PNG')
    sticker = r.engine.stickers.register(SCOPE, raw.getvalue(), {'name': '测试', 'tags': '开心'})
    m = source(r, '机器人发个表情', 1)
    plan = (await r.tools.run(SCOPE, 'default', USER, m['id'], 'message_send',
        {'text': '你好', 'sticker_id': sticker['id']}))['result']['delivery']
    async def demote(_): roles[USER] = 'member'
    r.engine.delivery_sleep = demote
    with pytest.raises(ToolDenied):
        await delivery.deliver(r.engine, SCOPE, 'default', m, [m], {}, '你好', {}, [],
            {'delivery_plan': plan}, lambda: True, False)
    assert len(r.bot.sent) == 1
    assert any(s['type'] == 'text' for s in r.bot.sent[0][1])
    assert not any(s['type'] == 'image' for s in r.bot.sent[0][1])


async def test_sticker_candidates_follow_profile_and_mixed_audience(runtime):
    from PIL import Image
    import io
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'sticker_list', 'disabled')
    configure(r, 'sticker_send', 'disabled'); configure(r, 'message_send', 'disabled')
    raw = io.BytesIO(); Image.new('RGB', (20, 20), 'red').save(raw, 'PNG')
    r.engine.stickers.register(SCOPE, raw.getvalue(), {'name': '测试', 'tags': '开心'})
    m = source(r, '机器人开心吗', 1); other = source(r, '我也想看', 2, actor=SECOND)
    await r.engine.permissions.refresh(SCOPE, [USER, SECOND])
    assert r.engine.stickers.candidates(SCOPE, 'default', [m])
    assert not r.engine.stickers.candidates(SCOPE, 'default', [m, other])
    roles[USER] = 'member'; await r.engine.permissions.refresh(SCOPE, [USER])
    assert not r.engine.stickers.candidates(SCOPE, 'default', [m])


async def test_read_result_not_shared_after_other_actor_demoted(runtime):
    r = runtime; roles = platform(r, {USER: 'admin', SECOND: 'admin'}); configure(r, 'member_info', 'disabled')
    original = r.tools.invoke
    async def invoke(*args):
        result = await original(*args); roles[SECOND] = 'member'; return result
    r.tools.invoke = invoke
    result = await r.tools.run(SCOPE, 'default', USER, 'shared', 'member_info', {'user_id': USER},
        mode='shared', audience=[USER, SECOND])
    assert result['status'] == 'denied'


def test_permission_backup_restores_only_permissions_and_increments_revision(runtime):
    spec = importlib.util.spec_from_file_location('permission_backup', Path(__file__).parents[1]/'ops/permission_backup.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    r = runtime
    path = r.db.rows('PRAGMA database_list')[0]['file']
    saved = module.snapshot(path)
    configure(r, 'member_title')
    assignments(r, members={USER: {'allow': ['tool:member_title']}})
    source(r, '升级之后的聊天应当保留', 1)
    group = r.db.get('group', SCOPE); group['name'] = '升级后改过群名'; group['revision'] = 10
    r.db.put('group', SCOPE, group)
    assert not module.restore(path, saved)['applied']
    assert r.db.get('group', SCOPE)['tools']['member_title']['request_access']
    assert module.restore(path, saved, apply=True)['applied']
    current = r.db.get('group', SCOPE)
    assert current['tools'] == saved['scopes'][SCOPE]['tools']
    assert current['name'] == '升级后改过群名' and current['revision'] > 10
    assert r.db.message(SCOPE, SCOPE+':1', 'default')['text'] == '升级之后的聊天应当保留'
    assert not r.engine.permissions.config(SCOPE)['members']
    assert r.engine.permissions.config(SCOPE)['revision'] > 0


@pytest.mark.parametrize('scope_rule', ['self', 'all'])
async def test_explicit_self_recall_denial_survives_new_profile(runtime, scope_rule):
    from rolebot.permissions import SELF_RECALL
    r = runtime; platform(r); configure(r, 'message_recall', scope_rule)
    assignments(r, members={USER: {'deny': [SELF_RECALL]}})
    own = source(r, '本人消息', 1); request = source(r, '撤回我自己的消息', 2)
    result = await r.tools.run(SCOPE, 'default', USER, request['id'], 'message_recall',
        {'message_id': own['message_id'], 'reason': '请求'}, source=request)
    assert result['status'] == 'denied' and result['error_code'] == 'user_permission_denied', result
    assert not any(c[2] for c in r.bot.calls)


async def test_recall_recovery_identity_failure_is_not_reported_as_missing_grant(runtime):
    r = runtime; configure(r, 'message_recall')
    async def offline(*args, **kwargs): raise RuntimeError('offline')
    r.bot.call = offline
    request = source(r, '撤回这条', 2, replies=['999'])
    result = await r.tools.run(SCOPE, 'default', USER, request['id'], 'message_recall',
        {'message_id': '999', 'reason': '请求'}, source=request)
    assert result['error_code'] == 'request_identity_unavailable', result


async def test_normal_reply_refreshes_read_permission_before_final_send(runtime):
    r = runtime; roles = platform(r, {USER: 'admin'}); configure(r, 'image_read', 'disabled')
    m = source(r, '机器人总结一下图片', 1)
    await r.engine.permissions.refresh(SCOPE, [USER])
    # Simulates the existing image/forward current() checks after model generation.
    def current(): return 'tool:image_read' in r.engine.permissions.resolve(SCOPE, USER)['allow']
    class Review:
        remember = True
        sources = set()
        async def check(self, *args, **kwargs):
            roles[USER] = 'member'
            return True
    status = await delivery.deliver(r.engine, SCOPE, 'default', m, [m], {}, '图中资料', {}, [], {}, current, False, review=Review())
    assert status == 'cancelled' and not r.bot.sent


OWNER='90000001'


def setup(r, *, role='owner'):
    r.group['tools']['message_recall']['enabled']=True
    r.db.put('group',SCOPE,r.group)
    state={'bot_role':role,'target_role':'member','lookup':None,'unknown':False}
    async def call(action, params=None, mutation=False, **kwargs):
        r.bot.calls.append((action,params,mutation))
        if action=='get_group_member_info':
            return {'user_id':params['user_id'],'group_id':params['group_id'],
                    'role':state['bot_role'] if params['user_id']==r.bot.self_id else state['target_role']}
        if action=='get_msg': return state['lookup']
        if action=='delete_msg' and state['unknown']: raise ActionUnknown('结果未知')
        return None
    r.bot.call=call
    return state


def request(r, actor=OWNER, reply='10'):
    m=source(r,'帮我撤回一下我的这条消息',20,actor=actor,replies=[reply])
    m['replies']=[reply]
    return m


def verdict(r, m, mode='request'):
    r.model.script=[{'content':json.dumps({'mode':mode,'request_message_id':m['id'],'source_matches':True,'context_request_ids':[]})}]


async def invoke(r,m,mid='10',reason='本人请求'):
    return await r.engine.bound_tool(SCOPE,'default',[m],'message_recall',{'message_id':mid,'reason':reason,'source_message_id':m['id']})


@pytest.mark.parametrize('mid',['10',SCOPE+':10','00010'])
async def test_owner_can_request_recall_of_own_verified_message(runtime,mid):
    r=runtime;setup(r)
    source(r,'上一条自己的消息',10,actor=OWNER)
    m=request(r);verdict(r,m)
    result=await invoke(r,m,mid)
    assert result['status']=='success'
    assert [x for x in r.bot.calls if x[0]=='delete_msg']==[('delete_msg',{'message_id':'10'},True)]
    assert active_recall.get() is None


async def test_protection_still_blocks_another_requester(runtime):
    r=runtime;setup(r);source(r,'管理员的消息',10,actor=OWNER)
    r.db.put('qq_access',SCOPE,AccessConfig(members={USER:{'allow':['tool:message_recall']}}).model_dump())
    m=request(r,USER);verdict(r,m)
    result=await invoke(r,m)
    assert result['error_code']=='implicit_target_protected'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('explicit',['group','requester'])
async def test_explicit_protection_is_not_bypassed_by_self_consent(runtime,explicit):
    r=runtime;setup(r)
    actor=OWNER if explicit=='group' else USER
    source(r,'自己的消息',10,actor=actor)
    if explicit=='group':
        r.group['tools']['message_recall']['protected_ids']=[actor];r.db.put('group',SCOPE,r.group)
    else:
        r.db.put('qq_access',SCOPE,AccessConfig(members={actor:{'allow':['tool:message_recall'],'protected_ids':[actor]}}).model_dump())
    m=request(r,actor);verdict(r,m)
    assert (await invoke(r,m))['error_code']=='explicit_target_protected'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('role,target_role,code',[('member','member','platform_admin_required'),('admin','admin','platform_hierarchy_denied'),('admin','owner','platform_hierarchy_denied')])
async def test_self_consent_does_not_override_qq_permissions(runtime,role,target_role,code):
    r=runtime;state=setup(r,role=role);state['target_role']=target_role
    source(r,'自己的消息',10,actor=OWNER);m=request(r);verdict(r,m)
    assert (await invoke(r,m))['error_code']==code
    assert not any(c[2] for c in r.bot.calls)


async def test_missing_real_quote_is_verified_without_importing_text(runtime):
    r=runtime;state=setup(r);m=request(r);verdict(r,m)
    state['lookup']={'message_id':10,'group_id':20000001,'user_id':int(OWNER),'sender':{'user_id':int(OWNER)},
                     'time':time.time(),'message':[{'type':'text','data':{'text':'仅用于平台返回，不写进记忆'}}]}
    assert (await invoke(r,m))['status']=='success'
    assert not r.db.message(SCOPE,'10','default')
    assert [x[0] for x in r.bot.calls].count('get_msg')==1
    assert '仅用于平台返回，不写进记忆' not in json.dumps(r.model.calls,ensure_ascii=False)
    assert any(x[0]=='delete_msg' for x in r.bot.calls)


@pytest.mark.parametrize('change',[{'group_id':20000002},{'message_id':99},{'user_id':SECOND,'sender':{'user_id':OWNER}},
                                  {'message_type':'private'},{'time':0},{'self_id':'99999999'}])
async def test_wrong_remote_identity_or_expired_quote_never_mutates(runtime,change):
    r=runtime;state=setup(r);m=request(r)
    state['lookup']={'message_id':10,'group_id':20000001,'user_id':OWNER,'time':time.time(),**change}
    result=await invoke(r,m)
    assert result['status']=='denied' and result['error_code'] in ('recall_target_mismatch','recall_target_unavailable')
    assert not any(x[2] for x in r.bot.calls)
    assert not r.model.calls


@pytest.mark.parametrize('kind',['unquoted','cross_group_id','forgotten','other_persona','expired'])
async def test_target_cannot_be_guessed_restored_or_cross_scope(runtime,kind):
    r=runtime;setup(r);m=request(r,reply='11' if kind=='unquoted' else '10')
    mid=OTHER+':10' if kind=='cross_group_id' else '10'
    if kind in ('forgotten','other_persona','expired'):
        r.db.add_message(SCOPE+':10',SCOPE,'another' if kind=='other_persona' else 'default',OWNER,'user','原文',{'message_id':10},
            excluded=kind=='forgotten',ts=time.time()-31*86400 if kind=='expired' else time.time())
    assert (await invoke(r,m,mid))['status']=='denied'
    assert not r.bot.calls


async def test_ungranted_member_cannot_fetch_or_delete_quoted_message(runtime):
    r=runtime;setup(r);m=request(r,USER)
    r.db.put('qq_access',SCOPE,AccessConfig(members={USER:{'deny':[SELF_RECALL]}}).model_dump())
    assert (await invoke(r,m))['error_code']=='user_permission_denied'
    assert not r.bot.calls


@pytest.mark.parametrize('saved',[True,False])
async def test_ordinary_member_can_only_request_own_recall(runtime,saved):
    r=runtime;state=setup(r)
    if saved: source(r,'成员自己的消息',10,actor=USER)
    else: state['lookup']={'message_id':10,'group_id':20000001,'user_id':USER,'time':time.time()}
    m=request(r,USER);verdict(r,m)
    assert (await invoke(r,m))['status']=='success'
    assert len([c for c in r.bot.calls if c[0]=='delete_msg'])==1
    assert 'tool:message_recall' not in r.tools.permissions.resolve(SCOPE,USER)['allow']


@pytest.mark.parametrize('author',[SECOND,'10000001'])
@pytest.mark.parametrize('saved',[True,False])
async def test_self_recall_grant_never_allows_other_authors(runtime,author,saved):
    r=runtime;state=setup(r)
    if saved: source(r,'其他作者的消息',10,actor=author)
    else: state['lookup']={'message_id':10,'group_id':20000001,'user_id':author,'time':time.time()}
    m=request(r,USER);verdict(r,m)
    assert (await invoke(r,m))['error_code']=='self_recall_only'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('assignment',[{'deny':[SELF_RECALL]},{'deny':['tool:message_recall']},
                                      {'tools_disabled':True},{'muted':True}])
async def test_explicit_restrictions_still_disable_own_recall(runtime,assignment):
    r=runtime;setup(r);source(r,'自己的消息',10,actor=USER)
    r.db.put('qq_access',SCOPE,AccessConfig(members={USER:assignment}).model_dump())
    m=request(r,USER);verdict(r,m)
    assert (await invoke(r,m))['status']=='denied'
    assert not any(c[2] for c in r.bot.calls)


@pytest.mark.parametrize('switch',['enabled','user_enabled'])
async def test_own_recall_still_requires_group_switches(runtime,switch):
    r=runtime;setup(r);source(r,'自己的消息',10,actor=USER)
    r.group['tools']['message_recall'][switch]=False;r.db.put('group',SCOPE,r.group)
    m=request(r,USER);verdict(r,m)
    assert (await invoke(r,m))['status']=='denied'
    assert not any(c[2] for c in r.bot.calls)


async def test_self_recall_permission_revoked_during_platform_check(runtime):
    r=runtime;setup(r);source(r,'自己的消息',10,actor=USER)
    m=request(r,USER);verdict(r,m)
    original=r.bot.call
    async def call(action,*args,**kwargs):
        result=await original(action,*args,**kwargs)
        if action=='get_group_member_info':
            r.db.put('qq_access',SCOPE,AccessConfig(members={USER:{'deny':[SELF_RECALL]}}).model_dump())
        return result
    r.bot.call=call
    assert (await invoke(r,m))['error_code']=='user_permission_denied'
    assert not any(c[2] for c in r.bot.calls)


async def test_own_request_cannot_be_routed_through_autonomous_permission(runtime):
    r=runtime;setup(r);source(r,'自己的消息',10,actor=USER)
    m=request(r,USER);verdict(r,m,'independent')
    assert (await invoke(r,m))['status']=='denied'
    assert not any(c[2] for c in r.bot.calls)


async def test_direct_runner_cannot_borrow_another_source_identity(runtime):
    r=runtime;setup(r);source(r,'自己的消息',10,actor=USER)
    m=request(r,SECOND)
    result=await r.tools.run(SCOPE,'default',USER,m['id'],'message_recall',{'message_id':'10','reason':'本人'},source=m)
    assert result['error_code']=='source_mismatch'
    assert not r.bot.calls


async def test_forgetting_during_remote_lookup_invalidates_result(runtime):
    r=runtime;state=setup(r);m=request(r,USER)
    state['lookup']={'message_id':10,'group_id':20000001,'user_id':USER,'time':time.time()}
    original=r.bot.call
    async def call(action,*args,**kwargs):
        result=await original(action,*args,**kwargs)
        if action=='get_msg': r.db.put('memory_epoch',SCOPE,{'value':1})
        return result
    r.bot.call=call
    assert (await invoke(r,m))['error_code']=='context_changed'
    assert not any(c[2] for c in r.bot.calls)


def test_self_recall_catalog_and_runtime_do_not_claim_full_grant(runtime):
    r=runtime;setup(r)
    caps={x['id']:x for x in r.tools.permissions.catalog()}
    assert caps[SELF_RECALL]['category']=='日常工具'
    tools=r.tools.runtime_status(SCOPE,[{'user_id':USER},{'user_id':OWNER}])['tools']
    recall=next(x for x in tools if x['name']=='message_recall')
    assert recall['authorized_requesters']==[OWNER] and recall['self_only_requesters']==[USER]
    assert all(not x.get('self_only_requesters') for x in tools if x['name']!='message_recall')
    r.db.put('qq_access',SCOPE,AccessConfig(member_tools=['tool:calculate']).model_dump())
    assert SELF_RECALL not in r.tools.permissions.resolve(SCOPE,USER)['allow']


def test_self_recall_permission_can_be_edited_through_existing_api(authed_client):
    c=authed_client
    from rolebot.engine import group_defaults
    group=group_defaults(SCOPE);group['enabled']=True;group['tools']['message_recall']['enabled']=True
    c.app.state.db.put('group',SCOPE,group)
    assert SELF_RECALL in {x['id'] for x in c.get('/api/v1/permissions/catalog').json()}
    cfg=c.get('/api/v1/permissions/'+SCOPE).json()
    assert SELF_RECALL in cfg['member_tools']
    effective=c.get('/api/v1/permissions/'+SCOPE+'/effective/'+USER).json()
    assert effective['tools']['message_recall_self']['available']
    assert not effective['tools']['message_recall']['available']
    cfg['members']={USER:{'deny':[SELF_RECALL]}}
    saved=c.put('/api/v1/permissions/'+SCOPE,json=cfg)
    assert saved.status_code==200 and saved.json()['members'][USER]['deny']==[SELF_RECALL]
    effective=c.get('/api/v1/permissions/'+SCOPE+'/effective/'+USER).json()
    assert not effective['tools']['message_recall_self']['available']


async def test_normalized_ids_and_changed_reason_do_not_repeat_unknown_recall(runtime):
    r=runtime;state=setup(r);state['unknown']=True
    source(r,'自己的消息',10,actor=OWNER);m=request(r);verdict(r,m)
    first=await invoke(r,m,SCOPE+':10')
    verdict(r,m)
    second=await invoke(r,m,'10',reason='换个理由再试')
    assert first['status']==second['status']=='unknown' and second['duplicate']
    assert len([c for c in r.bot.calls if c[0]=='delete_msg'])==1


@pytest.mark.parametrize('code,required',[('recall_target_unavailable','核实'),('explicit_target_protected','显式'),
                                         ('platform_hierarchy_denied','QQ')])
def test_failure_reply_uses_actual_cause(code,required):
    reply,reason=ground_reply('你是受保护账号，换条普通消息再试试。',[{'text':'帮我撤回这条消息'}],
        [{'name':'message_recall','status':'denied','error_code':code}])
    if code=='explicit_target_protected': assert '后台给这个操作设了目标保护' in reply
    else: assert required in reply
    assert reason=='grounded_tool_failure:'+code
