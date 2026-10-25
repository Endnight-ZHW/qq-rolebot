from support import tool_reply
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import time

import httpx
import pytest

from rolebot.context import ContextTooLarge, encode
from rolebot.dashboard import usage_summary
from rolebot.evaluation import replay
from rolebot.history import History, timestamp
from rolebot.providers import ModelConfigWrite
from rolebot.tool_loop import tool_result

from support import SECOND, OTHER, SCOPE, USER, fact, reply_decision, source


def register_context_sticker(r, **metadata):
    import io
    from PIL import Image
    raw = io.BytesIO()
    Image.new('RGB', (8, 8), 'green').save(raw, 'PNG')
    return r.engine.stickers.register(OTHER, raw.getvalue(), {
        'name': '开心猫猫', 'tags': '开心 摸头', 'usage': '被夸或摸头时回应', **metadata},
        [OTHER+':private-source'], [SECOND])


def test_sticker_menu_is_scoped_bounded_and_excludes_unavailable_items(runtime):
    from rolebot.messaging import sticker_path
    r = runtime
    r.group['interaction_enabled'] = True
    r.db.put('group', SCOPE, r.group)
    target = source(r, '机器人，摸摸头', 1)
    item = register_context_sticker(r)
    r.db.add_message('other-send', OTHER, 'default', r.bot.self_id, 'assistant', '',
                     {'sticker_id': item['id']})
    candidates = r.engine.stickers.candidates(SCOPE, 'default', [target], target['text'])
    assert len(candidates) == 1 and candidates[0]['recently_used'] is False
    assert not {'scope', 'origins', 'source_ids', 'actor_ids', 'file', 'sha256'} & candidates[0].keys()
    r.db.add_message('own-send', SCOPE, 'default', r.bot.self_id, 'assistant', '',
                     {'sticker_id': item['id']})
    assert r.engine.stickers.candidates(SCOPE, 'default', [target])[0]['recently_used'] is True
    for i in range(20):
        copy = {**item, 'id': 'other'+str(i)}
        sticker_path(r.settings, copy).write_bytes(sticker_path(r.settings, item).read_bytes())
        r.db.put('sticker', copy['id'], copy)
    assert len(r.engine.stickers.candidates(SCOPE, 'default', [target])) == 12
    for row in r.db.all('sticker'):
        row['enabled'] = False
        r.db.put('sticker', row['id'], row)
    assert not r.engine.stickers.candidates(SCOPE, 'default', [target])
    item['enabled'] = True
    r.db.put('sticker', item['id'], item)
    sticker_path(r.settings, item).unlink()
    assert not r.engine.stickers.candidates(SCOPE, 'default', [target])


@pytest.mark.parametrize('restriction', ['mixed_grants', 'read_disabled', 'send_disabled', 'interaction_disabled'])
def test_sticker_menu_respects_every_actor_and_tool_policy(runtime, restriction):
    from rolebot.permissions import AccessConfig
    r = runtime
    r.group['interaction_enabled'] = True
    target = source(r, '机器人，摸摸头', 1)
    second = source(r, '机器人，另一人', 2, actor=SECOND)
    register_context_sticker(r)
    if restriction == 'mixed_grants':
        r.db.put('qq_access', SCOPE, AccessConfig(members={
            SECOND: {'deny': ['tool:sticker_list', 'tool:sticker_search']}}).model_dump())
    elif restriction == 'interaction_disabled':
        r.group['interaction_enabled'] = False
    else:
        for name in (('sticker_list', 'sticker_search') if restriction == 'read_disabled' else ('message_send', 'sticker_send')):
            r.group['tools'][name]['enabled'] = False
    r.db.put('group', SCOPE, r.group)
    assert not r.engine.stickers.candidates(SCOPE, 'default', [target, second])


