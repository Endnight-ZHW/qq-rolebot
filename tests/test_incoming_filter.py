from unittest.mock import AsyncMock, Mock
import asyncio
import json
import time

from pydantic import ValidationError
import pytest

from rolebot.api import Group
from rolebot.engine import group_defaults
from rolebot.incoming_filter import IncomingFilter
from rolebot.permissions import AccessConfig

from support import OTHER, SCOPE, SECOND, USER, event, source


def queued(r, *scopes):
    for scope in scopes or (SCOPE,):
        r.engine.queues[scope] = asyncio.Queue(maxsize=200)


def segmented(parts, id=1, **kwargs):
    return {**event(id=id, **kwargs), 'message': parts}


def at(qq='10000001'):
    return {'type': 'at', 'data': {'qq': qq}}


def text(value):
    return {'type': 'text', 'data': {'text': value}}


@pytest.mark.parametrize('parts', [[], [text(' \t\n')], [at()], [at(), text(' \u200b\ufeff ')], [at(), at()]])
async def test_empty_group_inputs_never_enter_chat_or_memory(runtime, parts):
    r=runtime
    await r.engine.receive(segmented(parts))
    assert not r.engine.queues and not r.model.calls and not r.bot.calls and not r.bot.sent
    assert not r.db.recent(SCOPE, 'default') and not r.db.all('memory_job')
    audit=r.db.rows("SELECT data FROM audit WHERE kind='message_filter'")
    assert len(audit)==1 and json.loads(audit[0]['data'])['status']=='ignored'
    assert len(r.engine.visible_messages[SCOPE])==1  # Physical QQ quote distance stays accurate.


async def test_repeated_text_does_not_trigger_extra_models_tools_or_memory(runtime):
    r=runtime;queued(r)
    for id in range(1, 21):
        await r.engine.receive(event('机器人你好', id=id))
    assert r.engine.queues[SCOPE].qsize()==1
    assert len(r.engine.recent_events[SCOPE])==1
    assert len(r.db.recent(SCOPE, 'default'))==1
    assert r.engine.memory.enqueue(SCOPE, 'default') is None
    first=r.engine.queues[SCOPE].get_nowait()
    r.model.script=[{'content': json.dumps({'speak':False, 'text':'', 'intent':'旁听'})}]
    await r.engine.turn(SCOPE, [first])
    assert len(r.model.calls)==1 and not r.bot.calls and not r.bot.sent


async def test_window_starts_at_first_accepted_input_and_is_not_extended_by_spam(runtime):
    r=runtime;queued(r);tick=[100.0]
    r.engine.incoming_filter.clock=lambda: tick[0]
    for id, stamp in enumerate((100.0, 130.0, 159.9, 160.0), start=1):
        tick[0]=stamp
        await r.engine.receive(event('相同的话', id=id))
    assert [m['message_id'] for m in list(r.engine.queues[SCOPE]._queue)]==['1','4']


async def test_same_text_from_other_members_or_groups_is_not_suppressed(runtime):
    r=runtime;queued(r, SCOPE, OTHER)
    r.db.put('group', OTHER, {**r.group, 'id':OTHER})
    for id, scope, actor in ((1,SCOPE,USER),(2,SCOPE,SECOND),(3,OTHER,USER)):
        await r.engine.receive(event('我也一样', id=id, scope=scope, user=actor))
    assert r.engine.queues[SCOPE].qsize()==2 and r.engine.queues[OTHER].qsize()==1


async def test_blank_text_between_at_segments_and_split_text_are_normalized(runtime):
    r=runtime;queued(r)
    await r.engine.receive(segmented([at(),text(' 你好 ')]))
    await r.engine.receive(segmented([at(),text('你'),text('好')], id=2))
    assert r.engine.queues[SCOPE].qsize()==1


async def test_different_mentions_quotes_or_position_do_not_collide(runtime):
    r=runtime;queued(r)
    variants=[
        [text('你好'),at(USER)], [text('你好'),at(SECOND)],
        [at(USER),text('你好')],
        [{'type':'reply','data':{'id':10}},text('帮我撤回')],
        [{'type':'reply','data':{'id':11}},text('帮我撤回')],
        [text('[@QQ:'+USER+']你好')],
    ]
    for id, parts in enumerate(variants, start=1):
        await r.engine.receive(segmented(parts,id=id))
    assert r.engine.queues[SCOPE].qsize()==len(variants)


@pytest.mark.parametrize('part', [at(USER), {'type':'reply','data':{'id':10}},
    {'type':'image','data':{'url':''}}, {'type':'face','data':{'id':1}},
    {'type':'record','data':{}}, {'type':'video','data':{}}, {'type':'file','data':{}}, {'type':'unknown','data':{}}])
