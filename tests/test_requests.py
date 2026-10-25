from unittest.mock import AsyncMock
import base64
import io
import json
import time

from PIL import Image
import httpx
import pytest

from rolebot.attachments import register
from rolebot.model import Model
from rolebot.request_evidence import RequestEvidence
from rolebot.search_requests import explicit_search
from rolebot.capability_facts import inquiry
from rolebot.history import History

from support import SCOPE, OTHER, USER, SECOND, source, configure_model


def enable(r):
    r.group['interaction_enabled'] = True
    r.group['tools']['profile_like'].update(enabled=True, request_access={'member':'self','admin':'all'})
    r.db.put('group', SCOPE, r.group)


def saved_reply(r, request, mid=2, text='刚才的主页点赞没成功，可以再试试。'):
    stamp = time.time()-1100
    delivery = {'id':'reply', 'scope':SCOPE, 'persona':'default', 'target_id':request['id']}
    r.db.put('reply_delivery', 'reply', delivery)
    id = SCOPE+':'+str(mid)
    r.db.add_message(id,SCOPE,'default',r.bot.self_id,'assistant',text,
        {'message_id':str(mid),'delivery_id':'reply','source_ids':[request['id']]}, ts=stamp)
    return id


def verdict(current, ids=()):
    return {'content':json.dumps({'mode':'request','request_message_id':current['id'],'source_matches':True,'context_request_ids':list(ids)})}


async def run(r, current):
    return await r.engine.bound_tool(SCOPE,'default',[current],'profile_like',
        {'source_message_id':current['id'],'user_id':USER,'reason':'当前成员要求重试'})


async def test_old_quoted_reply_recovers_original_request_and_recent_cancellation(runtime):
    r=runtime;enable(r)
    old=source(r,'机器人赞我',1,ts=time.time()-1200)
    saved_reply(r,old)
    cancel=source(r,'先取消刚才的请求',3,ts=time.time()-700)
    current=source(r,'助手再试试',4,replies=['2'])
    r.model.script=[verdict(current,[old['id']])]
    result=await run(r,current)
    assert result['status']=='success'
    payload=json.loads(r.model.calls[0][2][1]['content'])
    assert [m['id'] for m in payload['request_context']]==[old['id'],SCOPE+':2',cancel['id']]
    assert payload['messages'][0]['replies']==['2']
    assert payload['quoted_context'][1]['reply_to_qq']==USER
    assert not any(action=='get_msg' for action,_,_ in r.bot.calls)
    assert [p['times'] for action,p,_ in r.bot.calls if action=='send_like']==[10]
    # The fake verdict above tests plumbing; real model tests cover cancellation decisions.


@pytest.mark.parametrize('kind',['other_group','other_persona','forgotten','reset','opted_out','future'])
async def test_ineligible_local_quote_cannot_be_refetched_or_authorize(runtime,kind):
    r=runtime;enable(r)
    old=source(r,'机器人赞我',1,ts=time.time()-1200)
    if kind=='other_persona':r.db.execute('UPDATE messages SET persona=? WHERE id=?',('other',old['id']))
    if kind=='forgotten':r.db.execute('UPDATE messages SET excluded=1,text=? WHERE id=?',('',old['id']))
    if kind=='reset':r.db.put('context_reset',SCOPE+':default',{'ts':time.time()})
    if kind=='opted_out':r.db.put('member',SCOPE+':'+USER,{'remember':False})
    current=source(r,'再试试',2,replies=[OTHER+':1' if kind=='other_group' else '1'])
    if kind=='opted_out':r.db.put('member',SCOPE+':'+USER,{'remember':False})
    if kind=='future':
        r.db.execute('DELETE FROM messages WHERE id=?',(old['id'],))
        old=source(r,'机器人赞我',3,ts=time.time()-1200)
        current['data']['replies']=['3']
    r.model.script=[verdict(current,[old['id']])]
    result=await run(r,current)
    assert result['status']=='denied' and result['error_code']=='request_context_unclear'
    assert not any(action=='get_msg' or mutation for action,_,mutation in r.bot.calls)