def test_sticker_menu_only_in_reply_context_and_trims_before_current_message(runtime):
    r = runtime
    target = source(r, '机器人，今天真开心', 1)
    candidates = [{'id': str(i), 'name': '开心', 'usage': '候选说明'*50} for i in range(12)]
    runtime_data = {'_sticker_candidates': candidates}
    _, preview = r.engine.memory.build(SCOPE, 'default', target['text'], [target], runtime=runtime_data)
    assert preview['context']['sticker_candidates'] == candidates
    assert '_sticker_candidates' not in preview['context']['runtime']
    for stage in ('gate', 'task'):
        _, preview_other = r.engine.memory.build(SCOPE, 'default', target['text'], [target], stage=stage, runtime=runtime_data)
        assert 'sticker_candidates' not in preview_other['context']
    cfg = r.db.config()
    cfg['working_context_budget'] = preview['estimated_tokens']-len(encode(candidates))//2
    r.db.put('settings', 'global', cfg)
    _, trimmed = r.engine.memory.build(SCOPE, 'default', target['text'], [target], runtime=runtime_data)
    assert len(trimmed['context']['sticker_candidates']) < len(candidates)
    assert target['id'] in trimmed['context']['current_ids']


def test_art_catalog_is_not_loaded_into_ordinary_chat(runtime):
    r=runtime;target=source(r,'机器人，做张休息卡片',101)
    tools={'tools':{'tools':[{'name':'creation_create','enabled':True}]}}
    _,reply=r.engine.memory.build(SCOPE,'default',target['text'],[target],runtime=tools)
    assert 'creation_asset_catalog' not in reply['context']
    assert 'art:character_front' not in json.dumps(reply,ensure_ascii=False)
    _,gate=r.engine.memory.build(SCOPE,'default',target['text'],[target],runtime=tools,stage='gate')
    assert 'creation_asset_catalog' not in gate['context']
    tools['tools']['tools'][0]['enabled']=False
    _,disabled=r.engine.memory.build(SCOPE,'default',target['text'],[target],runtime=tools)
    assert 'creation_asset_catalog' not in disabled['context']


def test_larger_settings_and_model_caps_are_validated(authed_client):
    assert authed_client.put('/api/v1/settings', json={'context_tokens': 256000}).status_code == 200
    assert authed_client.put('/api/v1/settings', json={'context_tokens': 1000001}).status_code == 200
    cfg = authed_client.get('/api/v1/models').json(); cfg.pop('key_configured')
    for key, value in {'chat': 65536, 'decision': 8192, 'memory': 16384}.items():
        cfg['routes'][key]['max_tokens'] = value
    assert authed_client.put('/api/v1/models', json=cfg).status_code == 200
    assert authed_client.get('/api/v1/models').json()['routes']['chat']['max_tokens'] == 65536
    cfg['routes']['chat']['max_tokens'] = 256001
    assert authed_client.put('/api/v1/models', json=cfg).status_code == 422
    cfg['routes']['chat']['max_tokens'] = 65536
    cfg['models'][0]['max_tokens'] = 32768
    assert authed_client.put('/api/v1/models', json=cfg).status_code == 422

def test_old_group_get_has_new_disabled_tools_without_database_mutation(authed_client):
    old = {'id': SCOPE, 'tools': {'calculate': {'enabled': False}}}
    authed_client.app.state.db.put('group', SCOPE, old)
    group = authed_client.get('/api/v1/groups').json()[0]
    assert not group['tools']['history_search']['enabled']
    assert not group['tools']['history_read']['enabled']
    assert not group['tools']['calculate']['enabled']
    assert authed_client.app.state.db.get('group', SCOPE) == old

@pytest.mark.parametrize('protocol', ['openai-completions', 'anthropic-messages'])
async def test_expanded_budgets_reach_provider_and_capacity_is_reserved(model, protocol):
    cfg = model.catalog.get(); cfg['providers'][0]['protocol'] = protocol
    for key, value in {'chat': 65536, 'decision': 8192, 'memory': 16384}.items():
        cfg['routes'][key]['max_tokens'] = value
    model.catalog.save(ModelConfigWrite(**cfg))
    settings = model.db.config(); settings['context_tokens'] = 1000000
    model.db.put('settings', 'global', settings)
    assert model.context_budget() == 1000000-65536-3000
    requested = []
    def handler(req):
        body = json.loads(req.content); requested.append(body['max_tokens'])
        return httpx.Response(200, json={'content': [{'type': 'text', 'text': 'OK'}]} if protocol == 'anthropic-messages' else {'choices': [{'message': {'role': 'assistant', 'content': 'OK'}}]})
    model.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for purpose in ('chat', 'decision', 'memory'):
        await model.chat(SCOPE, purpose, [{'role': 'user', 'content': 'OK'}])
    assert requested == [65536, 8192, 16384]