async def test_non_text_content_is_not_treated_as_empty_or_repeated_text(runtime, part):
    r=runtime;queued(r)
    await r.engine.receive(segmented([at(),part]))
    await r.engine.receive(segmented([at(),part],id=2))
    assert r.engine.queues[SCOPE].qsize()==2


async def test_full_text_is_used_instead_of_truncated_model_preview(runtime):
    r=runtime;queued(r)
    for id, suffix in enumerate(('第一条','第二条','第一条'), start=1):
        await r.engine.receive(event('长内容'*4000+suffix,id=id))
    assert r.engine.queues[SCOPE].qsize()==2


@pytest.mark.parametrize('change', ['reset','persona','group_revision','permission_revision'])
async def test_changed_conversation_or_permissions_starts_a_new_window(runtime,change):
    r=runtime;queued(r)
    await r.engine.receive(event('请重新处理',id=1))
    if change=='reset': r.db.put('memory_epoch',SCOPE,{'value':1})
    elif change=='permission_revision': r.db.put('qq_access',SCOPE,AccessConfig(revision=1).model_dump())
    else:
        r.group['persona' if change=='persona' else 'revision']='other' if change=='persona' else 2
        r.db.put('group',SCOPE,r.group)
    await r.engine.receive(event('请重新处理',id=2))
    assert r.engine.queues[SCOPE].qsize()==2


@pytest.mark.parametrize('unavailable', ['paused','model','queue_full'])
async def test_unqueued_input_does_not_suppress_a_later_usable_request(runtime,unavailable):
    r=runtime;queued(r)
    if unavailable=='paused': r.group['paused']=True;r.db.put('group',SCOPE,r.group)
    elif unavailable=='model': r.model.configured=False
    else:
        r.engine.queues[SCOPE]=asyncio.Queue(maxsize=1)
        r.engine.queues[SCOPE].put_nowait({'sentinel':True})
    await r.engine.receive(event('帮忙看看',id=1))
    r.group['paused']=False;r.db.put('group',SCOPE,r.group);r.model.configured=True
    if unavailable=='queue_full': r.engine.queues[SCOPE].get_nowait()
    await r.engine.receive(event('帮忙看看',id=2))
    assert r.engine.queues[SCOPE].qsize()==1


async def test_private_chats_and_commands_keep_existing_behavior(runtime):
    r=runtime;private='10000001:p:'+USER;queued(r,private)
    r.db.put('group',private,{**r.group,'id':private})
    for id in (1,2):
        e=event('你好',id=id,user=USER);e.pop('group_id');e['message_type']='private'
        await r.engine.receive(e)
        await r.engine.receive(event('/bot status',id=id+10,user='90000001'))
    assert r.engine.queues[private].qsize()==2 and len(r.bot.sent)==2


async def test_opted_out_member_is_filtered_without_persisting_content(runtime):
    r=runtime;queued(r)
    r.db.put('member',SCOPE+':'+USER,{'remember':False})
    for id in (1,2): await r.engine.receive(event('不保存的文字',id=id))
    assert r.engine.queues[SCOPE].qsize()==1 and not r.db.recent(SCOPE,'default')
    assert '不保存的文字' not in repr(r.engine.incoming_filter.accepted)
    assert '不保存的文字' not in repr(r.db.rows('SELECT data FROM audit'))


async def test_reply_to_a_new_bot_question_has_its_own_repeat_window(runtime):
    import time
    r=runtime;queued(r)
    await r.engine.receive(event('好', id=1))
    r.db.put('followup',SCOPE+':default',{'target':USER,'source_id':SCOPE+':1','expires':time.time()+120})
    await r.engine.receive(event('好', id=2))
    await r.engine.receive(event('好', id=3))
    assert [m['message_id'] for m in list(r.engine.queues[SCOPE]._queue)]==['1','2']


def test_filter_state_is_bounded_and_expires():
    tick=[0.0];gate=IncomingFilter(clock=lambda:tick[0],max_entries=2)
    for key in ('one','two','three'): gate.remember(key)
    assert list(gate.accepted)==['two','three']
    tick[0]=60
    gate.check({'scope':'10000001:p:30000001','event':{}},'10000001',())
    assert not gate.accepted
    assert not IncomingFilter().accepted