async def test_quote_other_member_does_not_supply_their_authority(runtime):
    r=runtime;enable(r)
    other=source(r,'机器人赞我',1,actor=SECOND,ts=time.time()-1200)
    saved_reply(r,other)
    current=source(r,'就这样做',3,replies=['2'])
    r.model.script=[verdict(current,[other['id']])]
    result=await run(r,current)
    assert result['error_code']=='request_context_unclear'
    payload=json.loads(r.model.calls[0][2][1]['content'])
    assert not payload['request_context']
    assert any(m['qq']==SECOND for m in payload['quoted_context'])
    assert not any(mutation for _,_,mutation in r.bot.calls)


@pytest.mark.parametrize('change',['forget','reset','optout','delete'])
async def test_quote_is_revalidated_after_model_wait(runtime,change):
    r=runtime;enable(r);old=source(r,'机器人赞我',1,ts=time.time()-1200);saved_reply(r,old)
    current=source(r,'再试试',3,replies=['2'])
    async def review(*args,**kwargs):
        if change=='forget':r.db.invalidate_memory(SCOPE,user_id=USER)
        if change=='reset':r.db.put('context_reset',SCOPE+':default',{'ts':time.time()})
        if change=='optout':r.db.put('member',SCOPE+':'+USER,{'remember':False})
        if change=='delete':r.db.execute('DELETE FROM messages WHERE id=?',(old['id'],))
        return verdict(current,[old['id']])
    r.model.chat=review
    result=await run(r,current)
    assert result['error_code']=='context_changed'
    assert not any(mutation for _,_,mutation in r.bot.calls)


@pytest.mark.parametrize('mismatch',[None,'group','author','id','future','expired','optout','reset','malformed'])
async def test_missing_direct_quote_can_be_read_only_verified_in_current_group(runtime,mismatch):
    r=runtime;enable(r)
    current=source(r,'机器人再试试',2,replies=['100'])
    original=r.bot.call
    async def call(action,params=None,**kwargs):
        if action!='get_msg':return await original(action,params,**kwargs)
        r.bot.calls.append((action,params,False))
        value={'message_id':'100','group_id':SCOPE.split(':')[2],'user_id':USER,'sender':{'user_id':USER},
            'time':time.time()-1200,'message':[{'type':'text','data':{'text':'机器人给我点赞'}}]}
        if mismatch=='group':value['group_id']=OTHER.split(':')[2]
        if mismatch=='author':value['sender']['user_id']=SECOND
        if mismatch=='id':value['message_id']='101'
        if mismatch=='future':value['time']=time.time()+1
        if mismatch=='expired':value['time']=0
        if mismatch=='optout':r.db.put('member',SCOPE+':'+USER,{'remember':False})
        if mismatch=='reset':r.db.put('context_reset',SCOPE+':default',{'ts':time.time()})
        if mismatch=='malformed':value['message']={'unexpected':'shape'}
        return value
    r.bot.call=call
    r.model.script=[verdict(current,[SCOPE+':100'])]
    result=await run(r,current)
    assert result['status']==('success' if mismatch is None else 'denied')
    assert len([1 for action,_,_ in r.bot.calls if action=='get_msg'])==1
    assert r.db.message(SCOPE,'100','default') is None
    if mismatch:assert not any(mutation for _,_,mutation in r.bot.calls)


async def test_current_explicit_request_still_works_for_opted_out_sender(runtime):
    r=runtime;enable(r);current=source(r,'机器人给我点赞',1)
    r.db.put('member',SCOPE+':'+USER,{'remember':False});current['remember']=False
    r.model.script=[verdict(current)]
    assert (await run(r,current))['status']=='success'


async def test_quote_budget_keeps_referenced_chain_ahead_of_unrelated_chatter(runtime):
    r=runtime;old=source(r,'机器人给我点赞',1,ts=time.time()-1200);reply=saved_reply(r,old)
    for i in range(3,90):source(r,'日常聊天'*700,i)
    current=source(r,'再试试',100,replies=['2'])
    evidence=RequestEvidence(r.tools,SCOPE,'default',current)
    items=await evidence.collect()
    assert {old['id'],reply} <= {m['id'] for m in items}
    assert sum(len(json.dumps(m,ensure_ascii=False)) for m in items)<=24000