def test_paged_history_has_hard_count_bound_and_ties_are_stable(db):
    ts = time.time()-10
    for i in range(2100):
        db.add_message(f'h{i:04d}', SCOPE, 'default', USER, 'user', 'x', ts=ts)
    rows = [r for p in db.recent_pages(SCOPE, 'default') for r in p]
    assert len(rows) == len({r['id'] for r in rows}) == 2000

def test_search_time_keywords_sender_and_literal_sql(db):
    ts = timestamp('2026-10-18T12:00:00')
    assert ts == timestamp('2026-10-18T04:00:00Z')
    # Whole seconds preserve the exact inclusive boundary through ISO serialization.
    ts = int(time.time())-100
    def iso(t): return datetime.fromtimestamp(t, ZoneInfo('Asia/Shanghai')).isoformat()
    db.add_message('a', SCOPE, 'default', USER, 'user', '草莓蛋糕周五取', ts=ts)
    db.add_message('b', SCOPE, 'default', USER, 'user', '草莓蛋糕周六取', ts=ts+10)
    db.add_message('c', SCOPE, 'default', '30000002', 'user', '草莓蛋糕周五取', ts=ts+20)
    h = History(db, SCOPE, 'default')
    result = h.search(query='草莓 周五', start_at=iso(ts), end_at=iso(ts+20), user_id=USER)
    assert [m['id'] for m in result['messages']] == ['a']
    assert not h.search(query="%' OR 1=1 --")['messages']
    with pytest.raises(ValueError): h.search()
    with pytest.raises(ValueError): h.search(start_at=iso(ts+20), end_at=iso(ts))
    with pytest.raises(ValueError): h.search(start_at='昨天')

def test_search_pagination_long_excerpt_and_read_segments(db):
    ts = time.time()-100
    for i in range(15):
        db.add_message(f'a{i:02d}', SCOPE, 'default', USER, 'user', '关键词'+str(i), ts=ts)
    h = History(db, SCOPE, 'default')
    first = h.search(query='关键词', limit=8)
    second = h.search(query='关键词', limit=8, before_message_id=first['next_before_message_id'])
    assert len({m['id'] for m in first['messages']+second['messages']}) == 15
    assert first['has_more'] and not second['has_more']
    text = '长'*10000+'隐藏关键词'
    db.add_message('long', SCOPE, 'default', USER, 'user', text)
    hit = h.search(query='隐藏关键词')['messages'][0]
    assert '隐藏关键词' in hit['text'] and hit['offset'] > 9000 and hit['excerpt']
    read = h.read('long', before=0, after=0)['messages'][0]
    assert len(read['text']) == 3000 and read['next_offset'] == 3000
    assert h.read('long', before=0, after=0, offset=10000)['messages'][0]['text'] == '隐藏关键词'