@pytest.mark.parametrize('speak', [False,True])
@pytest.mark.parametrize('previous_confirmation', [False,True])
async def test_quoted_mentions_follow_the_existing_model_decision_without_extra_calls(runtime, speak, previous_confirmation):
    r=runtime
    source(r,'没事' if previous_confirmation else '机器人，你在吗？',1,ts=time.time()-30)
    r.db.add_message(SCOPE+':900',SCOPE,'default',r.bot.self_id,'assistant',
        '那两点出发可以吗？' if speak else '好，那晚安。',{'message_id':'900'},ts=time.time()-20)
    m=source(r,'好' if speak else '没事',2,replies=['900']);m['mentions']=[r.bot.self_id];m['replies']=['900']
    r.db.patch_message_data(SCOPE,m['id'],{'mentions':m['mentions']})
    if speak:
        r.db.put('followup',SCOPE+':default',{'target':USER,'source_id':SCOPE+':1','expires':time.time()+120})
    answer='那就两点见。' if speak else ''
    r.model.script=[{'content':json.dumps({'speak':speak,'text':answer,'reply_to_message_id':m['id'],
        'expects_followup':False,'reason':'回应新问题' if speak else '重复收尾'})}]
    await r.engine.turn(SCOPE,[m])
    assert bool(r.bot.sent)==speak
    assert len(r.model.calls)==1
    first_input=r.model.calls[0][2]
    assert '最近60秒' in first_input[0]['content'] and '更换引用id或再次@不算新话题' in first_input[0]['content']
    assert ('那两点出发可以吗？' if speak else '好，那晚安。') in json.dumps(first_input,ensure_ascii=False)


def configure(r, probability=1, draw=.5):
    r.group.update(listening_probability=probability)
    r.db.put('group',SCOPE,r.group)
    r.engine.participation_random=Mock(return_value=draw)
    r.model.script=[{'content':json.dumps({'speak':False,'needs_context':False,'action_needed':False,'text':''})}]


@pytest.mark.parametrize('probability,draw,selected',[(0,0,False),(1,.0099,True),(1,.01,False),(1,.99,False),(100,.9999,True)])
async def test_sampling_boundaries_without_changing_the_selected_flow(runtime,probability,draw,selected):
    r=runtime;configure(r,probability,draw)
    m=source(r,'大家晚上吃什么',1)
    await r.engine.turn(SCOPE,[m])
    assert len(r.model.calls)==int(selected)
    if selected: assert r.model.calls[0][3]['stage']=='participation.gate'
    assert not r.bot.sent
    r.engine.participation_random.assert_called_once_with() if 0<probability<100 else r.engine.participation_random.assert_not_called()
    trace=r.db.all('trace')[0]
    sample=next(s['data'] for s in trace['stages'] if s['name']=='participation.sample')
    assert sample['selected'] is selected and sample['probability_percent']==probability
    assert trace['status']=='silent'


async def test_missed_batch_stops_before_images_tools_and_immediate_memory(runtime):
    r=runtime;configure(r)
    m=source(r,'请记住这一张图片',1);m['images']=['https://example.org/image.jpg']
    r.engine.media.image=AsyncMock();r.engine.vision.describe_blocks=AsyncMock()
    r.engine.stickers.enqueue=Mock();r.engine.memory.enqueue=Mock()
    await r.engine.turn(SCOPE,[m])
    r.engine.media.image.assert_not_called();r.engine.vision.describe_blocks.assert_not_called()
    r.engine.stickers.enqueue.assert_not_called();r.engine.memory.enqueue.assert_not_called()
    assert not r.model.calls and not r.bot.calls and not r.bot.sent


async def test_each_ordinary_batch_draws_only_once(runtime):
    r=runtime;configure(r)
    batch=[source(r,'普通聊天 '+str(i),i) for i in range(1,5)]
    await r.engine.turn(SCOPE,batch)
    r.engine.participation_random.assert_called_once_with()
    assert not r.model.calls


@pytest.mark.parametrize('wake',['at','nickname','reply','private'])
async def test_explicit_wakeups_bypass_zero_probability(runtime,wake):
    r=runtime;configure(r,0)
    m=source(r,'机器人你好' if wake=='nickname' else '你好',1)
    scope=SCOPE
    if wake=='at':m['mentions']=[r.bot.self_id]
    if wake=='reply':
        r.db.add_message(SCOPE+':900',SCOPE,'default',r.bot.self_id,'assistant','你好',{'message_id':'900'})
        m['replies']=['900']
    if wake=='private':
        scope=r.bot.self_id+':p:'+USER
        r.db.put('group',scope,{**r.group,'id':scope})
        m.update(scope=scope,id=scope+':1')
        r.db.add_message(m['id'],scope,'default',USER,'user',m['text'],m['data'])
    await r.engine.turn(scope,[m])
    assert len(r.model.calls)==1
    assert r.model.calls[0][3]['stage']=='conversation.agent'
    r.engine.participation_random.assert_not_called()