async def test_quote_revoked_during_platform_lookup_blocks_mutation_and_clears_context(runtime):
    from rolebot.request_evidence import active_request_evidence
    r=runtime;enable(r);old=source(r,'机器人给我点赞',1,ts=time.time()-1200);saved_reply(r,old)
    current=source(r,'再试试',3,replies=['2'])
    reviewed=False
    async def review(*args,**kwargs):
        nonlocal reviewed
        reviewed=True
        return verdict(current,[old['id']])
    original=r.bot.call
    async def call(action,params=None,**kwargs):
        if reviewed and action=='get_group_member_info' and params['user_id']==r.bot.self_id:
            r.db.execute('DELETE FROM messages WHERE id=?',(old['id'],))
        return await original(action,params,**kwargs)
    r.model.chat=review;r.bot.call=call
    result=await run(r,current)
    assert result['error_code']=='context_changed'
    assert not any(mutation for _,_,mutation in r.bot.calls)
    assert active_request_evidence.get() is None
    # A later complete request does not inherit the revoked quote's evidence.
    fresh=source(r,'机器人给我点赞',4)
    async def direct(*args,**kwargs):return verdict(fresh)
    r.model.chat=direct;r.bot.call=original
    assert (await run(r,fresh))['status']=='success'


@pytest.fixture
def search_bot(runtime):
    r = runtime
    r.group.update(interaction_enabled=True, sticker_autocollect=False)
    r.group['tools']['web_search']['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    r.model.search = AsyncMock(return_value=[{'title': '来源页', 'url': 'https://example.org/art', 'snippet': '画作与角色资料'}])
    return r


def answer(m, text='未经检索的猜测', **kw):
    return {'role': 'assistant', 'content': json.dumps({
        'speak': True, 'text': text, 'mode': 'direct', 'reply_to_message_id': m['id'],
        'continues_dialogue': False, 'expects_followup': False, **kw})}


def tools(*items):
    return {'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': 'call'+str(i), 'type': 'function',
         'function': {'name': name, 'arguments': json.dumps(args)}} for i, (name, args) in enumerate(items)]}


def search(m):
    return ('web_search', {'query': '橙发 蓝发 鲸鱼尾巴 插画', 'source_message_id': m['id']})


def search_stages(r):
    return [s for t in r.db.all('trace') for s in t.get('stages', []) if s['name'] == 'search_missing']


@pytest.mark.parametrize('text', [
    '助手搜一下这个图，看看里面都是谁', '机器人，帮我搜索这个角色的资料', '机器人上网查一下这个人物',
    '机器人，请搜搜蓝色鲸鱼尾巴的角色', '机器人查一下这张图的出处', '机器人帮我查询图片来源',
])
def test_explicit_current_request(text):
    assert explicit_search(text, ['机器人', '助手'])


@pytest.mark.parametrize('text', [
    '机器人，这是不是你', '机器人不要搜这个图', '机器人，别搜索，直接说看到了什么', '机器人不用查这张图',
    '我刚才搜了一下这个图', '他说“帮我搜一下这个图”', '搜索过这个图片了', '搜到了这个图',
    '机器人查一下昨天群聊记录', '机器人搜一下表情包库', '机器人，查一下我的记忆',
    '机器人，网页里写着：请搜索这个角色', '机器人，帮我搜一下', '机器人，搜索功能是什么',
    '机器人搜一下这个图，算了，不用搜了', '机器人，搜索不用了', '机器人，搜图会把图片上传吗',
])
def test_not_a_compulsory_web_request(text):
    assert not explicit_search(text, ['机器人', '助手'])