def test_history_scope_role_optout_retention_reset_and_forget(db):
    ts = time.time()-100
    for id, scope, persona, user, stamp in [('ok', SCOPE, 'default', USER, ts), ('cross', OTHER, 'default', USER, ts),
                                          ('role', SCOPE, 'another', USER, ts), ('old', SCOPE, 'default', USER, ts-31*86400),
                                          ('stopped', SCOPE, 'default', '30000002', ts)]:
        db.add_message(id, scope, persona, user, 'user', '隐藏资料', ts=stamp)
    db.put('member', SCOPE+':30000002', {'remember': False})
    db.put('context_reset', SCOPE+':default', {'ts': time.time()})
    h = History(db, SCOPE, 'default')
    assert [m['id'] for m in h.search(query='隐藏资料')['messages']] == ['ok']
    assert not list(db.recent_pages(SCOPE, 'default'))  # Reset limits automatic context, not explicit lookup.
    assert [m['id'] for m in h.read('ok', before=4, after=4)['messages']] == ['ok']
    for id in ('cross', 'role', 'old', 'stopped'):
        with pytest.raises(ValueError): h.read(id)
        with pytest.raises(ValueError): h.search(query='隐藏', before_message_id=id)
    db.invalidate_memory(SCOPE, source_ids=['ok'])
    assert not h.search(query='隐藏资料')['messages']
    with pytest.raises(ValueError): h.read('ok')

async def test_history_grants_identity_audit_and_immediate_revoke(runtime):
    r = runtime
    r.db.add_message('old', SCOPE, 'default', USER, 'user', '秘密菠萝')
    args = {'query': '秘密菠萝'}
    assert (await r.tools.run(SCOPE, 'default', '30000002', 'current', 'history_search', args))['status'] == 'denied'
    r.group['tools']['history_search']['enabled'] = True; r.db.put('group', SCOPE, r.group)
    result = await r.tools.run(SCOPE, 'default', '30000002', 'current', 'history_search', args)
    assert result['result']['messages'][0]['qq'] == USER
    audit = json.loads(r.db.rows("SELECT data FROM audit WHERE kind='tool' ORDER BY ts DESC LIMIT 1")[0]['data'])
    assert audit['actor'] == '30000002' and audit['event_id'] == 'current'
    assert '秘密菠萝' not in json.dumps(audit, ensure_ascii=False)
    assert (await r.tools.run(SCOPE, 'default', USER, 'current', 'history_search', {**args, 'scope': OTHER}))['status'] == 'denied'
    r.group['tools']['history_search']['enabled'] = False; r.db.put('group', SCOPE, r.group)
    assert (await r.tools.run(SCOPE, 'default', USER, 'current', 'history_search', args))['status'] == 'denied'

async def test_engine_search_read_chain_provenance_and_forget(runtime):
    r = runtime
    for name in ('history_search', 'history_read'):
        r.group['tools'][name]['enabled'] = True
    r.db.put('group', SCOPE, r.group)
    old = source(r, '周五拿草莓蛋糕', 11, ts=time.time()-3600)
    r.db.put('context_reset', SCOPE+':default', {'ts': time.time()-10})
    current = source(r, '机器人，之前约好什么时候取蛋糕？', 12, actor='30000002')
    def call(id, name, args):
        return tool_reply(name, {**args, 'source_message_id': current['id']}, id=id)
    r.model.script = [call('t1', 'history_search', {'query': '蛋糕'}), call('t2', 'history_read', {'message_id': old['id'], 'before': 0, 'after': 0}),
                      reply_decision(current, text='之前说的是周五拿草莓蛋糕，。')]
    await r.engine.turn(SCOPE, [current])
    assert r.bot.sent
    text = r.model.calls[-1][2][-1]['content']
    assert '周五拿草莓蛋糕' in text
    for _, _, messages, _ in r.model.calls:
        for m in messages:
            if m['role'] == 'tool': json.loads(m['content'])
    saved = r.db.recent(SCOPE, 'default')[-1]
    assert old['id'] in saved['data']['source_ids'] and USER in saved['data']['subject_ids']
    r.db.invalidate_memory(SCOPE, source_ids=[old['id']])
    assert r.db.message(SCOPE, saved['id'], 'default') is None

def test_history_results_are_bounded_valid_json(runtime):
    r = runtime
    for i in range(12):
        r.db.add_message(str(i), SCOPE, 'default', USER, 'user', '关键词'+'长'*11900, {'name': '名'*80})
    h = History(r.db, SCOPE, 'default')
    results = [h.search(query='关键词', limit=12), h.read('5', before=4, after=4)]
    for result in results:
        assert len(json.dumps(result, ensure_ascii=False)) < 9000