async def test_one_explicit_wakeup_in_a_batch_is_not_lost(runtime):
    r=runtime;configure(r,probability=0)
    batch=[source(r,'普通闲聊',1),source(r,'机器人，帮我看看',2)]
    await r.engine.turn(SCOPE,batch)
    assert len(r.model.calls)==1 and r.model.calls[0][3]['stage']=='conversation.agent'
    r.engine.participation_random.assert_not_called()


async def test_unaddressed_followup_is_sampled_too(runtime):
    import time
    r=runtime;configure(r,probability=0)
    r.db.put('followup',SCOPE+':default',{'target':USER,'source_id':SCOPE+':0','expires':time.time()+120})
    await r.engine.turn(SCOPE,[source(r,'好',1)])
    assert not r.model.calls


async def test_skipped_turns_retain_history_without_learning_backlog(runtime):
    r=runtime;configure(r,probability=0)
    r.engine.queues[SCOPE]=asyncio.Queue()
    for i in range(12):await r.engine.receive(event('普通聊天内容 '+str(i),id=i))
    batch=[r.engine.queues[SCOPE].get_nowait() for _ in range(12)]
    await r.engine.turn(SCOPE,batch)
    assert not r.model.calls and len(r.db.recent(SCOPE,'default'))==12
    job=r.engine.memory.enqueue(SCOPE,'default')
    assert job is None


async def test_group_request_events_are_not_lottery_filtered(runtime):
    r=runtime;configure(r,probability=0)
    m=source(r,'待审入群申请',1);m['event']['request_type']='group'
    await r.engine.turn(SCOPE,[m])
    assert r.model.calls
    r.engine.participation_random.assert_not_called()


async def test_legacy_saved_groups_default_to_one_percent(runtime):
    r=runtime;configure(r,draw=.02)
    r.group.pop('listening_probability');r.db.put('group',SCOPE,r.group)
    await r.engine.turn(SCOPE,[source(r,'普通消息',1)])
    assert not r.model.calls
    assert group_defaults(SCOPE)['listening_probability']==1


async def test_two_groups_use_independent_probability_settings(runtime):
    r=runtime;configure(r,probability=100)
    r.db.put('group',OTHER,{**r.group,'id':OTHER,'listening_probability':0})
    m=source(r,'普通闲聊',1)
    await r.engine.turn(SCOPE,[m])
    other={**m,'scope':OTHER,'id':OTHER+':1'}
    r.db.add_message(other['id'],OTHER,'default',USER,'user',other['text'],other['data'])
    await r.engine.turn(OTHER,[other])
    assert len(r.model.calls)==1 and r.model.calls[0][0]==SCOPE


@pytest.mark.parametrize('mode',['message','model'])
async def test_scheduled_tasks_bypass_zero_probability(runtime,mode):
    r=runtime;configure(r,probability=0)
    r.group['interaction_enabled']=True;r.db.put('group',SCOPE,r.group)
    r.model.script=[{'role':'assistant','content':'该喝水啦。'}]
    task=await r.engine.scheduler.create(SCOPE,'default',USER,{'text':'提醒喝水','delay_seconds':5,'mode':mode})
    await r.engine.scheduler.execute(task)
    assert r.db.get('reminder',task['id'])['status']=='sent'
    assert len(r.bot.sent)==1 and len(r.model.calls)==int(mode=='model')
    r.engine.participation_random.assert_not_called()


def test_probability_api_roundtrip_compatibility_and_group_isolation(authed_client):
    c=authed_client;db=c.app.state.db
    db.put('group',SCOPE,{'id':SCOPE,'persona':'default','enabled':False})
    db.put('group',OTHER,group_defaults(OTHER))
    assert next(g for g in c.get('/api/v1/groups').json() if g['id']==SCOPE)['listening_probability']==1
    for probability in (0,1,.5,100):
        response=c.put('/api/v1/groups/'+SCOPE,json={'listening_probability':probability})
        assert response.status_code==200 and response.json()['listening_probability']==probability
        assert db.get('group',OTHER)['listening_probability']==1
    assert c.put('/api/v1/groups/'+SCOPE,json={'name':'旧客户端保存'}).json()['listening_probability']==100
    for bad in (-1,101,'NaN','Infinity'):
        assert c.put('/api/v1/groups/'+SCOPE,json={'listening_probability':bad}).status_code==422
    with pytest.raises(ValidationError):Group(listening_probability=float('nan'))