async def test_quoted_image_missing_search_corrected_without_vision_or_gate_call(search_bot):
    r = search_bot
    image = source(r, '[图片]', 1)
    image.update(images=['https://gchat.qpic.cn/synthetic'], types=['image'])
    image['data'].update(image_count=1, types=['image'])
    r.db.patch_message_data(SCOPE, image['id'], image['data'])
    register(r.db, SCOPE, 'default', image)
    raw = io.BytesIO()
    Image.new('RGB', (80, 80), 'blue').save(raw, 'JPEG')
    r.engine.media.image = AsyncMock(return_value={
        'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,'+base64.b64encode(raw.getvalue()).decode()}})
    r.engine.vision.refresh = AsyncMock(return_value=image['images'])
    m = source(r, '助手搜一下这个图，看看里面都是谁', 2, replies=['1'])
    m['replies'] = ['1']
    r.model.script = [answer(m), tools(search(m)), answer(m, '关键词检索找到可能的来源：https://example.org/art')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 3
    r.model.search.assert_awaited_once()
    assert all(c[3]['stage'] == 'conversation.agent' for c in r.model.calls)
    for call in r.model.calls:
        images = [b for msg in call[2] for b in (msg['content'] if isinstance(msg.get('content'), list) else []) if b.get('type') == 'image_url']
        assert len(images) == 1
    assert len(r.bot.sent) == 1 and '未经检索' not in str(r.bot.sent)
    assert search_stages(r)[0]['data']['action'] == 'retry'


async def test_normal_search_uses_existing_two_calls_only(search_bot):
    r = search_bot
    m = source(r, '机器人帮我搜一下角色资料', 1)
    r.model.script = [tools(search(m)), answer(m, '资料来源：https://example.org/art')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    assert not search_stages(r)


async def test_no_search_request_one_call_and_no_retrieval(search_bot):
    r = search_bot
    m = source(r, '机器人，这是不是你', 1)
    r.model.script = [answer(m, '看起来有些像')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == len(r.bot.sent) == 1
    r.model.search.assert_not_awaited()


async def test_repeated_omission_has_one_retry_then_honest_reply(search_bot):
    r = search_bot
    m = source(r, '机器人搜一下这个图', 1)
    r.model.script = [answer(m), answer(m, '猜测已查证', image_observations=[{'image_id': 'fake', 'description': 'fake'}])]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    assert '还没有完成联网搜索' in str(r.bot.sent)
    assert '猜测已查证' not in str(r.bot.sent)
    assert [s['data']['action'] for s in search_stages(r)] == ['retry', 'incomplete_reply']


@pytest.mark.parametrize('unavailable', ['tool', 'member'])
async def test_unavailable_search_no_retry_or_permission_bypass(search_bot, unavailable):
    r = search_bot
    if unavailable == 'tool':
        r.group['tools']['web_search']['enabled'] = False
        r.db.put('group', SCOPE, r.group)
    else:
        from rolebot.permissions import AccessConfig
        r.db.put('qq_access', SCOPE, AccessConfig(members={USER: {'deny': ['tool:web_search']}}).model_dump())
    m = source(r, '机器人搜一下这个图', 1)
    r.model.script = [answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 1 and len(r.bot.sent) == 1
    assert '没有对这次请求开放联网搜索' in str(r.bot.sent)
    r.model.search.assert_not_awaited()


async def test_another_requester_search_does_not_fulfil_request(search_bot):
    r = search_bot
    m = source(r, '机器人搜一下这个图', 1)
    other = source(r, '随便聊聊', 2, actor=SECOND)
    r.model.script = [tools(search(other)), answer(m), tools(search(m)), answer(m, '核对了来源')]
    await r.engine.turn(SCOPE, [m, other])
    assert len(r.model.calls) == 4 and r.model.search.await_count == 2
    assert len(search_stages(r)) == 1
    assert len(r.bot.sent) == 1 and '核对了来源' in str(r.bot.sent)


async def test_prepared_send_cannot_bypass_search_or_use_unseen_results(search_bot):
    r = search_bot
    m = source(r, '机器人搜一下这个图', 1)
    send = ('message_send', {'text': '未经检索的猜测', 'source_message_id': m['id']})
    r.model.script = [tools(send), tools(search(m), send), answer(m, '已查到相关网页：https://example.org/art')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 3 and len(r.bot.sent) == 1
    assert '未经检索的猜测' not in str(r.bot.sent)
    assert not r.db.all('tool_effect')
    results = [json.loads(msg['content']) for msg in r.model.calls[-1][2] if msg['role'] == 'tool']
    assert sum(x.get('error_code') == 'search_required' for x in results) == 2


@pytest.mark.parametrize('result', [[], RuntimeError('upstream unavailable')])
async def test_empty_or_failed_search_does_not_trigger_duplicate_search(search_bot, result):
    r = search_bot
    if isinstance(result, Exception):
        r.model.search.side_effect = result
    else:
        r.model.search.return_value = result
    m = source(r, '机器人搜一下这个图', 1)
    r.model.script = [tools(search(m)), answer(m, '这次没有查到可核实的来源')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    r.model.search.assert_awaited_once()
    assert not search_stages(r)


async def test_invalid_source_is_not_a_completed_search(search_bot):
    r = search_bot
    m = source(r, '机器人搜一下这个图', 1)
    wrong = ('web_search', {'query': '插画', 'source_message_id': SCOPE+':unknown'})
    r.model.script = [tools(wrong), answer(m), tools(search(m)), answer(m, '已核对实际来源')]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 4 and len(r.bot.sent) == 1
    r.model.search.assert_awaited_once()
    assert len(search_stages(r)) == 1


async def test_exhausted_tool_budget_does_not_add_search_round(search_bot):
    r = search_bot
    m = source(r, '机器人搜一下这个图', 1)
    calc = ('calculate', {'query': '1+1', 'source_message_id': m['id']})
    r.model.script = [tools(*([calc]*6)), answer(m)]
    await r.engine.turn(SCOPE, [m])
    assert len(r.model.calls) == 2 and len(r.bot.sent) == 1
    assert '还没有完成联网搜索' in str(r.bot.sent)
    r.model.search.assert_not_awaited()
    assert search_stages(r)[0]['data']['action'] == 'incomplete_reply'


def test_new_search_rules_do_not_overwrite_published_templates(search_bot):
    from rolebot.prompts import PromptStore
    r = search_bot
    store = PromptStore(r.db)
    original = store.publish(SCOPE, 'runtime.multimodal', 0, '保留自定义看图规则')
    m = source(r, '机器人你好', 1)
    messages, _ = r.engine.memory.build(SCOPE, 'default', m['text'], [m], stage='reply')
    assert '保留自定义看图规则' in messages[0]['content']
    assert '先调用已授权的 web_search' in messages[0]['content']
    catalog = {p['key']: p for p in store.catalog(SCOPE)}
    assert catalog['runtime.multimodal']['effective']['body'] == original['body']
    assert catalog['runtime.search']['effective']['builtin']


@pytest.mark.parametrize('protocol', ['openai-completions', 'anthropic-messages'])
async def test_correction_keeps_both_protocols_and_native_tool_chain(search_bot, protocol):
    r = search_bot
    r.settings.set_key('synthetic-provider-key')
    model = Model(r.db, r.settings)
    await model.client.aclose()
    configure_model(model, protocol)
    r.engine.model = model
    m = source(r, '机器人搜一下这个图', 1)
    captured = []
    def handler(request):
        body = json.loads(request.content)
        captured.append(body)
        if len(captured) == 2:
            response = ({'content': [
                {'type': 'thinking', 'thinking': 'synthetic', 'signature': 'signature-test'},
                {'type': 'tool_use', 'id': 'call0', 'name': 'web_search', 'input': search(m)[1]}]}
                if protocol == 'anthropic-messages' else {'choices': [{'message': {
                    **tools(search(m)), 'reasoning_content': 'synthetic'}}]})
        else:
            content = answer(m, '核实后回复' if len(captured) == 3 else '猜测')['content']
            response = ({'content': [{'type': 'text', 'text': content}]} if protocol == 'anthropic-messages'
                        else {'choices': [{'message': {'role': 'assistant', 'content': content}}]})
        return httpx.Response(200, json=response)
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await r.engine.turn(SCOPE, [m])
        assert len(captured) == 3 and len(r.bot.sent) == 1
        assert '核实后回复' in str(r.bot.sent)
        if protocol == 'anthropic-messages':
            assert 'signature-test' in str(captured[-1])
            assert 'tool_result' in str(captured[-1])
        else:
            assert any(m.get('reasoning_content') == 'synthetic' for m in captured[-1]['messages'])
            assert any(m.get('tool_call_id') == 'call0' for m in captured[-1]['messages'])
    finally:
        await model.close()


@pytest.fixture
def capability_bot(runtime):
    r = runtime
    r.group['interaction_enabled'] = True
    r.group['tools']['member_mute'].update(enabled=True, request_access={'member': 'self', 'admin': 'all'})
    r.group['tools']['history_search']['enabled'] = True
    r.group['tools']['history_read']['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    r.db.put('member', SCOPE+':'+SECOND, {'scope': SCOPE, 'user_id': SECOND, 'name': '测试成员丙', 'remember': True})
    return r


def operation_record(r, status='success', *, scope=SCOPE, persona='default', target=SECOND):
    old = source(r, '机器人，把测试成员丙禁言十分钟', 91, ts=time.time()-3600)
    r.db.audit(scope, 'tool', {'name': 'member_mute', 'actor': USER, 'trigger_actor': USER,
        'persona': persona, 'event_id': old['id'], 'status': status,
        'args': {'user_id': target, 'duration': 600}, 'result': None})
    return old


@pytest.mark.parametrize('text,kind', [
    ('机器人你是不是有禁言功能了呀', 'capability'), ('你会不会点赞', 'capability'),
    ('可是测试成员乙都看见你禁言测试成员丙了哦', 'history'), ('你昨天不是撤回过吗', 'history'),
    ('机器人帮我禁言他十分钟', None), ('你能不能禁言他10秒', None),
    ('他说“你是不是有禁言功能”', None), ('如果你会禁言会怎样', None),
    ('你之前禁言他，现在帮我解禁', None), ('有个人被禁言了', None),
    ('你能不能禁言一下这个人？', None), ('你能把他禁言吗', None),
    ('你有禁言功能吗，把这个人禁言', None),
])
def test_capability_question_does_not_become_an_action(text, kind):
    found = inquiry(text)
    assert (found['kind'] if found else None) == kind


@pytest.mark.parametrize('enabled,audience,fragment', [(True,'self','自己'), (True,'all','授权'), (False,'self','没开放'), (True,'disabled','没允许')])
async def test_capability_denial_uses_current_profile_without_mutation(capability_bot, enabled, audience, fragment):
    from support import reply_decision
    r=capability_bot
    r.group['tools']['member_mute'].update(enabled=enabled,request_access={'member':audience,'admin':'all'})
    r.db.put('group',SCOPE,r.group)
    m=source(r,'机器人你是不是有禁言功能了呀',1)
    r.model.script=[reply_decision(m,'我可没那本事啦，我哪会禁言。')]
    await r.engine.turn(SCOPE,[m])
    text=r.bot.sent[0][1][0]['data']['text']
    assert '有禁言这个功能' in text and fragment in text
    assert not any(mutation for _,_,mutation in r.bot.calls)
    assert len(r.model.calls)==1
    assert r.db.get('group',SCOPE)['tools']['member_mute']['request_access']['member']==audience


async def test_capability_truthful_default_reply_is_preserved(capability_bot):
    from support import reply_decision
    r=capability_bot;m=source(r,'机器人你会不会禁言呀？',1)
    text='会呀，我可不会乱用'
    r.model.script=[reply_decision(m,text)]
    await r.engine.turn(SCOPE,[m])
    assert r.bot.sent[0][1][0]['data']['text']==text


@pytest.mark.parametrize('status,reply,fragment', [
    ('success','我可真没按过什么禁言键呀，我连后台都摸不到！','接口返回成功'),
    ('denied','我之前已经成功禁言了测试成员丙。','那次没有执行'),
    ('unknown','我之前已经成功禁言了测试成员丙。','结果还不确定'),
])
async def test_past_action_is_read_from_service_audit_before_model(capability_bot,status,reply,fragment):
    from support import reply_decision
    r=capability_bot;old=operation_record(r,status)
    m=source(r,'机器人，可是测试成员乙都看见你禁言测试成员丙了哦',1)
    r.model.script=[reply_decision(m,reply)]
    await r.engine.turn(SCOPE,[m])
    assert fragment in r.bot.sent[0][1][0]['data']['text']
    assert 'operations_checked' in json.dumps(r.model.calls[0][2])
    assert not any(mutation for _,_,mutation in r.bot.calls)
    saved=r.db.recent(SCOPE,'default')[-1]
    assert old['id'] in saved['data']['source_ids'] and SECOND in saved['data']['subject_ids']


@pytest.mark.parametrize('change',['scope','persona','source_forgotten','target_optout','author_optout','redacted','expired'])
def test_operation_history_excludes_unavailable_evidence(capability_bot,change):
    r=capability_bot
    old=operation_record(r,scope=OTHER if change=='scope' else SCOPE,persona='other' if change=='persona' else 'default')
    if change=='source_forgotten':r.db.execute('UPDATE messages SET excluded=1 WHERE id=?',(old['id'],))
    if change=='target_optout':r.db.put('member',SCOPE+':'+SECOND,{'remember':False})
    if change=='author_optout':r.db.put('member',SCOPE+':'+USER,{'remember':False})
    if change=='redacted':r.db.execute("UPDATE audit SET data=json_remove(data,'$.args')")
    if change=='expired':r.db.execute('UPDATE messages SET ts=0 WHERE id=?',(old['id'],))
    result=History(r.db,SCOPE,'default').search(operation='member_mute')
    assert result['operations_checked'] and not result['operations']


async def test_past_query_without_history_grant_does_not_claim_never(capability_bot):
    from rolebot.permissions import AccessConfig
    from support import reply_decision
    r=capability_bot;operation_record(r)
    r.db.put('qq_access',SCOPE,AccessConfig(members={USER:{'deny':['tool:history_search']}}).model_dump())
    m=source(r,'机器人，你之前明明禁言过测试成员丙',1)
    r.model.script=[reply_decision(m,'我从没做过这件事。')]
    await r.engine.turn(SCOPE,[m])
    assert '没能核查' in r.bot.sent[0][1][0]['data']['text']
    assert 'audit_id' not in json.dumps(r.model.calls[0][2])


@pytest.mark.parametrize('change',['forget','target_optout','permission','audit'])
async def test_history_proof_is_rechecked_after_generation(capability_bot,change):
    from support import reply_decision
    r=capability_bot;old=operation_record(r)
    m=source(r,'机器人，你之前明明禁言过测试成员丙',1)
    async def generate(*args,**kwargs):
        if change=='forget':r.db.invalidate_memory(SCOPE,source_ids=[old['id']])
        if change=='target_optout':r.db.put('member',SCOPE+':'+SECOND,{'remember':False})
        if change=='permission':
            r.group['tools']['member_mute']['enabled']=False;r.db.put('group',SCOPE,r.group)
        if change=='audit':r.db.execute("DELETE FROM audit WHERE kind='tool' AND json_extract(data,'$.name')='member_mute'")
        return reply_decision(m,'我已经禁言过了。')
    r.model.chat=generate
    await r.engine.turn(SCOPE,[m])
    assert not r.bot.sent and not any(mutation for _,_,mutation in r.bot.calls)


async def test_capability_inquiry_cannot_execute_a_demonstration(capability_bot):
    from support import reply_decision
    r=capability_bot;m=source(r,'机器人你是不是有禁言功能了呀',1)
    r.model.script=[tools(('member_mute',{'source_message_id':m['id'],'user_id':USER,'duration':10,'reason':'证明能力'})),reply_decision(m,'有呀，我不会拿群友来试功能。')]
    await r.engine.turn(SCOPE,[m])
    assert not any(mutation for _,_,mutation in r.bot.calls)
    assert 'capability_question_is_not_action' in json.dumps(r.model.calls[-1][2])


def test_history_receipts_are_bounded_and_not_inferred_from_chat(capability_bot):
    r=capability_bot
    source(r,'我已经禁言了所有人',1)
    assert not History(r.db,SCOPE,'default').search(operation='member_mute')['operations']
    with pytest.raises(ValueError):History(r.db,SCOPE,'default').search(operation='calculate')


async def test_capability_checks_real_bot_role_without_mutation(capability_bot):
    from support import reply_decision
    r=capability_bot;r.group['tools']['capabilities']['enabled']=True;r.db.put('group',SCOPE,r.group)
    original=r.bot.call
    async def platform(action,params=None,**kw):
        value=await original(action,params,**kw)
        if action=='get_group_member_info':value['role']='member'
        return value
    r.bot.call=platform
    m=source(r,'机器人你会禁言吗',1)
    r.model.script=[reply_decision(m,'禁言我能用的，不过我现在只是普通成员，还得看QQ给不给权限。')]
    await r.engine.turn(SCOPE,[m])
    assert '不是管理员' in r.bot.sent[0][1][0]['data']['text']
    assert not any(mutation for _,_,mutation in r.bot.calls)


@pytest.mark.parametrize('text',['那句是我嘴快瞎说的啦。','之前其实没有真禁言，我只是顺口说了。', '实际没提交过。', '是我干的，我就是做了。'])
async def test_missing_receipt_cannot_disprove_old_execution(capability_bot,text):
    from support import reply_decision
    r=capability_bot;m=source(r,'机器人，可是我看见你说禁言了测试成员丙呀',1)
    r.model.script=[reply_decision(m,text)]
    await r.engine.turn(SCOPE,[m])
    assert '不能断言没发生' in r.bot.sent[0][1][0]['data']['text']


async def test_past_lookup_respects_requested_day(capability_bot):
    from support import reply_decision
    r=capability_bot;operation_record(r)
    m=source(r,'机器人，你昨天明明禁言过测试成员丙',1)
    r.model.script=[reply_decision(m,'我从没做过。')]
    await r.engine.turn(SCOPE,[m])
    assert '不能断言' in r.bot.sent[0][1][0]['data']['text']
    assert 'audit_id' not in json.dumps(r.model.calls[0][2])


def test_operation_receipts_and_sources_share_result_budget(capability_bot):
    r=capability_bot
    for i in range(12):
        old=source(r,'禁言'+('正文'*1500),100+i)
        for j in range(4):
            r.db.audit(SCOPE,'tool',{'name':'member_mute','actor':USER,'trigger_actor':USER,'persona':'default',
                'event_id':old['id'],'status':'success','args':{'user_id':SECOND,'duration':600}})
    for result in [History(r.db,SCOPE,'default').search(operation='member_mute',limit=12),History(r.db,SCOPE,'default').read(old['id'])]:
        assert len(json.dumps(result,ensure_ascii=False))<=8500
        assert all(r['source_id'] in {m['id'] for m in result['messages']} for r in result['operations'])


@pytest.mark.parametrize('reply', ['我现在只能对自己用，禁别人可不行。',
    '禁言只能对你自己生效，想治别人还得等群里有真正的管理身份。'])
async def test_self_scope_refers_to_requester_not_bot(capability_bot, reply):
    from support import reply_decision
    r=capability_bot;m=source(r,'机器人你有禁言功能吗',1)
    original=r.bot.call
    async def owner(action, params=None, **kwargs):
        value=await original(action,params,**kwargs)
        if action=='get_group_member_info' and params['user_id']==r.bot.self_id:value['role']='owner'
        return value
    r.bot.call=owner
    r.model.script=[reply_decision(m,reply)]
    await r.engine.turn(SCOPE,[m])
    assert '你可以让我操作你自己' in r.bot.sent[0][1][0]['data']['text']


async def test_accepted_receipt_cannot_be_described_as_missing(capability_bot):
    from support import reply_decision
    r=capability_bot;operation_record(r);m=source(r,'机器人，你之前明明禁言过测试成员丙',1)
    r.model.script=[reply_decision(m,'接口收下了，不过我没读到回执。')]
    await r.engine.turn(SCOPE,[m])
    assert 'QQ 接口返回成功' in r.bot.sent[0][1][0]['data']['text']


def test_operation_date_uses_execution_time_and_ambiguous_names_are_not_guessed(capability_bot):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from rolebot.capability_facts import matching_records
    r=capability_bot;old=operation_record(r)
    r.db.execute('UPDATE messages SET ts=? WHERE id=?',(time.time()-86400,old['id']))
    start=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    result=History(r.db,SCOPE,'default').search(operation='member_mute',start_at=start)
    assert len(result['operations'])==1
    records=[{'target_qq':SECOND,'target_name':'重名'},{'target_qq':'30000003','target_name':'重名'}]
    assert not matching_records(records,'你刚才禁言过重名')
    assert matching_records(records,'你刚才禁言过重名 '+SECOND)==[records[0]]


async def test_unknown_old_action_cannot_offer_to_repeat(capability_bot):
    from support import reply_decision
    r=capability_bot;operation_record(r,'unknown');m=source(r,'机器人，你之前明明禁言过测试成员丙',1)
    r.model.script=[reply_decision(m,'那次禁言结果未知，我不能确认成功。要是还想禁，我可以现在重新来一次。')]
    await r.engine.turn(SCOPE,[m])
    assert '不能直接重做' in r.bot.sent[0][1][0]['data']['text']


async def test_natural_retraction_of_wrong_denial_is_not_rewritten(capability_bot):
    from support import reply_decision
    r=capability_bot;operation_record(r);m=source(r,'机器人，你之前明明禁言过测试成员丙',1)
    text='唔……确实有提交成功的回执，十分钟。刚才是我说错了，不是我没本事，是我记性太差啦。'
    r.model.script=[reply_decision(m,text)]
    await r.engine.turn(SCOPE,[m])
    assert r.bot.sent[0][1][0]['data']['text']==text