def test_quotes_scoped_expired_and_forgotten(runtime):
    r = runtime
    old = source(r, "原始地点蓝桥", 1)
    r.db.add_message("foreign", OTHER, "default", USER, "user", "隔壁秘密", {"message_id": "2"})
    r.db.add_message("role", SCOPE, "another", USER, "user", "另一角色秘密", {"message_id": "3"})
    m = source(r, "机器人，那里在哪？", 4);m["replies"] = ["1", "2", "3"]
    text = str(r.engine.memory.build(SCOPE, "default", m["text"], [m])[1])
    assert "原始地点蓝桥" in text and "隔壁秘密" not in text and "另一角色秘密" not in text
    r.db.invalidate_memory(SCOPE, source_ids=[old["id"]])
    text = str(r.engine.memory.build(SCOPE, "default", m["text"], [m])[1])
    assert "原始地点蓝桥" not in text and "无法取得" in text

def test_context_does_not_silently_cut_current_request(runtime):
    r=runtime;cfg=r.db.config();cfg["context_tokens"]=6000;r.db.put("settings", "global", cfg)
    m=source(r,"机器人"+"长"*6000,1)
    with pytest.raises(ContextTooLarge):r.engine.memory.build(SCOPE,"default",m["text"],[m])

def test_fixed_replay_is_isolated_and_passes_all_cases():
    result=replay()
    assert result["passed"]==result["total"]

def test_gate_and_reply_deduplicate_memory_and_share_persona(runtime):
    r=runtime;m=source(r,"我喜欢草莓",1)
    id=r.engine.memory.knowledge.apply(SCOPE,"default",[fact(m,"朋友喜欢草莓")],[m])[0]
    decision,dp=r.engine.memory.build(SCOPE,"default","草莓",[m],stage="gate")
    reply,rp=r.engine.memory.build(SCOPE,"default","草莓",[m],stage="reply")
    assert "合成测试" in decision[0]["content"] and "合成测试" in reply[0]["content"]
    blocks=dp["context"]
    assert blocks["成员档案"][0]["memory_ids"]==[id] and "facts" not in blocks["成员档案"][0]
    assert [x["id"] for x in blocks["相关记忆"]]==[id]
    assert "interpretation" not in blocks["相关记忆"][0]
    assert dp["stage"]=="gate" and rp["stage"]=="reply"


def test_slight_overrun_keeps_recent_exchange_after_idle(runtime):
    r = runtime
    r.db.put('group', SCOPE, r.group)
    config = r.db.config()
    config.update(working_context_budget=100000, working_initial_messages=40)
    r.db.put('settings', 'global', config)
    for i in range(39):
        source(r, '旧消息'+str(i)+'文'*100, i, ts=time.time()-900+i)
    current = source(r, '机器人，刚才说到哪里了', 39)
    # Deliberately leave the previous reply beyond the old 600-second protection.
    r.db.execute("UPDATE messages SET role='assistant',text=? WHERE id=?",
                 ('上一轮的最新结论', SCOPE+':38'))
    _, full = r.engine.memory.build(SCOPE, 'default', current['text'], [current], stage='reply')
    rows = full['context']['消息']
    # Reproduce the observed second block: 20 messages only slightly over budget.
    old_cost = sum(len(encode(row))+1 for row in rows[:20])
    config['working_context_budget'] = full['estimated_tokens']-old_cost-300
    r.db.put('settings', 'global', config)
    _, result = r.engine.memory.build(SCOPE, 'default', current['text'], [current], stage='reply')
    kept = result['context']['消息']
    assert 10 < len(kept) < 20
    assert '上一轮的最新结论' in str(kept)
    assert kept[-1]['id'] == current['id']
    assert result['estimated_tokens'] <= result['soft_budget']


def test_preferred_recent_messages_yield_to_input_hard_limit(runtime):
    r = runtime
    r.db.put('group', SCOPE, r.group)
    config = r.db.config()
    config['working_context_budget'] = 4096
    r.db.put('settings', 'global', config)
    for i in range(6):
        source(r, '较长的旧记录'+'文'*3000, i, ts=time.time()-900+i)
    current = source(r, '机器人你好', 7)
    r.model.context_budget = lambda: 14000
    _, preview = r.engine.memory.build(SCOPE, 'default', current['text'], [current], stage='reply')
    assert preview['estimated_tokens'] <= 14000
    assert preview['context']['消息'][-1]['id'] == current['id']
    assert 1 <= len(preview['context']['消息']) < 6


async def test_direct_one_call_and_silence(runtime):
    m = source(runtime, '机器人你好', 1)
    runtime.model.script = [reply_decision(m)]
    await runtime.engine.turn(SCOPE, [m])
    assert len(runtime.model.calls) == len(runtime.bot.sent) == 1
    assert runtime.model.calls[0][3]['stage'] == 'conversation.agent'
    m = source(runtime, '机器人，先别回复我', 2)
    runtime.model.script = [reply_decision(m, '', speak=False)]
    await runtime.engine.turn(SCOPE, [m])
    assert len(runtime.bot.sent) == 1


@pytest.mark.parametrize('gate,expected', [({'speak':False,'needs_context':False,'action_needed':False},1),
    ({'speak':False,'needs_context':True,'action_needed':False},2),
    ({'speak':False,'needs_context':False,'action_needed':True},2), ({},2)])
async def test_gate_and_escalation(runtime, gate, expected):
    m = source(runtime, '那刚才说的事情呢', 1)
    runtime.model.script = [{'role':'assistant','content':json.dumps(gate)}, reply_decision(m, '好呀。', mode='proactive')]
    await runtime.engine.turn(SCOPE, [m])
    assert len(runtime.model.calls) == expected
    assert runtime.model.calls[0][3]['stage'] == 'participation.gate'
    assert 'tools' not in runtime.model.calls[0][3]


def test_stable_prefix_and_dynamic_tail(runtime):
    a = source(runtime, '机器人，今天去散步', 1)
    first, _ = runtime.engine.memory.build(SCOPE,'default',a['text'],[a],runtime={'time':'first'})
    b = source(runtime, '机器人，继续说', 2)
    second, _ = runtime.engine.memory.build(SCOPE,'default',b['text'],[b],runtime={'time':'second'})
    assert first[0] == second[0]
    assert second[1]['content'].startswith(first[1]['content']+'\n')
    assert '"current"' not in second[1]['content']
    assert 'second' not in second[0]['content']+second[1]['content']
    assert 'second' in second[2]['content']


def test_gate_limits_and_quotes(runtime):
    for i in range(150): source(runtime, '前面的背景'+str(i)+'长'*100, i)
    m = source(runtime, '现在的问题', 500, replies=['1'])
    messages, preview = runtime.engine.memory.build(SCOPE,'default',m['text'],[m],stage='gate')
    assert preview['history_loaded'] <= 80
    assert preview['context']['消息'][-1]['id'] == m['id']
    assert preview['context']['quotes'][0]['id'] == SCOPE+':1'
    assert preview['estimated_tokens'] <= 32000
    assert len(messages) == 3


def test_tool_result_valid_and_bounded():
    raw={'status':'success','result':{'messages':[{'id':str(i),'qq':USER,'text':'长'*3000} for i in range(30)]}}
    content=tool_result(raw)
    result=json.loads(content)
    assert len(content)<=12000 and result['status']=='success' and result['truncated']
    assert raw['result']['messages'][0]['text']=='长'*3000


async def test_usage_stage_unknown_and_retry_accounting(model):
    results=[httpx.Response(503,json={'usage':{'prompt_tokens':10,'prompt_cache_hit_tokens':2,'completion_tokens':1}}),
             httpx.Response(200,json={'choices':[{'message':{'role':'assistant','content':'ok'}}],'usage':{'prompt_tokens':20,'completion_tokens':3}})]
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:results.pop(0)))
    await model.chat(SCOPE,'chat',[{'role':'user','content':'test'}],stage='conversation.agent')
    stats=usage_summary(model.db,SCOPE)
    row=stats['by_stage'][0]
    assert row['calls']==2 and row['input_tokens']==30 and row['cached_tokens']==2
    assert row['unknown_calls']==1 and row['cache_hit_ratio']==.2
    records=model.records.listing(scope=SCOPE)
    assert records[0]['usage']['cached_tokens'] is None


def test_budget_settings_validation(authed_client):
    c=authed_client
    assert c.get('/api/v1/settings').json()['gate_context_budget']==32000
    assert c.put('/api/v1/settings',json={'gate_max_messages':81}).status_code==200
    assert c.put('/api/v1/settings',json={'gate_max_messages':True}).status_code==422


@pytest.mark.parametrize('protocol',['openai-completions','anthropic-messages'])
async def test_tool_chain_replays_reasoning_and_calls_once_per_step(runtime, protocol):
    from rolebot.model import Model
    from support import configure_model
    runtime.settings.set_key('synthetic-provider-key')
    model=Model(runtime.db,runtime.settings)
    await model.client.aclose()
    configure_model(model,protocol)
    runtime.engine.model=model
    m=source(runtime,'机器人，计算一下 1+2',1)
    native=[{'type':'thinking','thinking':'synthetic reasoning','signature':'synthetic-signature'},
            {'type':'tool_use','id':'tool-1','name':'calculate','input':{'expression':'1+2','source_message_id':m['id']}}]
    captured=[]
    def handler(request):
        body=json.loads(request.content);captured.append(body)
        if len(captured)==1:
            response={'content':native} if protocol=='anthropic-messages' else {'choices':[{'message':{'role':'assistant','content':'','reasoning_content':'synthetic reasoning',
                'tool_calls':[{'id':'tool-1','type':'function','function':{'name':'calculate','arguments':json.dumps(native[1]['input'])}}]}}]}
        else:
            result=reply_decision(m,'结果是3，。')['content']
            response={'content':[{'type':'text','text':result}]} if protocol=='anthropic-messages' else {'choices':[{'message':{'role':'assistant','content':result}}]}
        response['usage']={'prompt_tokens':100,'prompt_cache_hit_tokens':50,'completion_tokens':20}
        return httpx.Response(200,json=response)
    model.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await runtime.engine.turn(SCOPE,[m])
        assert len(captured)==2 and len(runtime.bot.sent)==1
        assistants=[x for x in captured[1]['messages'] if x['role']=='assistant']
        if protocol=='anthropic-messages': assert assistants[0]['content']==native
        else: assert assistants[0]['reasoning_content']=='synthetic reasoning'
    finally: await model.close()


async def test_permission_change_during_generation_suppresses_send(runtime):
    from rolebot.permissions import AccessConfig
    m=source(runtime,'机器人你好',1)
    async def response(*args,**kwargs):
        runtime.db.put('qq_access',SCOPE,{**AccessConfig().model_dump(),'revision':9})
        return reply_decision(m)
    m['permission_revision']=0
    runtime.model.chat=response
    await runtime.engine.chat_turn(SCOPE,[m])
    assert not runtime.bot.sent


async def test_current_overflow_reports_limit_without_model_or_silent_trim(runtime):
    config=runtime.db.config();config['context_tokens']=4096;runtime.db.put('settings','global',config)
    m=source(runtime,'机器人，'+('很长的问题'*3000),1)
    status=await runtime.engine.chat_turn(SCOPE,[m])
    assert status=='context_limit' and not runtime.model.calls
    assert len(runtime.bot.sent)==1 and '预算' in runtime.bot.sent[0][1][0]['data']['text']


async def test_null_cache_details_remain_unknown(model):
    model.record(SCOPE,'fake','chat',{'prompt_tokens':15,'prompt_tokens_details':None,'completion_tokens':3},stage='conversation.agent')
    row=usage_summary(model.db,SCOPE)['by_stage'][0]
    assert row['cached_tokens'] is None and row['cache_hit_ratio'] is None and row['unknown_calls']==1
